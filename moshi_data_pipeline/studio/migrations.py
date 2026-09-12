from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

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


def _dataset_ownership_v1(connection: sqlite3.Connection) -> None:
    # SQLite cannot add a non-null foreign-key column to a populated table without
    # rebuilding it. Legacy rows intentionally remain ownerless until Plan 11.
    _add_columns(
        connection,
        "projects",
        {
            "owner_user_id": "TEXT REFERENCES users(id) ON DELETE RESTRICT",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE INDEX IF NOT EXISTS projects_owner_updated_idx
            ON projects(owner_user_id, updated_at);

        CREATE TABLE IF NOT EXISTS project_ownership_audit (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('transfer','delete')),
            project_id TEXT NOT NULL,
            previous_owner_user_id TEXT,
            new_owner_user_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS project_ownership_audit_project_idx
            ON project_ownership_audit(project_id, created_at);
        CREATE INDEX IF NOT EXISTS project_ownership_audit_actor_idx
            ON project_ownership_audit(actor_user_id, created_at);
        """,
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS projects_owner_required_insert
        BEFORE INSERT ON projects
        WHEN NEW.owner_user_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'projects.owner_user_id is required');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS projects_owner_required_update
        BEFORE UPDATE ON projects
        WHEN NEW.owner_user_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'projects.owner_user_id is required');
        END
        """
    )


def _recoverable_project_deletion(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "projects",
        {
            "deletion_state": (
                "TEXT NOT NULL DEFAULT 'active' "
                "CHECK(deletion_state IN ('active','deleting'))"
            ),
            "deletion_reservation_id": "TEXT",
            "deletion_reserved_at": "TEXT",
            "deletion_reserved_by": "TEXT",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS project_deletion_cleanup (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            actor_user_id TEXT NOT NULL,
            audit_id TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'reserved','quarantined','pending_purge','complete',
                'aborted','restore_failed','purge_failed'
            )),
            quarantine_path TEXT,
            manifest_json TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS project_deletion_cleanup_project_idx
            ON project_deletion_cleanup(project_id, created_at);
        CREATE INDEX IF NOT EXISTS project_deletion_cleanup_state_idx
            ON project_deletion_cleanup(state, updated_at);
        """,
    )
    # Legacy ownerless projects remain immutable except for the deletion reservation
    # fields needed for an administrator to remove them safely.
    connection.execute("DROP TRIGGER IF EXISTS projects_owner_required_update")
    connection.execute(
        """
        CREATE TRIGGER projects_owner_required_update
        BEFORE UPDATE ON projects
        WHEN NEW.owner_user_id IS NULL AND NOT (
            OLD.owner_user_id IS NULL
            AND NEW.id IS OLD.id
            AND NEW.name IS OLD.name
            AND NEW.language IS OLD.language
            AND NEW.created_at IS OLD.created_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'projects.owner_user_id is required');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS projects_deletion_state_insert
        BEFORE INSERT ON projects
        WHEN (
            NEW.deletion_state='active' AND NEW.deletion_reservation_id IS NOT NULL
        ) OR (
            NEW.deletion_state='deleting' AND NEW.deletion_reservation_id IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid project deletion reservation');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS projects_deletion_state_update
        BEFORE UPDATE ON projects
        WHEN (
            NEW.deletion_state='active' AND NEW.deletion_reservation_id IS NOT NULL
        ) OR (
            NEW.deletion_state='deleting' AND NEW.deletion_reservation_id IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid project deletion reservation');
        END
        """
    )


def _review_chapters_v1(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS review_chapter_sets (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 0),
            mode TEXT NOT NULL CHECK(mode IN ('max_duration','count','manual')),
            max_duration_seconds INTEGER NOT NULL CHECK(max_duration_seconds BETWEEN 60 AND 7200),
            requested_count INTEGER CHECK(requested_count IS NULL OR requested_count BETWEEN 1 AND 500),
            manual_boundaries_json TEXT NOT NULL DEFAULT '[]',
            boundary_search_seconds INTEGER NOT NULL CHECK(boundary_search_seconds BETWEEN 0 AND 300),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_chapters (
            id TEXT PRIMARY KEY,
            chapter_set_id TEXT NOT NULL REFERENCES review_chapter_sets(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
            start_sample INTEGER NOT NULL CHECK(start_sample >= 0),
            end_sample INTEGER NOT NULL CHECK(end_sample > start_sample),
            boundary_reason TEXT NOT NULL CHECK(boundary_reason IN ('source_edge','silence','segment','manual')),
            UNIQUE(chapter_set_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS chapter_reviews (
            chapter_id TEXT NOT NULL REFERENCES review_chapters(id) ON DELETE CASCADE,
            reviewer_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 0),
            status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('not_started','in_progress','complete')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(chapter_id, reviewer_user_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS review_chapter_sets_one_active_idx
            ON review_chapter_sets(source_id) WHERE active=1;
        CREATE INDEX IF NOT EXISTS review_chapters_set_range_idx
            ON review_chapters(chapter_set_id, start_sample, end_sample);
        CREATE INDEX IF NOT EXISTS chapter_reviews_reviewer_idx
            ON chapter_reviews(reviewer_user_id, status, updated_at);
        """,
    )


def _annotation_fingerprint(raw: str) -> str:
    """Fingerprint annotation content without its internal generation number."""
    value = json.loads(raw)
    value.pop("version", None)
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:32]}"


def _transcript_versions_and_approval_v1(connection: sqlite3.Connection) -> None:
    """Add P5 metadata without replacing or rewriting annotation payloads."""
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS corrected_versions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
            current_annotation_version INTEGER NOT NULL CHECK(current_annotation_version >= 1),
            content_fingerprint TEXT CHECK(
                content_fingerprint IS NULL OR length(content_fingerprint) = 64
            ),
            status TEXT NOT NULL DEFAULT 'unapproved' CHECK(status IN (
                'unapproved','pending','approved','rejected'
            )),
            created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            approved_at TEXT,
            approved_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            decision_note TEXT NOT NULL DEFAULT '',
            retention_state TEXT NOT NULL DEFAULT 'keep' CHECK(retention_state IN (
                'keep','archive_pending','archived','payload_removed'
            )),
            archive_eligible_at TEXT,
            UNIQUE(source_id, ordinal),
            UNIQUE(source_id, current_annotation_version)
        );

        CREATE TABLE IF NOT EXISTS machine_transcript_versions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
            producing_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            producer TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_revision TEXT,
            language TEXT,
            config_fingerprint TEXT,
            raw_transcript_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            aligned_transcript_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            diarization_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            raw_snapshot_path TEXT,
            aligned_snapshot_path TEXT,
            diarization_snapshot_path TEXT,
            artifact_fingerprint TEXT NOT NULL,
            artifact_manifest_json TEXT NOT NULL DEFAULT '{}',
            provenance_status TEXT NOT NULL DEFAULT 'historical_unknown' CHECK(
                provenance_status IN ('exact','historical_unknown')
            ),
            comparison_available INTEGER NOT NULL DEFAULT 0 CHECK(comparison_available IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(source_id, ordinal),
            UNIQUE(source_id, artifact_fingerprint)
        );

        CREATE TABLE IF NOT EXISTS annotation_approval_tasks (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            corrected_version_id TEXT NOT NULL REFERENCES corrected_versions(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 1),
            content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint) = 64),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                'pending','approved','rejected','returned','superseded'
            )),
            submitted_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            reviewed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            review_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS corrected_version_retention_decisions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            approved_corrected_version_id TEXT NOT NULL REFERENCES corrected_versions(id),
            mode TEXT NOT NULL CHECK(mode IN ('keep_all','archive_older')),
            decided_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            affected_versions_json TEXT NOT NULL DEFAULT '[]',
            estimated_bytes INTEGER NOT NULL DEFAULT 0 CHECK(estimated_bytes >= 0),
            backup_reference TEXT,
            execute_after TEXT,
            status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN (
                'recorded','waiting','complete','cancelled','blocked'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS annotation_shared_blobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('aligned_words')),
            content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint) = 64),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(kind, content_fingerprint)
        );

        CREATE INDEX IF NOT EXISTS corrected_versions_source_idx
            ON corrected_versions(source_id, ordinal DESC);
        CREATE INDEX IF NOT EXISTS machine_transcript_versions_source_idx
            ON machine_transcript_versions(source_id, ordinal DESC);
        CREATE INDEX IF NOT EXISTS annotation_approval_tasks_status_idx
            ON annotation_approval_tasks(status, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS annotation_approval_one_pending_source_idx
            ON annotation_approval_tasks(source_id) WHERE status='pending';
        """,
    )
    _add_columns(
        connection,
        "annotation_revisions",
        {
            "corrected_version_id": "TEXT REFERENCES corrected_versions(id)",
            "generation": "INTEGER NOT NULL DEFAULT 1",
            "content_fingerprint": "TEXT",
            "parent_annotation_version": "INTEGER",
            "created_by_user_id": "TEXT REFERENCES users(id) ON DELETE SET NULL",
            "origin": "TEXT NOT NULL DEFAULT 'historical'",
            "change_summary_json": "TEXT NOT NULL DEFAULT '{}'",
            "recoverable_until": "TEXT",
            "retention_state": "TEXT NOT NULL DEFAULT 'current'",
            "shared_aligned_words_id": "TEXT REFERENCES annotation_shared_blobs(id)",
        },
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS annotation_revisions_corrected_version_idx
        ON annotation_revisions(corrected_version_id, generation DESC)
        """
    )

    # Every old server revision remains available as the same visible V number.
    # Fingerprints and pointers live in lightweight corrected_versions rows;
    # annotation_revisions is not updated, so its large JSON records are not
    # physically rewritten by this migration.
    rows = connection.execute(
        """
        SELECT id,source_id,version,annotation_json,created_at
        FROM annotation_revisions ORDER BY source_id,version
        """
    ).fetchall()
    for row in rows:
        version_id = _stable_id("corrected", row[1], row[2])
        fingerprint = _annotation_fingerprint(str(row[3]))
        connection.execute(
            """
            INSERT OR IGNORE INTO corrected_versions(
                id,source_id,ordinal,current_annotation_version,content_fingerprint,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                version_id,
                row[1],
                row[2],
                row[2],
                fingerprint,
                "unapproved",
                row[4],
                row[4],
            ),
        )


def _p5_provenance_hardening(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "machine_transcript_versions",
        {
            "artifact_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "provenance_status": "TEXT NOT NULL DEFAULT 'historical_unknown'",
        },
    )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS annotation_version_references (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 1),
            reference_kind TEXT NOT NULL,
            reference_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, annotation_version, reference_kind, reference_id)
        );
        CREATE INDEX IF NOT EXISTS annotation_version_references_version_idx
            ON annotation_version_references(source_id, annotation_version);
        """,
    )

    # M rows created before provenance hardening are truthful historical records:
    # their immutable files and hashes are known, but the model/config must not
    # be claimed exact if old jobs did not persist those fields.
    machine_rows = connection.execute(
        """
        SELECT id,raw_transcript_artifact_id,aligned_transcript_artifact_id,
               diarization_artifact_id
        FROM machine_transcript_versions ORDER BY source_id,ordinal
        """
    ).fetchall()
    for row in machine_rows:
        manifest: dict[str, Any] = {}
        for label, artifact_id in (
            ("raw", row[1]),
            ("aligned", row[2]),
            ("diarization", row[3]),
        ):
            if not artifact_id:
                continue
            artifact = connection.execute(
                "SELECT id,role,sha256,size_bytes,media_type FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
            if artifact is not None:
                manifest[label] = {
                    "artifact_id": artifact[0],
                    "role": artifact[1],
                    "sha256": artifact[2],
                    "size_bytes": artifact[3],
                    "media_type": artifact[4],
                }
        connection.execute(
            """
            UPDATE machine_transcript_versions
            SET artifact_manifest_json=?,provenance_status='historical_unknown',
                model_name='historical model (metadata unavailable)',
                model_revision=NULL,config_fingerprint=NULL
            WHERE id=?
            """,
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True), row[0]),
        )

    # Completed jobs are immutable outputs. Register any annotation generation
    # explicitly frozen in their preconditions so retention cannot remove it.
    for job_id, source_id, preconditions_json, created_at in connection.execute(
        "SELECT id,source_id,preconditions_json,created_at FROM jobs WHERE source_id IS NOT NULL"
    ).fetchall():
        preconditions = json.loads(preconditions_json or "{}")
        annotation = preconditions.get("annotation") or {}
        version = annotation.get("version")
        if not isinstance(version, int) or version < 1:
            continue
        reference_id = _stable_id("annotation_ref", source_id, version, "job", job_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO annotation_version_references(
                id,source_id,annotation_version,reference_kind,reference_id,created_at
            ) VALUES(?,?,?,'job_input',?,?)
            """,
            (reference_id, source_id, version, job_id, created_at),
        )


def _p5_retention_audit(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "annotation_revisions",
        {"payload_removed_at": "TEXT"},
    )


def _p6_overlap_review(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS overlap_reviews (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 1),
            speaker_a_activity_id TEXT NOT NULL,
            speaker_b_activity_id TEXT NOT NULL,
            start_sample INTEGER NOT NULL CHECK(start_sample >= 0),
            end_sample INTEGER NOT NULL CHECK(end_sample > start_sample),
            classification TEXT NOT NULL CHECK(classification IN (
                'unreviewed','confirmed','false_positive','third_speaker','noise','unintelligible'
            )),
            training_decision TEXT NOT NULL CHECK(training_decision IN (
                'raw','separate','exclude','needs_work'
            )),
            state TEXT NOT NULL CHECK(state IN ('current','stale')),
            note TEXT NOT NULL DEFAULT '',
            reviewer_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            recovery_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_id, annotation_version, speaker_a_activity_id, speaker_b_activity_id)
        );
        CREATE TABLE IF NOT EXISTS overlap_review_events (
            id TEXT PRIMARY KEY,
            overlap_review_id TEXT NOT NULL REFERENCES overlap_reviews(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            annotation_version INTEGER NOT NULL,
            actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS overlap_reviews_source_idx
            ON overlap_reviews(source_id, annotation_version, start_sample);
        CREATE INDEX IF NOT EXISTS overlap_review_events_review_idx
            ON overlap_review_events(overlap_review_id, created_at);
        """,
    )


def _p7_evaluation_reports(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS evaluation_reports (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            machine_transcript_version_id TEXT NOT NULL
                REFERENCES machine_transcript_versions(id),
            corrected_version_id TEXT NOT NULL REFERENCES corrected_versions(id),
            annotation_version INTEGER NOT NULL CHECK(annotation_version >= 1),
            content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint) = 64),
            normalization_policy TEXT NOT NULL,
            overlap_policy TEXT NOT NULL CHECK(overlap_policy IN (
                'deduplicate','include','exclude'
            )),
            scope_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evaluation_reports_source_idx
            ON evaluation_reports(source_id, created_at DESC);
        """,
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "legacy_compatibility", _legacy_compatibility),
    (2, "worker_protocol_v1", _worker_protocol_v1),
    (3, "artifact_commit_journal", _artifact_commit_journal),
    (4, "gpu_push_dispatch_v2", _gpu_push_dispatch_v2),
    (5, "user_authentication_v1", _user_authentication_v1),
    (6, "dataset_ownership_v1", _dataset_ownership_v1),
    (7, "recoverable_project_deletion", _recoverable_project_deletion),
    (8, "review_chapters_v1", _review_chapters_v1),
    (9, "transcript_versions_and_approval_v1", _transcript_versions_and_approval_v1),
    (10, "p5_provenance_hardening", _p5_provenance_hardening),
    (11, "p5_retention_audit", _p5_retention_audit),
    (12, "p6_overlap_review", _p6_overlap_review),
    (13, "p7_evaluation_reports", _p7_evaluation_reports),
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
