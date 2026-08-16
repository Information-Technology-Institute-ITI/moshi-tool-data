from __future__ import annotations

from datetime import UTC, datetime, timedelta

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.lifecycle import LifecycleController, LocalLifecycleProvider


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: int) -> None:
        self.value += timedelta(**values)


def _controller(tmp_path, *, provider_state: str = "stopped"):
    clock = Clock()
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    project = catalog.create_project("Lifecycle")
    provider = LocalLifecycleProvider(provider_state)
    controller = LifecycleController(
        catalog,
        provider,
        generation="build-a",
        clock=clock,
        worker_fresh_seconds=60,
        idle_seconds=900,
        startup_grace_seconds=600,
    )
    return clock, catalog, project, provider, controller


def test_queued_work_starts_gpu_and_confirmed_idle_stops_it(tmp_path) -> None:
    clock, catalog, project, provider, controller = _controller(tmp_path)
    job = catalog.create_job(project["id"], "transcribe", None)
    state = controller.tick()
    assert provider.actions == ["start"]
    assert state["desired_state"] == "running"

    provider.current_state = "running"
    lease = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
    )
    assert lease is not None
    catalog.record_worker_state(
        "worker-1",
        boot_id="boot-1",
        protocol_version="1.0",
        build_id="build-a",
        supported_kinds=["transcribe"],
        status="busy",
        current_job_id=job["id"],
    )
    controller.tick()
    assert provider.actions == ["start"]
    catalog.complete_leased_job(
        job["id"], "worker-1", lease["lease_token"], {"ok": True}
    )
    # Keep the heartbeat fresh while advancing the worker's preserved idle_since.
    clock.advance(seconds=901)
    catalog.record_worker_state(
        "worker-1",
        boot_id="boot-1",
        protocol_version="1.0",
        build_id="build-a",
        supported_kinds=["transcribe"],
        status="idle",
    )
    controller.tick()
    assert provider.actions == ["start", "stop"]


def test_controller_never_stops_a_valid_lease(tmp_path) -> None:
    _, catalog, project, provider, controller = _controller(
        tmp_path, provider_state="running"
    )
    catalog.create_job(project["id"], "transcribe", None)
    lease = catalog.claim_leased_job(
        "worker-1",
        protocol_version="1.0",
        worker_build_id="build-a",
        supported_kinds=["transcribe"],
    )
    assert lease is not None
    controller.tick()
    assert "stop" not in provider.actions
    assert catalog.get_lifecycle_state()["desired_state"] == "running"


def test_running_gpu_without_work_is_stopped_by_cost_deadline(tmp_path) -> None:
    clock, _, _, provider, controller = _controller(
        tmp_path, provider_state="running"
    )
    first = controller.tick()
    assert provider.actions == []
    assert first["startup_deadline"] is not None
    clock.advance(seconds=901)
    stopped = controller.tick()
    assert provider.actions == ["stop"]
    assert stopped["desired_state"] == "stopped"


def test_failed_startup_recovers_once_then_blocks_restart_loop(tmp_path) -> None:
    clock, catalog, project, provider, controller = _controller(tmp_path)
    catalog.create_job(project["id"], "transcribe", None)
    controller.tick()
    assert provider.actions == ["start"]

    provider.current_state = "running"
    clock.advance(seconds=601)
    first_failure = controller.tick()
    assert provider.actions == ["start", "stop"]
    assert first_failure["recovery_count"] == 1
    assert first_failure["blocked_reason"] is None

    provider.current_state = "stopped"
    controller.tick()
    assert provider.actions == ["start", "stop", "start"]
    provider.current_state = "running"
    clock.advance(seconds=601)
    blocked = controller.tick()
    assert provider.actions == ["start", "stop", "start", "stop"]
    assert blocked["blocked_reason"]

    provider.current_state = "stopped"
    controller.tick()
    assert provider.actions == ["start", "stop", "start", "stop"]
    controller.retry_blocked()
    controller.tick()
    assert provider.actions[-1] == "start"


def test_incompatible_worker_is_visible_and_eventually_blocked(tmp_path) -> None:
    clock, catalog, project, provider, controller = _controller(
        tmp_path, provider_state="running"
    )
    catalog.create_job(project["id"], "transcribe", None)
    catalog.record_worker_state(
        "worker-old",
        boot_id="boot-old",
        protocol_version="0.9",
        build_id="old",
        supported_kinds=["transcribe"],
        status="ready",
    )
    controller.tick()
    clock.advance(seconds=601)
    controller.tick()
    provider.current_state = "stopped"
    controller.tick()
    provider.current_state = "running"
    catalog.record_worker_state(
        "worker-old",
        boot_id="boot-old",
        protocol_version="0.9",
        build_id="old",
        supported_kinds=["transcribe"],
        status="ready",
    )
    clock.advance(seconds=601)
    catalog.record_worker_state(
        "worker-old",
        boot_id="boot-old",
        protocol_version="0.9",
        build_id="old",
        supported_kinds=["transcribe"],
        status="ready",
    )
    state = controller.tick()
    assert state["blocked_reason"] == "Worker protocol is incompatible"
