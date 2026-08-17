from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from moshi_data_pipeline.config import load_config
from moshi_data_pipeline.studio.artifacts import ArtifactStore, UploadConflictError
from moshi_data_pipeline.studio.catalog import (
    GpuCheckRateLimitError,
    LeaseConflictError,
    StudioCatalog,
)
from moshi_data_pipeline.studio.job_contexts import JobContextBuilder
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION
from moshi_data_pipeline.studio.protocol import UploadCreate


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: int) -> None:
        self.value += timedelta(**values)


def _catalog_with_job(tmp_path, *, clock=None):
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    project = catalog.create_project("GPU catalog")
    job = catalog.create_job(project["id"], "transcribe", None)
    return catalog, project, job


def test_v3_workspace_upgrades_to_gpu_schema(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = StudioCatalog(path)
    with catalog.connect() as connection:
        connection.executescript(
            """
            DROP TABLE gpu_dispatch_inputs;
            DROP TABLE gpu_dispatches;
            DROP TABLE gpu_checks;
            DROP TABLE gpu_runtime_state;
            DROP TABLE gpu_dispatch_leader;
            ALTER TABLE lifecycle_state RENAME TO lifecycle_state_v4;
            CREATE TABLE lifecycle_state (
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
            INSERT INTO lifecycle_state(
                id,provider,instance_state,desired_state,last_transition_at,
                startup_deadline,recovery_count,blocked_reason,
                controller_generation,last_error,updated_at
            )
            SELECT id,provider,instance_state,desired_state,last_transition_at,
                startup_deadline,recovery_count,blocked_reason,
                controller_generation,last_error,updated_at
            FROM lifecycle_state_v4;
            DROP TABLE lifecycle_state_v4;
            DELETE FROM schema_migrations WHERE version=4;
            """
        )

    upgraded = StudioCatalog(path)
    with upgraded.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        lifecycle_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(lifecycle_state)").fetchall()
        }
    assert version == LATEST_SCHEMA_VERSION == 5
    assert {
        "gpu_runtime_state",
        "gpu_checks",
        "gpu_dispatches",
        "gpu_dispatch_inputs",
        "gpu_dispatch_leader",
    } <= tables
    assert {
        "instance_id",
        "last_aws_observation_at",
        "last_aws_error_at",
        "draining",
        "idle_stop_at",
    } <= lifecycle_columns
    assert upgraded.get_gpu_runtime_state()["instance_state"] == "unknown"
    assert upgraded.get_gpu_dispatch_leader()["fencing_epoch"] == 0


def test_gpu_dispatch_leadership_is_transactional_and_fenced(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "catalog.sqlite3"
    first = StudioCatalog(path, clock=clock)
    second = StudioCatalog(path, clock=clock)
    barrier = threading.Barrier(2)

    def acquire(catalog: StudioCatalog, owner: str):
        barrier.wait()
        return catalog.acquire_gpu_dispatch_leader(owner, lease_seconds=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: acquire(*item), ((first, "one"), (second, "two"))))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    winner = winners[0]
    winner_catalog = first if winner["owner_id"] == "one" else second
    loser_catalog = second if winner_catalog is first else first
    renewed = winner_catalog.renew_gpu_dispatch_leader(
        winner["owner_id"], winner["fencing_epoch"], lease_seconds=10
    )
    assert renewed["fencing_epoch"] == winner["fencing_epoch"]

    clock.advance(seconds=11)
    takeover = loser_catalog.acquire_gpu_dispatch_leader("takeover", lease_seconds=10)
    assert takeover is not None
    assert takeover["fencing_epoch"] == winner["fencing_epoch"] + 1
    with pytest.raises(LeaseConflictError):
        winner_catalog.renew_gpu_dispatch_leader(winner["owner_id"], winner["fencing_epoch"])
    assert (
        winner_catalog.release_gpu_dispatch_leader(winner["owner_id"], winner["fencing_epoch"])
        is False
    )


def test_dispatch_claim_recovers_deterministic_lease_without_plaintext(tmp_path) -> None:
    clock = Clock()
    catalog, _, job = _catalog_with_job(tmp_path, clock=clock)
    leader = catalog.acquire_gpu_dispatch_leader("dispatcher", lease_seconds=30)
    assert leader is not None
    secret = b"dummy-test-key-not-a-production-secret"

    def token_factory(job_id: str, attempt: int) -> str:
        return hmac.new(secret, f"{job_id}:{attempt}".encode(), hashlib.sha256).hexdigest()

    claimed = catalog.claim_job_for_gpu_dispatch(
        "gpu-worker",
        protocol_version="1.0",
        worker_build_id="build-a",
        required_build_id="build-a",
        token_factory=token_factory,
        supported_kinds=["transcribe"],
        leader_owner_id="dispatcher",
        leader_epoch=leader["fencing_epoch"],
    )
    assert claimed is not None
    assert claimed["recovered"] is False
    assert claimed["job"]["attempt"] == 1
    assert claimed["dispatch"]["id"].startswith("gpu:")
    token = claimed["lease_token"]

    assert (
        catalog.claim_job_for_gpu_dispatch(
            "different-worker",
            protocol_version="1.0",
            worker_build_id="build-a",
            required_build_id="build-a",
            token_factory=token_factory,
        )
        is None
    )
    assert catalog.active_gpu_dispatch()["id"] == claimed["dispatch"]["id"]

    restarted = StudioCatalog(catalog.path, clock=clock)
    recovered = restarted.claim_job_for_gpu_dispatch(
        "gpu-worker",
        protocol_version="1.0",
        worker_build_id="build-a",
        required_build_id="build-a",
        token_factory=token_factory,
        supported_kinds=["transcribe"],
        leader_owner_id="dispatcher",
        leader_epoch=leader["fencing_epoch"],
    )
    assert recovered is not None
    assert recovered["recovered"] is True
    assert recovered["lease_token"] == token
    assert recovered["dispatch"]["id"] == claimed["dispatch"]["id"]
    assert recovered["job"]["id"] == job["id"]
    blocked = restarted.update_gpu_dispatch(
        claimed["dispatch"]["id"], expected_states=["claimed"], state="blocked"
    )
    assert blocked["finished_at"] is None
    demand = restarted.gpu_demand_summary()
    assert demand["active_dispatches"] == 1
    assert demand["valid_leases"] == 1

    connection = sqlite3.connect(catalog.path)
    try:
        stored_hash = connection.execute(
            "SELECT lease_token_hash FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()[0]
        dispatch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(gpu_dispatches)")
        }
        database_text = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM gpu_dispatches").fetchall()
            for value in row
            if value is not None
        )
    finally:
        connection.close()
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token != stored_hash
    assert "lease_token" not in dispatch_columns
    assert token not in database_text

    with pytest.raises(LeaseConflictError):
        restarted.claim_job_for_gpu_dispatch(
            "gpu-worker",
            protocol_version="1.0",
            worker_build_id="build-a",
            required_build_id="build-a",
            token_factory=lambda _job_id, _attempt: "x" * 64,
        )


def test_gpu_check_deduplication_and_manual_limits(tmp_path) -> None:
    clock = Clock()
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    first, created = catalog.request_gpu_check("manual", requested_by="alice")
    assert created is True
    duplicate, created = catalog.request_gpu_check("manual", requested_by="bob")
    assert created is False
    assert duplicate["id"] == first["id"]
    catalog.update_gpu_check(
        first["id"],
        gpu_check_id="remote-check-1",
        status="passed",
        finished_at=clock().isoformat(),
    )

    with pytest.raises(GpuCheckRateLimitError) as cooldown:
        catalog.request_gpu_check("manual", requested_by="alice")
    assert 599 <= cooldown.value.retry_after <= 601

    clock.advance(seconds=601)
    repeated_remote_id = None
    for index in range(2):
        check, created = catalog.request_gpu_check("manual", requested_by="alice")
        assert created is True
        values = {
            "status": "failed",
            "finished_at": clock().isoformat(),
        }
        if index == 0:
            values["gpu_check_id"] = "remote-check-1"
            repeated_remote_id = check["id"]
        catalog.update_gpu_check(check["id"], **values)
        clock.advance(seconds=601)
    assert catalog.get_gpu_check_by_remote_id("remote-check-1")["id"] == repeated_remote_id
    with pytest.raises(GpuCheckRateLimitError) as hourly:
        catalog.request_gpu_check("manual", requested_by="alice")
    assert "hourly" in hourly.value.reason

    cold, created = catalog.request_gpu_check("manual", requested_by="bob", cold_start=True)
    assert created is True
    catalog.update_gpu_check(cold["id"], status="passed", finished_at=clock().isoformat())
    with pytest.raises(GpuCheckRateLimitError) as cold_limit:
        catalog.request_gpu_check("manual", requested_by="charlie", cold_start=True)
    assert "cold-start" in cold_limit.value.reason


def test_gpu_check_cooldowns_are_configurable(tmp_path) -> None:
    clock = Clock()
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    check, created = catalog.request_gpu_check(
        "manual",
        requested_by="alice",
        cold_start=True,
        manual_cooldown_seconds=7,
        manual_cold_start_cooldown_seconds=19,
    )
    assert created is True
    catalog.update_gpu_check(
        check["id"],
        status="passed",
        finished_at=clock().isoformat(),
    )

    with pytest.raises(GpuCheckRateLimitError) as user_limit:
        catalog.request_gpu_check(
            "manual",
            requested_by="alice",
            manual_cooldown_seconds=7,
            manual_cold_start_cooldown_seconds=19,
        )
    assert 6 <= user_limit.value.retry_after <= 8

    with pytest.raises(GpuCheckRateLimitError) as cold_limit:
        catalog.request_gpu_check(
            "manual",
            requested_by="bob",
            cold_start=True,
            manual_cooldown_seconds=7,
            manual_cold_start_cooldown_seconds=19,
        )
    assert 18 <= cold_limit.value.retry_after <= 20


def test_duplicate_active_job_creation_is_serialized(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    first = StudioCatalog(path)
    project = first.create_project("Duplicate jobs")
    second = StudioCatalog(path)
    fingerprint = "a" * 64
    barrier = threading.Barrier(2)

    def create(catalog: StudioCatalog):
        barrier.wait()
        return catalog.create_job(project["id"], "transcribe", None, input_fingerprint=fingerprint)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(create, (first, second)))
    assert jobs[0]["id"] == jobs[1]["id"]
    with first.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE input_fingerprint=?", (fingerprint,)
        ).fetchone()[0]
    assert count == 1
    assert first.gpu_demand_summary()["runnable_jobs"] == 1
    with first.connect() as connection:
        connection.execute("UPDATE jobs SET attempt=max_attempts WHERE id=?", (jobs[0]["id"],))
    assert first.gpu_demand_summary()["runnable_jobs"] == 0


def test_callback_upload_create_is_idempotent_and_cleans_unused_staging(tmp_path) -> None:
    paths = StudioPaths(tmp_path / "workspace")
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project("Idempotent upload")
    job = catalog.create_job(project["id"], "transcribe", None)
    claimed = catalog.claim_leased_job(
        "worker",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
    )
    assert claimed is not None
    store = ArtifactStore(catalog, paths)
    payload = UploadCreate(
        worker_id="worker",
        role="analysis.transcript",
        sha256=hashlib.sha256(b"x").hexdigest(),
        size_bytes=1,
        media_type="application/json",
        filename="result.json",
    )
    first = store.create_upload(job["id"], claimed["lease_token"], payload)
    second = store.create_upload(job["id"], claimed["lease_token"], payload)
    assert second["id"] == first["id"]
    assert list(paths.worker_staging.glob("*.part")) == [
        paths.resolve_relative(first["staging_path"])
    ]

    conflict = payload.model_copy(update={"sha256": hashlib.sha256(b"y").hexdigest()})
    with pytest.raises(UploadConflictError):
        store.create_upload(job["id"], claimed["lease_token"], conflict)
    assert len(list(paths.worker_staging.glob("*.part"))) == 1

    empty = UploadCreate(
        worker_id="worker",
        role="analysis.empty",
        sha256=hashlib.sha256(b"").hexdigest(),
        size_bytes=0,
        media_type="application/octet-stream",
        filename="empty.bin",
    )
    empty_first = store.create_upload(job["id"], claimed["lease_token"], empty)
    assert empty_first["state"] == "verified"
    catalog.update_artifact_upload(empty_first["id"], state="committed")
    empty_retry = store.create_upload(job["id"], claimed["lease_token"], empty)
    assert empty_retry["id"] == empty_first["id"]
    assert empty_retry["state"] == "committed"
    assert len(list(paths.worker_staging.glob("*.part"))) == 2

    now = datetime.now(UTC)
    expired = catalog.create_artifact_upload(
        job["id"],
        "worker",
        claimed["lease_token"],
        role="analysis.expired",
        staging_path="worker_staging/expired.part",
        expected_sha256=hashlib.sha256(b"old").hexdigest(),
        expected_size=3,
        media_type="application/octet-stream",
        filename="old.bin",
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    replacement = catalog.create_artifact_upload(
        job["id"],
        "worker",
        claimed["lease_token"],
        role="analysis.expired",
        staging_path="worker_staging/replacement.part",
        expected_sha256=hashlib.sha256(b"new").hexdigest(),
        expected_size=3,
        media_type="application/octet-stream",
        filename="new.bin",
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    assert replacement["id"] != expired["id"]
    assert catalog.get_artifact_upload(expired["id"])["state"] == "discarded"


def test_job_context_freezes_and_revalidates_artifact_metadata(tmp_path) -> None:
    paths = StudioPaths(tmp_path / "workspace")
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project("Immutable input")
    content = b"audio-fixture"
    original = paths.originals / "fixture.wav"
    original.write_bytes(content)
    source = catalog.create_source(
        project["id"],
        original.name,
        paths.relative(original),
        "audio/wav",
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    contexts = JobContextBuilder(catalog, paths, load_config())
    preconditions, _, fingerprint = contexts.snapshot(
        project_id=project["id"],
        kind="initialize",
        source_id=source["id"],
        payload={"mode": "manual"},
    )
    frozen = preconditions["input_artifacts"][0]
    assert {
        "artifact_id",
        "role",
        "sha256",
        "size_bytes",
        "media_type",
        "filename",
        "project_id",
        "source_id",
    } <= frozen.keys()
    job = catalog.create_job(
        project["id"],
        "initialize",
        source["id"],
        {"mode": "manual"},
        preconditions=preconditions,
        input_fingerprint=fingerprint,
    )
    claimed = catalog.claim_leased_job(
        "worker",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["initialize"],
    )
    assert claimed is not None and claimed["id"] == job["id"]
    assert contexts.create_context(claimed).inputs[0].filename == "fixture.wav"

    with catalog.connect() as connection:
        connection.execute(
            "UPDATE artifacts SET media_type='application/octet-stream' WHERE id=?",
            (frozen["artifact_id"],),
        )
    with pytest.raises(ValueError, match="metadata changed"):
        contexts.create_context(claimed)
