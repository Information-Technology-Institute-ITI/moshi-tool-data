from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.domain import AnnotationDocument, ClipPlanDocument, new_id
from moshi_data_pipeline.studio.migrations import apply_migrations

WORKER_PROTOCOL_VERSION = "1.0"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class VersionConflictError(RuntimeError):
    pass


class LeaseConflictError(RuntimeError):
    pass


class ProtocolMismatchError(RuntimeError):
    pass


class StudioCatalog:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.initialize()

    def _now_datetime(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _now(self) -> str:
        return self._now_datetime().astimezone(UTC).isoformat()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                init_mode TEXT,
                duration_samples INTEGER,
                inspection_json TEXT,
                origin TEXT NOT NULL DEFAULT '',
                rights_basis TEXT,
                rights_notes TEXT NOT NULL DEFAULT '',
                rights_confirmed INTEGER NOT NULL DEFAULT 0,
                active_annotation_version INTEGER NOT NULL DEFAULT 0,
                clips_stale INTEGER NOT NULL DEFAULT 1,
                clip_artifacts_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS annotation_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                annotation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_id, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS clip_plans (
                source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                annotation_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS clip_decisions (
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                clip_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                auditioned INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_id, clip_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS overlap_recoveries (
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                region_id TEXT NOT NULL,
                annotation_version INTEGER NOT NULL,
                start_sample INTEGER NOT NULL,
                end_sample INTEGER NOT NULL,
                status TEXT NOT NULL,
                decision TEXT,
                auditioned INTEGER NOT NULL DEFAULT 0,
                assistant_path TEXT,
                user_path TEXT,
                original_path TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_id, region_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                path TEXT,
                report_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS sources_project_idx ON sources(project_id)",
            "CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at)",
        ]
        with self.connect() as connection:
            for statement in statements:
                connection.execute(statement)
            apply_migrations(connection)
            # Local in-process claims intentionally have no lease. They cannot survive
            # a process restart and are safe to recover. Remote claims retain valid leases.
            connection.execute(
                """
                UPDATE jobs
                SET status='queued', message='Recovered local job after restart', updated_at=?
                WHERE status='running' AND lease_token_hash IS NULL
                """,
                (self._now(),),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_project(self, name: str, language: str = "ar") -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name cannot be blank")
        project_id = new_id("project")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO projects(id,name,language,created_at,updated_at) VALUES(?,?,?,?,?)",
                (project_id, clean_name, language, now, now),
            )
        return self.get_project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                    COUNT(s.id) AS source_count,
                    SUM(
                        CASE WHEN s.status IN ('ready','clips_ready') THEN 1 ELSE 0 END
                    ) AS ready_sources
                FROM projects p
                LEFT JOIN sources s ON s.project_id=p.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return dict(row)

    def update_project(
        self, project_id: str, *, name: str, language: str
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name cannot be blank")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET name=?,language=?,updated_at=? WHERE id=?",
                (clean_name, language, utc_now(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        return self.get_project(project_id)

    def create_source(
        self,
        project_id: str,
        original_name: str,
        stored_path: str,
        content_type: str,
        sha256: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        source_id = new_id("source")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    id,project_id,original_name,stored_path,content_type,sha256,size_bytes,
                    status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'uploaded',?,?)
                """,
                (
                    source_id,
                    project_id,
                    original_name,
                    stored_path,
                    content_type,
                    sha256,
                    size_bytes,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id)
            )
        return self.get_source(source_id)

    def list_sources(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [self._decode_source(dict(row)) for row in rows]

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._decode_source(dict(row))

    @staticmethod
    def _decode_source(value: dict[str, Any]) -> dict[str, Any]:
        value["rights_confirmed"] = bool(value["rights_confirmed"])
        value["clips_stale"] = bool(value["clips_stale"])
        value["inspection"] = _loads(value.pop("inspection_json"), {})
        return value

    def update_source(self, source_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "init_mode",
            "duration_samples",
            "origin",
            "rights_basis",
            "rights_notes",
            "rights_confirmed",
            "clips_stale",
            "clip_artifacts_path",
        }
        unknown = set(values) - allowed - {"inspection"}
        if unknown:
            raise ValueError(f"Unknown source fields: {sorted(unknown)}")
        updates: dict[str, Any] = {}
        for key, value in values.items():
            if key == "inspection":
                updates["inspection_json"] = _json(value)
            elif key in {"rights_confirmed", "clips_stale"}:
                updates[key] = int(bool(value))
            else:
                updates[key] = value
        updates["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE sources SET {assignments} WHERE id=?",
                (*updates.values(), source_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(source_id)
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_source(source_id)
        with self.connect() as connection:
            connection.execute("DELETE FROM sources WHERE id=?", (source_id,))
        return source

    def latest_annotation(self, source_id: str) -> AnnotationDocument:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT annotation_json FROM annotation_revisions
                WHERE source_id=? ORDER BY version DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return AnnotationDocument(source_id=source_id)
        return AnnotationDocument.model_validate_json(row["annotation_json"])

    def annotation_at(self, source_id: str, version: int) -> AnnotationDocument:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT annotation_json FROM annotation_revisions
                WHERE source_id=? AND version=?
                """,
                (source_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"{source_id}:v{version}")
        return AnnotationDocument.model_validate_json(row["annotation_json"])

    def annotation_revisions(self, source_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT version,created_at FROM annotation_revisions
                WHERE source_id=? ORDER BY version DESC
                """,
                (source_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_annotation(
        self, source_id: str, expected_version: int, annotation: AnnotationDocument
    ) -> AnnotationDocument:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT active_annotation_version FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(source_id)
            current = int(row["active_annotation_version"])
            if current != expected_version:
                connection.rollback()
                raise VersionConflictError(
                    f"Expected annotation version {expected_version}, current version is {current}"
                )
            next_version = current + 1
            value = annotation.model_copy(
                update={"source_id": source_id, "version": next_version}
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO annotation_revisions(source_id,version,annotation_json,created_at)
                VALUES(?,?,?,?)
                """,
                (source_id, next_version, value.model_dump_json(), now),
            )
            connection.execute(
                """
                UPDATE sources SET active_annotation_version=?,clips_stale=1,
                    clip_artifacts_path=NULL,updated_at=?
                WHERE id=?
                """,
                (next_version, now, source_id),
            )
            connection.execute("DELETE FROM clip_plans WHERE source_id=?", (source_id,))
            connection.execute("DELETE FROM clip_decisions WHERE source_id=?", (source_id,))
            connection.execute(
                "DELETE FROM overlap_recoveries WHERE source_id=?", (source_id,)
            )
            connection.commit()
        return value

    def replace_initial_annotation(
        self, source_id: str, annotation: AnnotationDocument
    ) -> AnnotationDocument:
        current = self.latest_annotation(source_id)
        return self.save_annotation(source_id, current.version, annotation)

    def save_clip_plan(self, plan: ClipPlanDocument) -> ClipPlanDocument:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clip_plans(source_id,annotation_version,plan_json,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                    annotation_version=excluded.annotation_version,
                    plan_json=excluded.plan_json,
                    updated_at=excluded.updated_at
                """,
                (
                    plan.source_id,
                    plan.annotation_version,
                    plan.model_dump_json(),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE sources SET clips_stale=1,clip_artifacts_path=NULL,updated_at=?
                WHERE id=?
                """,
                (now, plan.source_id),
            )
            connection.execute(
                "DELETE FROM clip_decisions WHERE source_id=?", (plan.source_id,)
            )
        return plan

    def get_clip_plan(self, source_id: str) -> ClipPlanDocument | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT plan_json FROM clip_plans WHERE source_id=?", (source_id,)
            ).fetchone()
        return (
            ClipPlanDocument.model_validate_json(row["plan_json"])
            if row is not None
            else None
        )

    def save_clip_decision(
        self, source_id: str, clip_id: str, decision: str, auditioned: bool
    ) -> dict[str, Any]:
        if decision == "approve" and not auditioned:
            raise ValueError("Clip approval requires audition")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clip_decisions(source_id,clip_id,decision,auditioned,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(source_id,clip_id) DO UPDATE SET
                    decision=excluded.decision,
                    auditioned=excluded.auditioned,
                    updated_at=excluded.updated_at
                """,
                (source_id, clip_id, decision, int(auditioned), now),
            )
        return {
            "source_id": source_id,
            "clip_id": clip_id,
            "decision": decision,
            "auditioned": auditioned,
            "updated_at": now,
        }

    def clip_decisions(self, source_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clip_decisions WHERE source_id=?", (source_id,)
            ).fetchall()
        values = {}
        for row in rows:
            value = dict(row)
            value["auditioned"] = bool(value["auditioned"])
            values[value["clip_id"]] = value
        return values

    def replace_overlap_recoveries(
        self, source_id: str, annotation_version: int, records: list[dict[str, Any]]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM overlap_recoveries WHERE source_id=?", (source_id,)
            )
            for record in records:
                connection.execute(
                    """
                    INSERT INTO overlap_recoveries(
                        source_id,region_id,annotation_version,start_sample,end_sample,status,
                        assistant_path,user_path,original_path,details_json,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source_id,
                        record["region_id"],
                        annotation_version,
                        record["start_sample"],
                        record["end_sample"],
                        record["status"],
                        record.get("assistant_path"),
                        record.get("user_path"),
                        record.get("original_path"),
                        _json(record.get("details", {})),
                        utc_now(),
                    ),
                )
            connection.execute(
                """
                UPDATE sources SET clips_stale=1,clip_artifacts_path=NULL,updated_at=?
                WHERE id=?
                """,
                (utc_now(), source_id),
            )
            connection.execute(
                "DELETE FROM clip_decisions WHERE source_id=?", (source_id,)
            )

    def overlap_recoveries(self, source_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM overlap_recoveries
                WHERE source_id=? ORDER BY start_sample,end_sample
                """,
                (source_id,),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["auditioned"] = bool(value["auditioned"])
            value["details"] = _loads(value.pop("details_json"), {})
            values.append(value)
        return values

    def decide_overlap(
        self, source_id: str, region_id: str, decision: str, auditioned: bool
    ) -> dict[str, Any]:
        if not auditioned:
            raise ValueError("A recovered-overlap decision requires audition")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE overlap_recoveries SET decision=?,auditioned=?,updated_at=?
                WHERE source_id=? AND region_id=? AND status='recovered'
                """,
                (decision, int(auditioned), utc_now(), source_id, region_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(region_id)
            connection.execute(
                """
                UPDATE sources SET clips_stale=1,clip_artifacts_path=NULL,updated_at=?
                WHERE id=?
                """,
                (utc_now(), source_id),
            )
            connection.execute(
                "DELETE FROM clip_decisions WHERE source_id=?", (source_id,)
            )
        return next(
            value
            for value in self.overlap_recoveries(source_id)
            if value["region_id"] == region_id
        )

    def update_overlap_details(
        self,
        source_id: str,
        region_id: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE overlap_recoveries SET details_json=?,updated_at=?
                WHERE source_id=? AND region_id=? AND status='recovered'
                """,
                (_json(details), utc_now(), source_id, region_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(region_id)
        return next(
            value
            for value in self.overlap_recoveries(source_id)
            if value["region_id"] == region_id
        )

    def create_job(
        self,
        project_id: str,
        kind: str,
        source_id: str | None,
        payload: dict[str, Any] | None = None,
        *,
        protocol_version: str = WORKER_PROTOCOL_VERSION,
        preconditions: dict[str, Any] | None = None,
        input_fingerprint: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        job_id = new_id("job")
        now = self._now()
        job_payload = payload or {}
        job_preconditions = preconditions or {}
        if input_fingerprint is None:
            canonical = json.dumps(
                {
                    "kind": kind,
                    "project_id": project_id,
                    "source_id": source_id,
                    "payload": job_payload,
                    "preconditions": job_preconditions,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            input_fingerprint = hashlib.sha256(canonical).hexdigest()
        if len(input_fingerprint) != 64:
            raise ValueError("input_fingerprint must be a SHA-256 hex digest")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id,project_id,source_id,kind,status,payload_json,
                    protocol_version,preconditions_json,input_fingerprint,max_attempts,
                    created_at,updated_at
                ) VALUES(?,?,?,?, 'queued',?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    project_id,
                    source_id,
                    kind,
                    _json(job_payload),
                    protocol_version,
                    _json(job_preconditions),
                    input_fingerprint,
                    max_attempts,
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode_job(dict(row))

    def list_jobs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE project_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [self._decode_job(dict(row)) for row in rows]

    def active_job(self, source_id: str, kind: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_id=? AND kind=? AND status IN ('queued','running')
                ORDER BY created_at LIMIT 1
                """,
                (source_id, kind),
            ).fetchone()
        return self._decode_job(dict(row)) if row is not None else None

    def active_jobs(self, source_id: str, kind: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_id=? AND kind=? AND status IN ('queued','running')
                ORDER BY created_at
                """,
                (source_id, kind),
            ).fetchall()
        return [self._decode_job(dict(row)) for row in rows]

    def active_source_jobs(self, source_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_id=? AND status IN ('queued','running')
                ORDER BY created_at
                """,
                (source_id,),
            ).fetchall()
        return [self._decode_job(dict(row)) for row in rows]

    @staticmethod
    def _decode_job(value: dict[str, Any]) -> dict[str, Any]:
        value["payload"] = _loads(value.pop("payload_json"), {})
        value["result"] = _loads(value.pop("result_json"), None)
        value["preconditions"] = _loads(value.pop("preconditions_json", None), {})
        value.pop("lease_token_hash", None)
        value["retryable"] = bool(value.get("retryable", 0))
        return value

    def claim_job(self) -> dict[str, Any] | None:
        """Compatibility claim for the in-process worker.

        Local claims deliberately have no durable lease. A restart recovers them to
        queued; remote workers must use :meth:`claim_leased_job`.
        """
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs SET status='running',progress=0,message='Starting',updated_at=?
                WHERE id=?
                """,
                (now, row["id"]),
            )
            connection.commit()
        return self.get_job(str(row["id"]))

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _bounded_text(value: str | None, limit: int = 1_000) -> str:
        return (value or "")[:limit]

    def _expire_leases(
        self,
        connection: sqlite3.Connection,
        now: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT id,attempt,max_attempts FROM jobs
            WHERE status='running' AND lease_token_hash IS NOT NULL
                AND lease_expires_at <= ?
            ORDER BY lease_expires_at,id
            """,
            (now,),
        ).fetchall()
        changed: list[str] = []
        for row in rows:
            exhausted = int(row["attempt"]) >= int(row["max_attempts"])
            next_status = "failed" if exhausted else "queued"
            message = (
                "Lease expired after maximum attempts"
                if exhausted
                else "Lease expired; queued for retry"
            )
            connection.execute(
                """
                UPDATE jobs SET status=?,message=?,retryable=?,
                    lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL,
                    finished_at=?,failure_class=?,updated_at=?
                WHERE id=? AND status='running'
                """,
                (
                    next_status,
                    message,
                    0 if exhausted else 1,
                    now if exhausted else None,
                    "lease_expired" if exhausted else None,
                    now,
                    row["id"],
                ),
            )
            connection.execute(
                """
                UPDATE job_attempts SET status='expired',finished_at=?,
                    failure_class='lease_expired',summary=?
                WHERE job_id=? AND attempt=? AND status='running'
                """,
                (now, message, row["id"], row["attempt"]),
            )
            changed.append(str(row["id"]))
        return changed

    def requeue_expired_jobs(self) -> list[str]:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._expire_leases(connection, self._now())
            connection.commit()
        return changed

    def claim_leased_job(
        self,
        worker_id: str,
        *,
        protocol_version: str,
        worker_build_id: str,
        supported_kinds: Sequence[str] | None = None,
        lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        if protocol_version != WORKER_PROTOCOL_VERSION:
            raise ProtocolMismatchError(
                f"Worker protocol {protocol_version} is incompatible with "
                f"{WORKER_PROTOCOL_VERSION}"
            )
        if not worker_id.strip() or not worker_build_id.strip():
            raise ValueError("worker_id and worker_build_id are required")
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        kinds = tuple(dict.fromkeys(supported_kinds or ()))
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        now_dt = self._now_datetime().astimezone(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_leases(connection, now)
            parameters: list[Any] = [protocol_version]
            kind_filter = ""
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                kind_filter = f" AND kind IN ({placeholders})"
                parameters.extend(kinds)
            row = connection.execute(
                f"""
                SELECT id,attempt FROM jobs
                WHERE status='queued' AND protocol_version=?
                    AND attempt < max_attempts {kind_filter}
                ORDER BY created_at,id LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            attempt = int(row["attempt"]) + 1
            cursor = connection.execute(
                """
                UPDATE jobs SET status='running',attempt=?,progress=0,
                    message='Starting',lease_owner=?,lease_token_hash=?,
                    lease_expires_at=?,worker_build_id=?,retryable=0,
                    started_at=COALESCE(started_at,?),finished_at=NULL,
                    failure_class=NULL,updated_at=?
                WHERE id=? AND status='queued'
                """,
                (
                    attempt,
                    worker_id,
                    token_hash,
                    expires_at,
                    worker_build_id,
                    now,
                    now,
                    row["id"],
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LeaseConflictError("Job was claimed concurrently")
            connection.execute(
                """
                INSERT INTO job_attempts(
                    job_id,attempt,worker_id,worker_build_id,
                    lease_started_at,lease_expires_at,status
                ) VALUES(?,?,?,?,?,?,'running')
                """,
                (
                    row["id"],
                    attempt,
                    worker_id,
                    worker_build_id,
                    now,
                    expires_at,
                ),
            )
            connection.commit()
        claimed = self.get_job(str(row["id"]))
        claimed["lease_token"] = token
        return claimed

    def _leased_row(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        worker_id: str,
        lease_token: str,
        now: str,
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        stored_hash = str(row["lease_token_hash"] or "")
        if (
            row["status"] != "running"
            or row["lease_owner"] != worker_id
            or not stored_hash
            or not hmac.compare_digest(stored_hash, self._token_hash(lease_token))
        ):
            raise LeaseConflictError("The worker does not hold the current lease")
        if str(row["lease_expires_at"] or "") <= now:
            raise LeaseConflictError("The job lease has expired")
        return row

    def heartbeat_leased_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        progress: float | None = None,
        message: str | None = None,
        lease_seconds: int = 120,
    ) -> dict[str, Any]:
        now_dt = self._now_datetime().astimezone(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, job_id, worker_id, lease_token, now)
            values: dict[str, Any] = {
                "lease_expires_at": expires_at,
                "updated_at": now,
            }
            if progress is not None:
                values["progress"] = max(0.0, min(1.0, progress))
            if message is not None:
                values["message"] = self._bounded_text(message)
            assignments = ",".join(f"{key}=?" for key in values)
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*values.values(), job_id),
            )
            connection.execute(
                """
                UPDATE job_attempts SET lease_expires_at=?
                WHERE job_id=? AND attempt=? AND status='running'
                """,
                (expires_at, job_id, row["attempt"]),
            )
            connection.commit()
        return self.get_job(job_id)

    def complete_leased_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        result: Any,
    ) -> dict[str, Any]:
        now = self._now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, job_id, worker_id, lease_token, now)
            connection.execute(
                """
                UPDATE jobs SET status='complete',progress=1,message='Complete',
                    result_json=?,error=NULL,retryable=0,finished_at=?,updated_at=?,
                    lease_expires_at=NULL
                WHERE id=?
                """,
                (_json(result), now, now, job_id),
            )
            connection.execute(
                """
                UPDATE job_attempts SET status='complete',finished_at=?,summary='Complete'
                WHERE job_id=? AND attempt=? AND status='running'
                """,
                (now, job_id, row["attempt"]),
            )
            connection.execute(
                """
                UPDATE worker_state SET status='idle',current_job_id=NULL,
                    idle_since=COALESCE(idle_since,?),last_heartbeat=?
                WHERE worker_id=?
                """,
                (now, now, worker_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def commit_leased_job_result(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        result: dict[str, Any],
        mutation: dict[str, Any],
        artifact_commit_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically apply one typed result mutation and finish its lease."""
        now = self._now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._leased_row(connection, job_id, worker_id, lease_token, now)
            kind = str(job["kind"])
            if mutation.get("kind") != kind:
                connection.rollback()
                raise ValueError("Remote mutation kind does not match leased job")
            source_id = str(job["source_id"]) if job["source_id"] else None

            def save_annotation(expected: int, raw: dict[str, Any]) -> int:
                if source_id is None:
                    raise ValueError("Annotation mutation requires a source")
                row = connection.execute(
                    "SELECT active_annotation_version FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(source_id)
                current = int(row["active_annotation_version"])
                if current != expected:
                    raise VersionConflictError(
                        f"Expected annotation version {expected}, current version is {current}"
                    )
                value = AnnotationDocument.model_validate(raw).model_copy(
                    update={"source_id": source_id, "version": current + 1}
                )
                connection.execute(
                    """
                    INSERT INTO annotation_revisions(
                        source_id,version,annotation_json,created_at
                    ) VALUES(?,?,?,?)
                    """,
                    (source_id, value.version, value.model_dump_json(), now),
                )
                connection.execute(
                    """
                    UPDATE sources SET active_annotation_version=?,clips_stale=1,
                        clip_artifacts_path=NULL,updated_at=? WHERE id=?
                    """,
                    (value.version, now, source_id),
                )
                connection.execute("DELETE FROM clip_plans WHERE source_id=?", (source_id,))
                connection.execute("DELETE FROM clip_decisions WHERE source_id=?", (source_id,))
                connection.execute(
                    "DELETE FROM overlap_recoveries WHERE source_id=?", (source_id,)
                )
                return value.version

            if kind == "initialize":
                if source_id != mutation.get("source_id"):
                    raise ValueError("Initialization source does not match leased job")
                row = connection.execute(
                    "SELECT active_annotation_version FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(source_id)
                save_annotation(int(row["active_annotation_version"]), mutation["annotation"])
                source_updates = dict(mutation["source_updates"])
                allowed = {
                    "status",
                    "init_mode",
                    "duration_samples",
                    "clips_stale",
                    "inspection",
                }
                unknown = set(source_updates) - allowed
                if unknown:
                    raise ValueError(f"Unsupported initialization updates: {sorted(unknown)}")
                columns: dict[str, Any] = {}
                for key, value in source_updates.items():
                    if key == "inspection":
                        columns["inspection_json"] = _json(value)
                    elif key == "clips_stale":
                        columns[key] = int(bool(value))
                    else:
                        columns[key] = value
                columns["updated_at"] = now
                assignments = ",".join(f"{key}=?" for key in columns)
                connection.execute(
                    f"UPDATE sources SET {assignments} WHERE id=?",
                    (*columns.values(), source_id),
                )
            elif kind in {"transcribe", "review_transcript", "rediarize", "realign"}:
                if source_id != mutation.get("source_id"):
                    raise ValueError("Annotation source does not match leased job")
                save_annotation(
                    int(mutation["expected_annotation_version"]),
                    mutation["annotation"],
                )
            elif kind == "recover_overlap":
                if source_id != mutation.get("source_id"):
                    raise ValueError("Recovery source does not match leased job")
                expected = int(mutation["expected_annotation_version"])
                row = connection.execute(
                    "SELECT active_annotation_version FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row is None or int(row["active_annotation_version"]) != expected:
                    raise VersionConflictError("Recovery annotation revision changed")
                connection.execute(
                    "DELETE FROM overlap_recoveries WHERE source_id=?", (source_id,)
                )
                for record in mutation["recoveries"]:
                    connection.execute(
                        """
                        INSERT INTO overlap_recoveries(
                            source_id,region_id,annotation_version,start_sample,end_sample,
                            status,decision,auditioned,assistant_path,user_path,original_path,
                            details_json,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            source_id,
                            record["region_id"],
                            expected,
                            record["start_sample"],
                            record["end_sample"],
                            record["status"],
                            record.get("decision"),
                            int(bool(record.get("auditioned", False))),
                            record.get("assistant_path"),
                            record.get("user_path"),
                            record.get("original_path"),
                            _json(record.get("details", {})),
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sources SET clips_stale=1,clip_artifacts_path=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (now, source_id),
                )
                connection.execute("DELETE FROM clip_decisions WHERE source_id=?", (source_id,))
            elif kind == "transcribe_overlap":
                if source_id != mutation.get("source_id"):
                    raise ValueError("Overlap transcript source does not match leased job")
                row = connection.execute(
                    """
                    SELECT annotation_version,details_json FROM overlap_recoveries
                    WHERE source_id=? AND region_id=? AND status='recovered'
                    """,
                    (source_id, mutation["region_id"]),
                ).fetchone()
                if (
                    row is None
                    or int(row["annotation_version"])
                    != int(mutation["expected_annotation_version"])
                ):
                    raise VersionConflictError("Overlap recovery revision changed")
                details = _loads(row["details_json"], {})
                details["stem_transcripts"] = mutation["stem_transcripts"]
                connection.execute(
                    """
                    UPDATE overlap_recoveries SET details_json=?,updated_at=?
                    WHERE source_id=? AND region_id=?
                    """,
                    (_json(details), now, source_id, mutation["region_id"]),
                )
            elif kind == "generate":
                if source_id != mutation.get("source_id"):
                    raise ValueError("Generation source does not match leased job")
                row = connection.execute(
                    "SELECT active_annotation_version FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if (
                    row is None
                    or int(row["active_annotation_version"])
                    != int(mutation["expected_annotation_version"])
                ):
                    raise VersionConflictError("Generation annotation revision changed")
                connection.execute(
                    """
                    UPDATE sources SET status='clips_ready',clips_stale=0,
                        clip_artifacts_path=?,updated_at=? WHERE id=?
                    """,
                    (mutation["clip_artifacts_path"], now, source_id),
                )
            elif kind == "export":
                export_id = str(mutation["export_id"])
                if export_id != _loads(job["payload_json"], {}).get("export_id"):
                    raise ValueError("Export result does not match leased job")
                cursor = connection.execute(
                    """
                    UPDATE exports SET status='complete',path=?,report_json=? WHERE id=?
                    """,
                    (mutation["path"], _json(mutation["report"]), export_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(export_id)
            else:
                raise ValueError(f"Unknown job kind: {kind}")

            if artifact_commit_id is not None:
                commit = connection.execute(
                    """
                    SELECT attempt,state,entries_json FROM artifact_commits
                    WHERE id=? AND job_id=?
                    """,
                    (artifact_commit_id, job_id),
                ).fetchone()
                if (
                    commit is None
                    or int(commit["attempt"]) != int(job["attempt"])
                    or commit["state"] != "moved"
                ):
                    raise ValueError("Artifact commit journal is not ready")
                for entry in _loads(commit["entries_json"], []):
                    cursor = connection.execute(
                        """
                        UPDATE artifacts SET state='active'
                        WHERE relative_path=? AND producing_job_id=? AND state='missing'
                        """,
                        (entry["final_path"], job_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Hidden artifact registry entry is not ready")
                    cursor = connection.execute(
                        """
                        UPDATE artifact_uploads SET state='committed',
                            final_relative_path=?,updated_at=?
                        WHERE id=? AND job_id=? AND attempt=? AND state='verified'
                        """,
                        (
                            entry["final_path"],
                            now,
                            entry["upload_id"],
                            job_id,
                            job["attempt"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Verified artifact upload is not ready")
                cursor = connection.execute(
                    """
                    UPDATE artifact_commits SET state='committed',updated_at=?
                    WHERE id=? AND job_id=? AND attempt=? AND state='moved'
                    """,
                    (now, artifact_commit_id, job_id, job["attempt"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Artifact commit journal is not ready")
            connection.execute(
                """
                UPDATE jobs SET status='complete',progress=1,message='Complete',
                    result_json=?,error=NULL,retryable=0,finished_at=?,updated_at=?,
                    lease_expires_at=NULL
                WHERE id=?
                """,
                (_json(result), now, now, job_id),
            )
            connection.execute(
                """
                UPDATE job_attempts SET status='complete',finished_at=?,summary='Complete'
                WHERE job_id=? AND attempt=? AND status='running'
                """,
                (now, job_id, job["attempt"]),
            )
            connection.execute(
                """
                UPDATE worker_state SET status='idle',current_job_id=NULL,
                    idle_since=COALESCE(idle_since,?),last_heartbeat=?
                WHERE worker_id=?
                """,
                (now, now, worker_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def fail_leased_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error: str,
        failure_class: str,
        retryable: bool,
    ) -> dict[str, Any]:
        now = self._now()
        clean_error = self._bounded_text(error, 4_000)
        clean_class = self._bounded_text(failure_class, 120)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                existing is not None
                and existing["status"] == "failed"
                and existing["lease_owner"] == worker_id
                and existing["lease_token_hash"]
                and hmac.compare_digest(
                    str(existing["lease_token_hash"]), self._token_hash(lease_token)
                )
            ):
                connection.rollback()
                return self._decode_job(dict(existing))
            row = self._leased_row(connection, job_id, worker_id, lease_token, now)
            should_requeue = retryable and int(row["attempt"]) < int(row["max_attempts"])
            status = "queued" if should_requeue else "failed"
            message = "Retryable failure; queued" if should_requeue else "Failed"
            connection.execute(
                """
                UPDATE jobs SET status=?,message=?,error=?,failure_class=?,retryable=?,
                    finished_at=?,updated_at=?,lease_owner=?,lease_token_hash=?,
                    lease_expires_at=NULL
                WHERE id=?
                """,
                (
                    status,
                    message,
                    clean_error,
                    clean_class,
                    1 if should_requeue else 0,
                    None if should_requeue else now,
                    now,
                    None if should_requeue else worker_id,
                    None if should_requeue else self._token_hash(lease_token),
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE job_attempts SET status=?,finished_at=?,failure_class=?,summary=?
                WHERE job_id=? AND attempt=? AND status='running'
                """,
                (
                    "requeued" if should_requeue else "failed",
                    now,
                    clean_class,
                    clean_error,
                    job_id,
                    row["attempt"],
                ),
            )
            connection.execute(
                """
                UPDATE worker_state SET status='idle',current_job_id=NULL,
                    idle_since=COALESCE(idle_since,?),last_heartbeat=?
                WHERE worker_id=?
                """,
                (now, now, worker_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def supersede_job(self, job_id: str, reason: str = "Inputs changed") -> dict[str, Any]:
        now = self._now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(job_id)
            if row["status"] not in {"queued", "running"}:
                connection.rollback()
                raise ValueError("Only queued or running jobs can be superseded")
            connection.execute(
                """
                UPDATE jobs SET status='superseded',message=?,finished_at=?,updated_at=?,
                    lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=?
                """,
                (self._bounded_text(reason), now, now, job_id),
            )
            if row["status"] == "running":
                connection.execute(
                    """
                    UPDATE job_attempts SET status='superseded',finished_at=?,summary=?
                    WHERE job_id=? AND attempt=? AND status='running'
                    """,
                    (now, self._bounded_text(reason), job_id, row["attempt"]),
                )
            connection.commit()
        return self.get_job(job_id)

    def list_job_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_worker_state(
        self,
        worker_id: str,
        *,
        boot_id: str,
        protocol_version: str,
        build_id: str,
        supported_kinds: Sequence[str],
        status: str,
        current_job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {"ready", "busy", "draining", "incompatible", "idle", "offline"}
        if protocol_version != WORKER_PROTOCOL_VERSION:
            status = "incompatible"
        if status not in allowed:
            raise ValueError(f"Invalid worker status: {status}")
        now = self._now()
        idle_since = now if status == "idle" else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_state(
                    worker_id,boot_id,protocol_version,build_id,supported_kinds_json,
                    status,current_job_id,last_heartbeat,idle_since,details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    boot_id=excluded.boot_id,
                    protocol_version=excluded.protocol_version,
                    build_id=excluded.build_id,
                    supported_kinds_json=excluded.supported_kinds_json,
                    status=excluded.status,
                    current_job_id=excluded.current_job_id,
                    last_heartbeat=excluded.last_heartbeat,
                    idle_since=CASE
                        WHEN excluded.status='idle' THEN COALESCE(worker_state.idle_since,excluded.idle_since)
                        ELSE NULL
                    END,
                    details_json=excluded.details_json
                """,
                (
                    worker_id,
                    boot_id,
                    protocol_version,
                    build_id,
                    _json(list(dict.fromkeys(supported_kinds))),
                    status,
                    current_job_id,
                    now,
                    idle_since,
                    _json(details or {}),
                ),
            )
        return self.get_worker_state(worker_id)

    def get_worker_state(self, worker_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_state WHERE worker_id=?", (worker_id,)
            ).fetchone()
        if row is None:
            raise KeyError(worker_id)
        value = dict(row)
        value["supported_kinds"] = _loads(value.pop("supported_kinds_json"), [])
        value["details"] = _loads(value.pop("details_json"), {})
        value["compatible"] = value["protocol_version"] == WORKER_PROTOCOL_VERSION
        return value

    def latest_worker_state(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT worker_id FROM worker_state ORDER BY last_heartbeat DESC LIMIT 1"
            ).fetchone()
        return self.get_worker_state(str(row["worker_id"])) if row is not None else None

    def register_artifact(
        self,
        *,
        role: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        project_id: str | None = None,
        source_id: str | None = None,
        producing_job_id: str | None = None,
        state: str = "active",
    ) -> dict[str, Any]:
        if len(sha256) != 64 or any(value not in "0123456789abcdef" for value in sha256):
            raise ValueError("Artifact SHA-256 must be lowercase hexadecimal")
        if size_bytes < 0:
            raise ValueError("Artifact size cannot be negative")
        if state not in {"active", "missing"}:
            raise ValueError(f"Invalid initial artifact state: {state}")
        normalized = Path(relative_path).as_posix()
        if (
            Path(normalized).is_absolute()
            or normalized in {"", ".", ".."}
            or ".." in Path(normalized).parts
        ):
            raise ValueError("Artifact path must be a safe workspace-relative path")
        identity = hashlib.sha256(f"{role}\0{normalized}".encode()).hexdigest()[:32]
        artifact_id = f"artifact_{identity}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id,role,relative_path,sha256,size_bytes,media_type,
                    project_id,source_id,producing_job_id,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    role=excluded.role,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    media_type=excluded.media_type,
                    project_id=excluded.project_id,
                    source_id=excluded.source_id,
                    producing_job_id=COALESCE(
                        excluded.producing_job_id,artifacts.producing_job_id
                    ),
                    state=excluded.state
                """,
                (
                    artifact_id,
                    role,
                    normalized,
                    sha256,
                    size_bytes,
                    media_type,
                    project_id,
                    source_id,
                    producing_job_id,
                    state,
                    self._now(),
                ),
            )
        return self.get_artifact_by_path(normalized)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return dict(row)

    def get_artifact_by_path(self, relative_path: str) -> dict[str, Any]:
        normalized = Path(relative_path).as_posix()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE relative_path=?", (normalized,)
            ).fetchone()
        if row is None:
            raise KeyError(relative_path)
        return dict(row)

    def list_artifacts(
        self,
        *,
        project_id: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = ["state='active'"]
        parameters: list[Any] = []
        if project_id is not None:
            where.append("project_id=?")
            parameters.append(project_id)
        if source_id is not None:
            where.append("source_id=?")
            parameters.append(source_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifacts WHERE {' AND '.join(where)} ORDER BY created_at,id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_artifact_state(self, artifact_id: str, state: str) -> dict[str, Any]:
        if state not in {"active", "superseded", "missing"}:
            raise ValueError(f"Invalid artifact state: {state}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE artifacts SET state=? WHERE id=?", (state, artifact_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(artifact_id)
        return self.get_artifact(artifact_id)

    def queue_summary(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        result = dict.fromkeys(("queued", "running", "complete", "failed", "superseded"), 0)
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def valid_running_lease_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM jobs
                WHERE status='running' AND lease_token_hash IS NOT NULL
                    AND lease_expires_at > ?
                """,
                (self._now(),),
            ).fetchone()
        return int(row["count"])

    def get_lifecycle_state(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM lifecycle_state WHERE id=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Lifecycle state is not initialized")
        return dict(row)

    def update_lifecycle_state(self, **values: Any) -> dict[str, Any]:
        allowed = {
            "provider",
            "instance_state",
            "desired_state",
            "last_transition_at",
            "startup_deadline",
            "recovery_count",
            "blocked_reason",
            "controller_generation",
            "last_error",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown lifecycle fields: {sorted(unknown)}")
        updates = {**values, "updated_at": self._now()}
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE lifecycle_state SET {assignments} WHERE id=1",
                tuple(updates.values()),
            )
        return self.get_lifecycle_state()

    def assert_current_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = self._leased_row(
                connection,
                job_id,
                worker_id,
                lease_token,
                self._now(),
            )
        value = dict(row)
        return self._decode_job(value)

    def terminal_lease_matches(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        status: str,
    ) -> bool:
        if status not in {"complete", "failed"}:
            raise ValueError("Terminal lease status must be complete or failed")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT status,lease_owner,lease_token_hash FROM jobs WHERE id=?
                """,
                (job_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["status"] == status
            and row["lease_owner"] == worker_id
            and row["lease_token_hash"]
            and hmac.compare_digest(
                str(row["lease_token_hash"]), self._token_hash(lease_token)
            )
        )

    def create_artifact_upload(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        role: str,
        staging_path: str,
        expected_sha256: str,
        expected_size: int,
        media_type: str,
        filename: str,
        expires_at: str,
    ) -> dict[str, Any]:
        if expected_size < 0 or len(expected_sha256) != 64:
            raise ValueError("Invalid upload size or SHA-256")
        upload_id = new_id("upload")
        now = self._now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._leased_row(connection, job_id, worker_id, lease_token, now)
            connection.execute(
                """
                INSERT INTO artifact_uploads(
                    id,job_id,attempt,role,staging_path,expected_sha256,
                    expected_size,media_type,filename,accepted_offset,state,
                    expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,0,'open',?,?,?)
                """,
                (
                    upload_id,
                    job_id,
                    job["attempt"],
                    role,
                    staging_path,
                    expected_sha256,
                    expected_size,
                    media_type,
                    filename,
                    expires_at,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_artifact_upload(upload_id)

    def get_artifact_upload(self, upload_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_uploads WHERE id=?", (upload_id,)
            ).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return dict(row)

    def update_artifact_upload(
        self,
        upload_id: str,
        *,
        accepted_offset: int | None = None,
        state: str | None = None,
        final_relative_path: str | None = None,
    ) -> dict[str, Any]:
        allowed_states = {"open", "verified", "committed", "discarded"}
        values: dict[str, Any] = {"updated_at": self._now()}
        if accepted_offset is not None:
            if accepted_offset < 0:
                raise ValueError("Upload offset cannot be negative")
            values["accepted_offset"] = accepted_offset
        if state is not None:
            if state not in allowed_states:
                raise ValueError(f"Invalid upload state: {state}")
            values["state"] = state
        if final_relative_path is not None:
            values["final_relative_path"] = final_relative_path
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE artifact_uploads SET {assignments} WHERE id=?",
                (*values.values(), upload_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(upload_id)
        return self.get_artifact_upload(upload_id)

    def list_artifact_uploads(
        self,
        job_id: str,
        *,
        attempt: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifact_uploads WHERE job_id=?"
        parameters: list[Any] = [job_id]
        if attempt is not None:
            query += " AND attempt=?"
            parameters.append(attempt)
        query += " ORDER BY created_at,id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def create_artifact_commit(
        self,
        job_id: str,
        attempt: int,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        commit_id = new_id("commit")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifact_commits(
                    id,job_id,attempt,state,entries_json,created_at,updated_at
                ) VALUES(?,?,?,'prepared',?,?,?)
                """,
                (commit_id, job_id, attempt, _json(entries), now, now),
            )
        return self.get_artifact_commit(commit_id)

    def get_artifact_commit(self, commit_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_commits WHERE id=?", (commit_id,)
            ).fetchone()
        if row is None:
            raise KeyError(commit_id)
        value = dict(row)
        value["entries"] = _loads(value.pop("entries_json"), [])
        return value

    def update_artifact_commit(self, commit_id: str, state: str) -> dict[str, Any]:
        if state not in {"prepared", "moved", "committed", "rolled_back"}:
            raise ValueError(f"Invalid artifact commit state: {state}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE artifact_commits SET state=?,updated_at=? WHERE id=?",
                (state, self._now(), commit_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(commit_id)
        return self.get_artifact_commit(commit_id)

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, progress))
        if message is not None:
            values["message"] = message
        if result is not None:
            values["result_json"] = _json(result)
        if error is not None:
            values["error"] = error
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*values.values(), job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def retry_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status='queued',progress=0,message='Queued for retry',
                    result_json=NULL,error=NULL,retryable=0,failure_class=NULL,
                    finished_at=NULL,lease_owner=NULL,lease_token_hash=NULL,
                    lease_expires_at=NULL,
                    max_attempts=CASE
                        WHEN max_attempts <= attempt THEN attempt + 1 ELSE max_attempts
                    END,
                    updated_at=?
                WHERE id=? AND status='failed'
                """,
                (self._now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only failed jobs can be retried")
        return self.get_job(job_id)

    def next_export_version(self, project_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS value FROM exports WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return int(row["value"])

    def create_export(
        self, project_id: str, name: str, version: int
    ) -> dict[str, Any]:
        export_id = new_id("export")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO exports(id,project_id,version,name,status,created_at)
                VALUES(?,?,?,?, 'queued',?)
                """,
                (export_id, project_id, version, name, utc_now()),
            )
        return self.get_export(export_id)

    def get_export(self, export_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM exports WHERE id=?", (export_id,)
            ).fetchone()
        if row is None:
            raise KeyError(export_id)
        value = dict(row)
        value["report"] = _loads(value.pop("report_json"), None)
        return value

    def list_exports(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM exports WHERE project_id=? ORDER BY version DESC",
                (project_id,),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["report"] = _loads(value.pop("report_json"), None)
            values.append(value)
        return values

    def update_export(
        self,
        export_id: str,
        *,
        status: str,
        path: str | None = None,
        report: Any = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE exports SET status=?,path=COALESCE(?,path),
                    report_json=COALESCE(?,report_json) WHERE id=?
                """,
                (status, path, _json(report) if report is not None else None, export_id),
            )
        return self.get_export(export_id)
