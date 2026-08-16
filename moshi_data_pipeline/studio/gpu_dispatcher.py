from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from moshi_data_pipeline.gpu_dispatch_client import (
    GpuDispatchAuthenticationError,
    GpuDispatchBlockedError,
    GpuDispatchClient,
    GpuDispatchConflictError,
    GpuDispatchError,
    GpuDispatchNotFoundError,
    GpuDispatchRateLimitError,
    GpuDispatchRetryableError,
)
from moshi_data_pipeline.gpu_dispatch_protocol import (
    GPU_DISPATCH_PROTOCOL_VERSION,
    DispatchCreate,
    DispatchInput,
    DispatchStart,
    SelfCheckRequest,
)
from moshi_data_pipeline.gpu_job_protocol import JOB_KINDS as GPU_JOB_KINDS
from moshi_data_pipeline.studio.catalog import (
    WORKER_PROTOCOL_VERSION,
    LeaseConflictError,
    StudioCatalog,
)
from moshi_data_pipeline.studio.job_contexts import JobContextBuilder, file_sha256
from moshi_data_pipeline.studio.media import StudioPaths

PRESTART_STATES = {"claimed", "prepared", "creating", "uploading", "starting"}
REMOTE_ACCEPTED_STATES = {
    "queued",
    "running",
    "outbox_pending",
    "callback_uploading",
    "auth_blocked",
    "acknowledged",
    "orphaned",
    "rejected",
    "failed",
}
REMOTE_TERMINAL_STATES = {"acknowledged", "orphaned", "rejected", "failed", "cancelled"}
LOCAL_TERMINAL_JOB_STATES = {"complete", "failed", "superseded"}


class GpuReadinessGateError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_summary(value: str, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?:[A-Za-z]:\\|/)[^\s]+", "[path]", text)
    return text[:limit] or "GPU operation failed"


def derive_dispatch_lease_token(secret: str, job_id: str, attempt: int) -> str:
    """Derive a restart-recoverable lease while persisting only its SHA-256."""
    if not secret:
        raise ValueError("Dispatcher lease key is required")
    message = f"moshi-dispatch-lease-v1\0{job_id}\0{attempt}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _receipt_matches(remote: dict[str, Any], manifest: DispatchCreate) -> bool:
    identity = {
        "dispatch_id": manifest.dispatch_id,
        "job_id": manifest.job_id,
        "attempt": manifest.attempt,
        "protocol_version": manifest.protocol_version,
        "required_build_id": manifest.required_build_id,
        "input_fingerprint": manifest.input_fingerprint,
    }
    if any(remote.get(key) != value for key, value in identity.items()):
        return False
    fields = ("artifact_id", "role", "sha256", "size_bytes", "media_type", "filename")
    remote_inputs = remote.get("inputs")
    if not isinstance(remote_inputs, list):
        return False
    expected = sorted(
        ({field: getattr(item, field) for field in fields} for item in manifest.inputs),
        key=lambda item: item["artifact_id"],
    )
    actual = sorted(
        (
            {field: item.get(field) for field in fields}
            for item in remote_inputs
            if isinstance(item, dict)
        ),
        key=lambda item: str(item["artifact_id"]),
    )
    return len(actual) == len(remote_inputs) and actual == expected


def _plain_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MOSHI_GPU_INTERNAL_URL must be an HTTP(S) origin")
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MOSHI_GPU_INTERNAL_URL must be a plain origin")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


@dataclass(frozen=True)
class GpuDispatcherSettings:
    internal_url: str
    required_build_id: str
    instance_id: str
    dispatch_token: str = field(repr=False)
    interval_seconds: float = 3.0
    worker_fresh_seconds: int = 60
    lease_seconds: int = 120
    lease_heartbeat_seconds: float = 15.0
    leader_lease_seconds: int = 60
    request_timeout_seconds: float = 30.0
    upload_chunk_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "internal_url", _plain_origin(self.internal_url))
        if not self.required_build_id or any(character.isspace() for character in self.required_build_id):
            raise ValueError("A whitespace-free MOSHI_GPU_REQUIRED_BUILD_ID is required")
        if not self.instance_id.startswith("i-"):
            raise ValueError("A concrete MOSHI_GPU_INSTANCE_ID is required")
        if len(self.dispatch_token) < 32 or any(
            ord(character) < 33 or ord(character) == 127
            for character in self.dispatch_token
        ):
            raise ValueError("MOSHI_DISPATCH_TOKEN must be a high-entropy header-safe value")
        if self.interval_seconds <= 0 or self.worker_fresh_seconds <= 0:
            raise ValueError("GPU dispatcher intervals must be positive")
        if self.lease_seconds < 30:
            raise ValueError("GPU job leases must be at least 30 seconds")
        if not 0 < self.lease_heartbeat_seconds < self.lease_seconds:
            raise ValueError("Lease heartbeat must be positive and shorter than the lease")
        if self.leader_lease_seconds < 1:
            raise ValueError("GPU dispatcher leader lease must be positive")

    @classmethod
    def from_environment(cls) -> GpuDispatcherSettings | None:
        values = {
            "internal_url": os.environ.get("MOSHI_GPU_INTERNAL_URL", "").strip(),
            "required_build_id": os.environ.get("MOSHI_GPU_REQUIRED_BUILD_ID", "").strip(),
            "instance_id": os.environ.get("MOSHI_GPU_INSTANCE_ID", "").strip(),
            "dispatch_token": os.environ.get("MOSHI_DISPATCH_TOKEN", ""),
        }
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "Incomplete GPU dispatcher configuration: " + ", ".join(sorted(missing))
            )
        return cls(**values)


class _LeaseHeartbeat:
    def __init__(
        self,
        catalog: StudioCatalog,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        interval_seconds: float,
        lease_seconds: int,
    ) -> None:
        self.catalog = catalog
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"moshi-dispatch-lease-{job_id}",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.catalog.heartbeat_leased_job(
                    self.job_id,
                    self.worker_id,
                    self.lease_token,
                    message="Uploading immutable inputs to GPU",
                    lease_seconds=self.lease_seconds,
                )
            except (KeyError, LeaseConflictError):
                self._lost.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1))


class _LeaderHeartbeat:
    def __init__(
        self,
        catalog: StudioCatalog,
        owner_id: str,
        epoch: int,
        *,
        lease_seconds: int,
    ) -> None:
        self.catalog = catalog
        self.owner_id = owner_id
        self.epoch = epoch
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="moshi-gpu-dispatch-leader-heartbeat",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self.catalog.renew_gpu_dispatch_leader(
                    self.owner_id,
                    self.epoch,
                    lease_seconds=self.lease_seconds,
                )
            except (RuntimeError, LeaseConflictError):
                self._lost.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.lease_seconds / 3 + 1))


class GpuPushDispatcher:
    def __init__(
        self,
        catalog: StudioCatalog,
        paths: StudioPaths,
        contexts: JobContextBuilder,
        settings: GpuDispatcherSettings,
        *,
        lifecycle_wake: Callable[[], None],
        client: GpuDispatchClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.catalog = catalog
        self.paths = paths
        self.contexts = contexts
        self.settings = settings
        self.lifecycle_wake = lifecycle_wake
        self.client = client or GpuDispatchClient(
            settings.internal_url,
            settings.dispatch_token,
            timeout_seconds=settings.request_timeout_seconds,
            upload_chunk_bytes=settings.upload_chunk_bytes,
        )
        self._clock = clock or _utc_now
        self.owner_id = f"dispatcher:{uuid4().hex}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="moshi-gpu-push-dispatcher",
            daemon=True,
        )

    def _now(self) -> datetime:
        value = self._clock()
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        if self._thread.is_alive():
            # Keep the durable leadership lease while an in-flight request unwinds.
            # Releasing it here would permit a second process to overlap this one.
            return
        leader = self.catalog.get_gpu_dispatch_leader()
        if leader.get("owner_id") == self.owner_id:
            self.catalog.release_gpu_dispatch_leader(
                self.owner_id, int(leader["fencing_epoch"])
            )

    def wake(self) -> None:
        self._wake.set()
        self.lifecycle_wake()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the durable supervisor alive.
                runtime = self.catalog.get_gpu_runtime_state()
                details = dict(runtime.get("details") or {})
                details.update(
                    error_class=type(exc).__name__[:120],
                    error_summary="Unexpected dispatcher failure",
                )
                self.catalog.update_gpu_runtime_state(details=details)
            self._wake.wait(self.settings.interval_seconds)
            self._wake.clear()

    def run_once(self) -> bool:
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            leader = self.catalog.acquire_gpu_dispatch_leader(
                self.owner_id, lease_seconds=self.settings.leader_lease_seconds
            )
            if leader is None:
                return False
            epoch = int(leader["fencing_epoch"])
            guard = _LeaderHeartbeat(
                self.catalog,
                self.owner_id,
                epoch,
                lease_seconds=self.settings.leader_lease_seconds,
            )
            guard.start()
            try:
                return self._leader_tick(epoch, guard)
            finally:
                guard.stop()
        finally:
            self._tick_lock.release()

    def _leader_tick(self, epoch: int, leader: _LeaderHeartbeat) -> bool:
        self.catalog.requeue_expired_jobs()
        demand = self.catalog.gpu_demand_summary()
        if not any(demand.values()):
            return False
        self.lifecycle_wake()
        lifecycle = self.catalog.get_lifecycle_state()
        if lifecycle.get("instance_state") != "running":
            return True
        if (
            lifecycle.get("blocked_reason")
            and self.catalog.active_gpu_dispatch() is None
            and self.catalog.active_gpu_check() is None
        ):
            return True
        runtime = self.catalog.get_gpu_runtime_state()
        intake_retry_at = _parse_timestamp(
            (runtime.get("details") or {}).get("next_intake_retry_at")
        )
        if intake_retry_at is not None and intake_retry_at > self._now():
            return True
        try:
            ready = self._observe_ready()
        except GpuDispatchAuthenticationError:
            self._block_system("GPU dispatch authentication failed", "authentication_failed")
            return True
        except GpuDispatchError as exc:
            self._record_intake_error(exc)
            return True
        except GpuReadinessGateError as exc:
            self._block_system(str(exc), "incompatible")
            return True
        if leader.lost:
            return True

        active_dispatch = self.catalog.active_gpu_dispatch()
        recovering_prestart = bool(
            active_dispatch and active_dispatch.get("state") in PRESTART_STATES
        )
        if active_dispatch is not None:
            retry_at = _parse_timestamp(active_dispatch.get("next_retry_at"))
            if retry_at is not None and retry_at > self._now():
                return True
            if active_dispatch.get("state") in PRESTART_STATES:
                active_job = self.catalog.get_job(str(active_dispatch["job_id"]))
                if (
                    active_job.get("status") != "running"
                    or int(active_job.get("attempt", 0))
                    != int(active_dispatch.get("attempt", -1))
                ):
                    self._cancel_receipt(active_dispatch, fenced=True)
                    return True
            if active_dispatch.get("state") not in PRESTART_STATES:
                self._reconcile_active_dispatch(active_dispatch)
                return True

        active_check = self.catalog.active_gpu_check()
        if active_check is not None:
            try:
                self._reconcile_check(active_check, ready)
            except GpuDispatchAuthenticationError:
                self._block_system(
                    "GPU dispatch authentication failed", "authentication_failed"
                )
            except GpuDispatchRetryableError as exc:
                self._record_intake_error(exc)
            except GpuDispatchBlockedError:
                self.catalog.update_gpu_check(
                    str(active_check["id"]),
                    status="failed",
                    finished_at=self._now().isoformat(),
                    failure_class="gpu_check_blocked",
                    failure_summary="GPU functional check could not be started",
                )
                self._block_system(
                    "GPU functional check could not be started",
                    "functional_check_blocked",
                )
            return True

        worker = self._compatible_push_worker()
        if worker is None or not ready.get("callback", {}).get("ready"):
            return True
        supported_kinds = self._compatible_job_kinds(ready, worker)
        if not supported_kinds:
            self._block_system(
                "GPU service and callback heartbeat have no compatible job kinds",
                "incompatible",
            )
            return True
        functional = ready.get("functional_check") or {}
        if not functional.get("ready"):
            check, _ = self.catalog.request_gpu_check(
                "job_preflight",
                instance_id=self.settings.instance_id,
                cold_start=False,
                expected_build_id=self.settings.required_build_id,
                dispatch_protocol=GPU_DISPATCH_PROTOCOL_VERSION,
                worker_protocol=WORKER_PROTOCOL_VERSION,
            )
            try:
                self._reconcile_check(check, ready)
            except GpuDispatchAuthenticationError:
                self._block_system(
                    "GPU dispatch authentication failed", "authentication_failed"
                )
            except GpuDispatchRetryableError as exc:
                self._record_intake_error(exc)
            return True
        if not ready.get("operational_ready"):
            return True
        if not ready.get("accepting_dispatches") and not recovering_prestart:
            return True

        claimed = self.catalog.claim_job_for_gpu_dispatch(
            str(worker["worker_id"]),
            protocol_version=WORKER_PROTOCOL_VERSION,
            worker_build_id=self.settings.required_build_id,
            token_factory=lambda job_id, attempt: derive_dispatch_lease_token(
                self.settings.dispatch_token, job_id, attempt
            ),
            required_build_id=self.settings.required_build_id,
            dispatch_protocol=GPU_DISPATCH_PROTOCOL_VERSION,
            supported_kinds=supported_kinds,
            lease_seconds=self.settings.lease_seconds,
            leader_owner_id=self.owner_id,
            leader_epoch=epoch,
        )
        if claimed is None:
            return True
        self._deliver(claimed, leader)
        return True

    def _reconcile_active_dispatch(self, dispatch: dict[str, Any]) -> None:
        dispatch_id = str(dispatch["id"])
        try:
            job = self.catalog.get_job(str(dispatch["job_id"]))
        except KeyError:
            self._cancel_receipt(dispatch, fenced=True)
            return
        if (
            job.get("status") not in LOCAL_TERMINAL_JOB_STATES
            and (
                job.get("status") != "running"
                or int(job.get("attempt", 0)) != int(dispatch.get("attempt", -1))
            )
        ):
            self._cancel_receipt(dispatch, fenced=True)
            return
        try:
            response = self.client.get_dispatch(dispatch_id)
        except GpuDispatchAuthenticationError:
            self._block_system("GPU dispatch authentication failed", "authentication_failed")
            return
        except GpuDispatchNotFoundError as exc:
            if job.get("status") in LOCAL_TERMINAL_JOB_STATES:
                self.catalog.update_gpu_dispatch(
                    dispatch_id,
                    state="complete" if job.get("status") == "complete" else "fenced",
                    remote_state="missing",
                    finished_at=self._now().isoformat(),
                    last_http_status=exc.status,
                )
            else:
                self._defer_dispatch(dispatch_id, exc)
            return
        except GpuDispatchRetryableError as exc:
            self._defer_dispatch(dispatch_id, exc)
            return
        remote = response.data or {}
        remote_state = str(remote.get("state") or "")
        manifest_value = dispatch.get("manifest")
        manifest = (
            DispatchCreate.model_validate(manifest_value)
            if isinstance(manifest_value, dict)
            else None
        )
        if manifest is None or not _receipt_matches(remote, manifest):
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="blocked",
                last_error_class="reconciliation_conflict",
                last_error_summary="GPU receipt does not match the fenced attempt",
            )
            self._block_system("GPU receipt does not match the fenced attempt", "incompatible")
            return
        if (
            job.get("status") == "running"
            and self.contexts.current_fingerprint(job) != job.get("input_fingerprint")
        ):
            job = self.catalog.supersede_job(
                str(job["id"]), "Authoritative inputs changed"
            )
        if job.get("status") in LOCAL_TERMINAL_JOB_STATES:
            if job.get("status") == "superseded":
                self._cancel_receipt(dispatch, fenced=True)
                return
            if remote_state in {"receiving", "verified", "queued"}:
                self._cancel_receipt(dispatch, fenced=job.get("status") == "superseded")
                return
            if remote_state in REMOTE_TERMINAL_STATES:
                local_state = (
                    "complete"
                    if job.get("status") == "complete" and remote_state == "acknowledged"
                    else "fenced"
                    if remote_state in {"orphaned", "cancelled"}
                    else "failed"
                )
                self.catalog.update_gpu_dispatch(
                    dispatch_id,
                    state=local_state,
                    remote_state=remote_state,
                    finished_at=self._now().isoformat(),
                    last_http_status=response.status,
                )
                return
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="completion_pending",
                remote_state=remote_state,
                last_http_status=response.status,
            )
            return
        if remote_state in REMOTE_TERMINAL_STATES:
            lease_token = derive_dispatch_lease_token(
                self.settings.dispatch_token,
                str(job["id"]),
                int(job["attempt"]),
            )
            with suppress(LeaseConflictError, ValueError):
                self.catalog.fail_leased_job(
                    str(job["id"]),
                    str(dispatch["worker_id"]),
                    lease_token,
                    error="GPU receipt became terminal before callback acknowledgement",
                    failure_class="gpu_receipt_terminal",
                    retryable=remote_state in {"orphaned"},
                )
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="fenced" if remote_state in {"orphaned", "cancelled"} else "failed",
                remote_state=remote_state,
                finished_at=self._now().isoformat(),
                last_http_status=response.status,
            )
            return
        local_state = (
            "completion_pending"
            if remote_state in {"outbox_pending", "callback_uploading", "auth_blocked"}
            else "running"
        )
        self.catalog.update_gpu_dispatch(
            dispatch_id,
            state=local_state,
            remote_state=remote_state,
            last_http_status=response.status,
            last_error_class=None,
            last_error_summary=None,
            next_retry_at=None,
        )

    def _observe_ready(self) -> dict[str, Any]:
        live = self.client.live().data or {}
        if live.get("protocol_version") != GPU_DISPATCH_PROTOCOL_VERSION:
            raise GpuReadinessGateError("GPU dispatch protocol is incompatible")
        if live.get("build_id") != self.settings.required_build_id:
            raise GpuReadinessGateError("GPU worker build is incompatible")
        response = self.client.ready()
        value = response.data or {}
        protocol = str(value.get("protocol_version") or "")
        build_id = str(value.get("build_id") or "")
        if protocol != GPU_DISPATCH_PROTOCOL_VERSION:
            raise GpuReadinessGateError("GPU dispatch protocol is incompatible")
        if build_id != self.settings.required_build_id:
            raise GpuReadinessGateError("GPU worker build is incompatible")
        capabilities = value.get("capabilities") or {}
        remote_job_kinds = capabilities.get("job_kinds")
        if not isinstance(remote_job_kinds, list) or not any(
            kind in GPU_JOB_KINDS for kind in remote_job_kinds
        ):
            raise GpuReadinessGateError("GPU service job capabilities are incompatible")
        service = value.get("service") or {}
        callback = value.get("callback") or {}
        functional = value.get("functional_check") or {}
        current = value.get("current_dispatch") or {}
        counts = value.get("dispatch_counts") or {}
        queue = self.catalog.queue_summary()
        self.catalog.update_gpu_runtime_state(
            instance_id=self.settings.instance_id,
            instance_state="running",
            desired_state="running",
            last_intake_observation_at=self._now().isoformat(),
            intake_reachable=True,
            intake_status=value.get("status"),
            dispatch_protocol=protocol,
            actual_build_id=build_id,
            expected_build_id=self.settings.required_build_id,
            host_boot_id=service.get("host_boot_id"),
            service_boot_id=service.get("service_boot_id"),
            callback_ready=bool(callback.get("ready")),
            functional_check_ready=bool(functional.get("ready")),
            operational_ready=bool(value.get("operational_ready")),
            accepting_dispatches=bool(value.get("accepting_dispatches")),
            safe_to_stop=bool(value.get("safe_to_stop")),
            current_dispatch_id=current.get("dispatch_id"),
            queued_count=int(queue.get("queued", 0)),
            running_count=int(queue.get("running", 0)),
            details={
                "capabilities": capabilities,
                "remote_dispatch_counts": counts,
                "functional_requirement_key": functional.get("requirement_key"),
                "auth_blocked": bool(callback.get("auth_blocked")),
                "dispatcher_leader": self.owner_id,
            },
        )
        return value

    def _record_intake_error(self, exc: GpuDispatchError) -> None:
        runtime = self.catalog.get_gpu_runtime_state()
        details = dict(runtime.get("details") or {})
        retry_count = int(details.get("intake_retry_count") or 0) + 1
        delay = min(60.0, 2 ** min(retry_count, 6))
        delay *= random.uniform(0.75, 1.25)
        details.update(
            error_class=type(exc).__name__[:120],
            error_summary="GPU intake request failed",
            auth_blocked=isinstance(exc, GpuDispatchAuthenticationError),
            intake_retry_count=retry_count,
            next_intake_retry_at=(self._now() + timedelta(seconds=delay)).isoformat(),
        )
        self.catalog.update_gpu_runtime_state(intake_reachable=False, details=details)

    def _block_system(self, summary: str, error_class: str) -> None:
        runtime = self.catalog.get_gpu_runtime_state()
        details = dict(runtime.get("details") or {})
        details.update(
            error_class=error_class[:120],
            error_summary=_sanitize_summary(summary),
            auth_blocked=error_class == "authentication_failed",
        )
        self.catalog.update_gpu_runtime_state(
            intake_reachable=error_class != "authentication_failed",
            operational_ready=False,
            details=details,
        )
        self.catalog.update_lifecycle_state(blocked_reason=_sanitize_summary(summary))

    def _compatible_push_worker(self) -> dict[str, Any] | None:
        cutoff = self._now() - timedelta(seconds=self.settings.worker_fresh_seconds)
        with self.catalog.connect() as connection:
            rows = connection.execute(
                "SELECT worker_id FROM worker_state ORDER BY last_heartbeat DESC"
            ).fetchall()
        for row in rows:
            worker = self.catalog.get_worker_state(str(row["worker_id"]))
            heartbeat = _parse_timestamp(worker.get("last_heartbeat"))
            details = worker.get("details") or {}
            if (
                heartbeat
                and heartbeat >= cutoff
                and worker.get("protocol_version") == WORKER_PROTOCOL_VERSION
                and worker.get("build_id") == self.settings.required_build_id
                and details.get("mode") == "push"
                and details.get("dispatch_protocol_version")
                == GPU_DISPATCH_PROTOCOL_VERSION
            ):
                self.catalog.update_gpu_runtime_state(
                    worker_protocol=WORKER_PROTOCOL_VERSION,
                    last_worker_heartbeat_at=worker.get("last_heartbeat"),
                )
                return worker
        return None

    @staticmethod
    def _compatible_job_kinds(
        ready: dict[str, Any],
        worker: dict[str, Any],
    ) -> tuple[str, ...]:
        capabilities = ready.get("capabilities") or {}
        remote = capabilities.get("job_kinds")
        heartbeat = worker.get("supported_kinds")
        if not isinstance(remote, list) or not isinstance(heartbeat, list):
            return ()
        return tuple(
            kind
            for kind in GPU_JOB_KINDS
            if kind in remote and kind in heartbeat
        )

    def _reconcile_check(self, check: dict[str, Any], ready: dict[str, Any]) -> None:
        remote_id = check.get("gpu_check_id")
        if remote_id:
            history = (self.client.self_checks(limit=20).data or {}).get("checks") or []
            remote = next(
                (value for value in history if str(value.get("id")) == str(remote_id)),
                None,
            )
            if remote is not None:
                updated = self._store_remote_check(check, remote, ready)
                if updated.get("status") == "failed":
                    self._block_system("GPU functional check failed", "functional_check_failed")
                return
        current_dispatch = ready.get("current_dispatch") or {}
        if current_dispatch.get("state") == "running":
            self.catalog.update_gpu_check(str(check["id"]), status="waiting")
            return
        try:
            response = self.client.trigger_self_check(
                SelfCheckRequest(trigger=str(check["trigger"]), force=False)
            )
        except GpuDispatchRateLimitError as exc:
            self.catalog.update_gpu_check(
                str(check["id"]),
                status="waiting",
                failure_class="gpu_cooldown",
                failure_summary=(
                    f"GPU cooldown active; retry after {exc.retry_after} seconds"
                    if exc.retry_after
                    else "GPU cooldown active"
                ),
            )
            return
        except GpuDispatchConflictError:
            self.catalog.update_gpu_check(str(check["id"]), status="waiting")
            return
        remote = response.data or {}
        self._store_remote_check(check, remote, ready)

    def _store_remote_check(
        self,
        local: dict[str, Any],
        remote: dict[str, Any],
        ready: dict[str, Any],
    ) -> dict[str, Any]:
        service = ready.get("service") or {}
        fixture_digest = str(remote.get("fixture_sha256") or "")
        status = str(remote.get("status") or "failed")
        if status not in {"queued", "running", "passed", "failed"}:
            status = "failed"
        updated = self.catalog.update_gpu_check(
            str(local["id"]),
            gpu_check_id=str(remote.get("id")) if remote.get("id") else None,
            status=status,
            requirement_key=remote.get("requirement_key"),
            host_boot_id=remote.get("host_boot_id"),
            service_boot_id=service.get("service_boot_id"),
            dispatch_protocol=remote.get("protocol_version"),
            worker_protocol=WORKER_PROTOCOL_VERSION,
            actual_build_id=remote.get("build_id"),
            expected_build_id=self.settings.required_build_id,
            model_revision=(
                remote.get("model_revision") or remote.get("requested_model_revision")
            ),
            config_fingerprint=remote.get("config_fingerprint"),
            fixture_id=remote.get("fixture_id"),
            fixture_hash_prefix=fixture_digest[:12] or None,
            started_at=remote.get("started_at"),
            finished_at=remote.get("finished_at"),
            valid_until=remote.get("valid_until"),
            gpu_name=remote.get("gpu_name"),
            device=remote.get("device"),
            segment_count=remote.get("segment_count"),
            cer=remote.get("cer"),
            cer_threshold=remote.get("max_cer"),
            model_load_ms=remote.get("model_load_ms"),
            inference_ms=remote.get("inference_ms"),
            total_ms=remote.get("total_ms"),
            failure_class=remote.get("failure_class"),
            failure_summary=(
                _sanitize_summary(str(remote.get("failure_summary")))
                if remote.get("failure_summary")
                else None
            ),
        )
        if status in {"passed", "failed"}:
            self.catalog.update_gpu_runtime_state(
                functional_check_ready=status == "passed",
                last_functional_check_at=remote.get("finished_at"),
            )
        return updated

    def _deliver(self, claimed: dict[str, Any], leader: _LeaderHeartbeat) -> None:
        job = dict(claimed["job"])
        lease_token = str(claimed["lease_token"])
        dispatch = dict(claimed["dispatch"])
        dispatch_id = str(dispatch["id"])
        if job.get("status") != "running":
            self._cancel_receipt(dispatch, fenced=True)
            return
        if self.contexts.current_fingerprint(job) != job.get("input_fingerprint"):
            self.catalog.supersede_job(str(job["id"]), "Authoritative inputs changed")
            self._cancel_receipt(dispatch, fenced=True)
            return

        manifest, start = self._prepare_dispatch(job, dispatch)
        heartbeat = _LeaseHeartbeat(
            self.catalog,
            str(job["id"]),
            str(dispatch["worker_id"]),
            lease_token,
            interval_seconds=self.settings.lease_heartbeat_seconds,
            lease_seconds=self.settings.lease_seconds,
        )
        heartbeat.start()
        try:
            self._create_or_reconcile(dispatch, manifest)
            self.catalog.update_gpu_dispatch(dispatch_id, state="uploading")
            for item in manifest.inputs:
                if heartbeat.lost or leader.lost:
                    raise LeaseConflictError("Dispatcher fencing was lost during upload")
                self._upload_input(dispatch, item)
            if heartbeat.lost or leader.lost:
                raise LeaseConflictError("Dispatcher fencing was lost before GPU start")
            current = self.catalog.get_job(str(job["id"]))
            if (
                current.get("status") != "running"
                or self.contexts.current_fingerprint(current)
                != current.get("input_fingerprint")
            ):
                if current.get("status") == "running":
                    self.catalog.supersede_job(str(job["id"]), "Authoritative inputs changed")
                self._cancel_receipt(dispatch, fenced=True)
                return
            self._start_or_reconcile(dispatch, start, lease_token)
        except GpuDispatchAuthenticationError:
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="blocked",
                last_http_status=401,
                last_error_class="authentication_failed",
                last_error_summary="GPU dispatch authentication failed",
            )
            self._block_system("GPU dispatch authentication failed", "authentication_failed")
        except GpuDispatchBlockedError as exc:
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="blocked",
                last_http_status=exc.status,
                last_error_class=type(exc).__name__[:120],
                last_error_summary="GPU rejected the immutable dispatch",
            )
            self._block_system("GPU rejected the immutable dispatch", "dispatch_blocked")
        except (GpuDispatchRetryableError, GpuDispatchNotFoundError) as exc:
            self._defer_dispatch(dispatch_id, exc)
        except GpuDispatchConflictError as exc:
            self._defer_dispatch(dispatch_id, exc)
        except LeaseConflictError:
            self._cancel_receipt(dispatch, fenced=True)
        finally:
            heartbeat.stop()

    def _prepare_dispatch(
        self, job: dict[str, Any], dispatch: dict[str, Any]
    ) -> tuple[DispatchCreate, DispatchStart]:
        if dispatch.get("manifest") and dispatch.get("context"):
            return (
                DispatchCreate.model_validate(dispatch["manifest"]),
                DispatchStart.model_validate(dispatch["context"]),
            )
        context = self.contexts.create_context(job)
        ordered_inputs = sorted(context.inputs, key=lambda item: item.artifact_id)
        context = context.model_copy(update={"inputs": ordered_inputs})
        manifest = DispatchCreate(
            dispatch_id=str(dispatch["id"]),
            job_id=str(job["id"]),
            attempt=int(job["attempt"]),
            protocol_version=GPU_DISPATCH_PROTOCOL_VERSION,
            required_build_id=self.settings.required_build_id,
            input_fingerprint=str(job["input_fingerprint"]),
            inputs=[
                DispatchInput(
                    artifact_id=item.artifact_id,
                    role=item.role,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    media_type=item.media_type,
                    filename=item.filename,
                )
                for item in ordered_inputs
            ],
        )
        start = DispatchStart(context=context.model_dump(mode="json"))
        self.catalog.configure_gpu_dispatch(
            str(dispatch["id"]),
            manifest=manifest.model_dump(mode="json"),
            context=start.model_dump(mode="json"),
            inputs=[item.model_dump(mode="json") for item in manifest.inputs],
        )
        return manifest, start

    def _create_or_reconcile(
        self, dispatch: dict[str, Any], manifest: DispatchCreate
    ) -> None:
        dispatch_id = str(dispatch["id"])
        self.catalog.update_gpu_dispatch(dispatch_id, state="creating")
        try:
            response = self.client.create_dispatch(manifest)
        except (GpuDispatchRetryableError, GpuDispatchConflictError):
            try:
                response = self.client.get_dispatch(dispatch_id)
            except GpuDispatchNotFoundError:
                raise
        remote = response.data or {}
        if not _receipt_matches(remote, manifest):
            raise GpuDispatchBlockedError(
                "GPU dispatch reconciliation conflict",
                status=409,
                method="GET",
                path=f"/internal/v2/dispatches/{dispatch_id}",
            )
        self.catalog.update_gpu_dispatch(
            dispatch_id,
            remote_state=remote.get("state"),
            last_http_status=response.status,
            last_error_class=None,
            last_error_summary=None,
            next_retry_at=None,
        )

    def _artifact_path(self, item: DispatchInput) -> Path:
        artifact = self.catalog.get_artifact(item.artifact_id)
        if (
            artifact.get("state") != "active"
            or artifact.get("role") != item.role
            or artifact.get("sha256") != item.sha256
            or int(artifact.get("size_bytes", -1)) != item.size_bytes
            or artifact.get("media_type") != item.media_type
        ):
            raise GpuDispatchBlockedError("Catalog artifact changed before dispatch")
        path = self.paths.resolve_relative(str(artifact["relative_path"]))
        if (
            not path.is_file()
            or path.stat().st_size != item.size_bytes
            or file_sha256(path) != item.sha256
        ):
            raise GpuDispatchBlockedError("Catalog artifact failed integrity validation")
        return path

    def _upload_input(self, dispatch: dict[str, Any], item: DispatchInput) -> None:
        dispatch_id = str(dispatch["id"])
        response = self.client.head_input(dispatch_id, item.artifact_id)
        try:
            offset = int(response.headers.get("x-accepted-offset", "-1"))
        except ValueError as exc:
            raise GpuDispatchBlockedError("GPU returned an invalid input offset") from exc
        if offset < 0 or offset > item.size_bytes:
            raise GpuDispatchBlockedError("GPU returned an invalid input offset")
        state = response.headers.get("x-input-state")
        self.catalog.update_gpu_dispatch_input(
            dispatch_id,
            item.artifact_id,
            accepted_offset=offset,
            state="verified" if state == "verified" else "uploading",
        )
        if state == "verified" and offset == item.size_bytes:
            return
        path = self._artifact_path(item)
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset < item.size_bytes:
                body = stream.read(min(self.client.upload_chunk_bytes, item.size_bytes - offset))
                if not body:
                    raise GpuDispatchBlockedError("Input ended before its registered size")
                end = offset + len(body) - 1
                try:
                    sent = self.client.put_input(
                        dispatch_id,
                        item.artifact_id,
                        body=body,
                        start=offset,
                        end=end,
                        total=item.size_bytes,
                    )
                    data = sent.data or {}
                    accepted = int(data.get("accepted_offset", end + 1))
                except (GpuDispatchRetryableError, GpuDispatchConflictError):
                    reconciled = self.client.head_input(dispatch_id, item.artifact_id)
                    accepted = int(reconciled.headers.get("x-accepted-offset", "-1"))
                    if accepted <= offset:
                        raise
                if accepted < offset or accepted > item.size_bytes:
                    raise GpuDispatchBlockedError("GPU returned an invalid input offset")
                offset = accepted
                stream.seek(offset)
                self.catalog.update_gpu_dispatch_input(
                    dispatch_id,
                    item.artifact_id,
                    accepted_offset=offset,
                    state="verified" if offset == item.size_bytes else "uploading",
                )
        final = self.client.head_input(dispatch_id, item.artifact_id)
        if (
            int(final.headers.get("x-accepted-offset", "-1")) != item.size_bytes
            or final.headers.get("x-input-state") != "verified"
        ):
            raise GpuDispatchConflictError(
                "GPU input is not verified",
                status=409,
                method="HEAD",
                path=f"/internal/v2/dispatches/{dispatch_id}/inputs/{item.artifact_id}",
            )

    def _start_or_reconcile(
        self,
        dispatch: dict[str, Any],
        start: DispatchStart,
        lease_token: str,
    ) -> None:
        dispatch_id = str(dispatch["id"])
        self.catalog.update_gpu_dispatch(dispatch_id, state="starting")
        try:
            response = self.client.start(dispatch_id, start, lease_token)
        except (GpuDispatchRetryableError, GpuDispatchConflictError):
            response = self.client.get_dispatch(dispatch_id)
            remote = response.data or {}
            if remote.get("state") not in REMOTE_ACCEPTED_STATES:
                raise
        remote = response.data or {}
        self.catalog.update_gpu_dispatch(
            dispatch_id,
            state="accepted",
            remote_state=remote.get("state", "queued"),
            accepted_at=self._now().isoformat(),
            last_http_status=response.status,
            next_retry_at=None,
            last_error_class=None,
            last_error_summary=None,
        )

    def _cancel_receipt(self, dispatch: dict[str, Any], *, fenced: bool) -> None:
        dispatch_id = str(dispatch["id"])
        try:
            response = self.client.cancel(dispatch_id)
        except GpuDispatchNotFoundError:
            response = None
        except GpuDispatchConflictError:
            self.catalog.update_gpu_dispatch(
                dispatch_id,
                state="cancel_requested",
                last_error_class="running_cancel_deferred",
                last_error_summary="GPU will observe cancellation through lease heartbeat",
            )
            return
        self.catalog.update_gpu_dispatch(
            dispatch_id,
            state="fenced" if fenced else "cancelled",
            remote_state=(response.data or {}).get("state") if response else None,
            finished_at=self._now().isoformat(),
        )

    def _defer_dispatch(self, dispatch_id: str, exc: GpuDispatchError) -> None:
        dispatch = self.catalog.get_gpu_dispatch(dispatch_id)
        retry_count = int(dispatch.get("retry_count", 0)) + 1
        delay = min(60.0, 2 ** min(retry_count, 6))
        delay *= random.uniform(0.75, 1.25)
        self.catalog.update_gpu_dispatch(
            dispatch_id,
            retry_count=retry_count,
            next_retry_at=(self._now() + timedelta(seconds=delay)).isoformat(),
            last_http_status=exc.status,
            last_error_class=type(exc).__name__[:120],
            last_error_summary="GPU dispatch will be reconciled",
        )
