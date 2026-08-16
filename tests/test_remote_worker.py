from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from moshi_data_pipeline.remote_worker import HttpWorkerApi, RemoteWorker, WorkerIdentity
from moshi_data_pipeline.studio.execution_runtime import ExecutionOutput, ProducedFile
from moshi_data_pipeline.studio.protocol import ArtifactRef, JobContext, ProducedArtifact


class FakeApi:
    def __init__(self, contexts: list[JobContext], input_content: bytes = b"input") -> None:
        self.contexts = list(contexts)
        self.input_content = input_content
        self.heartbeats: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.downloads = 0
        self.uploads = 0

    def worker_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.heartbeats.append(payload)
        return payload

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.contexts:
            return {"job": None, "lease_token": None}
        context = self.contexts.pop(0)
        return {
            "job": context.model_dump(mode="json"),
            "lease_token": f"lease-{'x' * 40}",
        }

    def job_heartbeat(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {"cancel_requested": False}

    def download_artifact(self, artifact: ArtifactRef, destination: Path) -> Path:
        self.downloads += 1
        destination.write_bytes(self.input_content)
        return destination

    def upload_artifact(self, *args: Any, **values: Any) -> ProducedArtifact:
        self.uploads += 1
        return ProducedArtifact(
            upload_id=f"upload_{self.uploads}",
            role=values["role"],
            sha256=values["sha256"],
            size_bytes=values["size_bytes"],
            media_type=values["media_type"],
        )

    def complete(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.completed.append(payload)
        return {"status": "complete"}

    def fail(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.failed.append(payload)
        return {"status": "failed"}


class FakeExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root

    def execute(self, context, inputs, progress) -> ExecutionOutput:
        assert all(path.is_file() for path in inputs.values())
        progress(0.5, "Deterministic fake")
        output = self.root / "result.json"
        output.write_text("{}", encoding="utf-8")
        return ExecutionOutput(
            result={"fixture": True},
            artifacts=[
                ProducedFile(
                    role="analysis.fixture",
                    path=output,
                    media_type="application/json",
                )
            ],
        )


class OomExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root

    def execute(self, context, inputs, progress) -> ExecutionOutput:
        raise RuntimeError("CUDA out of memory")


def _context(job_id: str, content: bytes = b"input") -> JobContext:
    digest = hashlib.sha256(content).hexdigest()
    return JobContext(
        job_id=job_id,
        kind="transcribe",
        attempt=1,
        lease_expires_at="2026-08-13T10:02:00+00:00",
        input_fingerprint="a" * 64,
        payload={},
        preconditions={},
        config={},
        inputs=[
            ArtifactRef(
                artifact_id="artifact_input",
                role="source.canonical",
                sha256=digest,
                size_bytes=len(content),
                media_type="audio/wav",
                filename="canonical.wav",
            )
        ],
    )


def test_remote_worker_caches_inputs_uploads_outputs_and_completes(tmp_path) -> None:
    content = b"input"
    api = FakeApi([_context("job_one", content), _context("job_two", content)], content)
    worker = RemoteWorker(
        api,
        FakeExecutor,
        tmp_path / "cache",
        WorkerIdentity("worker-1", "boot-1", "build-a"),
        heartbeat_seconds=0.01,
    )
    assert worker.run_once() is True
    assert worker.run_once() is True
    assert api.downloads == 1
    assert api.uploads == 2
    assert len(api.completed) == 2
    assert api.completed[0]["artifacts"][0]["role"] == "analysis.fixture"
    assert worker.run_once() is False
    assert api.heartbeats[-1]["status"] == "idle"


def test_remote_worker_classifies_gpu_oom_as_non_retryable(tmp_path) -> None:
    api = FakeApi([_context("job_oom")])
    worker = RemoteWorker(
        api,
        OomExecutor,
        tmp_path / "cache",
        WorkerIdentity("worker-1", "boot-1", "build-a"),
    )
    assert worker.run_once() is True
    assert not api.completed
    assert api.failed == [
        {
            "worker_id": "worker-1",
            "failure_class": "gpu_oom",
            "error": "CUDA out of memory",
            "retryable": False,
        }
    ]


def test_http_worker_download_streams_and_resumes_with_if_range(
    tmp_path, monkeypatch
) -> None:
    content = b"streamed artifact content"
    artifact = ArtifactRef(
        artifact_id="artifact_stream",
        role="source.canonical",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="audio/wav",
        filename="canonical.wav",
    )
    destination = tmp_path / "cache" / artifact.sha256
    destination.parent.mkdir()
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.write_bytes(content[:8])
    captured = {}

    class Response:
        status = 206

        def __init__(self) -> None:
            self.remaining = content[8:]

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, size: int) -> bytes:
            captured.setdefault("read_sizes", []).append(size)
            value, self.remaining = self.remaining[:size], self.remaining[size:]
            return value

    def open_request(request, timeout):
        captured["range"] = request.get_header("Range")
        captured["if_range"] = request.get_header("If-range")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = HttpWorkerApi("http://web:8765", "secret", timeout_seconds=12)
    assert client.download_artifact(artifact, destination) == destination
    assert destination.read_bytes() == content
    assert captured["range"] == "bytes=8-"
    assert captured["if_range"] == f'"sha256-{artifact.sha256}"'
    assert captured["read_sizes"][-1] == 1024 * 1024
