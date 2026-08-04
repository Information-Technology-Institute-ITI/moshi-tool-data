from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import (
    AnnotationDocument,
    ClipPlanRequest,
    SourceRights,
)
from moshi_data_pipeline.studio.exporter import validate_project_export
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.normalization import normalize_annotation_bounds
from moshi_data_pipeline.studio.planning import (
    derived_overlaps,
    derived_silences,
    propose_clip_plan,
    validate_annotation,
)
from moshi_data_pipeline.studio.processing import clip_artifacts
from moshi_data_pipeline.studio.quality_metrics import source_quality_metrics
from moshi_data_pipeline.studio.worker import StudioWorker


class StudioService:
    def __init__(self, workspace: Path, config: PipelineConfig):
        self.paths = StudioPaths(workspace)
        self.catalog = StudioCatalog(self.paths.database)
        self._repair_annotation_bounds()
        self.worker = StudioWorker(self.catalog, self.paths, config)

    def _repair_annotation_bounds(self) -> None:
        """Create a corrected revision for legacy model timestamps past EOF."""
        for project in self.catalog.list_projects():
            for source in self.catalog.list_sources(project["id"]):
                duration = int(source["duration_samples"] or 0)
                if duration <= 0:
                    continue
                annotation = self.catalog.latest_annotation(source["id"])
                normalized = normalize_annotation_bounds(annotation, duration)
                if normalized.model_dump(mode="json") != annotation.model_dump(mode="json"):
                    self.catalog.save_annotation(
                        source["id"], annotation.version, normalized
                    )

    def enqueue(
        self,
        project_id: str,
        kind: str,
        source_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source_id is not None:
            if kind == "realign" and payload and "annotation_version" in payload:
                requested_version = int(payload["annotation_version"])
                for active in self.catalog.active_jobs(source_id, kind):
                    if int(active["payload"].get("annotation_version", -1)) == requested_version:
                        return active
            else:
                active = self.catalog.active_job(source_id, kind)
                if active is not None:
                    return active
        job = self.catalog.create_job(project_id, kind, source_id, payload)
        self.worker.wake()
        return job

    def source_detail(self, source_id: str) -> dict[str, Any]:
        source = self.catalog.get_source(source_id)
        annotation = self.catalog.latest_annotation(source_id)
        duration = int(source["duration_samples"] or 0)
        overlap_recoveries = self.catalog.overlap_recoveries(source_id)
        return {
            **source,
            "annotation": annotation.model_dump(mode="json"),
            "annotation_revisions": self.catalog.annotation_revisions(source_id),
            "overlaps": [
                {"start_sample": start, "end_sample": end}
                for start, end in derived_overlaps(annotation.activities)
            ],
            "silences": [
                {"start_sample": start, "end_sample": end}
                for start, end in derived_silences(annotation.activities, duration)
            ]
            if duration
            else [],
            "overlap_recoveries": overlap_recoveries,
            "quality_dashboard": source_quality_metrics(
                annotation, overlap_recoveries
            ),
            "clip_plan": (
                self.catalog.get_clip_plan(source_id).model_dump(mode="json")
                if self.catalog.get_clip_plan(source_id)
                else None
            ),
            "clip_artifacts": clip_artifacts(self.catalog, self.paths, source_id),
            "urls": {
                "canonical_audio": f"/media/{source_id}/canonical",
                "video_proxy": (
                    f"/media/{source_id}/proxy"
                    if self.paths.video_proxy(source_id).exists()
                    else None
                ),
                "peaks": (
                    f"/media/{source_id}/peaks"
                    if self.paths.peaks(source_id).exists()
                    else None
                ),
                "original": f"/media/{source_id}/original",
            },
        }

    def save_rights(self, source_id: str, rights: SourceRights) -> dict[str, Any]:
        return self.catalog.update_source(
            source_id,
            origin=rights.origin,
            rights_basis=rights.rights_basis,
            rights_notes=rights.rights_notes,
            rights_confirmed=rights.rights_confirmed,
        )

    def save_annotation(
        self,
        source_id: str,
        expected_version: int,
        annotation: AnnotationDocument,
    ) -> AnnotationDocument:
        source = self.catalog.get_source(source_id)
        if annotation.source_id != source_id:
            raise ValueError("Annotation source_id does not match the route")
        current = self.catalog.latest_annotation(source_id)
        prior = {value.id: value for value in current.transcript}
        transcript = []
        for utterance in annotation.transcript:
            previous = prior.get(utterance.id)
            alignment_input_changed = previous is None or (
                previous.speaker,
                previous.start_sample,
                previous.end_sample,
                previous.text,
            ) != (
                utterance.speaker,
                utterance.start_sample,
                utterance.end_sample,
                utterance.text,
            )
            transcript.append(
                utterance.model_copy(
                    update={
                        "human_verified": (
                            False
                            if alignment_input_changed
                            else utterance.human_verified
                        )
                    }
                )
            )
        normalized = normalize_annotation_bounds(
            annotation.model_copy(update={"transcript": transcript}),
            int(source["duration_samples"] or 0),
        )
        errors = validate_annotation(
            normalized, int(source["duration_samples"] or 0)
        )
        if errors:
            raise ValueError("; ".join(errors))
        return self.catalog.save_annotation(
            source_id, expected_version, normalized
        )

    def plan_clips(
        self, source_id: str, request: ClipPlanRequest
    ) -> dict[str, Any]:
        source = self.catalog.get_source(source_id)
        annotation = self.catalog.latest_annotation(source_id)
        if annotation.assistant_speaker is None:
            raise ValueError("Choose the Moshi speaker before planning clips")
        if not annotation.activities_finalized:
            raise ValueError("Finalize the human speaker regions before planning clips")
        plan = propose_clip_plan(
            source_id,
            annotation,
            int(source["duration_samples"]),
            request,
        )
        return self.catalog.save_clip_plan(plan).model_dump(mode="json")

    def decide_clip(
        self,
        source_id: str,
        clip_id: str,
        decision: str,
        auditioned: bool,
    ) -> dict[str, Any]:
        artifacts = clip_artifacts(self.catalog, self.paths, source_id)
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
        if decision == "approve" and item["qc"]["status"] == "REJECT":
            raise ValueError("A rejected-QC clip cannot be approved")
        return self.catalog.save_clip_decision(
            source_id, clip_id, decision, auditioned
        )

    def create_export(self, project_id: str, name: str) -> dict[str, Any]:
        version = self.catalog.next_export_version(project_id)
        export = self.catalog.create_export(project_id, name, version)
        job = self.enqueue(
            project_id,
            "export",
            payload={"export_id": export["id"]},
        )
        return {"export": export, "job": job}

    def validate_project(self, project_id: str) -> dict[str, Any]:
        self.catalog.get_project(project_id)
        return validate_project_export(self.catalog, self.paths, project_id)

    def delete_source(self, source_id: str) -> dict[str, Any]:
        source = self.catalog.get_source(source_id)
        if self.catalog.active_source_jobs(source_id):
            raise ValueError("Wait for active source jobs to finish before deletion")
        if any(
            job["kind"] == "export" and job["status"] in {"queued", "running"}
            for job in self.catalog.list_jobs(source["project_id"])
        ):
            raise ValueError("Wait for the active project export before deletion")
        original = self.paths.resolve_relative(source["stored_path"])
        source_root = self.paths.source_root(source_id).resolve()
        if self.paths.root not in source_root.parents:
            raise ValueError("Refusing to remove a source outside the workspace")
        if source_root.exists():
            shutil.rmtree(source_root)
        if original.exists():
            original.unlink()
        return self.catalog.delete_source(source_id)
