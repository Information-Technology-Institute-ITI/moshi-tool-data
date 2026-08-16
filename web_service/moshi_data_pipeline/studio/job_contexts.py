from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.media import StudioPaths, load_json_file, safe_filename
from moshi_data_pipeline.studio.protocol import ArtifactRef, JobContext


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class JobContextBuilder:
    """Build immutable worker inputs without exposing SQL or absolute paths."""

    def __init__(
        self,
        catalog: StudioCatalog,
        paths: StudioPaths,
        config: PipelineConfig,
    ) -> None:
        self.catalog = catalog
        self.paths = paths
        self.config = config

    def _register_path(
        self,
        path: Path,
        role: str,
        *,
        project_id: str,
        source_id: str | None,
        known_sha256: str | None = None,
        known_size: int | None = None,
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        relative = self.paths.relative(path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return self.catalog.register_artifact(
            role=role,
            relative_path=relative,
            sha256=known_sha256 or file_sha256(path),
            size_bytes=known_size if known_size is not None else path.stat().st_size,
            media_type=media_type,
            project_id=project_id,
            source_id=source_id,
        )

    def _source_inputs(
        self,
        kind: str,
        source: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source_id = str(source["id"])
        project_id = str(source["project_id"])
        candidates: list[tuple[str, Path, str | None, int | None]] = []
        original = self.paths.resolve_relative(str(source["stored_path"]))
        if kind == "initialize":
            candidates.append(
                (
                    "source.original",
                    original,
                    str(source["sha256"]),
                    int(source["size_bytes"]),
                )
            )
        else:
            candidates.append(
                ("source.canonical", self.paths.canonical_audio(source_id), None, None)
            )
            candidates.append(
                ("source.channels", self.paths.canonical_channels(source_id), None, None)
            )
        if kind in {"realign", "review_transcript"}:
            candidates.append(
                (
                    "analysis.raw_transcript",
                    self.paths.artifact(source_id, "raw_transcript.json"),
                    None,
                    None,
                )
            )
        records = self.catalog.overlap_recoveries(source_id)
        if kind in {"transcribe_overlap", "generate", "export"}:
            for record in records:
                for field, role in (
                    ("original_path", "overlap.original"),
                    ("assistant_path", "overlap.assistant"),
                    ("user_path", "overlap.user"),
                ):
                    if record.get(field):
                        candidates.append(
                            (
                                f"overlap.{record['region_id']}.{role.split('.')[-1]}",
                                self.paths.resolve_relative(str(record[field])),
                                None,
                                None,
                            )
                        )
        if kind == "export" and source.get("clip_artifacts_path"):
            manifest_path = self.paths.resolve_relative(str(source["clip_artifacts_path"]))
            candidates.append(("clips.manifest", manifest_path, None, None))
            manifest = load_json_file(manifest_path, {}) or {}
            for item in manifest.get("artifacts", []):
                clip_id = str(item.get("clip", {}).get("id", "unknown"))
                for field, role in (("wav_path", "clip.audio"), ("json_path", "clip.alignment")):
                    if item.get(field):
                        candidates.append(
                            (
                                f"clip.{clip_id}.{role.split('.')[-1]}",
                                self.paths.resolve_relative(str(item[field])),
                                None,
                                None,
                            )
                        )
        registered_by_role = {
            str(item["role"]): item for item in self.catalog.list_artifacts(source_id=source_id)
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for role, path, known_hash, known_size in candidates:
            registered = registered_by_role.get(role)
            if registered is None:
                registered = self._register_path(
                    path,
                    role,
                    project_id=project_id,
                    source_id=source_id,
                    known_sha256=known_hash,
                    known_size=known_size,
                )
            if registered is not None and registered["id"] not in seen:
                result.append(registered)
                seen.add(str(registered["id"]))
        # Versioned remote artifacts are authoritative and may not have legacy paths.
        for role, registered in registered_by_role.items():
            if registered["id"] in seen:
                continue
            relevant = (
                role.startswith("source.")
                or (kind in {"realign", "review_transcript"} and role.startswith("analysis."))
                or (
                    kind in {"transcribe_overlap", "generate", "export"}
                    and role.startswith("overlap.")
                )
                or (kind == "export" and role.startswith("clip."))
            )
            if relevant:
                result.append(registered)
                seen.add(str(registered["id"]))
        return result

    @staticmethod
    def _source_snapshot(source: dict[str, Any]) -> dict[str, Any]:
        omitted = {"created_at", "updated_at", "inspection_json"}
        return {key: value for key, value in source.items() if key not in omitted}

    def snapshot(
        self,
        *,
        project_id: str,
        kind: str,
        source_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        project = self.catalog.get_project(project_id)
        preconditions: dict[str, Any] = {
            "project": {
                "id": project["id"],
                "name": project["name"],
                "language": project["language"],
            },
            "config_fingerprint": self.config.fingerprint(),
        }
        artifacts: list[dict[str, Any]] = []
        if source_id is not None:
            source = self.catalog.get_source(source_id)
            annotation = self.catalog.latest_annotation(source_id)
            preconditions["source"] = self._source_snapshot(source)
            preconditions["annotation"] = annotation.model_dump(mode="json")
            if kind in {"generate", "export"}:
                plan = self.catalog.get_clip_plan(source_id)
                preconditions["clip_plan"] = (
                    plan.model_dump(mode="json") if plan is not None else None
                )
                preconditions["overlap_recoveries"] = self.catalog.overlap_recoveries(source_id)
            if kind == "transcribe_overlap":
                region_id = str(payload.get("region_id", ""))
                preconditions["overlap_recovery"] = next(
                    (
                        record
                        for record in self.catalog.overlap_recoveries(source_id)
                        if record["region_id"] == region_id
                    ),
                    None,
                )
            artifacts.extend(self._source_inputs(kind, source))
        elif kind == "export":
            export_id = str(payload["export_id"])
            export = self.catalog.get_export(export_id)
            preconditions["export"] = export
            sources: list[dict[str, Any]] = []
            for source in self.catalog.list_sources(project_id):
                source_id_value = str(source["id"])
                annotation = self.catalog.latest_annotation(source_id_value)
                plan = self.catalog.get_clip_plan(source_id_value)
                sources.append(
                    {
                        "source": self._source_snapshot(source),
                        "annotation": annotation.model_dump(mode="json"),
                        "clip_plan": plan.model_dump(mode="json") if plan else None,
                        "clip_decisions": self.catalog.clip_decisions(source_id_value),
                        "overlap_recoveries": self.catalog.overlap_recoveries(source_id_value),
                    }
                )
                artifacts.extend(self._source_inputs(kind, source))
            preconditions["sources"] = sources
        artifact_snapshot = sorted(
            (
                {
                    "artifact_id": value["id"],
                    "role": value["role"],
                    "sha256": value["sha256"],
                    "size_bytes": value["size_bytes"],
                    "media_type": value["media_type"],
                    "filename": safe_filename(Path(str(value["relative_path"])).name),
                    "project_id": value.get("project_id"),
                    "source_id": value.get("source_id"),
                }
                for value in artifacts
            ),
            key=lambda value: str(value["artifact_id"]),
        )
        preconditions["input_artifacts"] = artifact_snapshot
        fingerprint = canonical_sha256(
            {
                "kind": kind,
                "project_id": project_id,
                "source_id": source_id,
                "payload": payload,
                "preconditions": preconditions,
            }
        )
        return preconditions, artifacts, fingerprint

    def create_context(self, job: dict[str, Any]) -> JobContext:
        artifact_snapshots = sorted(
            job["preconditions"].get("input_artifacts", []),
            key=lambda value: str(value["artifact_id"]),
        )
        inputs: list[ArtifactRef] = []
        for snapshot in artifact_snapshots:
            artifact_id = str(snapshot["artifact_id"])
            artifact = self.catalog.get_artifact(artifact_id)
            if artifact["state"] != "active":
                raise ValueError("Job input artifact is no longer active")
            expected_fields = {
                "role": str(snapshot["role"]),
                "sha256": str(snapshot["sha256"]),
                "size_bytes": int(snapshot["size_bytes"]),
            }
            for optional in ("media_type", "project_id", "source_id"):
                if optional in snapshot:
                    expected_fields[optional] = snapshot[optional]
            if any(artifact[key] != value for key, value in expected_fields.items()):
                raise ValueError("Job input artifact metadata changed after enqueue")
            path = self.paths.resolve_relative(str(artifact["relative_path"]))
            if not path.is_file():
                raise ValueError("Job input artifact is missing")
            if path.stat().st_size != int(snapshot["size_bytes"]):
                raise ValueError("Job input artifact size changed after enqueue")
            if file_sha256(path) != str(snapshot["sha256"]):
                raise ValueError("Job input artifact checksum changed after enqueue")
            filename = str(snapshot.get("filename") or safe_filename(path.name))
            if filename != safe_filename(path.name):
                raise ValueError("Job input artifact filename changed after enqueue")
            inputs.append(
                ArtifactRef(
                    artifact_id=artifact_id,
                    role=str(snapshot["role"]),
                    sha256=str(snapshot["sha256"]),
                    size_bytes=int(snapshot["size_bytes"]),
                    media_type=str(snapshot.get("media_type") or artifact["media_type"]),
                    filename=filename,
                    project_id=(
                        str(snapshot.get("project_id") or artifact["project_id"])
                        if snapshot.get("project_id") or artifact.get("project_id")
                        else None
                    ),
                    source_id=(
                        str(snapshot.get("source_id") or artifact["source_id"])
                        if snapshot.get("source_id") or artifact.get("source_id")
                        else None
                    ),
                )
            )
        return JobContext(
            protocol_version=str(job["protocol_version"]),
            job_id=str(job["id"]),
            kind=job["kind"],
            attempt=int(job["attempt"]),
            lease_expires_at=str(job["lease_expires_at"]),
            input_fingerprint=str(job["input_fingerprint"]),
            payload=dict(job["payload"]),
            preconditions=dict(job["preconditions"]),
            config=self.config.model_dump(mode="json"),
            inputs=inputs,
        )

    def current_fingerprint(self, job: dict[str, Any]) -> str:
        preconditions, _, fingerprint = self.snapshot(
            project_id=str(job["project_id"]),
            kind=str(job["kind"]),
            source_id=str(job["source_id"]) if job.get("source_id") else None,
            payload=dict(job["payload"]),
        )
        frozen_inputs = job.get("preconditions", {}).get("input_artifacts", [])
        if frozen_inputs:
            frozen_keys = {str(value["artifact_id"]): set(value) for value in frozen_inputs}
            current_inputs = preconditions.get("input_artifacts", [])
            current_by_id = {
                str(value["artifact_id"]): value for value in current_inputs
            }
            ordered_ids = [str(value["artifact_id"]) for value in frozen_inputs]
            ordered_ids.extend(
                str(value["artifact_id"])
                for value in current_inputs
                if str(value["artifact_id"]) not in frozen_keys
            )
            preconditions["input_artifacts"] = [
                {
                    key: value
                    for key, value in current_by_id[artifact_id].items()
                    if key in frozen_keys.get(artifact_id, set(current_by_id[artifact_id]))
                }
                for artifact_id in ordered_ids
                if artifact_id in current_by_id
            ]
            fingerprint = canonical_sha256(
                {
                    "kind": str(job["kind"]),
                    "project_id": str(job["project_id"]),
                    "source_id": (str(job["source_id"]) if job.get("source_id") else None),
                    "payload": dict(job["payload"]),
                    "preconditions": preconditions,
                }
            )
        return fingerprint
