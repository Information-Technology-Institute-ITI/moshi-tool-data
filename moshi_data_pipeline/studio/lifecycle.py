from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from moshi_data_pipeline.studio.catalog import WORKER_PROTOCOL_VERSION, StudioCatalog

INSTANCE_STATES = {"stopped", "pending", "running", "stopping", "unknown"}


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
        return datetime.fromisoformat(value) if value else None

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
                    last_error=f"{type(exc).__name__}: {str(exc)[:500]}"
                )
            if self.metrics_publisher is not None:
                try:
                    self.metrics_publisher(state)
                except Exception as exc:  # noqa: BLE001 - metrics cannot stop control.
                    self.catalog.update_lifecycle_state(
                        last_error=f"metrics: {type(exc).__name__}: {str(exc)[:450]}"
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
                last_error=None,
            )
        instance_state = self.provider.state()
        queue = self.catalog.queue_summary()
        valid_leases = self.catalog.valid_running_lease_count()
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
        needs_compute = queue["queued"] > 0 or valid_leases > 0
        updates: dict[str, Any] = {
            "provider": self.provider.name,
            "instance_state": instance_state,
            "desired_state": "running" if needs_compute else "stopped",
            "last_error": None,
        }

        if stored["blocked_reason"] and needs_compute:
            if instance_state == "running" and valid_leases == 0:
                self.provider.stop()
                updates["last_transition_at"] = now_text
            return self.catalog.update_lifecycle_state(**updates)

        if needs_compute:
            if instance_state == "stopped":
                self.provider.start()
                updates.update(
                    last_transition_at=now_text,
                    startup_deadline=(
                        now + timedelta(seconds=self.startup_grace_seconds)
                    ).isoformat(),
                )
                return self.catalog.update_lifecycle_state(**updates)
            if instance_state in {"pending", "stopping"}:
                return self.catalog.update_lifecycle_state(**updates)
            deadline = (
                None
                if stored["desired_state"] != "running"
                else self._parse(stored["startup_deadline"])
            )
            ready = worker_fresh and worker_compatible and worker["status"] in {
                "ready",
                "busy",
                "idle",
                "draining",
            }
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
            if int(stored["recovery_count"]) < 1 and valid_leases == 0:
                self.provider.stop()
                updates.update(
                    last_transition_at=now_text,
                    recovery_count=1,
                    startup_deadline=None,
                )
                return self.catalog.update_lifecycle_state(**updates)
            reason = (
                "Worker protocol is incompatible"
                if worker_fresh and not worker_compatible
                else "Worker did not become ready before startup deadline"
            )
            if valid_leases == 0 and instance_state == "running":
                self.provider.stop()
                updates["last_transition_at"] = now_text
            updates.update(blocked_reason=reason, startup_deadline=None)
            return self.catalog.update_lifecycle_state(**updates)

        # Never stop a valid lease, even if another state signal is stale.
        if valid_leases:
            updates["desired_state"] = "running"
            return self.catalog.update_lifecycle_state(**updates)
        idle_since = self._parse(worker["idle_since"]) if worker else None
        confirmed_idle = bool(
            worker_fresh
            and worker_compatible
            and worker["status"] == "idle"
            and idle_since
            and now - idle_since >= timedelta(seconds=self.idle_seconds)
        )
        if instance_state == "running" and confirmed_idle:
            self.provider.stop()
            updates.update(last_transition_at=now_text, startup_deadline=None)
        elif instance_state == "running":
            cost_deadline = self._parse(stored["startup_deadline"])
            if cost_deadline is None:
                updates["startup_deadline"] = (
                    now + timedelta(seconds=self.idle_seconds)
                ).isoformat()
            elif now >= cost_deadline:
                self.provider.stop()
                updates.update(last_transition_at=now_text, startup_deadline=None)
        else:
            updates["startup_deadline"] = None
        return self.catalog.update_lifecycle_state(**updates)
