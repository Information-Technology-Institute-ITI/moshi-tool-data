from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.domain import AnnotationDocument, ClipPlanDocument, new_id


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class VersionConflictError(RuntimeError):
    pass


class StudioCatalog:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

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
            source_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(sources)").fetchall()
            }
            if "clip_artifacts_path" not in source_columns:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN clip_artifacts_path TEXT"
                )
            connection.execute(
                "UPDATE jobs SET status='queued', message='Recovered after restart' "
                "WHERE status='running'"
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
    ) -> dict[str, Any]:
        job_id = new_id("job")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id,project_id,source_id,kind,status,payload_json,created_at,updated_at
                ) VALUES(?,?,?,?, 'queued',?,?,?)
                """,
                (job_id, project_id, source_id, kind, _json(payload or {}), now, now),
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
        return value

    def claim_job(self) -> dict[str, Any] | None:
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
                    result_json=NULL,error=NULL,updated_at=?
                WHERE id=? AND status='failed'
                """,
                (utc_now(), job_id),
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
