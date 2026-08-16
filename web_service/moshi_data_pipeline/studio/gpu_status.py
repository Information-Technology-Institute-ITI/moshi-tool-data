from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from moshi_data_pipeline.gpu_dispatch_protocol import GPU_DISPATCH_PROTOCOL_VERSION
from moshi_data_pipeline.studio.catalog import WORKER_PROTOCOL_VERSION, StudioCatalog

ACTIVE_CHECK_STATES = {"requested", "starting", "waiting", "queued", "running"}
ACTIVE_DISPATCH_STATES = {
    "claimed",
    "prepared",
    "creating",
    "uploading",
    "starting",
    "accepted",
    "running",
    "completion_pending",
    "cancel_requested",
}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _age(value: str | None, now: datetime) -> int | None:
    parsed = _parse_timestamp(value)
    return max(0, round((now - parsed).total_seconds())) if parsed else None


def _latest_push_worker(catalog: StudioCatalog) -> dict[str, Any] | None:
    with catalog.connect() as connection:
        rows = connection.execute(
            "SELECT worker_id FROM worker_state ORDER BY last_heartbeat DESC"
        ).fetchall()
    for row in rows:
        worker = catalog.get_worker_state(str(row["worker_id"]))
        details = worker.get("details") or {}
        if (
            details.get("mode") == "push"
            and details.get("dispatch_protocol_version")
            == GPU_DISPATCH_PROTOCOL_VERSION
        ):
            return worker
    return None


def public_gpu_check(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the bounded shared check record; never forward arbitrary GPU data."""
    if value is None:
        return None
    allowed = (
        "id",
        "gpu_check_id",
        "instance_id",
        "trigger",
        "requested_by",
        "status",
        "requirement_key",
        "host_boot_id",
        "service_boot_id",
        "dispatch_protocol",
        "worker_protocol",
        "actual_build_id",
        "expected_build_id",
        "model_revision",
        "config_fingerprint",
        "fixture_id",
        "fixture_hash_prefix",
        "requested_at",
        "started_at",
        "finished_at",
        "valid_until",
        "updated_at",
        "gpu_name",
        "device",
        "segment_count",
        "cer",
        "cer_threshold",
        "model_load_ms",
        "inference_ms",
        "total_ms",
        "failure_class",
        "failure_summary",
    )
    return {key: value.get(key) for key in allowed}


def derive_gpu_state(
    *,
    lifecycle: dict[str, Any],
    runtime: dict[str, Any],
    worker: dict[str, Any] | None,
    check: dict[str, Any] | None,
    dispatch: dict[str, Any] | None,
    now: datetime,
    expected_build_id: str,
) -> str:
    instance_state = str(lifecycle.get("instance_state") or "unknown")
    if instance_state == "stopped":
        return "OFF"
    if instance_state == "stopping":
        return "STOPPING"
    if instance_state == "pending":
        return "STARTING"
    if instance_state != "running":
        return "UNKNOWN"
    if lifecycle.get("blocked_reason"):
        return "BLOCKED"
    details = runtime.get("details") or {}
    if details.get("auth_blocked") or details.get("error_class") == "authentication_failed":
        return "ERROR"
    protocol = runtime.get("dispatch_protocol")
    actual_build = runtime.get("actual_build_id")
    worker_age = _age(worker.get("last_heartbeat") if worker else None, now)
    intake_age = _age(runtime.get("last_intake_observation_at"), now)
    if (
        (
            intake_age is not None
            and intake_age <= 120
            and (
                (protocol and protocol != GPU_DISPATCH_PROTOCOL_VERSION)
                or (expected_build_id and actual_build and actual_build != expected_build_id)
            )
        )
        or (
            worker
            and expected_build_id
            and worker_age is not None
            and worker_age <= 120
            and (
                worker.get("protocol_version") != WORKER_PROTOCOL_VERSION
                or worker.get("build_id") != expected_build_id
            )
        )
    ):
        return "INCOMPATIBLE"
    if check and check.get("status") in ACTIVE_CHECK_STATES:
        return "CHECKING"
    if dispatch and dispatch.get("state") in ACTIVE_DISPATCH_STATES:
        return "BUSY"
    if (
        runtime.get("operational_ready")
        and worker_age is not None
        and worker_age <= 60
        and intake_age is not None
        and intake_age <= 60
    ):
        return "READY"
    if runtime.get("intake_reachable") and intake_age is not None and intake_age <= 120:
        return "DEGRADED"
    if worker_age is not None and worker_age <= 60:
        return "DEGRADED"
    return "UNKNOWN"


def gpu_status_payload(
    catalog: StudioCatalog,
    *,
    instance_id: str | None,
    expected_build_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    lifecycle = catalog.get_lifecycle_state()
    runtime = catalog.get_gpu_runtime_state()
    worker = _latest_push_worker(catalog)
    checks = catalog.list_gpu_checks(limit=1)
    latest_check = checks[0] if checks else None
    dispatch = catalog.active_gpu_dispatch()
    queue = catalog.queue_summary()
    state = derive_gpu_state(
        lifecycle=lifecycle,
        runtime=runtime,
        worker=worker,
        check=latest_check,
        dispatch=dispatch,
        now=current,
        expected_build_id=expected_build_id,
    )

    intake_age = _age(runtime.get("last_intake_observation_at"), current)
    worker_age = _age(worker.get("last_heartbeat") if worker else None, current)
    worker_fresh = worker_age is not None and worker_age <= 60
    intake_fresh = intake_age is not None and intake_age <= 120
    if state == "OFF":
        service_state = "offline"
    elif state == "STARTING":
        service_state = "starting"
    elif lifecycle.get("draining") or runtime.get("draining"):
        service_state = "draining"
    elif state == "INCOMPATIBLE":
        service_state = "incompatible"
    elif state in {"BLOCKED", "ERROR"}:
        service_state = "error"
    elif state == "BUSY":
        service_state = "busy"
    elif not intake_fresh and not worker_fresh:
        service_state = "stale"
    else:
        service_state = "online"

    details = runtime.get("details") or {}
    worker_details = (worker or {}).get("details") or {}
    dispatch_protocol = (
        runtime.get("dispatch_protocol")
        or worker_details.get("dispatch_protocol_version")
    )
    actual_build_id = runtime.get("actual_build_id") or (
        worker.get("build_id") if worker else None
    )
    current_check = public_gpu_check(latest_check)
    if current_check and current_check.get("status") == "passed":
        valid_until = _parse_timestamp(current_check.get("valid_until"))
        current_check_is_valid = bool(
            lifecycle.get("instance_state") == "running"
            and runtime.get("functional_check_ready")
            and valid_until
            and valid_until > current
            and (
                not runtime.get("host_boot_id")
                or current_check.get("host_boot_id") == runtime.get("host_boot_id")
            )
            and (
                not expected_build_id
                or current_check.get("actual_build_id") == expected_build_id
            )
            and (
                not details.get("functional_requirement_key")
                or current_check.get("requirement_key")
                == details.get("functional_requirement_key")
            )
        )
        if not current_check_is_valid:
            current_check = {**current_check, "status": "stale"}
    return {
        "state": state,
        "machine": {
            "instance_id": instance_id or lifecycle.get("instance_id"),
            "instance_state": lifecycle.get("instance_state", "unknown"),
            "desired_state": lifecycle.get("desired_state", "stopped"),
            "last_aws_observation": lifecycle.get("last_aws_observation_at"),
            "observation_age_seconds": _age(
                lifecycle.get("last_aws_observation_at"), current
            ),
            "last_transition_at": lifecycle.get("last_transition_at"),
            "last_error": lifecycle.get("last_error"),
            "idle_stop_at": lifecycle.get("idle_stop_at"),
        },
        "service": {
            "state": service_state,
            "last_intake_observation": runtime.get("last_intake_observation_at"),
            "observation_age_seconds": intake_age,
            "last_worker_heartbeat": worker.get("last_heartbeat") if worker else None,
            "worker_age_seconds": worker_age,
            "current_job_id": worker.get("current_job_id") if worker else None,
            "gpu_name": latest_check.get("gpu_name") if latest_check else None,
            "dispatch_protocol_version": dispatch_protocol,
            "expected_dispatch_protocol_version": GPU_DISPATCH_PROTOCOL_VERSION,
            "worker_protocol_version": worker.get("protocol_version") if worker else None,
            "expected_worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "build_id": actual_build_id,
            "expected_build_id": expected_build_id,
            "queue_count": int(queue.get("queued", 0)),
            "running_count": int(queue.get("running", 0)),
            "accepting_dispatches": bool(runtime.get("accepting_dispatches")),
            "callback_ready": bool(runtime.get("callback_ready") or worker_fresh),
            "operational_ready": bool(runtime.get("operational_ready")),
        },
        "functional_check": current_check,
        "dispatcher": {
            "state": dispatch.get("state") if dispatch else "idle",
            "active_dispatch_id": dispatch.get("id") if dispatch else None,
            "last_error": dispatch.get("last_error_summary") if dispatch else None,
        },
    }
