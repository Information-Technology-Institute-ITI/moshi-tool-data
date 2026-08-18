from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from moshi_data_pipeline.studio.catalog import (
    ArtifactUploadManifestConflictError,
    LeaseConflictError,
    StudioCatalog,
)
from moshi_data_pipeline.studio.media import StudioPaths, safe_filename
from moshi_data_pipeline.studio.protocol import ProducedArtifact, UploadCreate

CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class UploadConflictError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(self, catalog: StudioCatalog, paths: StudioPaths) -> None:
        self.catalog = catalog
        self.paths = paths
        self._lock = threading.RLock()

    @contextmanager
    def mutation_guard(self):
        with self._lock:
            yield

    def _staging_file(self, identifier: str) -> Path:
        path = (self.paths.worker_staging / f"{identifier}.part").resolve()
        if self.paths.worker_staging.resolve() not in path.parents:
            raise ValueError("Invalid worker staging path")
        return path

    def create_upload(
        self,
        job_id: str,
        lease_token: str,
        payload: UploadCreate,
        *,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        with self._lock:
            return self._create_upload(
                job_id,
                lease_token,
                payload,
                ttl_hours=ttl_hours,
            )

    def _create_upload(
        self,
        job_id: str,
        lease_token: str,
        payload: UploadCreate,
        *,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        job = self.catalog.assert_current_lease(job_id, payload.worker_id, lease_token)
        now = datetime.now(UTC)
        expired_staging: list[tuple[str, str]] = []
        for existing in self.catalog.list_artifact_uploads(
            job_id, attempt=int(job["attempt"])
        ):
            if existing["role"] != payload.role or existing["state"] not in {
                "open",
                "verified",
            }:
                continue
            try:
                expired = datetime.fromisoformat(str(existing["expires_at"])) <= now
            except ValueError:
                expired = False
            if expired:
                expired_staging.append(
                    (str(existing["id"]), str(existing["staging_path"]))
                )
        staging = self._staging_file(uuid4().hex)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.touch(exist_ok=False)
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat()
        try:
            upload = self.catalog.create_artifact_upload(
                job_id,
                payload.worker_id,
                lease_token,
                role=payload.role,
                staging_path=self.paths.relative(staging),
                expected_sha256=payload.sha256,
                expected_size=payload.size_bytes,
                media_type=payload.media_type,
                filename=safe_filename(payload.filename),
                expires_at=expires_at,
            )
            for expired_id, expired_path in expired_staging:
                if self.catalog.get_artifact_upload(expired_id)["state"] == "discarded":
                    self.paths.resolve_relative(expired_path).unlink(missing_ok=True)
            if self.paths.resolve_relative(str(upload["staging_path"])) != staging:
                staging.unlink(missing_ok=True)
            if payload.size_bytes == 0 and upload["state"] == "open":
                empty_hash = hashlib.sha256(b"").hexdigest()
                if payload.sha256 != empty_hash:
                    self.catalog.update_artifact_upload(str(upload["id"]), state="discarded")
                    raise ValueError("Empty upload checksum does not match")
                upload = self.catalog.update_artifact_upload(str(upload["id"]), state="verified")
            return upload
        except ArtifactUploadManifestConflictError as exc:
            if staging.exists():
                staging.unlink()
            raise UploadConflictError(str(exc)) from exc
        except Exception:
            if staging.exists():
                staging.unlink()
            raise

    def upload_status(
        self,
        upload_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        upload = self.catalog.get_artifact_upload(upload_id)
        job = self.catalog.assert_current_lease(str(upload["job_id"]), worker_id, lease_token)
        if int(upload["attempt"]) != int(job["attempt"]):
            raise LeaseConflictError("Upload belongs to an obsolete attempt")
        if upload["state"] not in {"committed", "discarded"} and datetime.fromisoformat(
            str(upload["expires_at"])
        ) <= datetime.now(UTC):
            self.catalog.update_artifact_upload(upload_id, state="discarded")
            self._staging_file(Path(str(upload["staging_path"])).stem).unlink(missing_ok=True)
            raise UploadConflictError("Upload has expired")
        return upload

    @staticmethod
    def parse_content_range(value: str, expected_total: int) -> tuple[int, int]:
        match = CONTENT_RANGE.fullmatch(value.strip())
        if match is None:
            raise ValueError("Content-Range must be 'bytes start-end/total'")
        start, end, total = (int(part) for part in match.groups())
        if total != expected_total:
            raise UploadConflictError("Content-Range total does not match registered size")
        if start > end or end >= total:
            raise ValueError("Content-Range is outside the registered upload")
        return start, end

    async def append(
        self,
        upload_id: str,
        worker_id: str,
        lease_token: str,
        content_range: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        with self._lock:
            upload = self.upload_status(upload_id, worker_id, lease_token)
            if upload["state"] == "discarded":
                raise UploadConflictError("Upload was discarded")
            expected_size = int(upload["expected_size"])
            start, end = self.parse_content_range(content_range, expected_size)
            accepted = int(upload["accepted_offset"])
            if start > accepted:
                raise UploadConflictError(f"Upload must resume at byte {accepted}")
            duplicate = end < accepted
            if start < accepted and not duplicate:
                raise UploadConflictError("A chunk cannot partially overlap accepted bytes")
            expected_chunk_size = end - start + 1
            path = self.paths.resolve_relative(str(upload["staging_path"]))
            received = 0
            try:
                mode = "rb" if duplicate else "r+b"
                with path.open(mode) as stream:
                    stream.seek(start)
                    async for chunk in chunks:
                        if not chunk:
                            continue
                        if received + len(chunk) > expected_chunk_size:
                            raise UploadConflictError("Request body exceeds Content-Range")
                        if duplicate:
                            if stream.read(len(chunk)) != chunk:
                                raise UploadConflictError(
                                    "Repeated upload chunk conflicts with accepted bytes"
                                )
                        else:
                            stream.write(chunk)
                        received += len(chunk)
                    if received != expected_chunk_size:
                        raise UploadConflictError("Request body is shorter than Content-Range")
                    if not duplicate:
                        stream.flush()
                        os.fsync(stream.fileno())
            except Exception:
                if not duplicate and path.exists():
                    self.catalog.update_artifact_upload(
                        upload_id, accepted_offset=path.stat().st_size
                    )
                raise
            if duplicate:
                return self.catalog.get_artifact_upload(upload_id)
            accepted = end + 1
            state = "open"
            if accepted == expected_size:
                actual_hash = self._file_sha256(path)
                if actual_hash != upload["expected_sha256"]:
                    self.catalog.update_artifact_upload(
                        upload_id,
                        accepted_offset=accepted,
                        state="discarded",
                    )
                    path.unlink(missing_ok=True)
                    raise UploadConflictError("Completed upload checksum does not match")
                state = "verified"
            return self.catalog.update_artifact_upload(
                upload_id,
                accepted_offset=accepted,
                state=state,
            )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def commit_uploads(
        self,
        job: dict[str, Any],
        produced: list[ProducedArtifact],
    ) -> tuple[list[dict[str, Any]], str | None]:
        with self._lock:
            return self._commit_uploads(job, produced)

    def _commit_uploads(
        self,
        job: dict[str, Any],
        produced: list[ProducedArtifact],
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not produced:
            return [], None
        selected: list[tuple[dict[str, Any], ProducedArtifact]] = []
        seen: set[str] = set()
        for item in produced:
            if item.upload_id in seen:
                raise ValueError("Completion repeats an upload ID")
            seen.add(item.upload_id)
            upload = self.catalog.get_artifact_upload(item.upload_id)
            if (
                upload["job_id"] != job["id"]
                or int(upload["attempt"]) != int(job["attempt"])
                or upload["state"] != "verified"
                or upload["role"] != item.role
                or upload["expected_sha256"] != item.sha256
                or int(upload["expected_size"]) != item.size_bytes
                or upload["media_type"] != item.media_type
            ):
                raise ValueError("Completion artifact does not match its verified upload")
            selected.append((upload, item))
        final_root = self.paths.worker_artifacts / str(job["id"]) / f"attempt_{int(job['attempt'])}"
        final_root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for upload, _ in selected:
            source = self.paths.resolve_relative(str(upload["staging_path"]))
            destination = final_root / f"{upload['id']}_{safe_filename(str(upload['filename']))}"
            entries.append(
                {
                    "upload_id": upload["id"],
                    "staging_path": upload["staging_path"],
                    "final_path": self.paths.relative(destination),
                    "role": upload["role"],
                    "sha256": upload["expected_sha256"],
                    "size_bytes": upload["expected_size"],
                    "media_type": upload["media_type"],
                    "source_exists": source.exists(),
                }
            )
        commit = self.catalog.create_artifact_commit(str(job["id"]), int(job["attempt"]), entries)
        registered: list[dict[str, Any]] = []
        moved: list[tuple[Path, Path]] = []
        try:
            for entry in entries:
                source = self.paths.resolve_relative(str(entry["staging_path"]))
                destination = self.paths.resolve_relative(str(entry["final_path"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                moved.append((source, destination))
            self.catalog.update_artifact_commit(str(commit["id"]), "moved")
            for entry in entries:
                artifact = self.catalog.register_artifact(
                    role=str(entry["role"]),
                    relative_path=str(entry["final_path"]),
                    sha256=str(entry["sha256"]),
                    size_bytes=int(entry["size_bytes"]),
                    media_type=str(entry["media_type"]),
                    project_id=str(job["project_id"]),
                    source_id=str(job["source_id"]) if job.get("source_id") else None,
                    producing_job_id=str(job["id"]),
                    state="missing",
                )
                registered.append(artifact)
            # The service marks this journal committed in the same SQLite
            # transaction as the typed state change and job completion.
            return registered, str(commit["id"])
        except Exception:
            for artifact in registered:
                self.catalog.set_artifact_state(str(artifact["id"]), "missing")
            for source, destination in reversed(moved):
                if destination.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            for upload, _ in selected:
                self.catalog.update_artifact_upload(str(upload["id"]), state="verified")
            self.catalog.update_artifact_commit(str(commit["id"]), "rolled_back")
            raise

    def rollback_commit(self, commit_id: str) -> None:
        commit = self.catalog.get_artifact_commit(commit_id)
        if commit["state"] == "rolled_back":
            return
        for entry in reversed(commit["entries"]):
            source = self.paths.resolve_relative(str(entry["staging_path"]))
            destination = self.paths.resolve_relative(str(entry["final_path"]))
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            try:
                artifact = self.catalog.get_artifact_by_path(str(entry["final_path"]))
                self.catalog.set_artifact_state(str(artifact["id"]), "missing")
            except KeyError:
                pass
            self.catalog.update_artifact_upload(
                str(entry["upload_id"]),
                state="verified",
                final_relative_path=str(entry["final_path"]),
            )
        self.catalog.update_artifact_commit(commit_id, "rolled_back")

    def reconcile_commits(self) -> list[str]:
        """Roll back incomplete moves deterministically from the commit journal."""
        with self.catalog.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM artifact_commits
                WHERE state IN ('prepared','moved') ORDER BY created_at
                """
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            commit_id = str(row["id"])
            self.rollback_commit(commit_id)
            recovered.append(commit_id)
        return recovered
