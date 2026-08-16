from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from moshi_data_pipeline.gpu_dispatch_protocol import (
    GPU_DISPATCH_PROTOCOL_VERSION,
    DispatchCreate,
    DispatchStart,
    SelfCheckRequest,
)
from moshi_data_pipeline.gpu_dispatch_state import (
    DispatchCapacityError,
    DispatchConflictError,
    DispatchNotFoundError,
    DispatchStorageError,
    GpuDispatchStore,
)
from moshi_data_pipeline.gpu_execution import (
    GpuExecutionCoordinator,
    default_worker_identity,
)
from moshi_data_pipeline.gpu_self_check import (
    FunctionalCheckRunner,
    SelfCheckCoordinator,
    SelfCheckDefinition,
    SelfCheckRateLimitError,
    SelfCheckRepository,
)
from moshi_data_pipeline.remote_worker import HttpWorkerApi


def _positive_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _tcp_port(name: str, default: int) -> int:
    value = _positive_integer(name, default)
    if value > 65535:
        raise RuntimeError(f"{name} must be a valid TCP port")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _optional_tcp_port(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError(f"{name} must be a valid TCP port")
    return value


def _callback_origin(value: str, port_override: int | None = None) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("MOSHI_WEB_INTERNAL_URL must be an HTTP(S) origin")
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("MOSHI_WEB_INTERNAL_URL must be a plain origin")
    selected_port = port_override if port_override is not None else parsed.port
    port = f":{selected_port}" if selected_port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _host_boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return uuid4().hex


@dataclass(frozen=True)
class GpuIntakeSettings:
    cache_root: Path
    build_id: str
    callback_origin: str
    dispatch_token: str
    dispatch_token_next: str | None = None
    host: str = "0.0.0.0"
    port: int = 8766
    heartbeat_seconds: int = 15
    max_input_bytes: int = 20 * 1024**3
    min_free_bytes: int = 10 * 1024**3
    config_path: Path | None = None
    self_check_metadata: Path | None = None
    self_check_max_cer: float = 0.20
    self_check_validity_hours: int = 6
    self_check_manual_cooldown_seconds: int = 600
    callback_token: str | None = None
    callback_timeout_seconds: int = 15
    job_heartbeat_seconds: int = 15

    @classmethod
    def from_environment(cls) -> GpuIntakeSettings:
        token = os.environ.get("MOSHI_DISPATCH_TOKEN", "")
        build_id = os.environ.get("MOSHI_BUILD_ID", "").strip()
        callback = os.environ.get("MOSHI_WEB_INTERNAL_URL", "").strip()
        if not token:
            raise RuntimeError("MOSHI_DISPATCH_TOKEN is required")
        if not build_id:
            raise RuntimeError("MOSHI_BUILD_ID is required")
        if not callback:
            raise RuntimeError("MOSHI_WEB_INTERNAL_URL is required")
        callback_token = os.environ.get("MOSHI_WORKER_TOKEN", "")
        if not callback_token:
            raise RuntimeError("MOSHI_WORKER_TOKEN is required")
        return cls(
            cache_root=Path(os.environ.get("MOSHI_WORKER_CACHE", "/cache")),
            build_id=build_id,
            callback_origin=_callback_origin(
                callback, _optional_tcp_port("MOSHI_WEB_PORT")
            ),
            dispatch_token=token,
            dispatch_token_next=os.environ.get("MOSHI_DISPATCH_TOKEN_NEXT") or None,
            host=os.environ.get("MOSHI_GPU_INTAKE_HOST", "0.0.0.0"),
            port=_tcp_port("MOSHI_GPU_INTAKE_PORT", 8766),
            heartbeat_seconds=_positive_integer("MOSHI_GPU_STATE_HEARTBEAT_SECONDS", 15),
            max_input_bytes=_positive_integer("MOSHI_GPU_MAX_INPUT_BYTES", 20 * 1024**3),
            min_free_bytes=_positive_integer("MOSHI_GPU_MIN_FREE_BYTES", 10 * 1024**3),
            config_path=(
                Path(os.environ["MOSHI_CONFIG"]) if os.environ.get("MOSHI_CONFIG") else None
            ),
            self_check_metadata=(
                Path(os.environ["MOSHI_SELF_TEST_METADATA"])
                if os.environ.get("MOSHI_SELF_TEST_METADATA")
                else None
            ),
            self_check_max_cer=_positive_float("MOSHI_SELF_TEST_MAX_CER", 0.20),
            self_check_validity_hours=_positive_integer(
                "MOSHI_SELF_TEST_VALIDITY_HOURS", 6
            ),
            self_check_manual_cooldown_seconds=_positive_integer(
                "MOSHI_SELF_TEST_MANUAL_COOLDOWN_SECONDS", 600
            ),
            callback_token=callback_token,
            callback_timeout_seconds=_positive_integer(
                "MOSHI_CALLBACK_TIMEOUT_SECONDS", 15
            ),
            job_heartbeat_seconds=_positive_integer("MOSHI_JOB_HEARTBEAT_SECONDS", 15),
        )


def create_gpu_intake_app(settings: GpuIntakeSettings) -> FastAPI:
    store = GpuDispatchStore(
        settings.cache_root,
        max_input_bytes=settings.max_input_bytes,
        min_free_bytes=settings.min_free_bytes,
    )
    service_boot_id = uuid4().hex
    host_boot_id = _host_boot_id()
    compute_lock = asyncio.Lock()
    if bool(settings.config_path) != bool(settings.self_check_metadata):
        raise RuntimeError(
            "MOSHI_CONFIG and MOSHI_SELF_TEST_METADATA must be configured together"
        )
    self_check: SelfCheckCoordinator | None = None
    if settings.config_path and settings.self_check_metadata:
        runner = FunctionalCheckRunner(
            SelfCheckDefinition(
                metadata_path=settings.self_check_metadata,
                config_path=settings.config_path,
                build_id=settings.build_id,
                host_boot_id=host_boot_id,
                max_cer=settings.self_check_max_cer,
                validity_hours=settings.self_check_validity_hours,
                manual_cooldown_seconds=settings.self_check_manual_cooldown_seconds,
            )
        )
        self_check = SelfCheckCoordinator(
            runner,
            SelfCheckRepository(store.state_root),
            compute_lock=compute_lock,
        )
    execution: GpuExecutionCoordinator | None = None
    if settings.callback_token:
        callback_api = HttpWorkerApi(
            settings.callback_origin,
            settings.callback_token,
            timeout_seconds=settings.callback_timeout_seconds,
        )
        execution = GpuExecutionCoordinator(
            store,
            callback_api,
            default_worker_identity(settings.build_id, host_boot_id),
            compute_lock=compute_lock,
            heartbeat_seconds=settings.job_heartbeat_seconds,
        )

    def combined_status() -> dict[str, Any]:
        value = store.status()
        callback = value.get("callback") or {}
        last_callback_success = callback.get("last_success_at")
        callback_ready = bool(
            execution is not None
            and not callback.get("auth_blocked")
            and last_callback_success
            and datetime.fromisoformat(str(last_callback_success))
            > datetime.now(UTC) - timedelta(seconds=45)
        )
        callback["ready"] = callback_ready
        value["callback"] = callback
        if self_check is None:
            value["functional_check"] = {
                "available": False,
                "ready": False,
                "requirement_key": None,
                "latest": None,
            }
            functional_ready = False
        else:
            latest = self_check.latest()
            requirement_key = self_check.runner.requirement_key
            functional_ready = bool(
                latest
                and latest["requirement_key"] == requirement_key
                and latest["status"] == "passed"
                and latest["valid_until"]
                and datetime.fromisoformat(str(latest["valid_until"]))
                > datetime.now(UTC)
            )
            value["functional_check"] = {
                "available": True,
                "ready": functional_ready,
                "requirement_key": requirement_key,
                "latest": latest,
            }
        value["operational_ready"] = bool(
            execution is not None and callback_ready and functional_ready
        )
        return value

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            store.heartbeat()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.mark_service_started(
            host_boot_id=host_boot_id,
            service_boot_id=service_boot_id,
            protocol_version=GPU_DISPATCH_PROTOCOL_VERSION,
            build_id=settings.build_id,
            callback_origin=settings.callback_origin,
        )
        if execution is not None:
            execution.start()
        task = asyncio.create_task(heartbeat_loop())
        try:
            yield
        finally:
            if execution is not None:
                await execution.stop()
            if self_check is not None:
                await self_check.stop()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Moshi private GPU intake",
        version=GPU_DISPATCH_PROTOCOL_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.dispatch_store = store

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, separator, candidate = (authorization or "").partition(" ")
        valid = separator == " " and scheme.casefold() == "bearer" and bool(candidate)
        if valid:
            valid = secrets.compare_digest(candidate, settings.dispatch_token)
            if not valid and settings.dispatch_token_next:
                valid = secrets.compare_digest(candidate, settings.dispatch_token_next)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid GPU dispatch credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    authorization = Depends(authorize)

    @app.exception_handler(DispatchNotFoundError)
    async def not_found(_: Request, exc: DispatchNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DispatchConflictError)
    @app.exception_handler(DispatchCapacityError)
    async def conflict(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DispatchStorageError)
    async def insufficient_storage(_: Request, exc: DispatchStorageError) -> JSONResponse:
        return JSONResponse(status_code=507, content={"detail": str(exc)})

    @app.exception_handler(SelfCheckRateLimitError)
    async def self_check_rate_limit(
        _: Request, exc: SelfCheckRateLimitError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {
            "status": "alive",
            "service": "moshi-gpu-intake",
            "protocol_version": GPU_DISPATCH_PROTOCOL_VERSION,
            "build_id": settings.build_id,
        }

    @app.get("/internal/v2/health/ready", dependencies=[authorization])
    async def ready() -> dict[str, Any]:
        value = combined_status()
        return {
            "status": "ready" if value["operational_ready"] else "intake_ready",
            "protocol_version": GPU_DISPATCH_PROTOCOL_VERSION,
            "build_id": settings.build_id,
            "capabilities": {
                "input_receipt": True,
                "execution": execution is not None,
                "callback_outbox": execution is not None,
                "functional_check": self_check is not None,
            },
            **value,
        }

    @app.get("/internal/v2/status", dependencies=[authorization])
    async def gpu_status() -> dict[str, Any]:
        return combined_status()

    @app.get("/internal/v2/self-checks", dependencies=[authorization])
    async def self_check_history(
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> dict[str, Any]:
        if self_check is None:
            raise HTTPException(status_code=503, detail="Functional check is not configured")
        return {"checks": self_check.history(limit)}

    @app.post("/internal/v2/self-checks", dependencies=[authorization])
    async def trigger_self_check(
        payload: SelfCheckRequest, response: Response
    ) -> dict[str, Any]:
        if self_check is None:
            raise HTTPException(status_code=503, detail="Functional check is not configured")
        current = store.status().get("current_dispatch")
        if current and current["state"] == "running":
            raise HTTPException(status_code=409, detail="GPU is processing a job")
        record, created = await self_check.trigger(payload.trigger, payload.force)
        response.status_code = 202 if created else 200
        return record

    @app.post("/internal/v2/dispatches", dependencies=[authorization])
    async def create_dispatch(payload: DispatchCreate, response: Response) -> dict[str, Any]:
        if payload.protocol_version != GPU_DISPATCH_PROTOCOL_VERSION:
            raise HTTPException(status_code=409, detail="GPU dispatch protocol mismatch")
        if payload.required_build_id != settings.build_id:
            raise HTTPException(status_code=409, detail="GPU worker build mismatch")
        value, created = store.create_dispatch(payload)
        response.status_code = 201 if created else 200
        return value

    @app.get(
        "/internal/v2/dispatches/{dispatch_id}", dependencies=[authorization]
    )
    async def get_dispatch(dispatch_id: str) -> dict[str, Any]:
        return store.get_dispatch(dispatch_id)

    @app.head(
        "/internal/v2/dispatches/{dispatch_id}/inputs/{artifact_id}",
        dependencies=[authorization],
    )
    async def input_status(dispatch_id: str, artifact_id: str) -> Response:
        value = store.get_input(dispatch_id, artifact_id)
        return Response(
            headers={
                "X-Accepted-Offset": str(value["accepted_offset"]),
                "X-Input-State": str(value["state"]),
                "X-Expected-Size": str(value["size_bytes"]),
                "ETag": f'"sha256-{value["sha256"]}"',
            }
        )

    @app.put(
        "/internal/v2/dispatches/{dispatch_id}/inputs/{artifact_id}",
        dependencies=[authorization],
    )
    async def upload_input(
        dispatch_id: str,
        artifact_id: str,
        request: Request,
        content_range: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if not content_range:
            raise HTTPException(status_code=400, detail="Content-Range is required")
        try:
            return await store.append_input(
                dispatch_id, artifact_id, content_range, request.stream()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/internal/v2/dispatches/{dispatch_id}/start", dependencies=[authorization]
    )
    async def start_dispatch(
        dispatch_id: str,
        payload: DispatchStart,
        response: Response,
        x_lease_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        readiness = combined_status()
        if execution is not None and not readiness["callback"]["ready"]:
            raise HTTPException(
                status_code=503,
                detail="The m8i callback API has not passed a recent authenticated heartbeat",
            )
        if self_check is not None and not readiness["functional_check"]["ready"]:
            raise HTTPException(
                status_code=409,
                detail="A current functional check must pass before GPU execution",
            )
        try:
            value, queued = store.start_dispatch(
                dispatch_id, payload, x_lease_token or ""
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.status_code = 202 if queued else 200
        return value

    @app.post(
        "/internal/v2/dispatches/{dispatch_id}/cancel", dependencies=[authorization]
    )
    async def cancel_dispatch(dispatch_id: str) -> dict[str, Any]:
        value, _ = store.cancel_dispatch(dispatch_id)
        return value

    return app
