from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


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


def _gpu_push_dispatch_v2(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "lifecycle_state",
        {
            "instance_id": "TEXT",
            "last_aws_observation_at": "TEXT",
            "last_aws_error_at": "TEXT",
            "draining": "INTEGER NOT NULL DEFAULT 0 CHECK(draining IN (0,1))",
            "idle_stop_at": "TEXT",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS gpu_runtime_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            instance_id TEXT,
            instance_state TEXT NOT NULL DEFAULT 'unknown',
            desired_state TEXT NOT NULL DEFAULT 'stopped',
            draining INTEGER NOT NULL DEFAULT 0 CHECK(draining IN (0,1)),
            last_aws_observation_at TEXT,
            last_aws_error_at TEXT,
            last_aws_error TEXT,
            last_intake_observation_at TEXT,
            intake_reachable INTEGER NOT NULL DEFAULT 0 CHECK(intake_reachable IN (0,1)),
            intake_status TEXT,
            dispatch_protocol TEXT,
            worker_protocol TEXT,
            actual_build_id TEXT,
            expected_build_id TEXT,
            host_boot_id TEXT,
            service_boot_id TEXT,
            callback_ready INTEGER NOT NULL DEFAULT 0 CHECK(callback_ready IN (0,1)),
            functional_check_ready INTEGER NOT NULL DEFAULT 0
                CHECK(functional_check_ready IN (0,1)),
            operational_ready INTEGER NOT NULL DEFAULT 0 CHECK(operational_ready IN (0,1)),
            accepting_dispatches INTEGER NOT NULL DEFAULT 0
                CHECK(accepting_dispatches IN (0,1)),
            safe_to_stop INTEGER NOT NULL DEFAULT 0 CHECK(safe_to_stop IN (0,1)),
            current_dispatch_id TEXT,
            queued_count INTEGER NOT NULL DEFAULT 0 CHECK(queued_count >= 0),
            running_count INTEGER NOT NULL DEFAULT 0 CHECK(running_count >= 0),
            last_worker_heartbeat_at TEXT,
            last_functional_check_at TEXT,
            last_transition_at TEXT,
            idle_stop_at TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gpu_checks (
            id TEXT PRIMARY KEY,
            gpu_check_id TEXT,
            instance_id TEXT,
            trigger TEXT NOT NULL CHECK(trigger IN ('manual','job_preflight')),
            requested_by TEXT,
            cold_start INTEGER NOT NULL DEFAULT 0 CHECK(cold_start IN (0,1)),
            status TEXT NOT NULL CHECK(status IN (
                'requested','starting','waiting','queued','running','passed',
                'failed','timed_out','stale','cancelled'
            )),
            requirement_key TEXT,
            host_boot_id TEXT,
            service_boot_id TEXT,
            dispatch_protocol TEXT,
            worker_protocol TEXT,
            actual_build_id TEXT,
            expected_build_id TEXT,
            model_revision TEXT,
            config_fingerprint TEXT,
            fixture_id TEXT,
            fixture_hash_prefix TEXT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            valid_until TEXT,
            updated_at TEXT NOT NULL,
            gpu_name TEXT,
            device TEXT,
            segment_count INTEGER CHECK(segment_count IS NULL OR segment_count >= 0),
            cer REAL,
            cer_threshold REAL,
            model_load_ms INTEGER CHECK(model_load_ms IS NULL OR model_load_ms >= 0),
            inference_ms INTEGER CHECK(inference_ms IS NULL OR inference_ms >= 0),
            total_ms INTEGER CHECK(total_ms IS NULL OR total_ms >= 0),
            failure_class TEXT,
            failure_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS gpu_dispatches (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL CHECK(attempt >= 1),
            state TEXT NOT NULL CHECK(state IN (
                'claimed','prepared','creating','uploading','starting','accepted',
                'running','completion_pending','cancel_requested','complete','failed',
                'cancelled','fenced','blocked'
            )),
            remote_state TEXT,
            worker_id TEXT NOT NULL,
            worker_build_id TEXT NOT NULL,
            dispatch_protocol TEXT NOT NULL,
            required_build_id TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint) = 64),
            manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR length(manifest_sha256) = 64),
            manifest_json TEXT,
            context_json TEXT,
            check_id TEXT REFERENCES gpu_checks(id) ON DELETE SET NULL,
            requirement_key TEXT,
            leader_epoch INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            next_retry_at TEXT,
            last_http_status INTEGER,
            last_error_class TEXT,
            last_error_summary TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            accepted_at TEXT,
            finished_at TEXT,
            UNIQUE(job_id, attempt)
        );

        CREATE TABLE IF NOT EXISTS gpu_dispatch_inputs (
            dispatch_id TEXT NOT NULL REFERENCES gpu_dispatches(id) ON DELETE CASCADE,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            role TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 1),
            media_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            accepted_offset INTEGER NOT NULL DEFAULT 0 CHECK(accepted_offset >= 0),
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN (
                'pending','uploading','verified','failed','cancelled'
            )),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(dispatch_id, artifact_id),
            UNIQUE(dispatch_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS gpu_dispatch_leader (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            owner_id TEXT,
            fencing_epoch INTEGER NOT NULL DEFAULT 0 CHECK(fencing_epoch >= 0),
            acquired_at TEXT,
            heartbeat_at TEXT,
            lease_expires_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS jobs_active_fingerprint_idx
            ON jobs(input_fingerprint, status, created_at);
        CREATE INDEX IF NOT EXISTS gpu_checks_history_idx
            ON gpu_checks(requested_at DESC);
        CREATE INDEX IF NOT EXISTS gpu_checks_manual_user_idx
            ON gpu_checks(requested_by, trigger, requested_at DESC);
        CREATE INDEX IF NOT EXISTS gpu_checks_requirement_idx
            ON gpu_checks(requirement_key, status, valid_until);
        CREATE INDEX IF NOT EXISTS gpu_checks_remote_id_idx
            ON gpu_checks(gpu_check_id, requested_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS gpu_checks_one_active_idx
            ON gpu_checks((1))
            WHERE status IN ('requested','starting','waiting','queued','running');
        CREATE INDEX IF NOT EXISTS gpu_dispatches_job_idx
            ON gpu_dispatches(job_id, attempt);
        CREATE INDEX IF NOT EXISTS gpu_dispatches_state_idx
            ON gpu_dispatches(state, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS gpu_dispatches_one_active_idx
            ON gpu_dispatches((1))
            WHERE state IN (
                'claimed','prepared','creating','uploading','starting','accepted',
                'running','completion_pending','cancel_requested','blocked'
            );
        CREATE INDEX IF NOT EXISTS gpu_dispatch_inputs_state_idx
            ON gpu_dispatch_inputs(dispatch_id, state, ordinal);
        CREATE INDEX IF NOT EXISTS artifact_uploads_active_role_idx
            ON artifact_uploads(job_id, attempt, role)
            WHERE state IN ('open','verified','committed');
        """,
    )
    now = _utc_now()
    connection.execute(
        """
        INSERT OR IGNORE INTO gpu_runtime_state(id, updated_at)
        VALUES(1, ?)
        """,
        (now,),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO gpu_dispatch_leader(id, updated_at)
        VALUES(1, ?)
        """,
        (now,),
    )


def _user_authentication_v1(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','active','disabled')),
            group_name TEXT,
            email_verified_at TEXT,
            last_login_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS users_status_idx
            ON users(status, created_at);
        CREATE INDEX IF NOT EXISTS users_group_idx
            ON users(group_name, status);
        CREATE INDEX IF NOT EXISTS email_verification_expiry_idx
            ON email_verification_tokens(expires_at, consumed_at);
        CREATE UNIQUE INDEX IF NOT EXISTS email_verification_one_active_idx
            ON email_verification_tokens(user_id)
            WHERE consumed_at IS NULL;
        CREATE INDEX IF NOT EXISTS user_sessions_user_idx
            ON user_sessions(user_id, revoked_at, expires_at);
        CREATE INDEX IF NOT EXISTS user_sessions_expiry_idx
            ON user_sessions(expires_at, revoked_at);
        """,
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "legacy_compatibility", _legacy_compatibility),
    (2, "worker_protocol_v1", _worker_protocol_v1),
    (3, "artifact_commit_journal", _artifact_commit_journal),
    (4, "gpu_push_dispatch_v2", _gpu_push_dispatch_v2),
    (5, "user_authentication_v1", _user_authentication_v1),
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
    completed: list[int] = []
    for version, name, migration in MIGRATIONS:
        connection.execute("BEGIN IMMEDIATE")
        try:
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if already_applied is not None:
                connection.commit()
                continue
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
