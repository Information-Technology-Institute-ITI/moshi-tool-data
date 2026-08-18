from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.gpu_job_protocol import JOB_KINDS as GPU_JOB_KINDS
from moshi_data_pipeline.studio.artifacts import ArtifactStore
from moshi_data_pipeline.studio.catalog import PrincipalLike, StudioCatalog
from moshi_data_pipeline.studio.clip_registry import clip_artifacts
from moshi_data_pipeline.studio.domain import (
    AnnotationDocument,
    ClipPlanRequest,
    SourceRights,
)
from moshi_data_pipeline.studio.exporter import validate_project_export
from moshi_data_pipeline.studio.gpu_dispatcher import (
    GpuDispatcherSettings,
    GpuPushDispatcher,
)
from moshi_data_pipeline.studio.gpu_status import gpu_status_payload, public_gpu_check
from moshi_data_pipeline.studio.job_contexts import JobContextBuilder
from moshi_data_pipeline.studio.job_contracts import (
    AnnotationResult,
    ExportResult,
    GenerationResult,
    InitializeResult,
    OverlapTranscriptResult,
    RecoveryResult,
    validate_job_result,
)
from moshi_data_pipeline.studio.lifecycle import (
    LifecycleController,
    LifecycleProvider,
    LocalLifecycleProvider,
)
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.normalization import normalize_annotation_bounds
from moshi_data_pipeline.studio.planning import (
    derived_overlaps,
    derived_silences,
    propose_clip_plan,
    validate_annotation,
)
from moshi_data_pipeline.studio.quality_metrics import source_quality_metrics


class _DisabledLocalWorker:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wake(self) -> None:
        pass


class _DisabledGpuDispatcher:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wake(self) -> None:
        pass


class StudioService:
    def __init__(
        self,
        workspace: Path,
        config: PipelineConfig,
        *,
        lifecycle_provider: LifecycleProvider | None = None,
        deployment_generation: str = "local",
        enable_local_worker: bool = True,
        metrics_publisher: Callable[[dict[str, Any]], None] | None = None,
        gpu_dispatcher_settings: GpuDispatcherSettings | None = None,
        gpu_dispatch_client: Any = None,
        lifecycle_idle_seconds: int = 15 * 60,
        gpu_check_cooldown_seconds: int = 10 * 60,
        gpu_cold_start_cooldown_seconds: int = 30 * 60,
    ):
        if enable_local_worker and gpu_dispatcher_settings is not None:
            raise ValueError("The local worker and GPU push dispatcher cannot run together")
        self.config = config
        self.paths = StudioPaths(workspace)
        self.catalog = StudioCatalog(self.paths.database)
        self.artifacts = ArtifactStore(self.catalog, self.paths)
        self.artifacts.reconcile_commits()
        self.contexts = JobContextBuilder(self.catalog, self.paths, config)
        self._repair_annotation_bounds()
        self.catalog.repair_orphaned_processing_sources()
        if enable_local_worker:
            from moshi_data_pipeline.studio.worker import StudioWorker

            self.worker: Any = StudioWorker(self.catalog, self.paths, config)
        else:
            self.worker = _DisabledLocalWorker()
        namespace = os.environ.get("MOSHI_CLOUDWATCH_NAMESPACE")
        if metrics_publisher is None and namespace:
            from moshi_data_pipeline.studio.observability import (
                CloudWatchMetricsPublisher,
            )

            backup_value = os.environ.get("MOSHI_BACKUP_DIRECTORY")
            metrics_publisher = CloudWatchMetricsPublisher(
                self.catalog,
                self.paths.root,
                namespace=namespace,
                backup_directory=Path(backup_value) if backup_value else None,
            )
        self.lifecycle = LifecycleController(
            self.catalog,
            lifecycle_provider or LocalLifecycleProvider(),
            generation=deployment_generation,
            idle_seconds=lifecycle_idle_seconds,
            metrics_publisher=metrics_publisher,
        )
        self.gpu_settings = gpu_dispatcher_settings
        self.remote_job_kinds = set(GPU_JOB_KINDS) if gpu_dispatcher_settings is not None else set()
        if gpu_dispatcher_settings is not None and not enable_local_worker:
            self.catalog.fail_unsupported_queued_jobs(
                GPU_JOB_KINDS,
                reason="Job kind is not supported by the configured GPU service",
            )
        self.gpu_check_cooldown_seconds = gpu_check_cooldown_seconds
        self.gpu_cold_start_cooldown_seconds = gpu_cold_start_cooldown_seconds
        self.dispatcher: Any = (
            GpuPushDispatcher(
                self.catalog,
                self.paths,
                self.contexts,
                gpu_dispatcher_settings,
                lifecycle_wake=self.lifecycle.wake,
                client=gpu_dispatch_client,
            )
            if gpu_dispatcher_settings is not None
            else _DisabledGpuDispatcher()
        )

    def active_artifact_path(
        self,
        source_id: str,
        role: str,
        fallback: Path,
    ) -> Path:
        matches = [
            item
            for item in self.catalog.list_artifacts(source_id=source_id)
            if item["role"] == role
        ]
        if matches:
            return self.paths.resolve_relative(str(matches[-1]["relative_path"]))
        return fallback

    def _repair_annotation_bounds(self) -> None:
        """Create a corrected revision for legacy model timestamps past EOF."""
        for project in self.catalog.list_projects(viewer_id="system", is_admin=True):
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
        *,
        principal: PrincipalLike | None = None,
        source_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source_id is not None and principal is None:
            if kind == "realign" and payload and "annotation_version" in payload:
                requested_version = int(payload["annotation_version"])
                for active in self.catalog.active_jobs(source_id, kind):
                    if int(active["payload"].get("annotation_version", -1)) == requested_version:
                        return active
            else:
                active = self.catalog.active_job(source_id, kind)
                if active is not None:
                    return active
        if self.remote_job_kinds and kind not in self.remote_job_kinds:
            raise ValueError("Job kind is not supported by the configured GPU service")
        job_payload = payload or {}
        preconditions, _, fingerprint = self.contexts.snapshot(
            project_id=project_id,
            kind=kind,
            source_id=source_id,
            payload=job_payload,
            principal=principal,
        )
        job = self.catalog.create_job(
            project_id,
            kind,
            source_id,
            job_payload,
            preconditions=preconditions,
            input_fingerprint=fingerprint,
            principal=principal,
            source_updates=source_updates,
        )
        self.worker.wake()
        self.lifecycle.wake()
        self.dispatcher.wake()
        return job

    def gpu_status(self) -> dict[str, Any]:
        expected_build = (
            self.gpu_settings.required_build_id
            if self.gpu_settings is not None
            else str(self.catalog.get_gpu_runtime_state().get("expected_build_id") or "")
        )
        instance_id = (
            self.gpu_settings.instance_id
            if self.gpu_settings is not None
            else self.catalog.get_lifecycle_state().get("instance_id")
        )
        return gpu_status_payload(
            self.catalog,
            instance_id=str(instance_id) if instance_id else None,
            expected_build_id=expected_build,
        )

    def request_gpu_check(self, authenticated_user: str) -> tuple[dict[str, Any], bool]:
        if self.gpu_settings is None:
            raise RuntimeError("GPU push dispatch is not configured")
        lifecycle = self.catalog.get_lifecycle_state()
        check, created = self.catalog.request_gpu_check(
            "manual",
            requested_by=authenticated_user,
            instance_id=self.gpu_settings.instance_id,
            cold_start=lifecycle.get("instance_state") != "running",
            expected_build_id=self.gpu_settings.required_build_id,
            manual_cooldown_seconds=self.gpu_check_cooldown_seconds,
            manual_cold_start_cooldown_seconds=self.gpu_cold_start_cooldown_seconds,
        )
        if created:
            self.catalog.update_lifecycle_state(
                blocked_reason=None,
                last_error=None,
                recovery_count=0,
                startup_deadline=None,
            )
        self.dispatcher.wake()
        return public_gpu_check(check) or {}, created

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
                "canonical_channels": (
                    f"/media/{source_id}/channels"
                    if self.active_artifact_path(
                        source_id,
                        "source.channels",
                        self.paths.canonical_channels(source_id),
                    ).exists()
                    else None
                ),
                "video_proxy": (
                    f"/media/{source_id}/proxy"
                    if self.active_artifact_path(
                        source_id,
                        "source.proxy",
                        self.paths.video_proxy(source_id),
                    ).exists()
                    else None
                ),
                "peaks": (
                    f"/media/{source_id}/peaks"
                    if self.active_artifact_path(
                        source_id,
                        "source.peaks",
                        self.paths.peaks(source_id),
                    ).exists()
                    else None
                ),
                "original": f"/media/{source_id}/original",
            },
        }

    @staticmethod
    def _artifact_roles(
        artifacts: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for artifact in artifacts:
            role = str(artifact["role"])
            if role in result:
                raise ValueError(f"Remote result repeats artifact role: {role}")
            result[role] = artifact
        return result

    def _prepare_remote_mutation(
        self,
        job: dict[str, Any],
        result: Any,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        roles = self._artifact_roles(artifacts)
        if isinstance(result, InitializeResult):
            if result.source_id != job["source_id"]:
                raise ValueError("Initialization result source does not match job")
            allowed_updates = {
                "status",
                "init_mode",
                "duration_samples",
                "clips_stale",
                "inspection",
            }
            unknown = set(result.source_updates) - allowed_updates
            if unknown:
                raise ValueError(f"Unsupported initialization updates: {sorted(unknown)}")
            required = {"source.canonical", "source.peaks"}
            if missing := required - roles.keys():
                raise ValueError(
                    f"Initialization result is missing artifacts: {sorted(missing)}"
                )
            return {
                "kind": result.kind,
                "source_id": result.source_id,
                "annotation": result.annotation,
                "source_updates": result.source_updates,
            }
        elif isinstance(result, AnnotationResult):
            if result.source_id != job["source_id"]:
                raise ValueError("Annotation result source does not match job")
            required_by_kind = {
                "transcribe": {"analysis.raw_transcript", "analysis.aligned_transcript"},
                "review_transcript": set(),
                "rediarize": {"analysis.diarization"},
                "realign": {"analysis.aligned_transcript"},
            }
            if missing := required_by_kind[result.kind] - roles.keys():
                raise ValueError(
                    f"{result.kind} result is missing artifacts: {sorted(missing)}"
                )
            return {
                "kind": result.kind,
                "source_id": result.source_id,
                "expected_annotation_version": result.expected_annotation_version,
                "annotation": result.annotation,
            }
        elif isinstance(result, RecoveryResult):
            if result.source_id != job["source_id"]:
                raise ValueError("Recovery result source does not match job")
            records: list[dict[str, Any]] = []
            for record in result.recoveries:
                clean = dict(record)
                for field, suffix in (
                    ("assistant_path", "assistant"),
                    ("user_path", "user"),
                    ("original_path", "original"),
                ):
                    role = f"overlap.{record['region_id']}.{suffix}"
                    clean.pop(field, None)
                    if role in roles:
                        clean[field] = roles[role]["relative_path"]
                if clean.get("status") == "recovered" and not all(
                    clean.get(field)
                    for field in ("assistant_path", "user_path", "original_path")
                ):
                    raise ValueError(
                        f"Recovered region {record['region_id']} is missing audio artifacts"
                    )
                records.append(clean)
            return {
                "kind": result.kind,
                "source_id": result.source_id,
                "expected_annotation_version": result.expected_annotation_version,
                "recoveries": records,
            }
        elif isinstance(result, OverlapTranscriptResult):
            if result.source_id != job["source_id"]:
                raise ValueError("Overlap transcript source does not match job")
            return {
                "kind": result.kind,
                "source_id": result.source_id,
                "region_id": result.region_id,
                "expected_annotation_version": result.expected_annotation_version,
                "stem_transcripts": result.stem_transcripts,
            }
        elif isinstance(result, GenerationResult):
            if result.source_id != job["source_id"]:
                raise ValueError("Generation result source does not match job")
            for item in result.clip_manifest.get("artifacts", []):
                wav_role = str(item.get("wav_role", ""))
                json_role = str(item.get("json_role", ""))
                if wav_role not in roles or json_role not in roles:
                    raise ValueError("Generation result references a missing clip artifact")
            manifest = roles.get("clips.manifest")
            if manifest is None:
                raise ValueError("Generation result is missing clips.manifest")
            return {
                "kind": result.kind,
                "source_id": result.source_id,
                "expected_annotation_version": result.expected_annotation_version,
                "clip_artifacts_path": str(manifest["relative_path"]),
            }
        elif isinstance(result, ExportResult):
            if result.export_id != job["payload"].get("export_id"):
                raise ValueError("Export result does not match job")
            bundle = roles.get("export.bundle")
            if bundle is None:
                raise ValueError("Export result is missing export.bundle")
            return {
                "kind": result.kind,
                "export_id": result.export_id,
                "path": str(bundle["relative_path"]),
                "report": result.report,
            }
        else:  # pragma: no cover - the validator makes this unreachable.
            raise ValueError("Unsupported remote result")

    def complete_remote_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        input_fingerprint: str,
        kind: str,
        result_value: dict[str, Any],
        produced_artifacts: list[Any],
    ) -> dict[str, Any]:
        existing = self.catalog.get_job(job_id)
        if existing["status"] == "complete" and self.catalog.terminal_lease_matches(
            job_id, worker_id, lease_token, "complete"
        ):
            if existing["kind"] != kind or existing["input_fingerprint"] != input_fingerprint:
                raise ValueError("Duplicate completion does not match the completed job")
            duplicate_result = validate_job_result(kind, result_value).model_dump(mode="json")
            stored_result = existing.get("result") or {}
            if any(stored_result.get(key) != value for key, value in duplicate_result.items()):
                raise ValueError("Duplicate completion result does not match")
            return existing
        job = self.catalog.assert_current_lease(job_id, worker_id, lease_token)
        if job["kind"] != kind:
            raise ValueError("Completion kind does not match leased job")
        if job["input_fingerprint"] != input_fingerprint:
            raise ValueError("Completion input fingerprint does not match leased job")
        if self.contexts.current_fingerprint(job) != input_fingerprint:
            return self.catalog.supersede_job(job_id, "Authoritative inputs changed")
        result = validate_job_result(kind, result_value)
        with self.artifacts.mutation_guard():
            registered, commit_id = self.artifacts.commit_uploads(job, produced_artifacts)
            try:
                mutation = self._prepare_remote_mutation(job, result, registered)
                return self.catalog.commit_leased_job_result(
                    job_id,
                    worker_id,
                    lease_token,
                    result={
                        **result.model_dump(mode="json"),
                        "artifacts": [item["id"] for item in registered],
                    },
                    mutation=mutation,
                    artifact_commit_id=commit_id,
                )
            except Exception:
                if commit_id is not None:
                    self.artifacts.rollback_commit(commit_id)
                raise

    def save_rights(
        self,
        source_id: str,
        rights: SourceRights,
        *,
        principal: PrincipalLike,
    ) -> dict[str, Any]:
        return self.catalog.update_source(
            source_id,
            principal=principal,
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
        *,
        principal: PrincipalLike,
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
            source_id,
            expected_version,
            normalized,
            principal=principal,
        )

    def plan_clips(
        self,
        source_id: str,
        request: ClipPlanRequest,
        *,
        principal: PrincipalLike,
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
        return self.catalog.save_clip_plan(
            plan,
            principal=principal,
        ).model_dump(mode="json")

    def decide_clip(
        self,
        source_id: str,
        clip_id: str,
        decision: str,
        auditioned: bool,
        *,
        principal: PrincipalLike,
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
            source_id,
            clip_id,
            decision,
            auditioned,
            principal=principal,
        )

    def create_export(
        self,
        project_id: str,
        name: str,
        *,
        principal: PrincipalLike,
    ) -> dict[str, Any]:
        version = self.catalog.next_export_version(project_id)
        export = self.catalog.create_export(
            project_id,
            name,
            version,
            principal=principal,
        )
        try:
            job = self.enqueue(
                project_id,
                "export",
                payload={"export_id": export["id"]},
                principal=principal,
            )
        except Exception:
            self.catalog.discard_unstarted_export(str(export["id"]))
            raise
        return {"export": export, "job": job}

    def validate_project(self, project_id: str) -> dict[str, Any]:
        self.catalog.get_project(project_id)
        return validate_project_export(self.catalog, self.paths, project_id)

    def delete_source(
        self,
        source_id: str,
        *,
        principal: PrincipalLike,
    ) -> dict[str, Any]:
        source = self.catalog.get_source_for_mutation(source_id, principal=principal)
        original = self.paths.resolve_relative(source["stored_path"])
        source_root = self.paths.source_root(source_id).resolve()
        if self.paths.root not in source_root.parents:
            raise ValueError("Refusing to remove a source outside the workspace")
        deleted = self.catalog.delete_source(source_id, principal=principal)
        if source_root.exists():
            shutil.rmtree(source_root)
        if original.exists():
            original.unlink()
        return deleted

    def _registered_workspace_path(self, value: str) -> Path:
        relative = Path(value)
        if (
            relative.is_absolute()
            or value.strip() in {"", ".", ".."}
            or ".." in relative.parts
        ):
            raise ValueError("Refusing to remove an unsafe project target")
        return self.paths.root.joinpath(*relative.parts)

    def _validate_deletion_target(
        self,
        path: Path,
        *,
        allow_quarantine: bool = False,
    ) -> Path:
        root = self.paths.root.resolve()
        target = Path(os.path.abspath(path))
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "Refusing to remove a project target outside the workspace"
            ) from exc
        if relative == Path("."):
            raise ValueError("Refusing to remove a protected workspace path")
        in_quarantine = (
            target == self.paths.deletion_quarantine
            or self.paths.deletion_quarantine in target.parents
        )
        if in_quarantine and not allow_quarantine:
            raise ValueError("Refusing to remove the deletion quarantine")

        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        def validate_existing(candidate: Path) -> os.stat_result | None:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise ValueError("Refusing to remove a symlink or reparse point")
            resolved = candidate.resolve(strict=True)
            if resolved != root and root not in resolved.parents:
                raise ValueError(
                    "Refusing to remove a project target outside the workspace"
                )
            return info

        cursor = root
        for part in relative.parts:
            cursor /= part
            if validate_existing(cursor) is None:
                break

        stack = [target]
        while stack:
            candidate = stack.pop()
            info = validate_existing(candidate)
            if info is None or not stat.S_ISDIR(info.st_mode):
                continue
            with os.scandir(candidate) as entries:
                stack.extend(Path(entry.path) for entry in entries)
        return target

    def _project_deletion_targets(self, project_id: str) -> list[Path]:
        targets: set[Path] = set()

        def register_relative(value: str) -> None:
            targets.add(self._registered_workspace_path(value))

        for source in self.catalog.list_sources(project_id):
            register_relative(str(source["stored_path"]))
            targets.add(self.paths.source_root(str(source["id"])))
            for recovery in self.catalog.overlap_recoveries(str(source["id"])):
                for field in ("assistant_path", "user_path", "original_path"):
                    if recovery.get(field):
                        register_relative(str(recovery[field]))
        for artifact in self.catalog.list_project_artifacts(project_id):
            register_relative(str(artifact["relative_path"]))
        for upload in self.catalog.list_project_artifact_uploads(project_id):
            register_relative(str(upload["staging_path"]))
            if upload.get("final_relative_path"):
                register_relative(str(upload["final_relative_path"]))
        for commit in self.catalog.list_project_artifact_commits(project_id):
            for entry in commit["entries"]:
                for field in ("staging_path", "final_path"):
                    if entry.get(field):
                        register_relative(str(entry[field]))
        for export in self.catalog.list_exports(project_id):
            if export.get("path"):
                register_relative(str(export["path"]))

        validated = [self._validate_deletion_target(target) for target in targets]
        collapsed: list[Path] = []
        for target in sorted(validated, key=lambda value: (len(value.parts), str(value))):
            if any(target == parent or parent in target.parents for parent in collapsed):
                continue
            collapsed.append(target)
        return collapsed

    def _restore_project_quarantine(
        self,
        moved: list[tuple[Path, Path]],
    ) -> None:
        for original, quarantined in reversed(moved):
            self._validate_deletion_target(quarantined, allow_quarantine=True)
            self._validate_deletion_target(original)
            if not quarantined.exists():
                continue
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantined, original)

    def _purge_project_quarantine(self, quarantine: Path) -> None:
        self._validate_deletion_target(quarantine, allow_quarantine=True)
        if quarantine.exists():
            shutil.rmtree(quarantine)

    def delete_project(
        self,
        project_id: str,
        *,
        principal: PrincipalLike,
    ) -> dict[str, Any]:
        with self.artifacts.mutation_guard():
            project = self.catalog.reserve_project_deletion(
                project_id,
                principal=principal,
            )
            reservation_id = str(project["deletion_reservation_id"])
            quarantine = self.paths.deletion_quarantine / reservation_id
            moved: list[tuple[Path, Path]] = []
            try:
                targets = self._project_deletion_targets(project_id)
                quarantine.mkdir(parents=True, exist_ok=False)
                payload_root = quarantine / "payload"
                for target in targets:
                    target = self._validate_deletion_target(target)
                    if not target.exists():
                        continue
                    relative = target.relative_to(self.paths.root)
                    destination = payload_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._validate_deletion_target(target)
                    os.replace(target, destination)
                    moved.append((target, destination))
                manifest = [
                    {
                        "target": original.relative_to(self.paths.root).as_posix(),
                        "quarantine": quarantined.relative_to(self.paths.root).as_posix(),
                    }
                    for original, quarantined in moved
                ]
                self.catalog.record_project_quarantine(
                    reservation_id,
                    quarantine_path=quarantine.relative_to(self.paths.root).as_posix(),
                    manifest=manifest,
                )
                deleted = self.catalog.finalize_project_deletion(
                    project_id,
                    reservation_id=reservation_id,
                    principal=principal,
                )
            except Exception as exc:
                restore_error: Exception | None = None
                try:
                    self._restore_project_quarantine(moved)
                    if quarantine.exists():
                        shutil.rmtree(quarantine)
                except Exception as restore_exc:  # pragma: no cover - catastrophic I/O failure
                    restore_error = restore_exc
                self.catalog.abort_project_deletion(
                    reservation_id,
                    state="restore_failed" if restore_error else "aborted",
                    error=str(restore_error or exc),
                )
                if restore_error is not None:
                    raise RuntimeError(
                        "Project deletion failed and quarantined files could not be restored"
                    ) from restore_error
                raise

            try:
                self._purge_project_quarantine(quarantine)
            except Exception as exc:
                cleanup = self.catalog.finish_project_deletion_cleanup(
                    reservation_id,
                    state="purge_failed",
                    error=str(exc),
                )
            else:
                cleanup = self.catalog.finish_project_deletion_cleanup(
                    reservation_id,
                    state="complete",
                )
            return {**project, **deleted, "cleanup": cleanup}
