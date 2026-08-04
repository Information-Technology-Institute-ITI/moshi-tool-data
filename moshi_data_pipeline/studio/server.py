import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from moshi_data_pipeline.audio.ffmpeg import SUPPORTED_EXTENSIONS
from moshi_data_pipeline.config import PipelineConfig, load_config
from moshi_data_pipeline.studio.catalog import VersionConflictError
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
from moshi_data_pipeline.studio.media import store_upload
from moshi_data_pipeline.studio.processing import clip_artifacts
from moshi_data_pipeline.studio.service import StudioService


def create_studio_app(
    workspace: Path,
    config: PipelineConfig | None = None,
    *,
    start_worker: bool = True,
):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Request
        from fastapi.responses import (
            FileResponse,
            HTMLResponse,
            JSONResponse,
            StreamingResponse,
        )
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            'The studio requires: pip install -e ".[review]"'
        ) from exc

    service = StudioService(workspace.resolve(), config or load_config())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            service.worker.start()
        try:
            yield
        finally:
            if start_worker:
                service.worker.stop()

    app = FastAPI(
        title="Moshi Dataset Studio",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.studio = service

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

    @app.get("/api/health")
    def health():
        return {"status": "ok", "workspace": str(service.paths.root)}

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
                if job["status"] in {"complete", "failed"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(values(), media_type="text/event-stream")

    def source_file(source_id: str, kind: str) -> Path:
        source = service.catalog.get_source(source_id)
        if kind == "original":
            return service.paths.resolve_relative(source["stored_path"])
        if kind == "canonical":
            return service.paths.canonical_audio(source_id)
        if kind == "proxy":
            return service.paths.video_proxy(source_id)
        if kind == "peaks":
            return service.paths.peaks(source_id)
        raise KeyError(kind)

    @app.get("/media/{source_id}/{kind}")
    def media(source_id: str, kind: str):
        path = source_file(source_id, kind)
        if not path.is_file():
            raise KeyError(kind)
        media_types = {
            "canonical": "audio/wav",
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
        if path.startswith(("api/", "media/")):
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
