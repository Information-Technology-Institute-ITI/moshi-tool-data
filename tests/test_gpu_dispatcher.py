from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any

from moshi_data_pipeline.config import load_config
from moshi_data_pipeline.gpu_dispatch_client import (
    GpuDispatchNotFoundError,
    GpuDispatchResponse,
    GpuDispatchRetryableError,
)
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.gpu_dispatcher import (
    GpuDispatcherSettings,
    GpuPushDispatcher,
    derive_dispatch_lease_token,
)
from moshi_data_pipeline.studio.job_contexts import JobContextBuilder
from moshi_data_pipeline.studio.media import StudioPaths

BUILD = "gpu-build-test"
SECRET = "dummy-dispatch-test-secret-with-sufficient-entropy"


@dataclass
class FakeGpuState:
    receipt: dict[str, Any] | None = None
    received: dict[str, bytearray] = field(default_factory=dict)
    create_calls: int = 0
    get_calls: int = 0
    start_calls: int = 0
    cancel_calls: int = 0
    ambiguous_create: bool = False
    corrupt_receipt: bool = False
    ambiguous_put: bool = False
    fail_put_without_progress: bool = False
    accepting_dispatches: bool = True
    functional_ready: bool = True
    check_status: str = "queued"
    trigger_check_calls: int = 0
    ready_failures: int = 0
    ready_calls: int = 0
    live_build_id: str = BUILD
    job_kinds: list[str] = field(default_factory=lambda: ["transcribe"])
    put_started: threading.Event | None = None
    release_put: threading.Event | None = None


class FakeGpuClient:
    upload_chunk_bytes = 4

    def __init__(self, state: FakeGpuState) -> None:
        self.state = state
        self.started_lease_token: str | None = None

    def live(self) -> GpuDispatchResponse:
        return GpuDispatchResponse(
            200,
            {
                "status": "alive",
                "protocol_version": "2.0",
                "build_id": self.state.live_build_id,
            },
            {},
        )

    def ready(self) -> GpuDispatchResponse:
        self.state.ready_calls += 1
        if self.state.ready_failures:
            self.state.ready_failures -= 1
            raise GpuDispatchRetryableError(
                "readiness request failed",
                retryable=True,
                method="GET",
                path="/internal/v2/health/ready",
            )
        current = self.state.receipt if self.state.receipt else None
        return GpuDispatchResponse(
            200,
            {
                "status": "ready",
                "protocol_version": "2.0",
                "build_id": BUILD,
                "capabilities": {
                    "job_kinds": self.state.job_kinds,
                    "input_receipt": True,
                    "execution": True,
                    "callback_outbox": True,
                    "functional_check": True,
                },
                "service": {
                    "host_boot_id": "host-boot-a",
                    "service_boot_id": "service-boot-a",
                },
                "callback": {"ready": True, "auth_blocked": False},
                "functional_check": {
                    "ready": self.state.functional_ready,
                    "requirement_key": "requirement-a",
                    "latest": None,
                },
                "operational_ready": True,
                "accepting_dispatches": bool(
                    self.state.accepting_dispatches and current is None
                ),
                "safe_to_stop": current is None,
                "current_dispatch": current,
                "dispatch_counts": {},
            },
            {},
        )

    def create_dispatch(self, payload) -> GpuDispatchResponse:
        self.state.create_calls += 1
        proposed = {
            **payload.model_dump(mode="json"),
            "state": "receiving",
        }
        if self.state.receipt is None:
            self.state.receipt = proposed
            self.state.received = {
                item.artifact_id: bytearray() for item in payload.inputs
            }
            if self.state.corrupt_receipt:
                self.state.receipt["inputs"][0]["sha256"] = "0" * 64
        assert self.state.receipt["dispatch_id"] == payload.dispatch_id
        if self.state.ambiguous_create:
            self.state.ambiguous_create = False
            raise GpuDispatchRetryableError(
                "dispatch response was lost",
                retryable=True,
                method="POST",
                path="/internal/v2/dispatches",
            )
        return GpuDispatchResponse(201, dict(self.state.receipt), {})

    def get_dispatch(self, dispatch_id: str) -> GpuDispatchResponse:
        self.state.get_calls += 1
        if self.state.receipt is None:
            raise GpuDispatchNotFoundError(
                "missing receipt",
                status=404,
                method="GET",
                path=f"/internal/v2/dispatches/{dispatch_id}",
            )
        return GpuDispatchResponse(200, dict(self.state.receipt), {})

    def head_input(self, dispatch_id: str, artifact_id: str) -> GpuDispatchResponse:
        assert self.state.receipt and self.state.receipt["dispatch_id"] == dispatch_id
        expected = next(
            item
            for item in self.state.receipt["inputs"]
            if item["artifact_id"] == artifact_id
        )
        offset = len(self.state.received[artifact_id])
        return GpuDispatchResponse(
            200,
            None,
            {
                "x-accepted-offset": str(offset),
                "x-input-state": "verified" if offset == expected["size_bytes"] else "open",
            },
        )

    def put_input(
        self,
        dispatch_id: str,
        artifact_id: str,
        *,
        body: bytes,
        start: int,
        end: int,
        total: int,
    ) -> GpuDispatchResponse:
        assert self.state.receipt and self.state.receipt["dispatch_id"] == dispatch_id
        target = self.state.received[artifact_id]
        assert len(target) == start
        assert end == start + len(body) - 1
        assert end < total
        if self.state.put_started is not None:
            self.state.put_started.set()
        if self.state.release_put is not None:
            assert self.state.release_put.wait(timeout=5)
            self.state.release_put = None
        if self.state.fail_put_without_progress:
            self.state.fail_put_without_progress = False
            raise GpuDispatchRetryableError(
                "upload was interrupted",
                retryable=True,
                method="PUT",
                path="/internal/v2/dispatches/input",
            )
        target.extend(body)
        if self.state.ambiguous_put:
            self.state.ambiguous_put = False
            raise GpuDispatchRetryableError(
                "upload response was lost",
                retryable=True,
                method="PUT",
                path="/internal/v2/dispatches/input",
            )
        return GpuDispatchResponse(200, {"accepted_offset": len(target)}, {})

    def start(self, dispatch_id: str, payload, lease_token: str) -> GpuDispatchResponse:
        assert self.state.receipt and self.state.receipt["dispatch_id"] == dispatch_id
        self.state.start_calls += 1
        self.started_lease_token = lease_token
        self.state.receipt["state"] = "queued"
        return GpuDispatchResponse(202, dict(self.state.receipt), {})

    def cancel(self, dispatch_id: str) -> GpuDispatchResponse:
        assert self.state.receipt and self.state.receipt["dispatch_id"] == dispatch_id
        self.state.cancel_calls += 1
        self.state.receipt["state"] = "cancelled"
        return GpuDispatchResponse(200, dict(self.state.receipt), {})

    def self_checks(self, *, limit: int = 10) -> GpuDispatchResponse:
        checks = [self._check_record()] if self.state.trigger_check_calls else []
        return GpuDispatchResponse(200, {"checks": checks[:limit]}, {})

    def trigger_self_check(self, payload) -> GpuDispatchResponse:
        self.state.trigger_check_calls += 1
        return GpuDispatchResponse(202, self._check_record(), {})

    def _check_record(self) -> dict[str, Any]:
        now = "2026-08-16T12:00:00+00:00"
        terminal = self.state.check_status in {"passed", "failed"}
        return {
            "id": "remote-check-a",
            "status": self.state.check_status,
            "trigger": "job_preflight",
            "requirement_key": "requirement-a",
            "host_boot_id": "host-boot-a",
            "build_id": BUILD,
            "protocol_version": "2.0",
            "config_fingerprint": "c" * 64,
            "fixture_id": "fixture-a",
            "fixture_sha256": "f" * 64,
            "requested_model_revision": "model-a",
            "requested_at": now,
            "started_at": now if self.state.check_status != "queued" else None,
            "finished_at": now if terminal else None,
            "valid_until": "2026-08-17T12:00:00+00:00" if terminal else None,
            "gpu_name": "Tesla T4" if terminal else None,
            "device": "cuda" if terminal else None,
            "segment_count": 1 if terminal else None,
            "cer": 0.01 if self.state.check_status == "passed" else None,
            "max_cer": 0.1,
            "total_ms": 1000 if terminal else None,
            "failure_class": "self_check" if self.state.check_status == "failed" else None,
            "failure_summary": (
                "Functional check failed"
                if self.state.check_status == "failed"
                else None
            ),
        }


def _dispatcher_fixture(tmp_path, state: FakeGpuState, **settings_values):
    paths = StudioPaths(tmp_path / "workspace")
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project("GPU dispatcher")
    content = b"immutable-audio-input"
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
    canonical = paths.canonical_audio(source["id"])
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(content)
    contexts = JobContextBuilder(catalog, paths, load_config())
    payload = {}
    preconditions, _, fingerprint = contexts.snapshot(
        project_id=project["id"],
        kind="transcribe",
        source_id=source["id"],
        payload=payload,
    )
    job = catalog.create_job(
        project["id"],
        "transcribe",
        source["id"],
        payload,
        preconditions=preconditions,
        input_fingerprint=fingerprint,
    )
    catalog.update_lifecycle_state(
        instance_id="i-testgpu",
        instance_state="running",
        desired_state="running",
    )
    catalog.record_worker_state(
        "gpu-worker-a",
        boot_id="host-boot-a",
        protocol_version="1.0",
        build_id=BUILD,
        supported_kinds=["transcribe"],
        status="ready",
        details={"mode": "push", "dispatch_protocol_version": "2.0"},
    )
    values = {
        "internal_url": "http://gpu.internal:8766",
        "required_build_id": BUILD,
        "instance_id": "i-testgpu",
        "dispatch_token": SECRET,
        "lease_heartbeat_seconds": 0.02,
    }
    values.update(settings_values)
    client = FakeGpuClient(state)
    dispatcher = GpuPushDispatcher(
        catalog,
        paths,
        contexts,
        GpuDispatcherSettings(**values),
        lifecycle_wake=lambda: None,
        client=client,
    )
    return catalog, job, dispatcher, client


def test_dispatch_recovers_ambiguous_create_and_upload_response(tmp_path) -> None:
    state = FakeGpuState(ambiguous_create=True, ambiguous_put=True)
    catalog, job, dispatcher, client = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        current = catalog.get_job(job["id"])
        dispatch = catalog.active_gpu_dispatch()
        assert current["status"] == "running"
        assert current["attempt"] == 1
        assert dispatch is not None and dispatch["state"] == "accepted"
        assert state.create_calls == 1
        assert state.get_calls >= 1
        assert state.start_calls == 1
        assert client.started_lease_token == derive_dispatch_lease_token(
            SECRET, job["id"], 1
        )
        assert bytes(next(iter(state.received.values()))) == b"immutable-audio-input"
    finally:
        dispatcher.stop()


def test_conflicting_remote_manifest_blocks_instead_of_creating_new_attempt(tmp_path) -> None:
    state = FakeGpuState(corrupt_receipt=True)
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        dispatch = catalog.get_gpu_dispatch_for_attempt(job["id"], 1)
        assert dispatch["state"] == "blocked"
        assert catalog.get_lifecycle_state()["blocked_reason"]
        assert catalog.get_job(job["id"])["attempt"] == 1
        assert state.create_calls == 1
    finally:
        dispatcher.stop()


def test_dispatcher_recovers_stale_compatibility_block_and_dispatches(tmp_path) -> None:
    state = FakeGpuState()
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    catalog.update_lifecycle_state(
        blocked_reason="GPU worker build is incompatible",
    )
    try:
        assert dispatcher.run_once() is True
        assert catalog.get_lifecycle_state()["blocked_reason"] is None
        assert catalog.get_job(job["id"])["status"] == "running"
        assert catalog.active_gpu_dispatch() is not None
        assert state.create_calls == 1
    finally:
        dispatcher.stop()


def test_dispatcher_rejects_gpu_without_transcribe_capability(tmp_path) -> None:
    state = FakeGpuState(job_kinds=["initialize"])
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        assert catalog.get_job(job["id"])["status"] == "queued"
        assert catalog.active_gpu_dispatch() is None
        assert catalog.get_lifecycle_state()["blocked_reason"]
        assert state.create_calls == 0
    finally:
        dispatcher.stop()


def test_dispatcher_restart_resumes_same_attempt_and_durable_manifest(tmp_path) -> None:
    state = FakeGpuState(fail_put_without_progress=True)
    catalog, job, first, _ = _dispatcher_fixture(tmp_path, state)
    assert first.run_once() is True
    initial = catalog.active_gpu_dispatch()
    assert initial is not None and initial["state"] == "uploading"
    assert catalog.get_job(job["id"])["attempt"] == 1
    catalog.update_gpu_dispatch(initial["id"], next_retry_at=None)
    first.stop()

    paths = first.paths
    restarted_client = FakeGpuClient(state)
    restarted = GpuPushDispatcher(
        catalog,
        paths,
        first.contexts,
        first.settings,
        lifecycle_wake=lambda: None,
        client=restarted_client,
    )
    try:
        assert restarted.run_once() is True
        recovered = catalog.active_gpu_dispatch()
        assert recovered is not None and recovered["id"] == initial["id"]
        assert recovered["state"] == "accepted"
        assert catalog.get_job(job["id"])["attempt"] == 1
        assert restarted_client.started_lease_token == derive_dispatch_lease_token(
            SECRET, job["id"], 1
        )
        assert state.create_calls == 2
    finally:
        restarted.stop()


def test_restart_after_gpu_acceptance_reconciles_without_new_attempt(tmp_path) -> None:
    state = FakeGpuState()
    catalog, job, first, _ = _dispatcher_fixture(tmp_path, state)
    assert first.run_once() is True
    create_calls = state.create_calls
    first.stop()

    restarted = GpuPushDispatcher(
        catalog,
        first.paths,
        first.contexts,
        first.settings,
        lifecycle_wake=lambda: None,
        client=FakeGpuClient(state),
    )
    try:
        assert restarted.run_once() is True
        assert catalog.get_job(job["id"])["attempt"] == 1
        assert state.create_calls == create_calls
        assert state.get_calls >= 1
        assert catalog.active_gpu_dispatch()["state"] == "running"
    finally:
        restarted.stop()


def test_superseded_accepted_attempt_is_cancelled_and_fenced(tmp_path) -> None:
    state = FakeGpuState()
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        catalog.supersede_job(job["id"], "Authoritative inputs changed")

        assert dispatcher.run_once() is True
        dispatch = catalog.get_gpu_dispatch_for_attempt(job["id"], 1)
        assert state.cancel_calls == 1
        assert dispatch["state"] == "fenced"
        assert catalog.get_job(job["id"])["status"] == "superseded"
    finally:
        dispatcher.stop()


def test_dispatch_lease_is_heartbeated_while_upload_is_blocked(tmp_path) -> None:
    put_started = threading.Event()
    release_put = threading.Event()
    state = FakeGpuState(put_started=put_started, release_put=release_put)
    catalog, _, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    heartbeat_seen = threading.Event()
    original_heartbeat = catalog.heartbeat_leased_job

    def observed_heartbeat(*args, **kwargs):
        heartbeat_seen.set()
        return original_heartbeat(*args, **kwargs)

    catalog.heartbeat_leased_job = observed_heartbeat  # type: ignore[method-assign]
    thread = threading.Thread(target=dispatcher.run_once)
    try:
        thread.start()
        assert put_started.wait(timeout=3)
        assert heartbeat_seen.wait(timeout=3)
        release_put.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert catalog.active_gpu_dispatch()["state"] == "accepted"
    finally:
        release_put.set()
        thread.join(timeout=5)
        dispatcher.stop()


def test_build_mismatch_and_remote_backpressure_do_not_claim(tmp_path) -> None:
    mismatch = FakeGpuState(live_build_id="wrong-build")
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path / "mismatch", mismatch)
    try:
        assert dispatcher.run_once() is True
        assert catalog.get_job(job["id"])["status"] == "queued"
        assert "incompatible" in catalog.get_lifecycle_state()["blocked_reason"].casefold()
    finally:
        dispatcher.stop()

    busy = FakeGpuState(accepting_dispatches=False)
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path / "busy", busy)
    try:
        assert dispatcher.run_once() is True
        assert catalog.get_job(job["id"])["status"] == "queued"
        assert catalog.active_gpu_dispatch() is None
    finally:
        dispatcher.stop()


def test_job_preflight_check_is_deduplicated_and_persisted_before_claim(tmp_path) -> None:
    state = FakeGpuState(functional_ready=False)
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        active = catalog.active_gpu_check()
        assert active is not None and active["gpu_check_id"] == "remote-check-a"
        assert state.trigger_check_calls == 1
        assert catalog.get_job(job["id"])["status"] == "queued"

        state.check_status = "passed"
        assert dispatcher.run_once() is True
        assert catalog.active_gpu_check() is None
        assert catalog.list_gpu_checks(limit=1)[0]["status"] == "passed"
        assert state.trigger_check_calls == 1

        state.functional_ready = True
        assert dispatcher.run_once() is True
        assert catalog.get_job(job["id"])["status"] == "running"
    finally:
        dispatcher.stop()


def test_failed_job_preflight_blocks_without_restarting_check_loop(tmp_path) -> None:
    state = FakeGpuState(functional_ready=False)
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        state.check_status = "failed"
        assert dispatcher.run_once() is True
        assert catalog.list_gpu_checks(limit=1)[0]["status"] == "failed"
        assert catalog.get_lifecycle_state()["blocked_reason"]

        assert dispatcher.run_once() is True
        assert state.trigger_check_calls == 1
        assert catalog.get_job(job["id"])["status"] == "queued"
    finally:
        dispatcher.stop()


def test_intake_transport_failure_uses_durable_bounded_backoff(tmp_path) -> None:
    state = FakeGpuState(ready_failures=1)
    catalog, job, dispatcher, _ = _dispatcher_fixture(tmp_path, state)
    try:
        assert dispatcher.run_once() is True
        details = catalog.get_gpu_runtime_state()["details"]
        assert details["intake_retry_count"] == 1
        assert details["next_intake_retry_at"]
        assert catalog.get_job(job["id"])["status"] == "queued"

        assert dispatcher.run_once() is True
        assert state.ready_calls == 1
    finally:
        dispatcher.stop()
