from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from moshi_data_pipeline.callback_contract import CALLBACK_PROTOCOL_VERSION
from moshi_data_pipeline.gpu_dispatch_state import GpuDispatchStore
from moshi_data_pipeline.gpu_job_protocol import JOB_KINDS, JobContext
from moshi_data_pipeline.remote_worker import (
    HttpWorkerApi,
    WorkerIdentity,
    WorkerTransportError,
    sha256_file,
)
from moshi_data_pipeline.studio.execution_runtime import ContextJobExecutor, ExecutionOutput

LOGGER = logging.getLogger(__name__)


class ExecutionCancelledError(RuntimeError):
    pass


def _safe_output_name(value: str, ordinal: int) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", Path(value).name)[:160]
    if not candidate or candidate in {".", ".."}:
        candidate = "artifact.bin"
    return f"{ordinal:03d}_{candidate}"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _failure_payload(exc: Exception, worker_id: str) -> dict[str, Any]:
    message = str(exc)[:4000] or type(exc).__name__
    lowered = message.casefold()
    if isinstance(exc, MemoryError) or "out of memory" in lowered:
        failure_class, retryable = "gpu_oom", False
    elif isinstance(exc, ExecutionCancelledError):
        failure_class, retryable = "cancelled", False
    elif isinstance(exc, ValueError):
        failure_class, retryable = "invalid_input", False
    elif isinstance(exc, OSError):
        failure_class, retryable = "storage", True
    else:
        failure_class, retryable = "application", False
    return {
        "worker_id": worker_id,
        "failure_class": failure_class,
        "error": message,
        "retryable": retryable,
    }


class GpuJobRunner:
    def __init__(
        self,
        store: GpuDispatchStore,
        api: HttpWorkerApi,
        identity: WorkerIdentity,
        *,
        executor_factory: Any = ContextJobExecutor,
        heartbeat_seconds: float = 15,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.store = store
        self.api = api
        self.identity = identity
        self.executor_factory = executor_factory
        self.heartbeat_seconds = heartbeat_seconds
        self.stop_event = stop_event or threading.Event()
        self.attempts_root = store.cache_root / "attempts"
        self.attempts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attempts_root.chmod(0o700)

    @staticmethod
    def _retry_delay(retry_count: int) -> int:
        return min(60, max(2, 2 ** min(retry_count, 5)))

    def _preflight_lease(self, record: dict[str, Any]) -> bool:
        try:
            response = self.api.job_heartbeat(
                str(record["job_id"]),
                str(record["lease_token"]),
                {
                    "worker_id": self.identity.worker_id,
                    "progress": 0.0,
                    "message": "GPU accepted pushed attempt",
                },
            )
            if response.get("cancel_requested"):
                self.store.abandon_running(
                    str(record["dispatch_id"]),
                    state="orphaned",
                    error_summary="Web service cancelled or superseded the attempt",
                    http_status=409,
                )
                return False
            self.store.record_callback_health(
                success=True, http_status=200, error_class=None
            )
            return True
        except WorkerTransportError as exc:
            dispatch_id = str(record["dispatch_id"])
            if exc.status_code == 401:
                self.store.block_callback_auth(dispatch_id)
            elif exc.status_code == 409:
                self.store.abandon_running(
                    dispatch_id,
                    state="orphaned",
                    error_summary="Web lease is obsolete",
                    http_status=409,
                )
            else:
                delay = self._retry_delay(int(record.get("execution_retry_count", 0)))
                self.store.defer_execution(
                    dispatch_id,
                    delay_seconds=delay,
                    error_summary="Web lease heartbeat is unavailable",
                    http_status=exc.status_code,
                )
                self.store.record_callback_health(
                    success=False,
                    http_status=exc.status_code,
                    error_class="transport",
                    next_retry_seconds=delay,
                )
            return False

    def _verified_inputs(
        self, record: dict[str, Any], context: JobContext
    ) -> dict[str, Path]:
        registered = {str(item["artifact_id"]): item for item in record["inputs"]}
        inputs: dict[str, Path] = {}
        for artifact in context.inputs:
            item = registered.get(artifact.artifact_id)
            if item is None or item["state"] != "verified" or not item["cache_path"]:
                raise ValueError("A registered dispatch input is not verified")
            path = Path(str(item["cache_path"])).resolve()
            if self.store.input_root not in path.parents:
                raise ValueError("A cached input path escaped the persistent cache")
            if (
                not path.is_file()
                or path.stat().st_size != artifact.size_bytes
                or sha256_file(path) != artifact.sha256
            ):
                raise ValueError("A cached dispatch input failed integrity validation")
            inputs[artifact.artifact_id] = path
        return inputs

    def _persist_output(
        self,
        record: dict[str, Any],
        context: JobContext,
        attempt_root: Path,
        output: ExecutionOutput,
    ) -> None:
        dispatch_id = str(record["dispatch_id"])
        outbox_root = (self.store.outbox_root / dispatch_id).resolve()
        if self.store.outbox_root not in outbox_root.parents:
            raise ValueError("Outbox path escaped the persistent cache")
        if outbox_root.exists():
            shutil.rmtree(outbox_root)
        artifact_root = outbox_root / "artifacts"
        artifact_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        persisted: list[dict[str, Any]] = []
        for ordinal, artifact in enumerate(output.artifacts):
            source = artifact.path.resolve()
            if attempt_root.resolve() not in source.parents or not source.is_file():
                raise ValueError("Executor produced a file outside its attempt directory")
            destination = artifact_root / _safe_output_name(source.name, ordinal)
            os.replace(source, destination)
            destination.chmod(0o600)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            persisted.append(
                {
                    "role": artifact.role,
                    "path": str(destination),
                    "filename": destination.name,
                    "sha256": sha256_file(destination),
                    "size_bytes": destination.stat().st_size,
                    "media_type": artifact.media_type,
                }
            )
        _fsync_directory(artifact_root)
        _fsync_directory(outbox_root)
        self.store.record_execution_success(
            dispatch_id,
            {
                "worker_id": self.identity.worker_id,
                "input_fingerprint": context.input_fingerprint,
                "kind": context.kind,
                "result": output.result,
            },
            persisted,
        )

    def run_once(self) -> bool:
        if self.store.callback_auth_blocked():
            return False
        record = self.store.claim_queued_dispatch()
        if record is None:
            return False
        dispatch_id = str(record["dispatch_id"])
        if not self._preflight_lease(record):
            return True
        context_wrapper = record.get("context")
        if not isinstance(context_wrapper, dict) or "context" not in context_wrapper:
            self.store.record_execution_failure(
                dispatch_id,
                _failure_payload(ValueError("Missing execution context"), self.identity.worker_id),
            )
            return True
        try:
            context = JobContext.model_validate(context_wrapper["context"])
        except Exception as exc:  # noqa: BLE001 - stored as a bounded invalid-input failure.
            self.store.record_execution_failure(
                dispatch_id, _failure_payload(exc, self.identity.worker_id)
            )
            return True
        lease_token = str(record["lease_token"])
        progress_state: dict[str, Any] = {"progress": 0.0, "message": "Starting"}
        heartbeat_stop = threading.Event()
        cancelled: list[str] = []
        auth_failed = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(self.heartbeat_seconds):
                try:
                    response = self.api.job_heartbeat(
                        context.job_id,
                        lease_token,
                        {"worker_id": self.identity.worker_id, **progress_state},
                    )
                    if response.get("cancel_requested"):
                        cancelled.append("Web service cancelled or superseded the attempt")
                        heartbeat_stop.set()
                    else:
                        self.store.record_callback_health(
                            success=True, http_status=200, error_class=None
                        )
                except WorkerTransportError as exc:
                    if exc.status_code == 401:
                        auth_failed.set()
                        heartbeat_stop.set()
                    elif exc.status_code == 409:
                        cancelled.append("Web lease is obsolete")
                        heartbeat_stop.set()
                    else:
                        self.store.record_callback_health(
                            success=False,
                            http_status=exc.status_code,
                            error_class="transport",
                            next_retry_seconds=15,
                        )

        def progress(value: float, message: str) -> None:
            if self.stop_event.is_set():
                raise ExecutionCancelledError("GPU service is stopping")
            if cancelled:
                raise ExecutionCancelledError(cancelled[0])
            bounded = max(0.0, min(1.0, value))
            progress_state.update(progress=bounded, message=message[:1000])
            self.store.update_progress(dispatch_id, bounded, message)

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name=f"push-lease-heartbeat-{context.job_id}",
            daemon=True,
        )
        attempt_root = self.attempts_root / f"{dispatch_id}_{uuid4().hex}"
        try:
            heartbeat.start()
            attempt_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            inputs = self._verified_inputs(record, context)
            executor = self.executor_factory(attempt_root)
            output = executor.execute(context, inputs, progress)
            if cancelled:
                raise ExecutionCancelledError(cancelled[0])
            progress(1.0, "GPU execution complete")
            self._persist_output(record, context, attempt_root, output)
            if auth_failed.is_set():
                self.store.block_callback_auth(dispatch_id)
        except Exception as exc:  # noqa: BLE001 - converted to a bounded durable failure.
            self.store.record_execution_failure(
                dispatch_id, _failure_payload(exc, self.identity.worker_id)
            )
            if auth_failed.is_set():
                self.store.block_callback_auth(dispatch_id)
        finally:
            heartbeat_stop.set()
            if heartbeat.ident is not None:
                heartbeat.join(timeout=max(1.0, self.heartbeat_seconds + 1))
            if attempt_root.exists():
                shutil.rmtree(attempt_root)
        return True


class GpuOutboxSender:
    def __init__(
        self,
        store: GpuDispatchStore,
        api: HttpWorkerApi,
        identity: WorkerIdentity,
    ) -> None:
        self.store = store
        self.api = api
        self.identity = identity

    @staticmethod
    def _retry_delay(retry_count: int) -> int:
        return min(300, max(2, 2 ** min(retry_count, 8)))

    def _upload_outputs(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        produced: list[dict[str, Any]] = []
        for output in record["outputs"]:
            path = Path(str(output["path"])).resolve()
            if self.store.outbox_root not in path.parents:
                raise ValueError("Outbox artifact path escaped the persistent cache")
            if (
                not path.is_file()
                or path.stat().st_size != int(output["size_bytes"])
                or sha256_file(path) != str(output["sha256"])
            ):
                raise ValueError("A durable outbox artifact failed integrity validation")
            upload_id = output["upload_id"]
            if not upload_id:
                upload = self.api.create_artifact_upload(
                    str(record["job_id"]),
                    self.identity.worker_id,
                    str(record["lease_token"]),
                    role=str(output["role"]),
                    filename=str(output["filename"]),
                    sha256=str(output["sha256"]),
                    size_bytes=int(output["size_bytes"]),
                    media_type=str(output["media_type"]),
                )
                upload_id = str(upload["id"])
                self.store.set_output_upload_id(
                    str(record["dispatch_id"]), int(output["ordinal"]), upload_id
                )
            size = int(output["size_bytes"])
            offset = self.api.artifact_upload_offset(
                str(upload_id), self.identity.worker_id, str(record["lease_token"])
            )
            if offset < 0 or offset > size:
                raise WorkerTransportError("Web upload offset is invalid")
            with path.open("rb") as stream:
                stream.seek(offset)
                while offset < size:
                    chunk = stream.read(min(self.api.upload_chunk_bytes, size - offset))
                    if not chunk:
                        raise WorkerTransportError("Outbox artifact ended during upload")
                    end = offset + len(chunk) - 1
                    self.api.append_artifact_upload(
                        str(upload_id),
                        self.identity.worker_id,
                        str(record["lease_token"]),
                        body=chunk,
                        start=offset,
                        end=end,
                        total=size,
                    )
                    offset = end + 1
            self.store.mark_output_uploaded(
                str(record["dispatch_id"]), int(output["ordinal"])
            )
            produced.append(
                {
                    "upload_id": str(upload_id),
                    "role": str(output["role"]),
                    "sha256": str(output["sha256"]),
                    "size_bytes": size,
                    "media_type": str(output["media_type"]),
                }
            )
        return produced

    def _cleanup(self, dispatch_id: str) -> None:
        path = (self.store.outbox_root / dispatch_id).resolve()
        if self.store.outbox_root in path.parents and path.exists():
            shutil.rmtree(path)

    def run_once(self) -> bool:
        if self.store.callback_auth_blocked():
            return False
        record = self.store.claim_pending_callback()
        if record is None:
            return False
        dispatch_id = str(record["dispatch_id"])
        try:
            payload = dict(record["outbox_payload"] or {})
            if record["outbox_kind"] == "complete":
                payload["artifacts"] = self._upload_outputs(record)
                self.api.complete(
                    str(record["job_id"]), str(record["lease_token"]), payload
                )
            elif record["outbox_kind"] == "fail":
                self.api.fail(
                    str(record["job_id"]), str(record["lease_token"]), payload
                )
            else:
                raise ValueError("Unknown durable callback kind")
        except WorkerTransportError as exc:
            if exc.status_code == 401:
                self.store.block_callback_auth(dispatch_id)
            elif exc.status_code == 409:
                self.store.finish_callback(dispatch_id, "orphaned")
            else:
                delay = self._retry_delay(int(record["callback_retry_count"]))
                self.store.retry_callback(
                    dispatch_id,
                    delay_seconds=delay,
                    error_class="transport",
                    http_status=exc.status_code,
                )
                self.store.record_callback_health(
                    success=False,
                    http_status=exc.status_code,
                    error_class="transport",
                    next_retry_seconds=delay,
                )
            return True
        except (OSError, ValueError):
            self.store.finish_callback(dispatch_id, "rejected")
            return True
        self.store.finish_callback(dispatch_id, "acknowledged")
        self.store.record_callback_health(success=True, http_status=200, error_class=None)
        self._cleanup(dispatch_id)
        return True


class GpuExecutionCoordinator:
    def __init__(
        self,
        store: GpuDispatchStore,
        api: HttpWorkerApi,
        identity: WorkerIdentity,
        *,
        compute_lock: asyncio.Lock,
        heartbeat_seconds: float = 15,
    ) -> None:
        self.store = store
        self.api = api
        self.identity = identity
        self.compute_lock = compute_lock
        self.stop_event = threading.Event()
        self.job_runner = GpuJobRunner(
            store,
            api,
            identity,
            heartbeat_seconds=heartbeat_seconds,
            stop_event=self.stop_event,
        )
        self.outbox_sender = GpuOutboxSender(store, api, identity)
        self.heartbeat_seconds = heartbeat_seconds
        self._tasks: list[asyncio.Task[None]] = []

    def _heartbeat_payload(self) -> dict[str, Any]:
        state = self.store.status()
        current = state.get("current_dispatch")
        current_state = str(current["state"]) if current else None
        worker_status = "busy" if current_state in {
            "running",
            "outbox_pending",
            "callback_uploading",
        } else "idle"
        return {
            "protocol_version": CALLBACK_PROTOCOL_VERSION,
            "worker_id": self.identity.worker_id,
            "boot_id": self.identity.boot_id,
            "build_id": self.identity.build_id,
            "supported_kinds": list(JOB_KINDS),
            "status": worker_status,
            "current_job_id": current["job_id"] if current else None,
            "details": {"dispatch_protocol_version": "2.0", "mode": "push"},
        }

    async def _execution_loop(self) -> None:
        while not self.stop_event.is_set():
            processed = False
            if not self.store.callback_auth_blocked():
                try:
                    async with self.compute_lock:
                        processed = await asyncio.to_thread(self.job_runner.run_once)
                except Exception as exc:  # noqa: BLE001 - keep supervisor alive.
                    LOGGER.error("GPU execution loop error: %s", type(exc).__name__)
            await asyncio.sleep(0 if processed else 0.75)

    async def _outbox_loop(self) -> None:
        while not self.stop_event.is_set():
            processed = False
            if not self.store.callback_auth_blocked():
                try:
                    processed = await asyncio.to_thread(self.outbox_sender.run_once)
                except Exception as exc:  # noqa: BLE001 - keep supervisor alive.
                    LOGGER.error("GPU outbox loop error: %s", type(exc).__name__)
            await asyncio.sleep(0 if processed else 0.75)

    async def _worker_heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.store.callback_auth_blocked():
                try:
                    await asyncio.to_thread(
                        self.api.worker_heartbeat, self._heartbeat_payload()
                    )
                    self.store.record_callback_health(
                        success=True, http_status=200, error_class=None
                    )
                except WorkerTransportError as exc:
                    self.store.record_callback_health(
                        success=False,
                        http_status=exc.status_code,
                        error_class=(
                            "authentication_failed"
                            if exc.status_code == 401
                            else "transport"
                        ),
                        next_retry_seconds=(
                            None if exc.status_code == 401 else round(self.heartbeat_seconds)
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - keep supervisor alive.
                    LOGGER.error("GPU heartbeat loop error: %s", type(exc).__name__)
                    self.store.record_callback_health(
                        success=False,
                        http_status=None,
                        error_class=type(exc).__name__[:120],
                        next_retry_seconds=round(self.heartbeat_seconds),
                    )
            await asyncio.sleep(self.heartbeat_seconds)

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._execution_loop()),
            asyncio.create_task(self._outbox_loop()),
            asyncio.create_task(self._worker_heartbeat_loop()),
        ]

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()


def default_worker_identity(build_id: str, boot_id: str) -> WorkerIdentity:
    return WorkerIdentity(
        worker_id=os.environ.get("MOSHI_WORKER_ID", socket.gethostname()),
        boot_id=os.environ.get("MOSHI_WORKER_BOOT_ID", boot_id),
        build_id=build_id,
    )
