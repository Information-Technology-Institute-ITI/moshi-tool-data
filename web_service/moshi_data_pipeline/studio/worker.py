from __future__ import annotations

import logging
import threading
from typing import Any

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.exporter import build_project_export
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.processing import (
    generate_review_candidates,
    initialize_source,
    realign_source,
    recover_source_overlaps,
    rediarize_source,
    render_source_clips,
    transcribe_overlap_stems,
    transcribe_source,
)
from moshi_data_pipeline.transcription.whisperx_backend import release_model

LOGGER = logging.getLogger(__name__)


class StudioWorker:
    def __init__(
        self,
        catalog: StudioCatalog,
        paths: StudioPaths,
        config: PipelineConfig,
    ):
        self.catalog = catalog
        self.paths = paths
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="moshi-studio-worker",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.catalog.claim_job()
            if job is None:
                self._wake.wait(0.75)
                self._wake.clear()
                continue
            self._execute(job)

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])

        def progress(value: float, message: str) -> None:
            self.catalog.update_job(
                job_id,
                progress=value,
                message=message,
            )

        try:
            config = self.config.model_copy(deep=True)
            kind = str(job["kind"])
            source_id = job.get("source_id")
            if kind == "initialize":
                if source_id is None:
                    raise ValueError("Initialization job is missing source_id")
                result = initialize_source(
                    self.catalog,
                    self.paths,
                    source_id,
                    str(job["payload"].get("mode", "manual")),
                    config,
                    progress,
                )
            elif kind == "transcribe":
                if source_id is None:
                    raise ValueError("Transcription job is missing source_id")
                result = transcribe_source(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "review_transcript":
                if source_id is None:
                    raise ValueError("Transcript-review job is missing source_id")
                result = generate_review_candidates(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "rediarize":
                if source_id is None:
                    raise ValueError("Diarization job is missing source_id")
                result = rediarize_source(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "realign":
                if source_id is None:
                    raise ValueError("Realignment job is missing source_id")
                result = realign_source(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "recover_overlap":
                if source_id is None:
                    raise ValueError("Overlap job is missing source_id")
                result = recover_source_overlaps(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "transcribe_overlap":
                if source_id is None:
                    raise ValueError("Overlap transcription job is missing source_id")
                result = transcribe_overlap_stems(
                    self.catalog,
                    self.paths,
                    source_id,
                    str(job["payload"]["region_id"]),
                    config,
                    progress,
                )
            elif kind == "generate":
                if source_id is None:
                    raise ValueError("Generation job is missing source_id")
                result = render_source_clips(
                    self.catalog, self.paths, source_id, config, progress
                )
            elif kind == "export":
                export_id = str(job["payload"]["export_id"])
                result = build_project_export(
                    self.catalog, self.paths, export_id, config, progress
                )
                self.catalog.update_export(
                    export_id,
                    status="complete",
                    path=result["path"],
                    report=result["report"],
                )
            else:
                raise ValueError(f"Unknown job kind: {kind}")
            self.catalog.update_job(
                job_id,
                status="complete",
                progress=1.0,
                message="Complete",
                result=result,
            )
        except Exception as exc:
            message = str(exc)
            token = __import__("os").environ.get("HF_TOKEN")
            if token:
                message = message.replace(token, "<redacted>")
            LOGGER.exception("Studio job %s failed", job_id)
            if job["kind"] == "export" and job["payload"].get("export_id"):
                self.catalog.update_export(
                    str(job["payload"]["export_id"]),
                    status="failed",
                    report={"error": message},
                )
            self.catalog.update_job(
                job_id,
                status="failed",
                message="Failed",
                error=message,
            )
        finally:
            release_model()
