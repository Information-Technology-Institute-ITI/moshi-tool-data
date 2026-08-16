from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from moshi_data_pipeline.studio.execution_runtime import ExecutionOutput
from moshi_data_pipeline.studio.protocol import (
    JOB_KINDS,
    WORKER_PROTOCOL_VERSION,
    ArtifactRef,
    JobContext,
    ProducedArtifact,
)


class WorkerTransportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JobExecutor(Protocol):
    def execute(
        self,
        context: JobContext,
        inputs: dict[str, Path],
        progress: Any,
    ) -> ExecutionOutput: ...


class WorkerApi(Protocol):
    def worker_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def job_heartbeat(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def download_artifact(self, artifact: ArtifactRef, destination: Path) -> Path: ...

    def upload_artifact(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        role: str,
        path: Path,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> ProducedArtifact: ...

    def complete(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def fail(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HttpWorkerApi:
    """Small standard-library HTTP client for the private worker API."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 60,
        upload_chunk_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.upload_chunk_bytes = upload_chunk_bytes

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        lease_token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            **(headers or {}),
        }
        data = body
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if lease_token is not None:
            request_headers["X-Lease-Token"] = lease_token
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return (
                    int(response.status),
                    {key.casefold(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read(4_096).decode("utf-8", errors="replace")
            raise WorkerTransportError(
                f"Worker API {method} {path} returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise WorkerTransportError(
                f"Worker API {method} {path} is unavailable: {type(exc).__name__}"
            ) from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        _, _, body = self._request(
            method,
            path,
            payload=payload,
            lease_token=lease_token,
        )
        try:
            value = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise WorkerTransportError("Worker API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise WorkerTransportError("Worker API returned a non-object response")
        return value

    def worker_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/internal/v1/workers/heartbeat", payload=payload)

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/internal/v1/jobs/claim", payload=payload)

    def job_heartbeat(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/internal/v1/jobs/{urllib.parse.quote(job_id)}/heartbeat",
            payload=payload,
            lease_token=lease_token,
        )

    def download_artifact(self, artifact: ArtifactRef, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset > artifact.size_bytes:
            temporary.unlink()
            offset = 0
        if offset == artifact.size_bytes and sha256_file(temporary) == artifact.sha256:
            os.replace(temporary, destination)
            return destination
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        if offset:
            headers.update(
                {
                    "Range": f"bytes={offset}-",
                    "If-Range": f'"sha256-{artifact.sha256}"',
                }
            )
        request = urllib.request.Request(
            f"{self.base_url}/internal/v1/artifacts/"
            f"{urllib.parse.quote(artifact.artifact_id)}/content",
            headers=headers,
            method="GET",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
            with response:
                status = int(response.status)
                if status == 206 and offset:
                    mode = "ab"
                elif status == 200:
                    mode = "wb"
                else:
                    raise WorkerTransportError(
                        f"Unexpected artifact response status: {status}"
                    )
                with temporary.open(mode) as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        except urllib.error.HTTPError as exc:
            detail = exc.read(4_096).decode("utf-8", errors="replace")
            raise WorkerTransportError(
                f"Artifact download returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise WorkerTransportError(
                f"Artifact download was interrupted: {type(exc).__name__}"
            ) from exc
        if temporary.stat().st_size != artifact.size_bytes:
            raise WorkerTransportError("Artifact download did not reach expected size")
        if sha256_file(temporary) != artifact.sha256:
            temporary.unlink(missing_ok=True)
            raise WorkerTransportError("Artifact download checksum does not match")
        os.replace(temporary, destination)
        return destination

    def upload_artifact(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        role: str,
        path: Path,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> ProducedArtifact:
        upload = self.create_artifact_upload(
            job_id,
            worker_id,
            lease_token,
            role=role,
            filename=path.name,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        upload_id = str(upload["id"])
        if size_bytes:
            offset = self.artifact_upload_offset(upload_id, worker_id, lease_token)
            with path.open("rb") as stream:
                stream.seek(offset)
                while offset < size_bytes:
                    chunk = stream.read(min(self.upload_chunk_bytes, size_bytes - offset))
                    if not chunk:
                        raise WorkerTransportError("Output file ended during upload")
                    end = offset + len(chunk) - 1
                    self.append_artifact_upload(
                        upload_id,
                        worker_id,
                        lease_token,
                        body=chunk,
                        start=offset,
                        end=end,
                        total=size_bytes,
                    )
                    offset = end + 1
        return ProducedArtifact(
            upload_id=upload_id,
            role=role,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )

    def create_artifact_upload(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        role: str,
        filename: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/internal/v1/jobs/{urllib.parse.quote(job_id)}/uploads",
            payload={
                "worker_id": worker_id,
                "role": role,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "media_type": media_type,
                "filename": filename,
            },
            lease_token=lease_token,
        )

    def artifact_upload_offset(
        self, upload_id: str, worker_id: str, lease_token: str
    ) -> int:
        query = urllib.parse.urlencode({"worker_id": worker_id})
        _, headers, _ = self._request(
            "HEAD",
            f"/internal/v1/uploads/{urllib.parse.quote(upload_id)}?{query}",
            lease_token=lease_token,
        )
        return int(headers.get("upload-offset", "0"))

    def append_artifact_upload(
        self,
        upload_id: str,
        worker_id: str,
        lease_token: str,
        *,
        body: bytes,
        start: int,
        end: int,
        total: int,
    ) -> None:
        query = urllib.parse.urlencode({"worker_id": worker_id})
        self._request(
            "PUT",
            f"/internal/v1/uploads/{urllib.parse.quote(upload_id)}?{query}",
            body=body,
            lease_token=lease_token,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
        )

    def complete(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/internal/v1/jobs/{urllib.parse.quote(job_id)}/complete",
            payload=payload,
            lease_token=lease_token,
        )

    def fail(
        self,
        job_id: str,
        lease_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/internal/v1/jobs/{urllib.parse.quote(job_id)}/fail",
            payload=payload,
            lease_token=lease_token,
        )


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    boot_id: str
    build_id: str


class RemoteWorker:
    def __init__(
        self,
        api: WorkerApi,
        executor_factory: Any,
        cache_root: Path,
        identity: WorkerIdentity,
        *,
        heartbeat_seconds: float = 15,
    ) -> None:
        self.api = api
        self.executor_factory = executor_factory
        self.cache_root = cache_root.resolve()
        self.input_cache = self.cache_root / "inputs"
        self.attempts = self.cache_root / "attempts"
        self.input_cache.mkdir(parents=True, exist_ok=True)
        self.attempts.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.heartbeat_seconds = heartbeat_seconds

    def _identity_payload(self, status: str, job_id: str | None = None) -> dict[str, Any]:
        return {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "worker_id": self.identity.worker_id,
            "boot_id": self.identity.boot_id,
            "build_id": self.identity.build_id,
            "supported_kinds": list(JOB_KINDS),
            "status": status,
            "current_job_id": job_id,
            "details": {},
        }

    def _cached_input(self, artifact: ArtifactRef) -> Path:
        destination = self.input_cache / artifact.sha256
        if destination.is_file():
            if (
                destination.stat().st_size == artifact.size_bytes
                and sha256_file(destination) == artifact.sha256
            ):
                return destination
            destination.unlink()
        return self.api.download_artifact(artifact, destination)

    @staticmethod
    def _failure(exc: Exception) -> tuple[str, bool, str]:
        message = str(exc)[:4_000] or type(exc).__name__
        lowered = message.casefold()
        if isinstance(exc, MemoryError) or "out of memory" in lowered:
            return "gpu_oom", False, message
        if isinstance(exc, (WorkerTransportError, OSError)):
            return "transport", True, message
        if isinstance(exc, ValueError):
            return "invalid_input", False, message
        return "application", False, message

    def run_once(self) -> bool:
        self.api.worker_heartbeat(self._identity_payload("ready"))
        claimed = self.api.claim(
            {
                key: value
                for key, value in self._identity_payload("ready").items()
                if key not in {"status", "current_job_id", "details"}
            }
        )
        if claimed.get("job") is None:
            self.api.worker_heartbeat(self._identity_payload("idle"))
            return False
        context = JobContext.model_validate(claimed["job"])
        lease_token = str(claimed["lease_token"])
        job_id = context.job_id
        self.api.worker_heartbeat(self._identity_payload("busy", job_id))
        progress_state: dict[str, Any] = {"progress": 0.0, "message": "Starting"}
        stop_heartbeat = threading.Event()
        heartbeat_error: list[Exception] = []

        def heartbeat_loop() -> None:
            while not stop_heartbeat.wait(self.heartbeat_seconds):
                try:
                    response = self.api.job_heartbeat(
                        job_id,
                        lease_token,
                        {
                            "worker_id": self.identity.worker_id,
                            **progress_state,
                        },
                    )
                    if response.get("cancel_requested"):
                        heartbeat_error.append(RuntimeError("Job inputs were superseded"))
                        stop_heartbeat.set()
                except Exception as exc:  # noqa: BLE001 - propagated on the main thread.
                    heartbeat_error.append(exc)
                    stop_heartbeat.set()

        def progress(value: float, message: str) -> None:
            progress_state["progress"] = max(0.0, min(1.0, value))
            progress_state["message"] = message[:1_000]
            if heartbeat_error:
                raise heartbeat_error[0]

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name=f"lease-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat.start()
        attempt_root = self.attempts / f"{job_id}_{context.attempt}_{uuid4().hex}"
        attempt_root.mkdir(parents=True, exist_ok=False)
        try:
            inputs = {
                artifact.artifact_id: self._cached_input(artifact)
                for artifact in context.inputs
            }
            executor: JobExecutor = self.executor_factory(attempt_root)
            output = executor.execute(context, inputs, progress)
            if heartbeat_error:
                raise heartbeat_error[0]
            produced: list[ProducedArtifact] = []
            for artifact in output.artifacts:
                path = artifact.path.resolve()
                if attempt_root.resolve() not in path.parents:
                    raise ValueError("Executor produced a file outside its attempt directory")
                digest = sha256_file(path)
                produced.append(
                    self.api.upload_artifact(
                        job_id,
                        self.identity.worker_id,
                        lease_token,
                        role=artifact.role,
                        path=path,
                        sha256=digest,
                        size_bytes=path.stat().st_size,
                        media_type=artifact.media_type,
                    )
                )
            self.api.complete(
                job_id,
                lease_token,
                {
                    "worker_id": self.identity.worker_id,
                    "input_fingerprint": context.input_fingerprint,
                    "kind": context.kind,
                    "result": output.result,
                    "artifacts": [item.model_dump(mode="json") for item in produced],
                },
            )
            return True
        except Exception as exc:
            failure_class, retryable, message = self._failure(exc)
            with suppress(Exception):
                self.api.fail(
                    job_id,
                    lease_token,
                    {
                        "worker_id": self.identity.worker_id,
                        "failure_class": failure_class,
                        "error": message,
                        "retryable": retryable,
                    },
                )
            return True
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_seconds + 1))
            if attempt_root.exists():
                shutil.rmtree(attempt_root)
