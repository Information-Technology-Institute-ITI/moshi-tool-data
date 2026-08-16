from __future__ import annotations

import json
import mimetypes
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import Field

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.gpu_job_protocol import JobContext, StrictModel
from moshi_data_pipeline.studio.domain import AnnotationDocument, ClipPlanDocument
from moshi_data_pipeline.studio.exporter import build_project_export
from moshi_data_pipeline.studio.job_contracts import (
    AnnotationResult,
    ExportResult,
    GenerationResult,
    InitializeResult,
    OverlapTranscriptResult,
    RecoveryResult,
)
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

Progress = Any


class ProducedFile(StrictModel):
    role: str = Field(min_length=1, max_length=120)
    path: Path
    media_type: str = Field(max_length=200)


class ExecutionOutput(StrictModel):
    result: dict[str, Any]
    artifacts: list[ProducedFile] = Field(default_factory=list)


class AttemptPaths:
    """Isolated filesystem implementation used for one GPU processing attempt."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.originals = self.root / "originals"
        self.sources = self.root / "sources"
        self.exports = self.root / "exports"
        for directory in (self.root, self.originals, self.sources, self.exports):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_relative(self, value: str) -> Path:
        if Path(value).is_absolute():
            raise ValueError("Attempt paths must be relative")
        path = (self.root / value).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("Path escapes the job attempt")
        return path

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError("Path is outside the job attempt")
        return resolved.relative_to(self.root).as_posix()

    def source_root(self, source_id: str) -> Path:
        if Path(source_id).name != source_id or source_id in {"", ".", ".."}:
            raise ValueError("Invalid source ID")
        result = self.sources / source_id
        result.mkdir(parents=True, exist_ok=True)
        return result

    def canonical_audio(self, source_id: str) -> Path:
        return self.source_root(source_id) / "canonical.wav"

    def canonical_channels(self, source_id: str) -> Path:
        return self.source_root(source_id) / "canonical_channels.wav"

    def video_proxy(self, source_id: str) -> Path:
        return self.source_root(source_id) / "proxy.mp4"

    def peaks(self, source_id: str) -> Path:
        return self.source_root(source_id) / "peaks.json"

    def artifact(self, source_id: str, name: str) -> Path:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("Invalid artifact name")
        return self.source_root(source_id) / name


class MemoryProcessingState:
    """Job-scoped state. It has no connection to the authoritative web database."""

    def __init__(self, context: JobContext) -> None:
        snapshot = deepcopy(context.preconditions)
        project = snapshot["project"]
        self.projects = {str(project["id"]): project}
        self.sources: dict[str, dict[str, Any]] = {}
        self.annotations: dict[str, AnnotationDocument] = {}
        self.plans: dict[str, ClipPlanDocument] = {}
        self.decisions: dict[str, dict[str, dict[str, Any]]] = {}
        self.recoveries: dict[str, list[dict[str, Any]]] = {}
        self.exports: dict[str, dict[str, Any]] = {}
        source_snapshots = snapshot.get("sources", [])
        if "source" in snapshot:
            source_snapshots = [
                {
                    "source": snapshot["source"],
                    "annotation": snapshot.get("annotation"),
                    "clip_plan": snapshot.get("clip_plan"),
                    "clip_decisions": snapshot.get("clip_decisions", {}),
                    "overlap_recoveries": snapshot.get("overlap_recoveries", []),
                }
            ]
        for value in source_snapshots:
            source = deepcopy(value["source"])
            source_id = str(source["id"])
            self.sources[source_id] = source
            self.annotations[source_id] = AnnotationDocument.model_validate(
                value.get("annotation") or {"source_id": source_id}
            )
            if value.get("clip_plan"):
                self.plans[source_id] = ClipPlanDocument.model_validate(value["clip_plan"])
            self.decisions[source_id] = deepcopy(value.get("clip_decisions", {}))
            recoveries = deepcopy(value.get("overlap_recoveries", []))
            if snapshot.get("overlap_recovery") is not None:
                recoveries = [deepcopy(snapshot["overlap_recovery"])]
            self.recoveries[source_id] = recoveries
        if snapshot.get("export"):
            export = deepcopy(snapshot["export"])
            self.exports[str(export["id"])] = export

    def get_project(self, project_id: str) -> dict[str, Any]:
        return deepcopy(self.projects[project_id])

    def get_source(self, source_id: str) -> dict[str, Any]:
        return deepcopy(self.sources[source_id])

    def list_sources(self, project_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(value) for value in self.sources.values() if value["project_id"] == project_id
        ]

    def latest_annotation(self, source_id: str) -> AnnotationDocument:
        return self.annotations[source_id].model_copy(deep=True)

    def save_annotation(
        self,
        source_id: str,
        expected_version: int,
        annotation: AnnotationDocument,
    ) -> AnnotationDocument:
        current = self.annotations[source_id]
        if current.version != expected_version:
            raise ValueError(
                f"Expected annotation version {expected_version}, current is {current.version}"
            )
        saved = annotation.model_copy(
            deep=True,
            update={"source_id": source_id, "version": expected_version + 1},
        )
        self.annotations[source_id] = saved
        self.sources[source_id]["active_annotation_version"] = saved.version
        self.sources[source_id]["clips_stale"] = True
        self.sources[source_id]["clip_artifacts_path"] = None
        self.plans.pop(source_id, None)
        self.decisions[source_id] = {}
        self.recoveries[source_id] = []
        return saved.model_copy(deep=True)

    def replace_initial_annotation(
        self,
        source_id: str,
        annotation: AnnotationDocument,
    ) -> AnnotationDocument:
        return self.save_annotation(
            source_id,
            self.annotations[source_id].version,
            annotation,
        )

    def update_source(self, source_id: str, **values: Any) -> dict[str, Any]:
        self.sources[source_id].update(deepcopy(values))
        return self.get_source(source_id)

    def get_clip_plan(self, source_id: str) -> ClipPlanDocument | None:
        plan = self.plans.get(source_id)
        return plan.model_copy(deep=True) if plan is not None else None

    def clip_decisions(self, source_id: str) -> dict[str, dict[str, Any]]:
        return deepcopy(self.decisions.get(source_id, {}))

    def overlap_recoveries(self, source_id: str) -> list[dict[str, Any]]:
        return deepcopy(self.recoveries.get(source_id, []))

    def replace_overlap_recoveries(
        self,
        source_id: str,
        annotation_version: int,
        records: list[dict[str, Any]],
    ) -> None:
        self.recoveries[source_id] = [
            {
                **deepcopy(record),
                "source_id": source_id,
                "annotation_version": annotation_version,
                "decision": record.get("decision"),
                "auditioned": bool(record.get("auditioned", False)),
            }
            for record in records
        ]
        self.sources[source_id]["clips_stale"] = True
        self.sources[source_id]["clip_artifacts_path"] = None

    def update_overlap_details(
        self,
        source_id: str,
        region_id: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        for record in self.recoveries[source_id]:
            if record["region_id"] == region_id and record["status"] == "recovered":
                record["details"] = deepcopy(details)
                return deepcopy(record)
        raise KeyError(region_id)

    def get_export(self, export_id: str) -> dict[str, Any]:
        return deepcopy(self.exports[export_id])


class ContextJobExecutor:
    def __init__(self, attempt_root: Path) -> None:
        self.paths = AttemptPaths(attempt_root)

    @staticmethod
    def _copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def materialize(
        self,
        context: JobContext,
        inputs: dict[str, Path],
        state: MemoryProcessingState,
    ) -> None:
        by_source: dict[str, list[tuple[Any, Path]]] = {}
        for artifact in context.inputs:
            try:
                source_path = inputs[artifact.artifact_id]
            except KeyError as exc:
                raise ValueError(f"Missing materialized input {artifact.artifact_id}") from exc
            if not source_path.is_file():
                raise ValueError(f"Materialized input does not exist: {artifact.artifact_id}")
            by_source.setdefault(artifact.source_id or "", []).append((artifact, source_path))
        for source_id, source in state.sources.items():
            for artifact, source_path in by_source.get(source_id, []):
                role = str(artifact.role)
                if role == "source.original":
                    destination = self.paths.originals / artifact.filename
                    self._copy(source_path, destination)
                    source["stored_path"] = self.paths.relative(destination)
                elif role == "source.canonical":
                    self._copy(source_path, self.paths.canonical_audio(source_id))
                elif role == "source.channels":
                    self._copy(source_path, self.paths.canonical_channels(source_id))
                elif role == "source.proxy":
                    self._copy(source_path, self.paths.video_proxy(source_id))
                elif role == "source.peaks":
                    self._copy(source_path, self.paths.peaks(source_id))
                elif role == "analysis.raw_transcript":
                    self._copy(
                        source_path,
                        self.paths.artifact(source_id, "raw_transcript.json"),
                    )
                elif role == "analysis.aligned_transcript":
                    self._copy(
                        source_path,
                        self.paths.artifact(source_id, "aligned_transcript.json"),
                    )
                elif role == "analysis.diarization":
                    self._copy(
                        source_path,
                        self.paths.artifact(source_id, "diarization.json"),
                    )
                elif role.startswith("overlap."):
                    _, region_id, channel = role.split(".", 2)
                    destination = (
                        self.paths.source_root(source_id)
                        / "recoveries"
                        / region_id
                        / f"{channel}.wav"
                    )
                    self._copy(source_path, destination)
                    field = {
                        "assistant": "assistant_path",
                        "user": "user_path",
                        "original": "original_path",
                    }.get(channel)
                    if field:
                        for record in state.recoveries.get(source_id, []):
                            if record["region_id"] == region_id:
                                record[field] = self.paths.relative(destination)
                elif role == "clips.manifest":
                    destination = (
                        self.paths.source_root(source_id) / "input_clips" / "manifest.json"
                    )
                    self._copy(source_path, destination)
                    source["clip_artifacts_path"] = self.paths.relative(destination)
            self._materialize_clip_inputs(
                source_id,
                by_source.get(source_id, []),
                state,
            )

    def _materialize_clip_inputs(
        self,
        source_id: str,
        inputs: list[tuple[Any, Path]],
        state: MemoryProcessingState,
    ) -> None:
        stored = state.sources[source_id].get("clip_artifacts_path")
        if not stored:
            return
        manifest_path = self.paths.resolve_relative(str(stored))
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_role = {str(artifact.role): path for artifact, path in inputs}
        for item in manifest.get("artifacts", []):
            clip_id = str(item["clip"]["id"])
            for suffix, field, extension in (
                ("audio", "wav_path", ".wav"),
                ("alignment", "json_path", ".json"),
            ):
                role = f"clip.{clip_id}.{suffix}"
                if role not in by_role:
                    continue
                destination = manifest_path.parent / f"{clip_id}{extension}"
                self._copy(by_role[role], destination)
                item[field] = self.paths.relative(destination)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    @staticmethod
    def _media_type(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def _artifact(self, role: str, path: Path) -> ProducedFile | None:
        if not path.is_file():
            return None
        return ProducedFile(role=role, path=path, media_type=self._media_type(path))

    def _source_artifacts(self, source_id: str, kind: str) -> list[ProducedFile]:
        candidates: list[tuple[str, Path]] = []
        if kind == "initialize":
            candidates.extend(
                [
                    ("source.canonical", self.paths.canonical_audio(source_id)),
                    ("source.channels", self.paths.canonical_channels(source_id)),
                    ("source.proxy", self.paths.video_proxy(source_id)),
                    ("source.peaks", self.paths.peaks(source_id)),
                    (
                        "analysis.raw_transcript",
                        self.paths.artifact(source_id, "raw_transcript.json"),
                    ),
                    (
                        "analysis.aligned_transcript",
                        self.paths.artifact(source_id, "aligned_transcript.json"),
                    ),
                    ("analysis.diarization", self.paths.artifact(source_id, "diarization.json")),
                ]
            )
        elif kind == "transcribe":
            candidates.extend(
                [
                    (
                        "analysis.raw_transcript",
                        self.paths.artifact(source_id, "raw_transcript.json"),
                    ),
                    (
                        "analysis.aligned_transcript",
                        self.paths.artifact(source_id, "aligned_transcript.json"),
                    ),
                ]
            )
        elif kind == "rediarize":
            candidates.append(
                ("analysis.diarization", self.paths.artifact(source_id, "diarization.json"))
            )
        elif kind == "realign":
            candidates.append(
                (
                    "analysis.aligned_transcript",
                    self.paths.artifact(source_id, "aligned_transcript.json"),
                )
            )
        return [value for role, path in candidates if (value := self._artifact(role, path))]

    def execute(
        self,
        context: JobContext,
        inputs: dict[str, Path],
        progress: Progress,
    ) -> ExecutionOutput:
        config = PipelineConfig.model_validate(context.config)
        state = MemoryProcessingState(context)
        self.materialize(context, inputs, state)
        source_id = (
            str(context.preconditions["source"]["id"])
            if "source" in context.preconditions
            else None
        )
        expected_version = (
            int(context.preconditions["annotation"].get("version", 0))
            if "annotation" in context.preconditions
            else 0
        )
        kind = context.kind
        artifacts: list[ProducedFile] = []
        if kind == "initialize":
            assert source_id is not None
            initialize_source(
                state,
                self.paths,
                source_id,
                str(context.payload["mode"]),
                config,
                progress,
            )
            source = state.get_source(source_id)
            result = InitializeResult(
                source_id=source_id,
                annotation=state.latest_annotation(source_id).model_dump(mode="json"),
                inspection=source.get("inspection", {}),
                duration_samples=int(source.get("duration_samples") or 0),
                source_updates={
                    key: source.get(key)
                    for key in (
                        "status",
                        "init_mode",
                        "duration_samples",
                        "clips_stale",
                        "inspection",
                    )
                },
            )
            artifacts = self._source_artifacts(source_id, kind)
        elif kind in {"transcribe", "review_transcript", "rediarize", "realign"}:
            assert source_id is not None
            operation = {
                "transcribe": transcribe_source,
                "review_transcript": generate_review_candidates,
                "rediarize": rediarize_source,
                "realign": realign_source,
            }[kind]
            operation(state, self.paths, source_id, config, progress)
            result = AnnotationResult(
                kind=kind,
                source_id=source_id,
                expected_annotation_version=expected_version,
                annotation=state.latest_annotation(source_id).model_dump(mode="json"),
            )
            artifacts = self._source_artifacts(source_id, kind)
        elif kind == "recover_overlap":
            assert source_id is not None
            recover_source_overlaps(state, self.paths, source_id, config, progress)
            recoveries = state.overlap_recoveries(source_id)
            for record in recoveries:
                for field, suffix in (
                    ("original_path", "original"),
                    ("assistant_path", "assistant"),
                    ("user_path", "user"),
                ):
                    if record.get(field):
                        path = self.paths.resolve_relative(str(record[field]))
                        value = self._artifact(f"overlap.{record['region_id']}.{suffix}", path)
                        if value:
                            artifacts.append(value)
                    record.pop(field, None)
            result = RecoveryResult(
                source_id=source_id,
                expected_annotation_version=expected_version,
                recoveries=recoveries,
            )
        elif kind == "transcribe_overlap":
            assert source_id is not None
            region_id = str(context.payload["region_id"])
            transcribe_overlap_stems(
                state,
                self.paths,
                source_id,
                region_id,
                config,
                progress,
            )
            record = next(
                item
                for item in state.overlap_recoveries(source_id)
                if item["region_id"] == region_id
            )
            result = OverlapTranscriptResult(
                source_id=source_id,
                region_id=region_id,
                expected_annotation_version=expected_version,
                stem_transcripts=record["details"].get("stem_transcripts", {}),
            )
        elif kind == "generate":
            assert source_id is not None
            render_source_clips(state, self.paths, source_id, config, progress)
            source = state.get_source(source_id)
            manifest_path = self.paths.resolve_relative(str(source["clip_artifacts_path"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("artifacts", []):
                clip_id = str(item["clip"]["id"])
                wav_path = self.paths.resolve_relative(str(item.pop("wav_path")))
                json_path = self.paths.resolve_relative(str(item.pop("json_path")))
                wav_role = f"clip.{clip_id}.audio"
                json_role = f"clip.{clip_id}.alignment"
                item["wav_role"] = wav_role
                item["json_role"] = json_role
                artifacts.extend(
                    [
                        ProducedFile(role=wav_role, path=wav_path, media_type="audio/wav"),
                        ProducedFile(role=json_role, path=json_path, media_type="application/json"),
                    ]
                )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            artifacts.append(
                ProducedFile(
                    role="clips.manifest",
                    path=manifest_path,
                    media_type="application/json",
                )
            )
            result = GenerationResult(
                source_id=source_id,
                expected_annotation_version=expected_version,
                clip_manifest=manifest,
            )
        elif kind == "export":
            export_id = str(context.payload["export_id"])
            built = build_project_export(
                state,
                self.paths,
                export_id,
                config,
                progress,
            )
            export_root = self.paths.resolve_relative(str(built["path"]))
            archive = self.paths.exports / f"{export_id}.zip"
            files: list[str] = []
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(export_root.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(export_root).as_posix()
                        files.append(relative)
                        bundle.write(path, relative)
            artifacts = [
                ProducedFile(
                    role="export.bundle",
                    path=archive,
                    media_type="application/zip",
                )
            ]
            result = ExportResult(
                export_id=export_id,
                report=built["report"],
                export_manifest={"files": files, "format": "zip"},
            )
        else:  # pragma: no cover - JobContext rejects unknown kinds.
            raise ValueError(f"Unknown job kind: {kind}")
        return ExecutionOutput(
            result=result.model_dump(mode="json"),
            artifacts=artifacts,
        )
