from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    definitions: dict[str, str],
) -> None:
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    # The migration scripts contain only simple DDL statements. Avoid
    # sqlite3.executescript(), which commits an active transaction implicitly.
    for statement in script.split(";"):
        if clean := statement.strip():
            connection.execute(clean)


def _legacy_compatibility(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "sources", {"clip_artifacts_path": "TEXT"})


def _worker_protocol_v1(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "jobs",
        {
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "lease_owner": "TEXT",
            "lease_token_hash": "TEXT",
            "lease_expires_at": "TEXT",
            "retryable": "INTEGER NOT NULL DEFAULT 0",
            "protocol_version": "TEXT NOT NULL DEFAULT '1.0'",
            "worker_build_id": "TEXT",
            "preconditions_json": "TEXT NOT NULL DEFAULT '{}'",
            "input_fingerprint": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
            "failure_class": "TEXT",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS worker_state (
            worker_id TEXT PRIMARY KEY,
            boot_id TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            build_id TEXT NOT NULL,
            supported_kinds_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK(
                status IN ('ready','busy','draining','incompatible','idle','offline')
            ),
            current_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            last_heartbeat TEXT NOT NULL,
            idle_since TEXT,
            details_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS job_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            worker_build_id TEXT,
            lease_started_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK(
                status IN ('running','complete','failed','expired','superseded','requeued')
            ),
            failure_class TEXT,
            summary TEXT,
            UNIQUE(job_id, attempt)
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            relative_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
            source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
            producing_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            state TEXT NOT NULL DEFAULT 'active' CHECK(
                state IN ('active','superseded','missing')
            ),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifact_uploads (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL,
            role TEXT NOT NULL,
            staging_path TEXT NOT NULL UNIQUE,
            expected_sha256 TEXT NOT NULL CHECK(length(expected_sha256) = 64),
            expected_size INTEGER NOT NULL CHECK(expected_size >= 0),
            media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            accepted_offset INTEGER NOT NULL DEFAULT 0 CHECK(accepted_offset >= 0),
            state TEXT NOT NULL DEFAULT 'open' CHECK(
                state IN ('open','verified','committed','discarded')
            ),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lifecycle_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            provider TEXT NOT NULL DEFAULT 'local',
            instance_state TEXT NOT NULL DEFAULT 'unknown',
            desired_state TEXT NOT NULL DEFAULT 'stopped',
            last_transition_at TEXT,
            startup_deadline TEXT,
            recovery_count INTEGER NOT NULL DEFAULT 0,
            blocked_reason TEXT,
            controller_generation TEXT NOT NULL DEFAULT 'local',
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS jobs_claim_idx
            ON jobs(status, protocol_version, created_at);
        CREATE INDEX IF NOT EXISTS jobs_lease_expiry_idx
            ON jobs(status, lease_expires_at);
        CREATE INDEX IF NOT EXISTS job_attempts_job_idx
            ON job_attempts(job_id, attempt);
        CREATE INDEX IF NOT EXISTS artifacts_owner_idx
            ON artifacts(project_id, source_id, role);
        CREATE INDEX IF NOT EXISTS artifact_uploads_expiry_idx
            ON artifact_uploads(state, expires_at);
        """,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO lifecycle_state(id, updated_at)
        VALUES(1, ?)
        """,
        (_utc_now(),),
    )


def _artifact_commit_journal(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "artifact_uploads",
        {
            "filename": "TEXT NOT NULL DEFAULT 'artifact.bin'",
            "final_relative_path": "TEXT",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS artifact_commits (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','moved','committed','rolled_back')),
            entries_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS artifact_commits_state_idx
            ON artifact_commits(state, created_at);
        """,
    )
    # These rows predate leases and are unsafe to leave running after an upgrade.
    connection.execute(
        """
        UPDATE jobs
        SET status='queued', message='Recovered legacy running job during migration',
            lease_owner=NULL, lease_token_hash=NULL, lease_expires_at=NULL,
            updated_at=?
        WHERE status='running' AND lease_token_hash IS NULL
        """,
        (_utc_now(),),
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "legacy_compatibility", _legacy_compatibility),
    (2, "worker_protocol_v1", _worker_protocol_v1),
    (3, "artifact_commit_journal", _artifact_commit_journal),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    """Apply pending numbered migrations and return their versions."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    completed: list[int] = []
    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (version, name, _utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        completed.append(version)
    return completed
