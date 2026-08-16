from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GpuCallbackTransportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HttpGpuCallbackApi:
    """HTTP client used by the GPU service to return progress and results to m8i."""

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
            raise GpuCallbackTransportError(
                f"GPU callback API {method} {path} returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise GpuCallbackTransportError(
                f"GPU callback API {method} {path} is unavailable: {type(exc).__name__}"
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
            raise GpuCallbackTransportError("GPU callback API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise GpuCallbackTransportError("GPU callback API returned a non-object response")
        return value

    def service_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Protocol 1.0 retains this route name for wire compatibility.
        return self._json("POST", "/internal/v1/workers/heartbeat", payload=payload)

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

    def create_artifact_upload(
        self,
        job_id: str,
        gpu_service_id: str,
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
                "worker_id": gpu_service_id,
                "role": role,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "media_type": media_type,
                "filename": filename,
            },
            lease_token=lease_token,
        )

    def artifact_upload_offset(
        self,
        upload_id: str,
        gpu_service_id: str,
        lease_token: str,
    ) -> int:
        query = urllib.parse.urlencode({"worker_id": gpu_service_id})
        _, headers, _ = self._request(
            "HEAD",
            f"/internal/v1/uploads/{urllib.parse.quote(upload_id)}?{query}",
            lease_token=lease_token,
        )
        return int(headers.get("upload-offset", "0"))

    def append_artifact_upload(
        self,
        upload_id: str,
        gpu_service_id: str,
        lease_token: str,
        *,
        body: bytes,
        start: int,
        end: int,
        total: int,
    ) -> None:
        query = urllib.parse.urlencode({"worker_id": gpu_service_id})
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
class GpuServiceIdentity:
    service_id: str
    boot_id: str
    build_id: str
