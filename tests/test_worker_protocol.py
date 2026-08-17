from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from moshi_data_pipeline.studio.catalog import (
    LeaseConflictError,
    ProtocolMismatchError,
    StudioCatalog,
)
from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION
from moshi_data_pipeline.studio.protocol import JobContext

ROOT = Path(__file__).parents[1]
WORKER_SCHEMA = ROOT / "protocol" / "worker_protocol.schema.json"
EXPECTED_SCHEMA_SHA256 = "bd390dc8f225f286bbb1924efbea24234db5f5fc745f3f9c0ad010a70aeade18"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: int) -> None:
        self.value += timedelta(**values)


def _catalog(tmp_path: Path, clock: MutableClock) -> tuple[StudioCatalog, str]:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    project = catalog.create_project("Remote worker")
    return catalog, str(project["id"])


def test_protocol_schema_copies_are_identical_and_validate_golden_context() -> None:
    schema_bytes = WORKER_SCHEMA.read_bytes()
    assert hashlib.sha256(schema_bytes).hexdigest() == EXPECTED_SCHEMA_SHA256
    schema = json.loads(schema_bytes)
    jsonschema.Draft202012Validator.check_schema(schema)
    message = {
        "message_type": "job_context",
        "body": {
            "protocol_version": "1.0",
            "job_id": "job_example",
            "kind": "initialize",
            "attempt": 1,
            "lease_expires_at": "2026-08-13T10:02:00+00:00",
            "input_fingerprint": "a" * 64,
            "payload": {"mode": "manual"},
            "preconditions": {},
            "config": {},
            "inputs": [],
        },
    }
    jsonschema.validate(message, schema, format_checker=jsonschema.FormatChecker())
    JobContext.model_validate(message["body"])


def test_numbered_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = StudioCatalog(path)
    with catalog.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [int(row["version"]) for row in versions] == list(
            range(1, LATEST_SCHEMA_VERSION + 1)
        )
    StudioCatalog(path)
    with catalog.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == LATEST_SCHEMA_VERSION


def test_lease_claim_heartbeat_and_completion_require_current_token(tmp_path: Path) -> None:
    clock = MutableClock()
    catalog, project_id = _catalog(tmp_path, clock)
    created = catalog.create_job(project_id, "transcribe", None)

    claimed = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
    )
    assert claimed is not None
    assert claimed["id"] == created["id"]
    assert claimed["attempt"] == 1
    assert "lease_token_hash" not in claimed
    token = claimed.pop("lease_token")

    with pytest.raises(LeaseConflictError):
        catalog.heartbeat_leased_job(created["id"], "worker-1", "wrong-token")
    clock.advance(seconds=15)
    heartbeat = catalog.heartbeat_leased_job(
        created["id"],
        "worker-1",
        token,
        progress=0.25,
        message="Downloading",
    )
    assert heartbeat["progress"] == 0.25
    assert heartbeat["message"] == "Downloading"

    completed = catalog.complete_leased_job(
        created["id"], "worker-1", token, {"ok": True}
    )
    assert completed["status"] == "complete"
    assert completed["result"] == {"ok": True}
    with pytest.raises(LeaseConflictError):
        catalog.complete_leased_job(created["id"], "worker-1", token, {"ok": True})
    assert catalog.list_job_attempts(created["id"])[0]["status"] == "complete"


def test_expired_lease_requeues_then_fails_after_maximum_attempts(tmp_path: Path) -> None:
    clock = MutableClock()
    catalog, project_id = _catalog(tmp_path, clock)
    created = catalog.create_job(project_id, "transcribe", None, max_attempts=2)

    first = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
        lease_seconds=30,
    )
    assert first is not None
    clock.advance(seconds=31)
    second = catalog.claim_leased_job(
        "worker-2",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
        lease_seconds=30,
    )
    assert second is not None
    assert second["id"] == created["id"]
    assert second["attempt"] == 2
    with pytest.raises(LeaseConflictError):
        catalog.complete_leased_job(
            created["id"], "worker-1", first["lease_token"], {"stale": True}
        )

    clock.advance(seconds=31)
    assert catalog.requeue_expired_jobs() == [created["id"]]
    failed = catalog.get_job(created["id"])
    assert failed["status"] == "failed"
    assert failed["failure_class"] == "lease_expired"
    assert [item["status"] for item in catalog.list_job_attempts(created["id"])] == [
        "expired",
        "expired",
    ]


def test_retryable_and_deterministic_failures(tmp_path: Path) -> None:
    clock = MutableClock()
    catalog, project_id = _catalog(tmp_path, clock)
    retry_job = catalog.create_job(project_id, "transcribe", None)
    lease = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
    )
    assert lease is not None
    requeued = catalog.fail_leased_job(
        retry_job["id"],
        "worker-1",
        lease["lease_token"],
        error="connection reset",
        failure_class="transport",
        retryable=True,
    )
    assert requeued["status"] == "queued"
    assert requeued["retryable"] is True

    deterministic = catalog.create_job(project_id, "generate", None)
    lease = catalog.claim_leased_job(
        "worker-2",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["generate"],
    )
    assert lease is not None
    failed = catalog.fail_leased_job(
        deterministic["id"],
        "worker-2",
        lease["lease_token"],
        error="invalid clip plan",
        failure_class="invalid_input",
        retryable=False,
    )
    assert failed["status"] == "failed"
    assert failed["retryable"] is False


def test_protocol_mismatch_leaves_job_queued_and_worker_visible(tmp_path: Path) -> None:
    clock = MutableClock()
    catalog, project_id = _catalog(tmp_path, clock)
    created = catalog.create_job(project_id, "transcribe", None)
    with pytest.raises(ProtocolMismatchError):
        catalog.claim_leased_job(
            "old-worker",
            protocol_version="0.9",
            worker_build_id="old-build",
            supported_kinds=["transcribe"],
        )
    assert catalog.get_job(created["id"])["status"] == "queued"

    state = catalog.record_worker_state(
        "old-worker",
        boot_id="boot-1",
        protocol_version="0.9",
        build_id="old-build",
        supported_kinds=["transcribe"],
        status="ready",
    )
    assert state["status"] == "incompatible"
    assert state["compatible"] is False


def test_superseding_running_job_revokes_lease(tmp_path: Path) -> None:
    clock = MutableClock()
    catalog, project_id = _catalog(tmp_path, clock)
    created = catalog.create_job(project_id, "realign", None, {"annotation_version": 2})
    lease = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["realign"],
    )
    assert lease is not None
    superseded = catalog.supersede_job(created["id"], "Annotation revision changed")
    assert superseded["status"] == "superseded"
    with pytest.raises(LeaseConflictError):
        catalog.heartbeat_leased_job(
            created["id"], "worker-1", lease["lease_token"]
        )
