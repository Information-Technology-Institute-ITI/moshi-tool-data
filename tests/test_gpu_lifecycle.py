from __future__ import annotations

from datetime import UTC, datetime, timedelta

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.lifecycle import LifecycleController, LocalLifecycleProvider


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: int) -> None:
        self.value += timedelta(**values)


class Ec2LikeProvider(LocalLifecycleProvider):
    name = "aws-ec2"
    instance_id = "i-0123456789abcdef0"

    def __init__(self, initial_state: str) -> None:
        super().__init__(initial_state)
        self.observation_error: Exception | None = None
        self.stop_error: Exception | None = None

    def state(self) -> str:
        if self.observation_error is not None:
            raise self.observation_error
        return super().state()

    def stop(self) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        super().stop()


def _controller(tmp_path, *, state: str = "running", idle_seconds: int = 900):
    clock = Clock()
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3", clock=clock)
    project = catalog.create_project(
        "GPU lifecycle", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    provider = Ec2LikeProvider(state)
    controller = LifecycleController(
        catalog,
        provider,
        generation="web-build-a",
        clock=clock,
        worker_fresh_seconds=60,
        idle_seconds=idle_seconds,
        startup_grace_seconds=60,
    )
    return clock, catalog, project, provider, controller


def _record_push_worker(
    catalog: StudioCatalog,
    *,
    build_id: str = "gpu-build-a",
) -> None:
    catalog.record_worker_state(
        "gpu-worker-1",
        boot_id="worker-boot-1",
        protocol_version="1.0",
        build_id=build_id,
        supported_kinds=["transcribe"],
        status="idle",
        details={"mode": "push", "dispatch_protocol_version": "2.0"},
    )


def _record_push_runtime(
    catalog: StudioCatalog,
    clock: Clock,
    *,
    expected_build: str = "gpu-build-a",
    actual_build: str = "gpu-build-a",
    callback_ready: bool = True,
    functional_ready: bool = True,
    operational_ready: bool = True,
) -> None:
    catalog.update_gpu_runtime_state(
        last_intake_observation_at=clock.value.isoformat(),
        intake_reachable=True,
        dispatch_protocol="2.0",
        worker_protocol="1.0",
        expected_build_id=expected_build,
        actual_build_id=actual_build,
        callback_ready=callback_ready,
        functional_check_ready=functional_ready,
        operational_ready=operational_ready,
    )


def _empty_demand() -> dict[str, int]:
    return {
        "runnable_jobs": 0,
        "valid_leases": 0,
        "active_dispatches": 0,
        "active_checks": 0,
        "pending_acknowledgements": 0,
    }


def test_manual_functional_check_is_compute_demand_and_starts_stopped_gpu(
    tmp_path,
) -> None:
    _, catalog, _, provider, controller = _controller(tmp_path, state="stopped")
    check, created = catalog.request_gpu_check(
        "manual",
        requested_by="alice",
        instance_id=provider.instance_id,
        cold_start=True,
        expected_build_id="gpu-build-a",
    )
    assert created is True
    assert check["status"] == "requested"

    state = controller.tick()

    assert provider.actions == ["start"]
    assert state["desired_state"] == "running"
    assert state["instance_id"] == provider.instance_id
    assert state["last_aws_observation_at"] is not None
    assert state["idle_stop_at"] is None


def test_active_check_fences_startup_recovery_stop(tmp_path) -> None:
    clock, catalog, _, provider, controller = _controller(tmp_path)
    catalog.request_gpu_check(
        "manual",
        requested_by="alice",
        instance_id=provider.instance_id,
        expected_build_id="gpu-build-a",
    )

    controller.tick()
    clock.advance(seconds=61)
    state = controller.tick()

    assert "stop" not in provider.actions
    assert state["desired_state"] == "running"
    assert state["blocked_reason"]
    assert not state["draining"]


def test_push_readiness_requires_current_callback_functional_and_exact_build(
    tmp_path,
) -> None:
    clock, catalog, project, provider, controller = _controller(tmp_path)
    catalog.create_job(project["id"], "transcribe", None)
    _record_push_worker(catalog)
    _record_push_runtime(
        catalog,
        clock,
        actual_build="wrong-build",
        callback_ready=False,
    )

    waiting = controller.tick()
    assert waiting["startup_deadline"] is not None
    assert provider.actions == []

    _record_push_runtime(catalog, clock)
    ready = controller.tick()

    assert ready["startup_deadline"] is None
    assert ready["recovery_count"] == 0
    assert ready["blocked_reason"] is None


def test_push_readiness_rejects_a_legacy_pull_worker(tmp_path) -> None:
    clock, catalog, project, _, controller = _controller(tmp_path)
    catalog.create_job(project["id"], "transcribe", None)
    catalog.record_worker_state(
        "legacy-worker",
        boot_id="worker-boot-1",
        protocol_version="1.0",
        build_id="gpu-build-a",
        supported_kinds=["transcribe"],
        status="idle",
        details={"mode": "pull"},
    )
    _record_push_runtime(catalog, clock)

    waiting = controller.tick()

    assert waiting["startup_deadline"] is not None


def test_aws_observation_and_error_timestamps_are_persisted(tmp_path) -> None:
    clock, catalog, _, provider, controller = _controller(tmp_path)
    provider.observation_error = RuntimeError("describe unavailable")

    failed = controller.tick()
    assert failed["instance_id"] == provider.instance_id
    assert failed["last_aws_observation_at"] is None
    assert failed["last_aws_error_at"] == clock.value.isoformat()
    assert failed["last_error"] == "RuntimeError: describe unavailable"

    clock.advance(seconds=5)
    provider.observation_error = None
    observed = controller.tick()
    assert observed["last_aws_observation_at"] == clock.value.isoformat()
    assert observed["last_aws_error_at"] == failed["last_aws_error_at"]
    assert observed["last_error"] is None
    assert observed["idle_stop_at"] is not None


def test_idle_stop_rechecks_and_reissues_stop_while_aws_still_reports_running(
    tmp_path,
) -> None:
    clock, catalog, _, provider, controller = _controller(tmp_path, idle_seconds=30)
    deadline = (clock.value - timedelta(seconds=1)).isoformat()
    catalog.update_lifecycle_state(
        desired_state="stopped",
        idle_stop_at=deadline,
        startup_deadline=deadline,
        controller_generation="web-build-a",
    )

    stopped = controller.tick()
    assert provider.actions == ["stop"]
    assert stopped["draining"]
    assert stopped["idle_stop_at"] is None
    assert stopped["last_transition_at"] == clock.value.isoformat()

    controller.tick()
    assert provider.actions == ["stop", "stop"]


def test_second_demand_check_closes_stop_race_and_pending_ack_is_not_added(
    tmp_path,
) -> None:
    clock, catalog, _, provider, controller = _controller(tmp_path, idle_seconds=30)
    deadline = (clock.value - timedelta(seconds=1)).isoformat()
    catalog.update_lifecycle_state(
        desired_state="stopped",
        idle_stop_at=deadline,
        startup_deadline=deadline,
        controller_generation="web-build-a",
    )
    observations = [
        _empty_demand(),
        {
            **_empty_demand(),
            "active_dispatches": 1,
            # This is the same completion-pending dispatch, not another demand.
            "pending_acknowledgements": 1,
        },
    ]

    def demand_summary() -> dict[str, int]:
        return observations.pop(0)

    catalog.gpu_demand_summary = demand_summary  # type: ignore[method-assign]
    state = controller.tick()

    assert observations == []
    assert provider.actions == []
    assert state["desired_state"] == "running"
    assert not state["draining"]
    assert state["idle_stop_at"] is None


def test_stop_failure_records_aws_error_and_clears_draining(tmp_path) -> None:
    clock, catalog, _, provider, controller = _controller(tmp_path, idle_seconds=30)
    deadline = (clock.value - timedelta(seconds=1)).isoformat()
    catalog.update_lifecycle_state(
        idle_stop_at=deadline,
        startup_deadline=deadline,
        controller_generation="web-build-a",
    )
    provider.stop_error = RuntimeError("stop denied")

    state = controller.tick()

    assert provider.actions == []
    assert not state["draining"]
    assert state["last_aws_error_at"] == clock.value.isoformat()
    assert state["last_error"] == "RuntimeError: stop denied"
