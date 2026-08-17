from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from moshi_data_pipeline.gpu_dispatch_client import (
    MAX_RESPONSE_BYTES,
    GpuDispatchAuthenticationError,
    GpuDispatchBlockedError,
    GpuDispatchClient,
    GpuDispatchConflictError,
    GpuDispatchError,
    GpuDispatchMalformedResponseError,
    GpuDispatchNotFoundError,
    GpuDispatchRateLimitError,
    GpuDispatchRetryableError,
    GpuTransportResponse,
)
from moshi_data_pipeline.gpu_dispatch_protocol import (
    DispatchCreate,
    DispatchStart,
    SelfCheckRequest,
)
from moshi_data_pipeline.gpu_job_protocol import ArtifactRef, JobContext

TOKEN = "dispatch-secret-that-must-never-leak"
ROOT = Path(__file__).parents[1]


class RecordingTransport:
    def __init__(self, *responses: GpuTransportResponse | Exception | object) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GpuTransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected GPU request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _response(
    status: int = 200,
    data: dict[str, Any] | list[Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> GpuTransportResponse:
    value: dict[str, Any] | list[Any] = {"ok": True} if data is None else data
    return GpuTransportResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(value).encode("utf-8"),
    )


def _manifest(content: bytes = b"audio") -> DispatchCreate:
    return DispatchCreate(
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
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": "audio/wav",
                "filename": "canonical.wav",
            }
        ],
    )


def _start(content: bytes = b"audio") -> DispatchStart:
    manifest = _manifest(content)
    item = manifest.inputs[0]
    return DispatchStart(
        context=JobContext(
            job_id=manifest.job_id,
            kind="transcribe",
            attempt=manifest.attempt,
            lease_expires_at="2026-08-16T18:00:00+00:00",
            input_fingerprint=manifest.input_fingerprint,
            payload={},
            preconditions={},
            config={},
            inputs=[
                ArtifactRef(
                    artifact_id=item.artifact_id,
                    role=item.role,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    media_type=item.media_type,
                    filename=item.filename,
                )
            ],
        )
    )


def test_liveness_is_unauthenticated_but_private_routes_use_bearer_token() -> None:
    transport = RecordingTransport(
        _response(data={"status": "alive"}, headers={"X-Service": "gpu"}),
        _response(data={"status": "ready"}),
    )
    client = GpuDispatchClient(
        "http://gpu.internal:8766/",
        TOKEN,
        timeout_seconds=12,
        transport=transport,
    )

    live = client.live()
    ready = client.ready()

    assert live.status == 200
    assert live.data == {"status": "alive"}
    assert live.headers == {"x-service": "gpu"}
    assert ready.data == {"status": "ready"}
    assert transport.calls[0] == {
        "method": "GET",
        "url": "http://gpu.internal:8766/health/live",
        "headers": {},
        "body": None,
        "timeout_seconds": 12.0,
    }
    assert transport.calls[1]["headers"] == {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize(
    "origin",
    (
        "ftp://gpu.internal",
        "http://user:password@gpu.internal",
        "http://gpu.internal/private",
        "http://gpu.internal?token=value",
        "http://gpu.internal/#fragment",
        " http://gpu.internal",
    ),
)
def test_client_rejects_anything_other_than_a_plain_http_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="plain HTTP"):
        GpuDispatchClient(origin, TOKEN, transport=RecordingTransport())


def test_create_self_check_and_start_require_typed_models_and_serialize_them() -> None:
    transport = RecordingTransport(_response(201), _response(202), _response(202))
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    with pytest.raises(TypeError, match="DispatchCreate"):
        client.create_dispatch(_manifest().model_dump())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="SelfCheckRequest"):
        client.trigger_self_check({"trigger": "manual"})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DispatchStart"):
        client.start("dispatch-1", _start().model_dump(), "x" * 40)  # type: ignore[arg-type]
    assert transport.calls == []

    manifest = _manifest()
    self_check = SelfCheckRequest(trigger="job_preflight", force=False)
    start = _start()
    lease_token = "lease-" + "x" * 40

    assert client.create_dispatch(manifest).status == 201
    assert client.trigger_self_check(self_check).status == 202
    assert client.start("dispatch-1", start, lease_token).status == 202

    assert json.loads(transport.calls[0]["body"]) == manifest.model_dump(mode="json")
    assert json.loads(transport.calls[1]["body"]) == self_check.model_dump(mode="json")
    assert json.loads(transport.calls[2]["body"]) == start.model_dump(mode="json")
    assert transport.calls[2]["headers"]["X-Lease-Token"] == lease_token
    assert lease_token not in transport.calls[2]["url"]
    assert lease_token not in transport.calls[2]["body"].decode("utf-8")


def test_head_and_put_use_exact_resumable_upload_headers() -> None:
    transport = RecordingTransport(
        GpuTransportResponse(
            status=200,
            headers={"X-Accepted-Offset": "4", "X-Input-State": "open"},
            body=b"",
        ),
        _response(data={"accepted_offset": 8, "state": "verified"}),
    )
    client = GpuDispatchClient(
        "http://gpu.internal:8766",
        TOKEN,
        upload_chunk_bytes=4,
        transport=transport,
    )

    head = client.head_input("dispatch:1", "input:1")
    uploaded = client.put_input(
        "dispatch:1",
        "input:1",
        body=b"5678",
        start=4,
        end=7,
        total=8,
    )

    expected_path = "/internal/v2/dispatches/dispatch%3A1/inputs/input%3A1"
    assert head.data is None
    assert head.headers["x-accepted-offset"] == "4"
    assert transport.calls[0]["method"] == "HEAD"
    assert transport.calls[0]["url"].endswith(expected_path)
    assert uploaded.data == {"accepted_offset": 8, "state": "verified"}
    assert transport.calls[1]["method"] == "PUT"
    assert transport.calls[1]["url"].endswith(expected_path)
    assert transport.calls[1]["body"] == b"5678"
    assert transport.calls[1]["headers"] == {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
        "Content-Range": "bytes 4-7/8",
    }

    with pytest.raises(ValueError, match="chunk size"):
        client.put_input(
            "dispatch-1",
            "input-1",
            body=b"12345",
            start=0,
            end=4,
            total=5,
        )
    with pytest.raises(ValueError, match="do not agree"):
        client.put_input(
            "dispatch-1",
            "input-1",
            body=b"1234",
            start=0,
            end=2,
            total=4,
        )


def test_remaining_endpoint_methods_use_exact_paths() -> None:
    transport = RecordingTransport(*(_response() for _ in range(4)))
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    client.status()
    client.self_checks(limit=20)
    client.get_dispatch("dispatch:1")
    client.cancel("dispatch:1")

    assert [(call["method"], call["url"]) for call in transport.calls] == [
        ("GET", "http://gpu.internal:8766/internal/v2/status"),
        ("GET", "http://gpu.internal:8766/internal/v2/self-checks?limit=20"),
        ("GET", "http://gpu.internal:8766/internal/v2/dispatches/dispatch%3A1"),
        (
            "POST",
            "http://gpu.internal:8766/internal/v2/dispatches/dispatch%3A1/cancel",
        ),
    ]
    assert transport.calls[-1]["body"] is None


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    (
        (401, GpuDispatchAuthenticationError, False),
        (404, GpuDispatchNotFoundError, False),
        (409, GpuDispatchConflictError, False),
        (400, GpuDispatchBlockedError, False),
        (422, GpuDispatchBlockedError, False),
        (507, GpuDispatchBlockedError, False),
        (500, GpuDispatchRetryableError, True),
        (503, GpuDispatchRetryableError, True),
    ),
)
def test_http_failures_have_explicit_classification(
    status: int,
    error_type: type[GpuDispatchError],
    retryable: bool,
) -> None:
    transport = RecordingTransport(
        _response(
            status,
            data={"detail": f"server echoed {TOKEN}"},
            headers={"X-Echoed-Authorization": f"Bearer {TOKEN}"},
        )
    )
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    with pytest.raises(error_type) as captured:
        client.ready()

    error = captured.value
    assert error.status == status
    assert error.retryable is retryable
    assert TOKEN not in str(error)
    assert "Authorization" not in str(error)


def test_rate_limit_exposes_retry_after_without_exposing_response_content() -> None:
    transport = RecordingTransport(
        _response(
            429,
            data={"detail": TOKEN},
            headers={"Retry-After": "17", "X-Secret": TOKEN},
        )
    )
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    with pytest.raises(GpuDispatchRateLimitError) as captured:
        client.trigger_self_check(SelfCheckRequest(trigger="manual"))

    assert captured.value.retry_after == 17
    assert captured.value.retryable is True
    assert TOKEN not in str(captured.value)


def test_transport_failure_is_sanitized_and_retryable() -> None:
    transport = RecordingTransport(OSError(f"socket failed with {TOKEN}"))
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    with pytest.raises(GpuDispatchRetryableError) as captured:
        client.status()

    assert captured.value.status is None
    assert captured.value.retryable is True
    assert TOKEN not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "response",
    (
        GpuTransportResponse(200, {}, b"not-json"),
        GpuTransportResponse(200, {}, b"[]"),
        GpuTransportResponse(200, {}, b"x" * (MAX_RESPONSE_BYTES + 1)),
        object(),
    ),
)
def test_malformed_success_responses_are_protocol_errors(response: object) -> None:
    transport = RecordingTransport(response)
    client = GpuDispatchClient("http://gpu.internal:8766", TOKEN, transport=transport)

    with pytest.raises(GpuDispatchMalformedResponseError) as captured:
        client.ready()

    assert captured.value.retryable is False
    assert TOKEN not in str(captured.value)
