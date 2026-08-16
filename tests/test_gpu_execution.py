from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from moshi_data_pipeline.gpu_callback import GpuCallbackTransportError, GpuServiceIdentity
from moshi_data_pipeline.gpu_dispatch_protocol import DispatchCreate, DispatchStart
from moshi_data_pipeline.gpu_dispatch_state import GpuDispatchStore
from moshi_data_pipeline.gpu_execution import GpuJobRunner, GpuOutboxSender
from moshi_data_pipeline.gpu_job_protocol import ArtifactRef, JobContext
from moshi_data_pipeline.studio.execution_runtime import ExecutionOutput, ProducedFile


class FakeCallbackApi:
    upload_chunk_bytes = 4

    def __init__(self) -> None:
        self.heartbeats: list[dict] = []
        self.uploads: dict[str, bytearray] = {}
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    def job_heartbeat(self, job_id, lease_token, payload):
        self.heartbeats.append(payload)
        return {"cancel_requested": False}

    def create_artifact_upload(
        self,
        job_id,
        gpu_service_id,
        lease_token,
        *,
        role,
        filename,
        sha256,
        size_bytes,
        media_type,
    ):
        identifier = f"upload-{len(self.uploads) + 1}"
        self.uploads[identifier] = bytearray()
        return {"id": identifier}

    def artifact_upload_offset(self, upload_id, gpu_service_id, lease_token):
        return len(self.uploads[upload_id])

    def append_artifact_upload(
        self,
        upload_id,
        gpu_service_id,
        lease_token,
        *,
        body,
        start,
        end,
        total,
    ):
        assert start == len(self.uploads[upload_id])
        assert end == start + len(body) - 1
        self.uploads[upload_id].extend(body)

    def complete(self, job_id, lease_token, payload):
        self.completed.append(payload)
        return {"status": "complete"}

    def fail(self, job_id, lease_token, payload):
        self.failed.append(payload)
        return {"status": "failed"}


class FakeExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root

    def execute(self, context, inputs, progress):
        assert inputs["input-1"].read_bytes() == b"input audio"
        progress(0.5, "Fake inference")
        output = self.root / "result.json"
        output.write_bytes(b'{"ok":true}')
        return ExecutionOutput(
            result={
                "kind": "transcribe",
                "source_id": "source-1",
                "expected_annotation_version": 1,
                "annotation": {},
            },
            artifacts=[
                ProducedFile(
                    role="analysis.raw_transcript",
                    path=output,
                    media_type="application/json",
                )
            ],
        )


class FailingExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root

    def execute(self, context, inputs, progress):
        raise RuntimeError("CUDA out of memory")


def _prepare_dispatch(tmp_path) -> tuple[GpuDispatchStore, bytes]:
    content = b"input audio"
    digest = hashlib.sha256(content).hexdigest()
    store = GpuDispatchStore(tmp_path / "cache", min_free_bytes=1)
    payload = DispatchCreate(
        dispatch_id="dispatch-1",
        job_id="job-1",
        attempt=1,
        protocol_version="2.0",
        required_build_id="build-a",
        input_fingerprint="a" * 64,
        inputs=[
            {
                "artifact_id": "input-1",
                "role": "source.canonical",
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": "audio/wav",
                "filename": "canonical.wav",
            }
        ],
    )
    store.create_dispatch(payload)

    async def chunks():
        yield content

    asyncio.run(
        store.append_input(
            "dispatch-1",
            "input-1",
            f"bytes 0-{len(content) - 1}/{len(content)}",
            chunks(),
        )
    )
    context = JobContext(
        job_id="job-1",
        kind="transcribe",
        attempt=1,
        lease_expires_at="2026-08-16T18:00:00+00:00",
        input_fingerprint="a" * 64,
        payload={},
        preconditions={
            "source": {"id": "source-1"},
            "annotation": {"version": 1},
        },
        config={},
        inputs=[
            ArtifactRef(
                artifact_id="input-1",
                role="source.canonical",
                sha256=digest,
                size_bytes=len(content),
                media_type="audio/wav",
                filename="canonical.wav",
            )
        ],
    )
    store.start_dispatch("dispatch-1", DispatchStart(context=context), "lease-" + "x" * 40)
    return store, content


def test_pushed_job_executes_then_persists_uploads_and_completion(tmp_path) -> None:
    store, _ = _prepare_dispatch(tmp_path)
    api = FakeCallbackApi()
    identity = GpuServiceIdentity("gpu-1", "boot-1", "build-a")
    runner = GpuJobRunner(
        store,
        api,
        identity,
        executor_factory=FakeExecutor,
        heartbeat_seconds=3600,
    )
    assert runner.run_once() is True
    pending = store.get_dispatch("dispatch-1")
    assert pending["state"] == "outbox_pending"
    assert pending["progress"] == 1.0
    assert store.status()["safe_to_stop"] is False

    sender = GpuOutboxSender(store, api, identity)
    assert sender.run_once() is True
    finished = store.get_dispatch("dispatch-1")
    assert finished["state"] == "acknowledged"
    assert store.status()["safe_to_stop"] is True
    assert bytes(api.uploads["upload-1"]) == b'{"ok":true}'
    assert api.completed[0]["worker_id"] == "gpu-1"
    assert api.completed[0]["artifacts"] == [
        {
            "upload_id": "upload-1",
            "role": "analysis.raw_transcript",
            "sha256": hashlib.sha256(b'{"ok":true}').hexdigest(),
            "size_bytes": 11,
            "media_type": "application/json",
        }
    ]
    assert not (store.outbox_root / "dispatch-1").exists()


def test_lease_401_blocks_execution_without_losing_dispatch(tmp_path) -> None:
    store, _ = _prepare_dispatch(tmp_path)

    class UnauthorizedApi(FakeCallbackApi):
        def job_heartbeat(self, job_id, lease_token, payload):
            raise GpuCallbackTransportError("unauthorized", status_code=401)

    runner = GpuJobRunner(
        store,
        UnauthorizedApi(),
        GpuServiceIdentity("gpu-1", "boot-1", "build-a"),
        executor_factory=FakeExecutor,
    )
    assert runner.run_once() is True
    record = store.get_dispatch("dispatch-1")
    assert record["state"] == "auth_blocked"
    assert record["callback_http_status"] == 401
    assert store.callback_auth_blocked() is True


def test_interrupted_running_dispatch_is_requeued_on_store_restart(tmp_path) -> None:
    store, _ = _prepare_dispatch(tmp_path)
    claimed = store.claim_queued_dispatch()
    assert claimed is not None
    assert store.get_dispatch("dispatch-1")["state"] == "running"

    recovered = GpuDispatchStore(store.cache_root, min_free_bytes=1)
    record = recovered.get_dispatch("dispatch-1")
    assert record["state"] == "queued"
    assert record["last_error"] == "GPU service restarted during execution"


def test_execution_failure_is_delivered_through_durable_failure_callback(tmp_path) -> None:
    store, _ = _prepare_dispatch(tmp_path)
    api = FakeCallbackApi()
    identity = GpuServiceIdentity("gpu-1", "boot-1", "build-a")
    runner = GpuJobRunner(
        store,
        api,
        identity,
        executor_factory=FailingExecutor,
        heartbeat_seconds=3600,
    )
    assert runner.run_once() is True
    assert store.get_dispatch("dispatch-1")["state"] == "outbox_pending"
    assert GpuOutboxSender(store, api, identity).run_once() is True
    assert store.get_dispatch("dispatch-1")["state"] == "acknowledged"
    assert api.failed == [
        {
            "worker_id": "gpu-1",
            "failure_class": "gpu_oom",
            "error": "CUDA out of memory",
            "retryable": False,
        }
    ]


def test_ambiguous_completion_retry_reuses_persisted_upload(tmp_path) -> None:
    store, _ = _prepare_dispatch(tmp_path)

    class FlakyApi(FakeCallbackApi):
        def __init__(self) -> None:
            super().__init__()
            self.fail_completion = True

        def complete(self, job_id, lease_token, payload):
            if self.fail_completion:
                raise GpuCallbackTransportError("response was lost")
            return super().complete(job_id, lease_token, payload)

    api = FlakyApi()
    identity = GpuServiceIdentity("gpu-1", "boot-1", "build-a")
    runner = GpuJobRunner(
        store,
        api,
        identity,
        executor_factory=FakeExecutor,
        heartbeat_seconds=3600,
    )
    assert runner.run_once() is True
    sender = GpuOutboxSender(store, api, identity)
    assert sender.run_once() is True
    assert store.get_dispatch("dispatch-1")["state"] == "outbox_pending"
    assert list(api.uploads) == ["upload-1"]

    with store.connect() as connection:
        connection.execute("UPDATE dispatches SET next_callback_at=NULL WHERE id='dispatch-1'")
    api.fail_completion = False
    assert sender.run_once() is True
    assert store.get_dispatch("dispatch-1")["state"] == "acknowledged"
    assert list(api.uploads) == ["upload-1"]
    assert len(api.completed) == 1
