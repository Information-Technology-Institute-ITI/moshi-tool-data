from __future__ import annotations

import io
import json
import urllib.error

import pytest

from moshi_data_pipeline.gpu_callback import (
    GpuCallbackTransportError,
    HttpGpuCallbackApi,
)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes = b"{}", headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_gpu_service_heartbeat_uses_v1_compatibility_route(monkeypatch) -> None:
    captured = {}

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(b'{"compatible":true}')

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = HttpGpuCallbackApi("http://web.internal", "secret", timeout_seconds=12)
    result = client.service_heartbeat({"worker_id": "gpu-1"})

    assert result == {"compatible": True}
    assert captured == {
        "url": "http://web.internal/internal/v1/workers/heartbeat",
        "authorization": "Bearer secret",
        "payload": {"worker_id": "gpu-1"},
        "timeout": 12,
    }


def test_gpu_callback_upload_status_uses_protocol_worker_id_query(monkeypatch) -> None:
    captured = {}

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["lease"] = request.get_header("X-lease-token")
        return FakeResponse(headers={"Upload-Offset": "17"})

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = HttpGpuCallbackApi("http://web.internal", "secret")

    assert client.artifact_upload_offset("upload-1", "gpu-1", "lease-token") == 17
    assert captured["url"].endswith("/internal/v1/uploads/upload-1?worker_id=gpu-1")
    assert captured["lease"] == "lease-token"


def test_gpu_callback_preserves_http_status_for_retry_classification(monkeypatch) -> None:
    def open_request(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"detail":"invalid"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = HttpGpuCallbackApi("http://web.internal", "secret")

    with pytest.raises(GpuCallbackTransportError) as raised:
        client.fail("job-1", "lease-token", {"worker_id": "gpu-1"})
    assert raised.value.status_code == 401
