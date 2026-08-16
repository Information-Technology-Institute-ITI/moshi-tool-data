from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.gpu_dispatcher import GpuDispatcherSettings
from moshi_data_pipeline.studio.gpu_status import derive_gpu_state, gpu_status_payload
from moshi_data_pipeline.studio.server import create_studio_app

BUILD = "gpu-build-test"


def _settings() -> GpuDispatcherSettings:
    return GpuDispatcherSettings(
        internal_url="http://gpu.internal:8766",
        required_build_id=BUILD,
        instance_id="i-testgpu",
        dispatch_token="dummy-status-api-test-token-with-enough-entropy",
    )


def _status_app(tmp_path, *, trusted_header: str | None = "X-Moshi-Authenticated-User"):
    return create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        gpu_dispatcher_settings=_settings(),
        trusted_authenticated_user_header=trusted_header,
    )


def test_manual_check_requires_same_origin_and_trusted_proxy_identity(tmp_path) -> None:
    app = _status_app(tmp_path)
    valid = {
        "origin": "http://testserver",
        "X-Moshi-Authenticated-User": "alice",
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/system/gpu/checks",
                headers={"X-Moshi-Authenticated-User": "alice"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/system/gpu/checks",
                headers={"origin": "http://testserver"},
            ).status_code
            == 401
        )
        created = client.post("/api/system/gpu/checks", headers=valid)
        assert created.status_code == 202
        assert created.json()["created"] is True
        assert created.json()["check"]["requested_by"] == "alice"
        assert "cost" in created.json()["cost_notice"].casefold()

        duplicate = client.post("/api/system/gpu/checks", headers=valid)
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
        assert duplicate.json()["check"]["id"] == created.json()["check"]["id"]


def test_browser_username_header_is_ignored_without_explicit_proxy_trust(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("MOSHI_AUTHENTICATED_USER_HEADER", raising=False)
    monkeypatch.delenv("MOSHI_TRUST_PROXY_AUTH", raising=False)
    app = _status_app(tmp_path, trusted_header=None)
    with TestClient(app) as client:
        response = client.post(
            "/api/system/gpu/checks",
            headers={
                "origin": "http://testserver",
                "X-Moshi-Authenticated-User": "forged-browser-value",
            },
        )
    assert response.status_code == 503


def test_loopback_console_identity_does_not_trust_a_browser_header(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        gpu_dispatcher_settings=_settings(),
        loopback_authenticated_user="local-console",
    )
    with TestClient(app, client=("127.0.0.1", 50_000)) as client:
        response = client.post(
            "/api/system/gpu/checks",
            headers={
                "origin": "http://testserver",
                "X-Moshi-Authenticated-User": "forged-browser-value",
            },
        )
    assert response.status_code == 202
    assert response.json()["check"]["requested_by"] == "local-console"


def test_fixed_source_trial_identity_is_limited_to_the_configured_ip(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MOSHI_TRIAL_OPERATOR_IPS", "198.51.100.10")
    monkeypatch.setenv("MOSHI_TRIAL_AUTHENTICATED_USER", "trial-operator")
    app = _status_app(tmp_path, trusted_header=None)
    headers = {
        "origin": "http://testserver",
        "X-Moshi-Authenticated-User": "forged-browser-value",
    }

    with TestClient(app, client=("198.51.100.11", 50_000)) as denied_client:
        denied = denied_client.post("/api/system/gpu/checks", headers=headers)
    assert denied.status_code == 503

    with TestClient(app, client=("198.51.100.10", 50_000)) as allowed_client:
        allowed = allowed_client.post("/api/system/gpu/checks", headers=headers)
    assert allowed.status_code == 202
    assert allowed.json()["check"]["requested_by"] == "trial-operator"


def test_manual_check_rate_limit_and_history_are_bounded_public_records(tmp_path) -> None:
    app = _status_app(tmp_path)
    service = app.state.studio
    service.catalog.update_lifecycle_state(instance_state="stopped")
    headers = {
        "origin": "http://testserver",
        "X-Moshi-Authenticated-User": "alice",
    }
    with TestClient(app) as client:
        first = client.post("/api/system/gpu/checks", headers=headers)
        check_id = first.json()["check"]["id"]
        service.catalog.update_gpu_check(
            check_id,
            status="passed",
            finished_at=datetime.now(UTC).isoformat(),
        )
        limited = client.post("/api/system/gpu/checks", headers=headers)
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0

        history = client.get("/api/system/gpu/checks?limit=1")
        assert history.status_code == 200
        assert len(history.json()["checks"]) == 1
        serialized = str(history.json()).casefold()
        assert "dispatch_token" not in serialized
        assert "lease_token" not in serialized
        assert "fixture transcript" not in serialized


def test_stopped_machine_is_off_and_previous_pass_is_only_historical(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    now = datetime.now(UTC)
    check, _ = catalog.request_gpu_check(
        "manual",
        requested_by="alice",
        instance_id="i-testgpu",
        expected_build_id=BUILD,
    )
    catalog.update_gpu_check(
        check["id"],
        status="passed",
        requirement_key="requirement-old-boot",
        host_boot_id="old-boot",
        actual_build_id=BUILD,
        finished_at=now.isoformat(),
        valid_until=(now + timedelta(hours=4)).isoformat(),
    )
    catalog.update_lifecycle_state(
        instance_id="i-testgpu",
        instance_state="stopped",
        desired_state="stopped",
    )
    catalog.update_gpu_runtime_state(
        host_boot_id="old-boot",
        actual_build_id=BUILD,
        expected_build_id=BUILD,
        operational_ready=True,
        intake_reachable=True,
        last_intake_observation_at=now.isoformat(),
        details={"functional_requirement_key": "requirement-old-boot"},
    )
    payload = gpu_status_payload(
        catalog,
        instance_id="i-testgpu",
        expected_build_id=BUILD,
        now=now,
    )
    assert payload["state"] == "OFF"
    assert payload["functional_check"]["status"] == "stale"
    assert catalog.list_gpu_checks(limit=1)[0]["status"] == "passed"


def test_gpu_state_derivation_distinguishes_fresh_stale_and_incompatible() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    lifecycle = {"instance_state": "running", "blocked_reason": None}
    runtime = {
        "operational_ready": True,
        "intake_reachable": True,
        "last_intake_observation_at": now.isoformat(),
        "dispatch_protocol": "2.0",
        "actual_build_id": BUILD,
        "details": {},
    }
    worker = {
        "last_heartbeat": now.isoformat(),
        "protocol_version": "1.0",
        "build_id": BUILD,
    }
    assert (
        derive_gpu_state(
            lifecycle=lifecycle,
            runtime=runtime,
            worker=worker,
            check=None,
            dispatch=None,
            now=now,
            expected_build_id=BUILD,
        )
        == "READY"
    )
    stale = {
        **runtime,
        "last_intake_observation_at": (now - timedelta(seconds=121)).isoformat(),
    }
    stale_worker = {
        **worker,
        "last_heartbeat": (now - timedelta(seconds=121)).isoformat(),
    }
    assert (
        derive_gpu_state(
            lifecycle=lifecycle,
            runtime=stale,
            worker=stale_worker,
            check=None,
            dispatch=None,
            now=now,
            expected_build_id=BUILD,
        )
        == "UNKNOWN"
    )
    assert (
        derive_gpu_state(
            lifecycle=lifecycle,
            runtime={
                **runtime,
                "intake_reachable": False,
                "last_intake_observation_at": None,
            },
            worker=worker,
            check=None,
            dispatch=None,
            now=now,
            expected_build_id=BUILD,
        )
        == "DEGRADED"
    )
    incompatible = {**runtime, "actual_build_id": "wrong-build"}
    assert (
        derive_gpu_state(
            lifecycle=lifecycle,
            runtime=incompatible,
            worker=worker,
            check=None,
            dispatch=None,
            now=now,
            expected_build_id=BUILD,
        )
        == "INCOMPATIBLE"
    )


def test_fresh_push_heartbeat_populates_degraded_status_before_intake_poll(
    tmp_path,
) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    now = datetime.now(UTC)
    catalog.update_lifecycle_state(
        instance_id="i-testgpu",
        instance_state="running",
        desired_state="stopped",
        last_aws_observation_at=now.isoformat(),
    )
    catalog.record_worker_state(
        "gpu-worker",
        boot_id="worker-boot",
        protocol_version="1.0",
        build_id=BUILD,
        supported_kinds=["transcribe"],
        status="idle",
        details={"mode": "push", "dispatch_protocol_version": "2.0"},
    )

    payload = gpu_status_payload(
        catalog,
        instance_id="i-testgpu",
        expected_build_id=BUILD,
        now=now,
    )

    assert payload["state"] == "DEGRADED"
    assert payload["service"]["state"] == "online"
    assert payload["service"]["callback_ready"] is True
    assert payload["service"]["dispatch_protocol_version"] == "2.0"
    assert payload["service"]["worker_protocol_version"] == "1.0"
    assert payload["service"]["build_id"] == BUILD
