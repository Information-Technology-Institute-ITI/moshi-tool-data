import asyncio
import hmac
import ipaddress
import json
import os
import re
import smtplib
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from moshi_data_pipeline.audio.ffmpeg import SUPPORTED_EXTENSIONS
from moshi_data_pipeline.config import PipelineConfig, load_config
from moshi_data_pipeline.studio.artifacts import UploadConflictError
from moshi_data_pipeline.studio.auth import (
    ActivationMailer,
    ActivationRequest,
    AuthenticationService,
    AuthSettings,
    ResendActivationRequest,
    SigninRequest,
    SignupRequest,
    public_user,
)
from moshi_data_pipeline.studio.authorization import (
    CatalogAuthorization,
    RequestPrincipal,
    require_admin,
    require_principal,
)
from moshi_data_pipeline.studio.catalog import (
    WORKER_PROTOCOL_VERSION,
    GpuCheckRateLimitError,
    LeaseConflictError,
    ProjectDeletionConflictError,
    ProtocolMismatchError,
    VersionConflictError,
)
from moshi_data_pipeline.studio.dataset_export import (
    NothingToExportError,
    archive_filename,
    build_dataset_archive,
)
from moshi_data_pipeline.studio.domain import (
    AnnotationSave,
    ProjectCreate,
    ProjectOwnerUpdate,
    ProjectUpdate,
)
from moshi_data_pipeline.studio.gpu_dispatcher import GpuDispatcherSettings
from moshi_data_pipeline.studio.gpu_status import public_gpu_check
from moshi_data_pipeline.studio.lifecycle import LifecycleProvider
from moshi_data_pipeline.studio.media import store_upload
from moshi_data_pipeline.studio.protocol import (
    ClaimRequest,
    JobCompletion,
    JobFailure,
    JobHeartbeat,
    UploadCreate,
    WorkerHeartbeat,
)
from moshi_data_pipeline.studio.service import StudioService


def create_studio_app(
    workspace: Path,
    config: PipelineConfig | None = None,
    *,
    start_worker: bool = True,
    start_lifecycle: bool = True,
    worker_token: str | None = None,
    lifecycle_provider: LifecycleProvider | None = None,
    deployment_generation: str = "local",
    metrics_publisher: Callable[[dict[str, Any]], None] | None = None,
    gpu_dispatcher_settings: GpuDispatcherSettings | None = None,
    gpu_dispatch_client: Any = None,
    start_dispatcher: bool = True,
    trusted_authenticated_user_header: str | None = None,
    loopback_authenticated_user: str | None = None,
    lifecycle_idle_seconds: int = 15 * 60,
    gpu_check_cooldown_seconds: int = 10 * 60,
    gpu_cold_start_cooldown_seconds: int = 30 * 60,
    auth_settings: AuthSettings | None = None,
    auth_mailer: ActivationMailer | None = None,
):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Request
        from fastapi.responses import (
            FileResponse,
            HTMLResponse,
            JSONResponse,
            Response,
            StreamingResponse,
        )
        from fastapi.staticfiles import StaticFiles
        from starlette.background import BackgroundTask
    except ImportError as exc:
        raise RuntimeError('The studio requires: pip install -e ".[review]"') from exc

    service = StudioService(
        workspace.resolve(),
        config or load_config(),
        lifecycle_provider=lifecycle_provider,
        deployment_generation=deployment_generation,
        enable_local_worker=start_worker,
        metrics_publisher=metrics_publisher,
        gpu_dispatcher_settings=gpu_dispatcher_settings,
        gpu_dispatch_client=gpu_dispatch_client,
        lifecycle_idle_seconds=lifecycle_idle_seconds,
        gpu_check_cooldown_seconds=gpu_check_cooldown_seconds,
        gpu_cold_start_cooldown_seconds=gpu_cold_start_cooldown_seconds,
    )
    authentication = AuthenticationService(
        service.catalog,
        auth_settings or AuthSettings.from_environment(),
        mailer=auth_mailer,
    )
    authorization = CatalogAuthorization(service.catalog)
    local_catalog_user = (
        service.catalog.ensure_local_admin()
        if not authentication.settings.require_sign_in
        else None
    )
    local_principal = (
        RequestPrincipal.from_catalog_user(local_catalog_user)
        if local_catalog_user is not None
        else None
    )
    local_user = public_user(local_catalog_user) if local_catalog_user is not None else None
    configured_worker_tokens = tuple(
        value
        for value in (
            worker_token,
            os.environ.get("MOSHI_WORKER_TOKEN"),
            os.environ.get("MOSHI_WORKER_TOKEN_NEXT"),
        )
        if value
    )
    worker_source_ips = {
        value.strip()
        for value in os.environ.get("MOSHI_WORKER_SOURCE_IPS", "").split(",")
        if value.strip()
    }
    identity_header = (
        trusted_authenticated_user_header or os.environ.get("MOSHI_AUTHENTICATED_USER_HEADER", "")
    ).strip()
    if identity_header and not re.fullmatch(r"[A-Za-z0-9-]{1,80}", identity_header):
        raise RuntimeError("MOSHI_AUTHENTICATED_USER_HEADER is invalid")
    trust_proxy_identity = bool(
        trusted_authenticated_user_header
        or os.environ.get("MOSHI_TRUST_PROXY_AUTH", "").strip() == "1"
    )
    loopback_identity = (
        loopback_authenticated_user or os.environ.get("MOSHI_LOOPBACK_AUTHENTICATED_USER", "")
    ).strip()
    if len(loopback_identity) > 200 or any(ord(character) < 32 for character in loopback_identity):
        raise RuntimeError("MOSHI_LOOPBACK_AUTHENTICATED_USER is invalid")
    trial_operator_ips: set[str] = set()
    for value in os.environ.get("MOSHI_TRIAL_OPERATOR_IPS", "").split(","):
        candidate = value.strip()
        if not candidate:
            continue
        try:
            trial_operator_ips.add(str(ipaddress.ip_address(candidate)))
        except ValueError as exc:
            raise RuntimeError("MOSHI_TRIAL_OPERATOR_IPS must contain exact IP addresses") from exc
    trial_operator_identity = os.environ.get("MOSHI_TRIAL_AUTHENTICATED_USER", "").strip()
    if bool(trial_operator_ips) != bool(trial_operator_identity):
        raise RuntimeError(
            "MOSHI_TRIAL_OPERATOR_IPS and MOSHI_TRIAL_AUTHENTICATED_USER "
            "must be configured together"
        )
    if len(trial_operator_identity) > 200 or any(
        ord(character) < 32 for character in trial_operator_identity
    ):
        raise RuntimeError("MOSHI_TRIAL_AUTHENTICATED_USER is invalid")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            service.worker.start()
        if start_lifecycle:
            service.lifecycle.start()
        if start_dispatcher:
            service.dispatcher.start()
        try:
            yield
        finally:
            if start_dispatcher:
                service.dispatcher.stop()
            if start_worker:
                service.worker.stop()
            if start_lifecycle:
                service.lifecycle.stop()

    app = FastAPI(
        title="Moshi Dataset Studio",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.studio = service
    app.state.authentication = authentication
    app.state.authorization = authorization
    public_auth_paths = {
        "/api/health",
        "/api/auth/signup",
        "/api/auth/activate",
        "/api/auth/resend-activation",
        "/api/auth/signin",
        "/api/auth/signout",
        "/api/auth/me",
    }

    @app.middleware("http")
    async def enforce_trusted_origin(request: Request, call_next):
        if request.url.path.startswith("/internal/v1/") and worker_source_ips:
            source = request.client.host if request.client else ""
            if source not in worker_source_ips:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Worker callback source is not allowed"},
                )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith(
            "/internal/"
        ):
            origin = request.headers.get("origin")
            if origin:
                configured = {
                    value.strip().rstrip("/")
                    for value in os.environ.get("MOSHI_TRUSTED_ORIGINS", "").split(",")
                    if value.strip()
                }
                if not configured:
                    configured = {f"{request.url.scheme}://{request.headers['host']}"}
                if origin.rstrip("/") not in configured:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Untrusted request origin"},
                    )
        session_user = authentication.current_user(
            request.cookies.get(authentication.settings.cookie_name)
        )
        principal = (
            local_principal
            if local_principal is not None
            else (
                RequestPrincipal.from_catalog_user(session_user)
                if session_user is not None
                else None
            )
        )
        visible_user = local_user if local_user is not None else session_user
        if visible_user is not None:
            request.state.authenticated_user = visible_user
        if principal is not None:
            request.state.principal = principal
        protected_browser_path = (
            request.url.path.startswith("/api/") and request.url.path not in public_auth_paths
        ) or request.url.path.startswith("/media/")
        if protected_browser_path and principal is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Sign in is required"},
            )
        if protected_browser_path:
            path = request.url.path
            try:
                if match := re.match(r"^/api/admin/projects/([^/]+)", path):
                    require_admin(principal)
                    authorization.authorize_project(principal, match.group(1))
                elif path.startswith(("/api/admin/", "/api/system/")):
                    require_admin(principal)
                elif match := re.match(r"^/api/projects/([^/]+)", path):
                    authorization.authorize_project(principal, match.group(1))
                elif match := re.match(r"^/api/sources/([^/]+)", path):
                    authorization.authorize_source(principal, match.group(1))
                elif match := re.match(r"^/api/jobs/([^/]+)", path):
                    authorization.authorize_job(principal, match.group(1))
                elif match := re.match(r"^/media/exports/([^/]+)", path):
                    authorization.authorize_export(principal, match.group(1))
                elif match := re.match(r"^/media/([^/]+)", path):
                    authorization.authorize_source(principal, match.group(1))
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            except KeyError:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "Not found"},
                )
        response = await call_next(request)
        if protected_browser_path and request.url.path.startswith("/media/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing_handler(_: Request, __: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.exception_handler(VersionConflictError)
    async def conflict_handler(_: Request, exc: VersionConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProjectDeletionConflictError)
    async def deletion_conflict_handler(_: Request, exc: ProjectDeletionConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_handler(_: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def permission_handler(_: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(LeaseConflictError)
    async def lease_conflict_handler(_: Request, exc: LeaseConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProtocolMismatchError)
    async def protocol_conflict_handler(_: Request, exc: ProtocolMismatchError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UploadConflictError)
    async def upload_conflict_handler(_: Request, exc: UploadConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GpuCheckRateLimitError)
    async def gpu_check_rate_limit_handler(_: Request, exc: GpuCheckRateLimitError):
        return JSONResponse(
            status_code=429,
            content={"detail": exc.reason},
            headers={"Retry-After": str(exc.retry_after)},
        )

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="Origin is required")
        configured = {
            value.strip().rstrip("/")
            for value in os.environ.get("MOSHI_TRUSTED_ORIGINS", "").split(",")
            if value.strip()
        }
        if not configured:
            configured = {f"{request.url.scheme}://{request.headers['host']}"}
        if origin.rstrip("/") not in configured:
            raise HTTPException(status_code=403, detail="Untrusted request origin")

    def authenticated_user(request: Request) -> str:
        session_user = getattr(request.state, "authenticated_user", None)
        if authentication.settings.require_sign_in and session_user is not None:
            return str(session_user["email"])
        source = request.client.host if request.client else ""
        try:
            is_loopback = ipaddress.ip_address(source).is_loopback
        except ValueError:
            is_loopback = False
        if loopback_identity and is_loopback:
            return loopback_identity
        if trial_operator_identity and source in trial_operator_ips:
            return trial_operator_identity
        if not trust_proxy_identity or not identity_header:
            raise HTTPException(
                status_code=503,
                detail="Trusted website authentication identity is not configured",
            )
        value = request.headers.get(identity_header, "").strip()
        if not value or len(value) > 200 or any(ord(character) < 32 for character in value):
            raise HTTPException(status_code=401, detail="Authenticated user is required")
        return value

    def require_worker_token(authorization: str | None) -> None:
        if not configured_worker_tokens:
            raise HTTPException(status_code=503, detail="Worker API is not configured")
        scheme, _, token = (authorization or "").partition(" ")
        valid = scheme.casefold() == "bearer" and any(
            hmac.compare_digest(token, expected) for expected in configured_worker_tokens
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="Invalid worker token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_lease_token(value: str | None) -> str:
        if not value:
            raise HTTPException(status_code=401, detail="Missing lease token")
        return value

    def worker_auth(authorization: str | None) -> None:
        require_worker_token(authorization)

    @app.get("/api/auth/me")
    def auth_me(request: Request):
        return {
            "user": getattr(request.state, "authenticated_user", None),
            "required": authentication.settings.require_sign_in,
        }

    @app.post("/api/auth/signup", status_code=202)
    def auth_signup(payload: SignupRequest, request: Request):
        require_same_origin(request)
        if not authentication.settings.email_configured:
            raise HTTPException(
                status_code=503,
                detail="Activation email delivery is not configured",
            )
        try:
            authentication.signup(
                email=payload.email,
                password=payload.password,
                display_name=payload.display_name,
            )
        except (OSError, RuntimeError, smtplib.SMTPException):
            raise HTTPException(
                status_code=503,
                detail="Activation email could not be sent",
            ) from None
        return {"message": ("If the address can be registered, an activation email has been sent.")}

    @app.post("/api/auth/resend-activation", status_code=202)
    def auth_resend_activation(payload: ResendActivationRequest, request: Request):
        require_same_origin(request)
        if not authentication.settings.email_configured:
            raise HTTPException(
                status_code=503,
                detail="Activation email delivery is not configured",
            )
        try:
            authentication.resend_activation(payload.email)
        except (OSError, RuntimeError, smtplib.SMTPException):
            raise HTTPException(
                status_code=503,
                detail="Activation email could not be sent",
            ) from None
        return {"message": ("If the account is awaiting activation, a new email has been sent.")}

    @app.post("/api/auth/activate")
    def auth_activate(payload: ActivationRequest, request: Request):
        require_same_origin(request)
        return {"user": authentication.activate(payload.token)}

    @app.post("/api/auth/signin")
    def auth_signin(payload: SigninRequest, request: Request):
        require_same_origin(request)
        user, token = authentication.signin(payload.email, payload.password)
        response = JSONResponse(content={"user": user})
        response.set_cookie(
            key=authentication.settings.cookie_name,
            value=token,
            max_age=authentication.settings.session_ttl_seconds,
            httponly=True,
            secure=authentication.settings.cookie_secure,
            samesite="lax",
            path="/",
            domain=None,
        )
        return response

    @app.post("/api/auth/signout")
    def auth_signout(request: Request):
        require_same_origin(request)
        authentication.signout(request.cookies.get(authentication.settings.cookie_name))
        response = JSONResponse(content={"signed_out": True})
        response.delete_cookie(
            key=authentication.settings.cookie_name,
            path="/",
            secure=authentication.settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/internal/v1/workers/heartbeat")
    def worker_heartbeat(
        payload: WorkerHeartbeat,
        authorization: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        state = service.catalog.record_worker_state(
            payload.worker_id,
            boot_id=payload.boot_id,
            protocol_version=payload.protocol_version,
            build_id=payload.build_id,
            supported_kinds=payload.supported_kinds,
            status=payload.status,
            current_job_id=payload.current_job_id,
            details=payload.details,
        )
        service.lifecycle.wake()
        service.dispatcher.wake()
        return state

    @app.post("/internal/v1/jobs/claim")
    def claim_remote_job(
        payload: ClaimRequest,
        authorization: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        state = service.catalog.record_worker_state(
            payload.worker_id,
            boot_id=payload.boot_id,
            protocol_version=payload.protocol_version,
            build_id=payload.build_id,
            supported_kinds=payload.supported_kinds,
            status="ready",
        )
        if not state["compatible"]:
            raise ProtocolMismatchError(
                f"Worker protocol {payload.protocol_version} is incompatible with "
                f"{WORKER_PROTOCOL_VERSION}"
            )
        job = service.catalog.claim_leased_job(
            payload.worker_id,
            protocol_version=payload.protocol_version,
            worker_build_id=payload.build_id,
            supported_kinds=payload.supported_kinds,
        )
        if job is None:
            service.catalog.record_worker_state(
                payload.worker_id,
                boot_id=payload.boot_id,
                protocol_version=payload.protocol_version,
                build_id=payload.build_id,
                supported_kinds=payload.supported_kinds,
                status="idle",
            )
            return {"job": None, "lease_token": None}
        lease_token = str(job.pop("lease_token"))
        service.catalog.record_worker_state(
            payload.worker_id,
            boot_id=payload.boot_id,
            protocol_version=payload.protocol_version,
            build_id=payload.build_id,
            supported_kinds=payload.supported_kinds,
            status="busy",
            current_job_id=str(job["id"]),
        )
        return {
            "job": service.contexts.create_context(job).model_dump(mode="json"),
            "lease_token": lease_token,
        }

    @app.post("/internal/v1/jobs/{job_id}/heartbeat")
    def heartbeat_remote_job(
        job_id: str,
        payload: JobHeartbeat,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        lease_token = require_lease_token(x_lease_token)
        job = service.catalog.heartbeat_leased_job(
            job_id,
            payload.worker_id,
            lease_token,
            progress=payload.progress,
            message=payload.message,
        )
        return {
            "job": job,
            "cancel_requested": (
                service.contexts.current_fingerprint(job) != job["input_fingerprint"]
            ),
        }

    @app.get("/internal/v1/artifacts/{artifact_id}/content")
    def download_artifact(
        artifact_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        artifact = service.catalog.get_artifact(artifact_id)
        if artifact["state"] != "active":
            raise KeyError(artifact_id)
        path = service.paths.resolve_relative(str(artifact["relative_path"]))
        if not path.is_file():
            raise KeyError(artifact_id)
        size = int(artifact["size_bytes"])
        etag = f'"sha256-{artifact["sha256"]}"'
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Disposition": f'attachment; filename="{path.name}"',
        }
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if not range_header or (if_range and if_range != etag):
            return FileResponse(
                path,
                media_type=str(artifact["media_type"]),
                headers=headers,
            )
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match is None or "," in range_header:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        first, last = match.groups()
        if not first and not last:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        else:
            suffix = int(last)
            if suffix <= 0:
                return Response(
                    status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{size}"},
                )
            start = max(0, size - suffix)
            end = size - 1
        if start >= size or end < start:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        end = min(end, size - 1)

        def content():
            remaining = end - start + 1
            with path.open("rb") as stream:
                stream.seek(start)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            content(),
            status_code=206,
            media_type=str(artifact["media_type"]),
            headers={
                **headers,
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{size}",
            },
        )

    @app.post("/internal/v1/jobs/{job_id}/uploads", status_code=201)
    def create_remote_upload(
        job_id: str,
        payload: UploadCreate,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        return service.artifacts.create_upload(
            job_id,
            require_lease_token(x_lease_token),
            payload,
        )

    @app.api_route(
        "/internal/v1/uploads/{upload_id}",
        methods=["HEAD"],
    )
    def remote_upload_status(
        upload_id: str,
        worker_id: str,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        upload = service.artifacts.upload_status(
            upload_id,
            worker_id,
            require_lease_token(x_lease_token),
        )
        return Response(
            status_code=200,
            headers={
                "Upload-Offset": str(upload["accepted_offset"]),
                "Upload-Length": str(upload["expected_size"]),
                "Upload-State": str(upload["state"]),
            },
        )

    @app.put("/internal/v1/uploads/{upload_id}")
    async def append_remote_upload(
        upload_id: str,
        worker_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
        content_range: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        if content_range is None:
            raise HTTPException(status_code=411, detail="Content-Range is required")
        upload = await service.artifacts.append(
            upload_id,
            worker_id,
            require_lease_token(x_lease_token),
            content_range,
            request.stream(),
        )
        return {
            "upload_id": upload["id"],
            "accepted_offset": upload["accepted_offset"],
            "size_bytes": upload["expected_size"],
            "state": upload["state"],
        }

    @app.post("/internal/v1/jobs/{job_id}/complete")
    def complete_remote_job(
        job_id: str,
        payload: JobCompletion,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        job = service.complete_remote_job(
            job_id,
            payload.worker_id,
            require_lease_token(x_lease_token),
            input_fingerprint=payload.input_fingerprint,
            kind=payload.kind,
            result_value=payload.result,
            produced_artifacts=payload.artifacts,
        )
        service.lifecycle.wake()
        service.dispatcher.wake()
        return job

    @app.post("/internal/v1/jobs/{job_id}/fail")
    def fail_remote_job(
        job_id: str,
        payload: JobFailure,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        job = service.catalog.fail_leased_job(
            job_id,
            payload.worker_id,
            require_lease_token(x_lease_token),
            error=payload.error,
            failure_class=payload.failure_class,
            retryable=payload.retryable,
        )
        service.lifecycle.wake()
        service.dispatcher.wake()
        return job

    @app.get("/api/health")
    def health():
        return {"status": "ok", "workspace": str(service.paths.root)}

    @app.get("/api/system/worker")
    def worker_status(request: Request):
        require_admin(require_principal(request))
        return {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "worker": service.catalog.latest_worker_state(),
            "queue": service.catalog.queue_summary(),
            "mode": "local" if start_worker else "remote",
            "lifecycle": service.catalog.get_lifecycle_state(),
        }

    @app.post("/api/system/worker/retry-startup", status_code=202)
    def retry_worker_startup(request: Request):
        require_admin(require_principal(request))
        return service.lifecycle.retry_blocked()

    @app.get("/api/system/gpu")
    def gpu_status(request: Request):
        require_admin(require_principal(request))
        return service.gpu_status()

    @app.get("/api/system/gpu/checks")
    def gpu_check_history(request: Request, limit: int = 10):
        require_admin(require_principal(request))
        return {
            "checks": [
                public_gpu_check(check) for check in service.catalog.list_gpu_checks(limit=limit)
            ]
        }

    @app.post("/api/system/gpu/checks")
    def trigger_gpu_check(request: Request):
        require_admin(require_principal(request))
        require_same_origin(request)
        user = authenticated_user(request)
        if service.gpu_settings is None:
            raise HTTPException(status_code=503, detail="GPU push dispatch is not configured")
        check, created = service.request_gpu_check(user)
        return JSONResponse(
            status_code=202 if created else 200,
            content={
                "check": check,
                "created": created,
                "cost_notice": "Starting a stopped GPU incurs cost.",
            },
        )

    @app.get("/api/admin/projects/{project_id}/export")
    def export_dataset(project_id: str, request: Request):
        """Downloads a finished dataset: its audio, and one CSV of every segment.

        Administrators only. The CSV carries the reviewer's final text from the
        newest saved revision, so nothing here needs the GPU or starts a job.
        """
        principal = require_admin(require_principal(request))
        authorization.authorize_project(principal, project_id)
        project = service.catalog.get_project(project_id)
        destination = (
            service.paths.exports
            / f"{project_id}_{uuid4().hex}.zip"
        )
        try:
            build_dataset_archive(
                service.catalog, service.paths, project_id, destination
            )
        except NothingToExportError as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            destination,
            media_type="application/zip",
            filename=archive_filename(str(project["name"])),
            # The archive is a one-off download, so it is not kept afterwards.
            background=BackgroundTask(destination.unlink, missing_ok=True),
        )

    @app.get("/api/admin/users")
    def list_admin_users(request: Request):
        require_admin(require_principal(request))
        return {"users": service.catalog.list_active_users()}

    @app.patch("/api/admin/projects/{project_id}/owner")
    def transfer_project_owner(
        project_id: str,
        payload: ProjectOwnerUpdate,
        request: Request,
    ):
        principal = require_admin(require_principal(request))
        authorization.authorize_project(principal, project_id)
        return service.catalog.transfer_project_owner(
            project_id,
            owner_user_id=payload.owner_user_id,
            principal=principal,
        )

    @app.get("/api/projects")
    def list_projects(request: Request):
        principal = require_principal(request)
        return {
            "projects": service.catalog.list_projects(
                viewer_id=principal.user_id,
                is_admin=principal.is_admin,
            )
        }

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate, request: Request):
        principal = require_principal(request)
        return service.catalog.create_project(
            payload.name,
            payload.language,
            owner_user_id=principal.user_id,
        )

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return {
            "project": service.catalog.get_project(project_id),
            "sources": service.catalog.list_sources(project_id),
            "jobs": service.catalog.list_jobs(project_id),
            "exports": service.catalog.list_exports(project_id),
        }

    @app.put("/api/projects/{project_id}")
    def update_project(project_id: str, payload: ProjectUpdate, request: Request):
        return service.catalog.update_project(
            project_id,
            name=payload.name,
            language=payload.language,
            principal=require_principal(request),
        )

    @app.delete("/api/projects/{project_id}")
    def delete_project(
        project_id: str,
        request: Request,
        x_confirm_delete: str = Header(...),
    ):
        principal = require_principal(request)
        authorization.authorize_project(principal, project_id)
        if x_confirm_delete != project_id:
            raise HTTPException(
                status_code=400,
                detail="X-Confirm-Delete must exactly match the project id",
            )
        deleted = service.delete_project(project_id, principal=principal)
        cleanup = deleted["cleanup"]
        return {
            "deleted": deleted["id"],
            "recoverable": False,
            "cleanup_state": cleanup["state"],
        }

    @app.post("/api/projects/{project_id}/sources", status_code=201)
    async def upload_source(
        project_id: str,
        request: Request,
        x_filename: str = Header(...),
    ):
        principal = require_principal(request)
        if Path(x_filename).suffix.casefold() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported media extension: {Path(x_filename).suffix}",
            )
        destination, digest, size = await store_upload(service.paths, x_filename, request.stream())
        try:
            return service.catalog.create_source(
                project_id,
                Path(x_filename).name,
                service.paths.relative(destination),
                request.headers.get("content-type", "application/octet-stream"),
                digest,
                size,
                principal=principal,
            )
        except Exception:
            if destination.exists():
                destination.unlink()
            raise

    @app.get("/api/sources/{source_id}")
    def get_source(source_id: str):
        return service.source_detail(source_id)

    @app.delete("/api/sources/{source_id}")
    def delete_source(
        source_id: str,
        request: Request,
        x_confirm_delete: str = Header(...),
    ):
        if x_confirm_delete != source_id:
            raise HTTPException(
                status_code=400,
                detail="X-Confirm-Delete must exactly match the source id",
            )
        deleted = service.delete_source(
            source_id,
            principal=require_principal(request),
        )
        return {"deleted": deleted["id"], "recoverable": False}

    @app.post("/api/sources/{source_id}/initialize", status_code=202)
    def initialize_source_job(
        source_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ):
        source = service.catalog.get_source(source_id)
        mode = str(payload.get("mode", "manual"))
        if mode not in {"manual", "assisted"}:
            raise ValueError("Initialization mode must be manual or assisted")
        return service.enqueue(
            source["project_id"],
            "initialize",
            source_id,
            {"mode": mode},
            principal=require_principal(request),
            source_updates={"status": "processing", "init_mode": mode},
        )

    def enqueue_source(source_id: str, kind: str, request: Request):
        source = service.catalog.get_source(source_id)
        return service.enqueue(
            source["project_id"],
            kind,
            source_id,
            principal=require_principal(request),
        )

    @app.get("/api/sources/{source_id}/annotations")
    def get_annotation(source_id: str):
        return {
            "annotation": service.catalog.latest_annotation(source_id).model_dump(mode="json"),
            "revisions": service.catalog.annotation_revisions(source_id),
        }

    @app.get("/api/sources/{source_id}/annotations/{version}")
    def get_annotation_revision(source_id: str, version: int):
        return service.catalog.annotation_at(source_id, version).model_dump(mode="json")

    @app.put("/api/sources/{source_id}/annotations")
    def save_annotation(source_id: str, payload: AnnotationSave, request: Request):
        return service.save_annotation(
            source_id,
            payload.expected_version,
            payload.annotation,
            principal=require_principal(request),
        ).model_dump(mode="json")

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return service.catalog.get_job(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str, request: Request):
        job = service.catalog.retry_job(
            job_id,
            principal=require_principal(request),
        )
        service.worker.wake()
        service.lifecycle.wake()
        service.dispatcher.wake()
        return job

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        principal = require_principal(request)
        authorization.authorize_job(principal, job_id)
        session_token = request.cookies.get(authentication.settings.cookie_name)

        def current_stream_principal() -> RequestPrincipal | None:
            if local_principal is not None:
                try:
                    user = service.catalog.get_user(local_principal.user_id)
                except KeyError:
                    return None
                if user["status"] != "active":
                    return None
                return RequestPrincipal.from_catalog_user(user)
            user = authentication.current_user(session_token)
            return RequestPrincipal.from_catalog_user(user) if user is not None else None

        async def values():
            previous = None
            while True:
                try:
                    current_principal = current_stream_principal()
                    if current_principal is None:
                        raise KeyError(job_id)
                    job = authorization.authorize_job(current_principal, job_id)
                except KeyError:
                    yield 'event: access_revoked\ndata: {"detail":"Not found"}\n\n'
                    break
                serialized = json.dumps(job, ensure_ascii=False)
                if serialized != previous:
                    yield f"data: {serialized}\n\n"
                    previous = serialized
                if job["status"] in {"complete", "failed", "superseded"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            values(),
            media_type="text/event-stream",
            headers={"Cache-Control": "private, no-store"},
        )

    def source_file(source_id: str, kind: str) -> Path:
        source = service.catalog.get_source(source_id)
        if kind == "original":
            return service.paths.resolve_relative(source["stored_path"])
        if kind == "canonical":
            return service.active_artifact_path(
                source_id, "source.canonical", service.paths.canonical_audio(source_id)
            )
        if kind == "channels":
            return service.active_artifact_path(
                source_id, "source.channels", service.paths.canonical_channels(source_id)
            )
        if kind == "proxy":
            return service.active_artifact_path(
                source_id, "source.proxy", service.paths.video_proxy(source_id)
            )
        if kind == "peaks":
            return service.active_artifact_path(
                source_id, "source.peaks", service.paths.peaks(source_id)
            )
        raise KeyError(kind)

    @app.get("/media/{source_id}/{kind}")
    def media(source_id: str, kind: str):
        path = source_file(source_id, kind)
        if not path.is_file():
            raise KeyError(kind)
        media_types = {
            "canonical": "audio/wav",
            "channels": "audio/wav",
            "proxy": "video/mp4",
            "peaks": "application/json",
        }
        return FileResponse(
            path,
            media_type=media_types.get(kind),
            filename=path.name if kind == "original" else None,
        )

    static_root = Path(__file__).with_name("static")
    assets = static_root / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="studio-assets")

    @app.get("/studio-intro.png", include_in_schema=False)
    def studio_intro_asset():
        path = static_root / "studio-intro.png"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/")
    def index():
        index_path = static_root / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse(
            "<h1>Moshi Dataset Studio</h1><p>Build the React application first.</p>"
        )

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith(("api/", "media/", "internal/")):
            raise HTTPException(status_code=404)
        index_path = static_root / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        raise HTTPException(status_code=404)

    return app


def serve_studio(
    workspace: Path,
    host: str = "127.0.0.1",
    port: int | None = None,
    *,
    config_path: Path | None = None,
    allow_remote: bool = False,
) -> None:
    if port is None:
        from moshi_data_pipeline.studio.web_main import web_port_from_environment

        port = web_port_from_environment()
    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        raise ValueError("Non-loopback studio hosting requires --allow-remote")
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError('The studio requires: pip install -e ".[review]"') from exc
    uvicorn.run(
        create_studio_app(workspace, load_config(config_path)),
        host=host,
        port=port,
        log_level="info",
    )
