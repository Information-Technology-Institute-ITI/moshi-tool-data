from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from moshi_data_pipeline.gpu_dispatch_protocol import (
    DispatchCreate,
    DispatchStart,
    SelfCheckRequest,
)

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 300.0
MAX_UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class GpuTransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class GpuDispatchResponse:
    status: int
    data: dict[str, Any] | None
    headers: dict[str, str]


class GpuDispatchTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GpuTransportResponse: ...


class GpuDispatchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.status_code = status
        self.retryable = retryable
        self.method = method
        self.path = path


class GpuDispatchAuthenticationError(GpuDispatchError):
    """The dispatch credential was rejected and must not be retried."""


class GpuDispatchConflictError(GpuDispatchError):
    """The request conflicted with durable GPU state and needs reconciliation."""


class GpuDispatchRateLimitError(GpuDispatchError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None,
        status: int,
        method: str,
        path: str,
    ) -> None:
        super().__init__(
            message,
            status=status,
            retryable=True,
            method=method,
            path=path,
        )
        self.retry_after = retry_after


class GpuDispatchBlockedError(GpuDispatchError):
    """The request is invalid or the GPU requires operator/configuration action."""


class GpuDispatchNotFoundError(GpuDispatchError):
    """Durable receipt state was not found; the coordinator must reconcile."""


class GpuDispatchRetryableError(GpuDispatchError):
    """A transport failure or server failure may be retried with bounded backoff."""


class GpuDispatchMalformedResponseError(GpuDispatchError):
    """A successful response did not satisfy the GPU protocol envelope."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class UrllibGpuDispatchTransport:
    """Direct standard-library transport that refuses credential-bearing redirects."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    @staticmethod
    def _body(stream: Any) -> bytes:
        return stream.read(MAX_RESPONSE_BYTES + 1)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GpuTransportResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return GpuTransportResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._body(response),
                )
        except urllib.error.HTTPError as exc:
            try:
                return GpuTransportResponse(
                    status=int(exc.code),
                    headers=dict(exc.headers.items()) if exc.headers else {},
                    body=self._body(exc),
                )
            finally:
                exc.close()


def _plain_origin(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("GPU intake URL must be a plain HTTP(S) origin")
    if any(character.isspace() for character in value):
        raise ValueError("GPU intake URL must be a plain HTTP(S) origin")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GPU intake URL must be a plain HTTP(S) origin")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("GPU intake URL must contain a valid port") from exc
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GPU intake URL must be a plain HTTP(S) origin")
    return f"{parsed.scheme.casefold()}://{parsed.netloc}"


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return max(0, round((timestamp - datetime.now(UTC)).total_seconds()))


class GpuDispatchClient:
    """Typed protocol-2.0 client; retry and reconciliation policy stays with its caller."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 30.0,
        upload_chunk_bytes: int = 8 * 1024 * 1024,
        transport: GpuDispatchTransport | None = None,
    ) -> None:
        if not isinstance(bearer_token, str) or not bearer_token:
            raise ValueError("GPU dispatch bearer token is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS:g}")
        if (
            isinstance(upload_chunk_bytes, bool)
            or not isinstance(upload_chunk_bytes, int)
            or upload_chunk_bytes <= 0
            or upload_chunk_bytes > MAX_UPLOAD_CHUNK_BYTES
        ):
            raise ValueError(f"upload_chunk_bytes must be between 1 and {MAX_UPLOAD_CHUNK_BYTES}")
        self.base_url = _plain_origin(base_url)
        self._bearer_token = bearer_token
        self.timeout_seconds = float(timeout_seconds)
        self.upload_chunk_bytes = upload_chunk_bytes
        self._transport = transport or UrllibGpuDispatchTransport()

    @staticmethod
    def _identifier(value: str, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} is required")
        return urllib.parse.quote(value, safe="")

    @staticmethod
    def _require_model(value: Any, model: type[Any], name: str) -> None:
        if not isinstance(value, model):
            raise TypeError(f"{name} must be a {model.__name__}")

    @staticmethod
    def _malformed(method: str, path: str) -> GpuDispatchMalformedResponseError:
        return GpuDispatchMalformedResponseError(
            f"GPU intake {method} {path} returned a malformed response",
            method=method,
            path=path,
        )

    @staticmethod
    def _classify_http_error(
        method: str,
        path: str,
        status: int,
        headers: Mapping[str, str],
    ) -> GpuDispatchError:
        message = f"GPU intake {method} {path} returned HTTP {status}"
        if status == 401:
            return GpuDispatchAuthenticationError(message, status=status, method=method, path=path)
        if status == 404:
            return GpuDispatchNotFoundError(message, status=status, method=method, path=path)
        if status == 409:
            return GpuDispatchConflictError(message, status=status, method=method, path=path)
        if status == 429:
            return GpuDispatchRateLimitError(
                message,
                retry_after=_retry_after_seconds(headers.get("retry-after")),
                status=status,
                method=method,
                path=path,
            )
        if status in {400, 422, 507}:
            return GpuDispatchBlockedError(message, status=status, method=method, path=path)
        if 500 <= status <= 599:
            return GpuDispatchRetryableError(
                message,
                status=status,
                retryable=True,
                method=method,
                path=path,
            )
        return GpuDispatchBlockedError(message, status=status, method=method, path=path)

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
    ) -> GpuDispatchResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GPU intake path must be origin-relative")
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self._bearer_token}"
        if payload is not None:
            if body is not None:
                raise ValueError("A request cannot contain JSON and raw bodies")
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        try:
            raw = self._transport.request(
                method,
                f"{self.base_url}{path}",
                headers=request_headers,
                body=body,
                timeout_seconds=self.timeout_seconds,
            )
        except GpuDispatchError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise GpuDispatchRetryableError(
                f"GPU intake {method} {path} is unavailable: {type(exc).__name__}",
                retryable=True,
                method=method,
                path=path,
            ) from None
        if not isinstance(raw, GpuTransportResponse):
            raise self._malformed(method, path)
        if isinstance(raw.status, bool) or not isinstance(raw.status, int):
            raise self._malformed(method, path)
        try:
            normalized_headers = {
                str(key).casefold(): str(value) for key, value in raw.headers.items()
            }
        except (AttributeError, TypeError, ValueError):
            raise self._malformed(method, path) from None
        if raw.status < 200 or raw.status >= 300:
            raise self._classify_http_error(method, path, raw.status, normalized_headers)
        if not isinstance(raw.body, bytes) or len(raw.body) > MAX_RESPONSE_BYTES:
            raise self._malformed(method, path)
        if not expect_json:
            return GpuDispatchResponse(raw.status, None, normalized_headers)
        try:
            data = json.loads(raw.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise self._malformed(method, path) from None
        if not isinstance(data, dict):
            raise self._malformed(method, path)
        return GpuDispatchResponse(raw.status, data, normalized_headers)

    def live(self) -> GpuDispatchResponse:
        return self._request("GET", "/health/live", authenticated=False)

    def ready(self) -> GpuDispatchResponse:
        return self._request("GET", "/internal/v2/health/ready")

    def status(self) -> GpuDispatchResponse:
        return self._request("GET", "/internal/v2/status")

    def self_checks(self, *, limit: int = 10) -> GpuDispatchResponse:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        query = urllib.parse.urlencode({"limit": limit})
        return self._request("GET", f"/internal/v2/self-checks?{query}")

    def trigger_self_check(self, payload: SelfCheckRequest) -> GpuDispatchResponse:
        self._require_model(payload, SelfCheckRequest, "payload")
        return self._request(
            "POST",
            "/internal/v2/self-checks",
            payload=payload.model_dump(mode="json"),
        )

    def create_dispatch(self, payload: DispatchCreate) -> GpuDispatchResponse:
        self._require_model(payload, DispatchCreate, "payload")
        return self._request(
            "POST",
            "/internal/v2/dispatches",
            payload=payload.model_dump(mode="json"),
        )

    def get_dispatch(self, dispatch_id: str) -> GpuDispatchResponse:
        identifier = self._identifier(dispatch_id, "dispatch_id")
        return self._request("GET", f"/internal/v2/dispatches/{identifier}")

    def head_input(self, dispatch_id: str, artifact_id: str) -> GpuDispatchResponse:
        dispatch = self._identifier(dispatch_id, "dispatch_id")
        artifact = self._identifier(artifact_id, "artifact_id")
        return self._request(
            "HEAD",
            f"/internal/v2/dispatches/{dispatch}/inputs/{artifact}",
            expect_json=False,
        )

    def put_input(
        self,
        dispatch_id: str,
        artifact_id: str,
        *,
        body: bytes,
        start: int,
        end: int,
        total: int,
    ) -> GpuDispatchResponse:
        if not isinstance(body, bytes) or not body:
            raise ValueError("body must contain one input chunk")
        if len(body) > self.upload_chunk_bytes:
            raise ValueError("body exceeds the configured upload chunk size")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or isinstance(total, bool)
            or not all(isinstance(value, int) for value in (start, end, total))
            or start < 0
            or end < start
            or total <= 0
            or end >= total
            or end - start + 1 != len(body)
        ):
            raise ValueError("body and Content-Range values do not agree")
        dispatch = self._identifier(dispatch_id, "dispatch_id")
        artifact = self._identifier(artifact_id, "artifact_id")
        return self._request(
            "PUT",
            f"/internal/v2/dispatches/{dispatch}/inputs/{artifact}",
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
        )

    def start(
        self,
        dispatch_id: str,
        payload: DispatchStart,
        lease_token: str,
    ) -> GpuDispatchResponse:
        self._require_model(payload, DispatchStart, "payload")
        if not isinstance(lease_token, str) or len(lease_token) < 32:
            raise ValueError("lease_token must contain at least 32 characters")
        identifier = self._identifier(dispatch_id, "dispatch_id")
        return self._request(
            "POST",
            f"/internal/v2/dispatches/{identifier}/start",
            payload=payload.model_dump(mode="json"),
            headers={"X-Lease-Token": lease_token},
        )

    def cancel(self, dispatch_id: str) -> GpuDispatchResponse:
        identifier = self._identifier(dispatch_id, "dispatch_id")
        return self._request("POST", f"/internal/v2/dispatches/{identifier}/cancel")
