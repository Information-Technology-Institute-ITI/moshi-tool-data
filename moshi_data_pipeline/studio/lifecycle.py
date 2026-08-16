from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from moshi_data_pipeline.studio.catalog import (
    GPU_DISPATCH_PROTOCOL_VERSION,
    WORKER_PROTOCOL_VERSION,
    StudioCatalog,
)

INSTANCE_STATES = {"stopped", "pending", "running", "stopping", "unknown"}
GPU_DEMAND_FIELDS = (
    "runnable_jobs",
    "valid_leases",
    "active_dispatches",
    "active_checks",
    "pending_acknowledgements",
)
GPU_STOP_FENCE_FIELDS = (
    "valid_leases",
    "active_dispatches",
    "active_checks",
    "pending_acknowledgements",
)


class LifecycleProvider(Protocol):
    name: str

    def state(self) -> str: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class LocalLifecycleProvider:
    """Records desired actions and never starts a container or calls AWS."""

    name = "local"

    def __init__(self, initial_state: str = "stopped") -> None:
        if initial_state not in INSTANCE_STATES:
            raise ValueError(f"Invalid instance state: {initial_state}")
        self.current_state = initial_state
        self.actions: list[str] = []

    def state(self) -> str:
        return self.current_state

    def start(self) -> None:
        self.actions.append("start")

    def stop(self) -> None:
        self.actions.append("stop")


class Ec2LifecycleProvider:
    name = "aws-ec2"

    def __init__(
        self,
        instance_id: str,
        *,
        region_name: str | None = None,
        client: Any = None,
    ) -> None:
        if not instance_id.startswith("i-"):
            raise ValueError("A concrete EC2 instance ID is required")
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - AWS image installs boto3.
                raise RuntimeError("AWS lifecycle requires boto3") from exc
            client = boto3.client("ec2", region_name=region_name)
        self.client = client
        self.instance_id = instance_id

    def state(self) -> str:
        response = self.client.describe_instances(InstanceIds=[self.instance_id])
        reservations = response.get("Reservations", [])
        instances = [
            instance
            for reservation in reservations
            for instance in reservation.get("Instances", [])
        ]
        if len(instances) != 1 or instances[0].get("InstanceId") != self.instance_id:
            raise RuntimeError("EC2 describe did not return the configured GPU instance")
        state = str(instances[0].get("State", {}).get("Name", "unknown"))
        return state if state in INSTANCE_STATES else "unknown"

    def start(self) -> None:
        self.client.start_instances(InstanceIds=[self.instance_id])

    def stop(self) -> None:
        self.client.stop_instances(InstanceIds=[self.instance_id])


class LifecycleController:
    def __init__(
        self,
        catalog: StudioCatalog,
        provider: LifecycleProvider,
        *,
        generation: str,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = 30,
        worker_fresh_seconds: int = 60,
        idle_seconds: int = 15 * 60,
        startup_grace_seconds: int = 10 * 60,
        metrics_publisher: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.provider = provider
        self.generation = generation
        self._clock = clock or (lambda: datetime.now(UTC))
        self.interval_seconds = interval_seconds
        self.worker_fresh_seconds = worker_fresh_seconds
        self.idle_seconds = idle_seconds
        self.startup_grace_seconds = startup_grace_seconds
        self.metrics_publisher = metrics_publisher
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="moshi-lifecycle-controller",
            daemon=True,
        )

    def _now(self) -> datetime:
        value = self._clock()
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)

    @staticmethod
    def _has_demand(summary: dict[str, int]) -> bool:
        # pending_acknowledgements is a subset of active_dispatches. Boolean
        # gates deliberately avoid adding the counters and double-counting it.
        return any(int(summary.get(field, 0)) > 0 for field in GPU_DEMAND_FIELDS)

    @staticmethod
    def _has_stop_fence(summary: dict[str, int]) -> bool:
        return any(int(summary.get(field, 0)) > 0 for field in GPU_STOP_FENCE_FIELDS)

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        message = " ".join(str(exc).split())
        message = re.sub(r"(?i)\b(?:authorization|bearer|token)\b\s*[:=]?\s*\S+", "[credential]", message)
        message = re.sub(r"(?:[A-Za-z]:\\|/)[^\s]+", "[path]", message)
        return f"{type(exc).__name__}: {message[:500]}"

    @staticmethod
    def _instance_id(provider: LifecycleProvider, stored: dict[str, Any]) -> str | None:
        value = getattr(provider, "instance_id", None) or stored.get("instance_id")
        return str(value) if value else None

    def _push_ready(
        self,
        runtime: dict[str, Any],
        worker: dict[str, Any] | None,
        now: datetime,
    ) -> bool:
        expected_build = str(runtime.get("expected_build_id") or "").strip()
        if not expected_build:
            return True
        worker_details = (worker or {}).get("details") or {}
        intake_observed = self._parse(runtime.get("last_intake_observation_at"))
        intake_fresh = bool(
            intake_observed
            and now - intake_observed <= timedelta(seconds=self.worker_fresh_seconds)
        )
        return bool(
            intake_fresh
            and runtime.get("intake_reachable")
            and runtime.get("callback_ready")
            and runtime.get("functional_check_ready")
            and runtime.get("operational_ready")
            and runtime.get("dispatch_protocol") == GPU_DISPATCH_PROTOCOL_VERSION
            and runtime.get("actual_build_id") == expected_build
            and worker
            and worker.get("protocol_version") == WORKER_PROTOCOL_VERSION
            and worker.get("build_id") == expected_build
            and worker_details.get("mode") == "push"
            and worker_details.get("dispatch_protocol_version")
            == GPU_DISPATCH_PROTOCOL_VERSION
        )

    def _stop_after_recheck(
        self,
        now: datetime,
        updates: dict[str, Any],
        *,
        allow_runnable_jobs: bool,
    ) -> dict[str, Any]:
        """Mark draining, then make a fresh SQLite demand observation before stop."""
        now_text = now.isoformat()
        draining_updates = {
            **updates,
            "draining": True,
            "idle_stop_at": None,
            "startup_deadline": None,
        }
        self.catalog.update_lifecycle_state(**draining_updates)
        current = self.catalog.gpu_demand_summary()
        unsafe = self._has_stop_fence(current) if allow_runnable_jobs else self._has_demand(current)
        if unsafe:
            return self.catalog.update_lifecycle_state(
                desired_state="running",
                draining=False,
                idle_stop_at=None,
                startup_deadline=None,
            )
        try:
            self.provider.stop()
        except Exception as exc:  # noqa: BLE001 - persist bounded provider failure.
            return self.catalog.update_lifecycle_state(
                draining=False,
                last_aws_error_at=now_text,
                last_error=self._error_summary(exc),
            )
        return self.catalog.update_lifecycle_state(
            last_transition_at=now_text,
            draining=True,
            idle_stop_at=None,
            startup_deadline=None,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def retry_blocked(self) -> dict[str, Any]:
        state = self.catalog.get_lifecycle_state()
        self.catalog.update_lifecycle_state(
            blocked_reason=None,
            last_error=None,
            recovery_count=0,
            startup_deadline=None,
            idle_stop_at=None,
            draining=False,
            controller_generation=self.generation,
        )
        self.wake()
        return {**state, "retry_requested": True}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.tick()
            except Exception as exc:  # noqa: BLE001 - controller must remain alive.
                state = self.catalog.update_lifecycle_state(
                    last_error=self._error_summary(exc)
                )
            if self.metrics_publisher is not None:
                try:
                    self.metrics_publisher(state)
                except Exception as exc:  # noqa: BLE001 - metrics cannot stop control.
                    self.catalog.update_lifecycle_state(
                        last_error=f"metrics: {self._error_summary(exc)[:450]}"
                    )
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def tick(self) -> dict[str, Any]:
        now = self._now()
        now_text = now.isoformat()
        self.catalog.requeue_expired_jobs()
        stored = self.catalog.get_lifecycle_state()
        if stored["controller_generation"] != self.generation:
            stored = self.catalog.update_lifecycle_state(
                controller_generation=self.generation,
                blocked_reason=None,
                recovery_count=0,
                startup_deadline=None,
                idle_stop_at=None,
                draining=False,
                last_error=None,
            )
        demand = self.catalog.gpu_demand_summary()
        needs_compute = self._has_demand(demand)
        instance_id = self._instance_id(self.provider, stored)
        try:
            instance_state = self.provider.state()
        except Exception as exc:  # noqa: BLE001 - persist bounded provider failure.
            return self.catalog.update_lifecycle_state(
                provider=self.provider.name,
                instance_id=instance_id,
                desired_state="running" if needs_compute else "stopped",
                last_aws_error_at=now_text,
                last_error=self._error_summary(exc),
            )

        runtime = self.catalog.get_gpu_runtime_state()
        worker = self.catalog.latest_worker_state()
        worker_heartbeat = self._parse(worker["last_heartbeat"]) if worker else None
        worker_fresh = bool(
            worker_heartbeat
            and now - worker_heartbeat <= timedelta(seconds=self.worker_fresh_seconds)
        )
        worker_compatible = bool(
            worker
            and worker["protocol_version"] == WORKER_PROTOCOL_VERSION
            and worker["status"] != "incompatible"
        )
        expected_build = str(runtime.get("expected_build_id") or "").strip()
        push_configured = bool(expected_build)
        updates: dict[str, Any] = {
            "provider": self.provider.name,
            "instance_id": instance_id,
            "instance_state": instance_state,
            "desired_state": "running" if needs_compute else "stopped",
            "last_aws_observation_at": now_text,
            "last_error": None,
        }
        if instance_state == "stopped":
            updates["draining"] = False

        if stored["blocked_reason"] and needs_compute:
            updates.update(idle_stop_at=None, startup_deadline=None, draining=False)
            if instance_state == "running" and not self._has_stop_fence(demand):
                return self._stop_after_recheck(
                    now,
                    updates,
                    allow_runnable_jobs=True,
                )
            return self.catalog.update_lifecycle_state(**updates)

        if needs_compute:
            updates.update(idle_stop_at=None, draining=False)
            if instance_state == "stopped":
                try:
                    self.provider.start()
                except Exception as exc:  # noqa: BLE001 - persist provider failure.
                    updates.update(
                        last_aws_error_at=now_text,
                        last_error=self._error_summary(exc),
                    )
                    return self.catalog.update_lifecycle_state(**updates)
                updates.update(
                    last_transition_at=now_text,
                    startup_deadline=(
                        now + timedelta(seconds=self.startup_grace_seconds)
                    ).isoformat(),
                )
                return self.catalog.update_lifecycle_state(**updates)
            if instance_state in {"pending", "stopping"}:
                return self.catalog.update_lifecycle_state(**updates)
            if instance_state == "unknown":
                return self.catalog.update_lifecycle_state(**updates)
            deadline = (
                None
                if stored["desired_state"] != "running"
                else self._parse(stored["startup_deadline"])
            )
            worker_ready = bool(
                worker_fresh
                and worker_compatible
                and worker
                and worker["status"] in {"ready", "busy", "idle", "draining"}
            )
            ready = worker_ready and self._push_ready(runtime, worker, now)
            if ready:
                updates.update(startup_deadline=None, recovery_count=0)
                return self.catalog.update_lifecycle_state(**updates)
            if deadline is None:
                updates["startup_deadline"] = (
                    now + timedelta(seconds=self.startup_grace_seconds)
                ).isoformat()
                return self.catalog.update_lifecycle_state(**updates)
            if now < deadline:
                return self.catalog.update_lifecycle_state(**updates)
            if int(stored["recovery_count"]) < 1 and not self._has_stop_fence(demand):
                updates.update(
                    recovery_count=1,
                    startup_deadline=None,
                )
                return self._stop_after_recheck(
                    now,
                    updates,
                    allow_runnable_jobs=True,
                )
            if worker_fresh and not worker_compatible:
                reason = "Worker protocol is incompatible"
            elif push_configured and (
                runtime.get("dispatch_protocol")
                not in {
                    None,
                    GPU_DISPATCH_PROTOCOL_VERSION,
                }
                or runtime.get("worker_protocol")
                not in {
                    None,
                    WORKER_PROTOCOL_VERSION,
                }
                or (
                    runtime.get("actual_build_id")
                    and runtime.get("actual_build_id") != expected_build
                )
                or (worker and worker.get("build_id") != expected_build)
            ):
                reason = "GPU protocol or build is incompatible"
            else:
                reason = (
                    "GPU application did not become operational before startup deadline"
                    if push_configured
                    else "Worker did not become ready before startup deadline"
                )
            updates.update(
                blocked_reason=reason,
                startup_deadline=None,
                idle_stop_at=None,
            )
            if instance_state == "running" and not self._has_stop_fence(demand):
                return self._stop_after_recheck(
                    now,
                    updates,
                    allow_runnable_jobs=True,
                )
            return self.catalog.update_lifecycle_state(**updates)

        if instance_state == "stopped":
            updates.update(
                draining=False,
                idle_stop_at=None,
                startup_deadline=None,
            )
            return self.catalog.update_lifecycle_state(**updates)
        if instance_state == "stopping":
            updates.update(
                draining=bool(stored.get("draining")),
                idle_stop_at=None,
                startup_deadline=None,
            )
            return self.catalog.update_lifecycle_state(**updates)
        if instance_state != "running":
            return self.catalog.update_lifecycle_state(**updates)
        if stored.get("draining"):
            updates.update(draining=True, idle_stop_at=None, startup_deadline=None)
            return self._stop_after_recheck(
                now,
                updates,
                allow_runnable_jobs=False,
            )

        idle_since = self._parse(worker["idle_since"]) if worker else None
        confirmed_idle = bool(
            worker_fresh
            and worker_compatible
            and worker["status"] == "idle"
            and idle_since
            and now - idle_since >= timedelta(seconds=self.idle_seconds)
        )
        if confirmed_idle:
            return self._stop_after_recheck(
                now,
                updates,
                allow_runnable_jobs=False,
            )

        idle_stop = self._parse(stored.get("idle_stop_at"))
        if idle_stop is None:
            idle_stop = now + timedelta(seconds=self.idle_seconds)
            # startup_deadline remains populated for compatibility with legacy
            # status consumers; idle_stop_at is the authoritative idle timer.
            updates.update(
                idle_stop_at=idle_stop.isoformat(),
                startup_deadline=idle_stop.isoformat(),
                draining=False,
            )
        elif now >= idle_stop:
            return self._stop_after_recheck(
                now,
                updates,
                allow_runnable_jobs=False,
            )
        else:
            updates.update(
                idle_stop_at=idle_stop.isoformat(),
                startup_deadline=idle_stop.isoformat(),
                draining=False,
            )
        return self.catalog.update_lifecycle_state(**updates)
