import asyncio
import hmac
import json
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from moshi_data_pipeline.audio.ffmpeg import SUPPORTED_EXTENSIONS
from moshi_data_pipeline.config import PipelineConfig, load_config
from moshi_data_pipeline.studio.artifacts import UploadConflictError
from moshi_data_pipeline.studio.catalog import (
    WORKER_PROTOCOL_VERSION,
    LeaseConflictError,
    ProtocolMismatchError,
    VersionConflictError,
)
from moshi_data_pipeline.studio.clip_registry import clip_artifacts
from moshi_data_pipeline.studio.domain import (
    AnnotationSave,
    ClipPlanRequest,
    DecisionPayload,
    ExportCreate,
    OverlapDecisionPayload,
    ProjectCreate,
    ProjectUpdate,
    SourceRights,
)
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
    except ImportError as exc:
        raise RuntimeError(
            'The studio requires: pip install -e ".[review]"'
        ) from exc

    service = StudioService(
        workspace.resolve(),
        config or load_config(),
        lifecycle_provider=lifecycle_provider,
        deployment_generation=deployment_generation,
        enable_local_worker=start_worker,
        metrics_publisher=metrics_publisher,
    )
    configured_worker_tokens = tuple(
        value
        for value in (
            worker_token,
            os.environ.get("MOSHI_WORKER_TOKEN"),
            os.environ.get("MOSHI_WORKER_TOKEN_NEXT"),
        )
        if value
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            service.worker.start()
        if start_lifecycle:
            service.lifecycle.start()
        try:
            yield
        finally:
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

    @app.middleware("http")
    async def enforce_trusted_origin(request: Request, call_next):
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not request.url.path.startswith("/internal/")
        ):
            origin = request.headers.get("origin")
            if origin:
                configured = {
                    value.strip().rstrip("/")
                    for value in os.environ.get("MOSHI_TRUSTED_ORIGINS", "").split(",")
                    if value.strip()
                }
                if not configured:
                    scheme = request.headers.get(
                        "x-forwarded-proto", request.url.scheme
                    ).split(",", 1)[0]
                    configured = {f"{scheme}://{request.headers['host']}"}
                if origin.rstrip("/") not in configured:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Untrusted request origin"},
                    )
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing_handler(_: Request, exc: KeyError):
        return JSONResponse(
            status_code=404, content={"detail": f"Not found: {exc.args[0]}"}
        )

    @app.exception_handler(VersionConflictError)
    async def conflict_handler(_: Request, exc: VersionConflictError):
        return JSONResponse(
            status_code=409, content={"detail": str(exc)}
        )

    @app.exception_handler(ValueError)
    async def value_handler(_: Request, exc: ValueError):
        return JSONResponse(
            status_code=400, content={"detail": str(exc)}
        )

    @app.exception_handler(LeaseConflictError)
    async def lease_conflict_handler(_: Request, exc: LeaseConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProtocolMismatchError)
    async def protocol_conflict_handler(_: Request, exc: ProtocolMismatchError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UploadConflictError)
    async def upload_conflict_handler(_: Request, exc: UploadConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def require_worker_token(authorization: str | None) -> None:
        if not configured_worker_tokens:
            raise HTTPException(status_code=503, detail="Worker API is not configured")
        scheme, _, token = (authorization or "").partition(" ")
        valid = scheme.casefold() == "bearer" and any(
            hmac.compare_digest(token, expected)
            for expected in configured_worker_tokens
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

    @app.post("/internal/v1/workers/heartbeat")
    def worker_heartbeat(
        payload: WorkerHeartbeat,
        authorization: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        return service.catalog.record_worker_state(
            payload.worker_id,
            boot_id=payload.boot_id,
            protocol_version=payload.protocol_version,
            build_id=payload.build_id,
            supported_kinds=payload.supported_kinds,
            status=payload.status,
            current_job_id=payload.current_job_id,
            details=payload.details,
        )

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
                service.contexts.current_fingerprint(job)
                != job["input_fingerprint"]
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
        return service.complete_remote_job(
            job_id,
            payload.worker_id,
            require_lease_token(x_lease_token),
            input_fingerprint=payload.input_fingerprint,
            kind=payload.kind,
            result_value=payload.result,
            produced_artifacts=payload.artifacts,
        )

    @app.post("/internal/v1/jobs/{job_id}/fail")
    def fail_remote_job(
        job_id: str,
        payload: JobFailure,
        authorization: str | None = Header(default=None),
        x_lease_token: str | None = Header(default=None),
    ):
        worker_auth(authorization)
        return service.catalog.fail_leased_job(
            job_id,
            payload.worker_id,
            require_lease_token(x_lease_token),
            error=payload.error,
            failure_class=payload.failure_class,
            retryable=payload.retryable,
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "workspace": str(service.paths.root)}

    @app.get("/api/system/worker")
    def worker_status():
        return {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "worker": service.catalog.latest_worker_state(),
            "queue": service.catalog.queue_summary(),
            "mode": "local" if start_worker else "remote",
            "lifecycle": service.catalog.get_lifecycle_state(),
        }

    @app.post("/api/system/worker/retry-startup", status_code=202)
    def retry_worker_startup():
        return service.lifecycle.retry_blocked()

    @app.get("/api/projects")
    def list_projects():
        return {"projects": service.catalog.list_projects()}

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate):
        return service.catalog.create_project(payload.name, payload.language)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return {
            "project": service.catalog.get_project(project_id),
            "sources": service.catalog.list_sources(project_id),
            "jobs": service.catalog.list_jobs(project_id),
            "exports": service.catalog.list_exports(project_id),
        }

    @app.put("/api/projects/{project_id}")
    def update_project(project_id: str, payload: ProjectUpdate):
        return service.catalog.update_project(
            project_id,
            name=payload.name,
            language=payload.language,
        )

    @app.post("/api/projects/{project_id}/sources", status_code=201)
    async def upload_source(
        project_id: str,
        request: Request,
        x_filename: str = Header(...),
    ):
        service.catalog.get_project(project_id)
        if Path(x_filename).suffix.casefold() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported media extension: {Path(x_filename).suffix}",
            )
        destination, digest, size = await store_upload(
            service.paths, x_filename, request.stream()
        )
        try:
            return service.catalog.create_source(
                project_id,
                Path(x_filename).name,
                service.paths.relative(destination),
                request.headers.get("content-type", "application/octet-stream"),
                digest,
                size,
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
        x_confirm_delete: str = Header(...),
    ):
        if x_confirm_delete != source_id:
            raise HTTPException(
                status_code=400,
                detail="X-Confirm-Delete must exactly match the source id",
            )
        deleted = service.delete_source(source_id)
        return {"deleted": deleted["id"], "recoverable": False}

    @app.put("/api/sources/{source_id}/rights")
    def save_rights(source_id: str, payload: SourceRights):
        return service.save_rights(source_id, payload)

    @app.post("/api/sources/{source_id}/initialize", status_code=202)
    def initialize_source_job(
        source_id: str,
        payload: dict[str, Any] = Body(...),
    ):
        source = service.catalog.get_source(source_id)
        mode = str(payload.get("mode", "manual"))
        if mode not in {"manual", "assisted"}:
            raise ValueError("Initialization mode must be manual or assisted")
        service.catalog.update_source(source_id, status="processing", init_mode=mode)
        return service.enqueue(
            source["project_id"],
            "initialize",
            source_id,
            {"mode": mode},
        )

    def enqueue_source(source_id: str, kind: str):
        source = service.catalog.get_source(source_id)
        return service.enqueue(source["project_id"], kind, source_id)

    @app.post("/api/sources/{source_id}/transcribe", status_code=202)
    def transcribe_source_job(source_id: str):
        return enqueue_source(source_id, "transcribe")

    @app.post("/api/sources/{source_id}/review-transcript", status_code=202)
    def review_transcript_job(source_id: str):
        return enqueue_source(source_id, "review_transcript")

    @app.post("/api/sources/{source_id}/rediarize", status_code=202)
    def rediarize_source_job(source_id: str):
        return enqueue_source(source_id, "rediarize")

    @app.post("/api/sources/{source_id}/realign", status_code=202)
    def realign_source_job(source_id: str):
        source = service.catalog.get_source(source_id)
        return service.enqueue(
            source["project_id"],
            "realign",
            source_id,
            {"annotation_version": source["active_annotation_version"]},
        )

    @app.post("/api/sources/{source_id}/recover-overlap", status_code=202)
    def overlap_source_job(source_id: str):
        annotation = service.catalog.latest_annotation(source_id)
        if annotation.assistant_speaker is None:
            raise ValueError("Choose the Moshi speaker before recovering overlap")
        if not annotation.activities_finalized:
            raise ValueError(
                "Finalize and save the human speaker regions before recovering overlap"
            )
        return enqueue_source(source_id, "recover_overlap")

    @app.post(
        "/api/sources/{source_id}/overlaps/{region_id}/transcribe",
        status_code=202,
    )
    def transcribe_overlap_job(source_id: str, region_id: str):
        source = service.catalog.get_source(source_id)
        return service.enqueue(
            source["project_id"],
            "transcribe_overlap",
            source_id,
            {"region_id": region_id},
        )

    @app.get("/api/sources/{source_id}/annotations")
    def get_annotation(source_id: str):
        return {
            "annotation": service.catalog.latest_annotation(source_id).model_dump(
                mode="json"
            ),
            "revisions": service.catalog.annotation_revisions(source_id),
        }

    @app.get("/api/sources/{source_id}/annotations/{version}")
    def get_annotation_revision(source_id: str, version: int):
        return service.catalog.annotation_at(source_id, version).model_dump(mode="json")

    @app.put("/api/sources/{source_id}/annotations")
    def save_annotation(source_id: str, payload: AnnotationSave):
        return service.save_annotation(
            source_id,
            payload.expected_version,
            payload.annotation,
        ).model_dump(mode="json")

    @app.get("/api/sources/{source_id}/overlaps")
    def get_overlaps(source_id: str):
        return {"overlaps": service.catalog.overlap_recoveries(source_id)}

    @app.post("/api/sources/{source_id}/overlaps/{region_id}/decision")
    def decide_overlap(
        source_id: str,
        region_id: str,
        payload: OverlapDecisionPayload,
    ):
        return service.catalog.decide_overlap(
            source_id,
            region_id,
            payload.decision,
            payload.auditioned,
        )

    @app.post("/api/sources/{source_id}/clip-plan")
    def create_clip_plan(source_id: str, payload: ClipPlanRequest):
        return service.plan_clips(source_id, payload)

    @app.get("/api/sources/{source_id}/clip-plan")
    def get_clip_plan(source_id: str):
        plan = service.catalog.get_clip_plan(source_id)
        return {"plan": plan.model_dump(mode="json") if plan else None}

    @app.post("/api/sources/{source_id}/generate", status_code=202)
    def generate_source_job(source_id: str):
        return enqueue_source(source_id, "generate")

    @app.get("/api/sources/{source_id}/clips")
    def get_clips(source_id: str):
        return clip_artifacts(service.catalog, service.paths, source_id)

    @app.post("/api/sources/{source_id}/clips/{clip_id}/decision")
    def decide_clip(
        source_id: str,
        clip_id: str,
        payload: DecisionPayload,
    ):
        return service.decide_clip(
            source_id,
            clip_id,
            payload.decision,
            payload.auditioned,
        )

    @app.post("/api/projects/{project_id}/exports", status_code=202)
    def create_export(project_id: str, payload: ExportCreate):
        service.catalog.get_project(project_id)
        return service.create_export(project_id, payload.name)

    @app.get("/api/projects/{project_id}/validate")
    def validate_project(project_id: str):
        return service.validate_project(project_id)

    @app.get("/api/projects/{project_id}/exports")
    def list_exports(project_id: str):
        return {"exports": service.catalog.list_exports(project_id)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return service.catalog.get_job(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str):
        job = service.catalog.retry_job(job_id)
        if job["kind"] == "export" and job["payload"].get("export_id"):
            service.catalog.update_export(
                str(job["payload"]["export_id"]),
                status="queued",
            )
        service.worker.wake()
        return job

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str):
        service.catalog.get_job(job_id)

        async def values():
            previous = None
            while True:
                job = service.catalog.get_job(job_id)
                serialized = json.dumps(job, ensure_ascii=False)
                if serialized != previous:
                    yield f"data: {serialized}\n\n"
                    previous = serialized
                if job["status"] in {"complete", "failed", "superseded"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(values(), media_type="text/event-stream")

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

    @app.get("/media/{source_id}/overlap/{region_id}/{channel}")
    def overlap_media(source_id: str, region_id: str, channel: str):
        field = {
            "original": "original_path",
            "assistant": "assistant_path",
            "user": "user_path",
        }.get(channel)
        if field is None:
            raise KeyError(channel)
        record = next(
            (
                value
                for value in service.catalog.overlap_recoveries(source_id)
                if value["region_id"] == region_id
            ),
            None,
        )
        if record is None or not record.get(field):
            raise KeyError(region_id)
        return FileResponse(
            service.paths.resolve_relative(record[field]),
            media_type="audio/wav",
        )

    @app.get("/media/{source_id}/clips/{clip_id}/{kind}")
    def clip_media(source_id: str, clip_id: str, kind: str):
        artifacts = clip_artifacts(service.catalog, service.paths, source_id)
        item = next(
            (
                value
                for value in artifacts.get("artifacts", [])
                if value["clip"]["id"] == clip_id
            ),
            None,
        )
        if item is None:
            raise KeyError(clip_id)
        field = {"audio": "wav_path", "alignment": "json_path"}.get(kind)
        if field is None:
            raise KeyError(kind)
        return FileResponse(
            service.paths.resolve_relative(item[field]),
            media_type="audio/wav" if kind == "audio" else "application/json",
        )

    static_root = Path(__file__).with_name("static")
    assets = static_root / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="studio-assets")

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
    port: int = 8765,
    *,
    config_path: Path | None = None,
    allow_remote: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        raise ValueError("Non-loopback studio hosting requires --allow-remote")
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            'The studio requires: pip install -e ".[review]"'
        ) from exc
    uvicorn.run(
        create_studio_app(workspace, load_config(config_path)),
        host=host,
        port=port,
        log_level="info",
    )
