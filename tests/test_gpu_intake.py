from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moshi_data_pipeline.gpu_intake import GpuIntakeSettings, create_gpu_intake_app

TOKEN = "dispatch-token-for-tests"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def test_settings_from_environment_honor_explicit_ports(tmp_path, monkeypatch) -> None:
    values = {
        "MOSHI_DISPATCH_TOKEN": TOKEN,
        "MOSHI_WORKER_TOKEN": "callback-token-for-tests",
        "MOSHI_BUILD_ID": "build-a",
        "MOSHI_WEB_INTERNAL_URL": "http://web.internal:80",
        "MOSHI_WEB_PORT": "8080",
        "MOSHI_GPU_INTAKE_PORT": "9000",
        "MOSHI_GPU_CACHE": str(tmp_path / "cache"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    settings = GpuIntakeSettings.from_environment()

    assert settings.callback_origin == "http://web.internal:8080"
    assert settings.port == 9000


@pytest.mark.parametrize("name", ["MOSHI_WEB_PORT", "MOSHI_GPU_INTAKE_PORT"])
@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_settings_reject_invalid_ports(tmp_path, monkeypatch, name, value) -> None:
    values = {
        "MOSHI_DISPATCH_TOKEN": TOKEN,
        "MOSHI_WORKER_TOKEN": "callback-token-for-tests",
        "MOSHI_BUILD_ID": "build-a",
        "MOSHI_WEB_INTERNAL_URL": "http://web.internal",
        "MOSHI_GPU_CACHE": str(tmp_path / "cache"),
        name: value,
    }
    for key, setting in values.items():
        monkeypatch.setenv(key, setting)

    with pytest.raises(RuntimeError, match="port|integer|positive"):
        GpuIntakeSettings.from_environment()


def _settings(root: Path, *, build_id: str = "build-a") -> GpuIntakeSettings:
    return GpuIntakeSettings(
        cache_root=root,
        build_id=build_id,
        callback_origin="http://web.internal",
        dispatch_token=TOKEN,
        heartbeat_seconds=3600,
        min_free_bytes=1,
    )


def _manifest(
    content: bytes,
    *,
    dispatch_id: str = "dispatch-1",
    job_id: str = "job-1",
    attempt: int = 1,
    role: str = "source.canonical",
    media_type: str = "audio/wav",
    filename: str = "canonical.wav",
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "job_id": job_id,
        "attempt": attempt,
        "protocol_version": "2.0",
        "required_build_id": "build-a",
        "input_fingerprint": "a" * 64,
        "inputs": [
            {
                "artifact_id": "input-1",
                "role": role,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": media_type,
                "filename": filename,
            }
        ],
    }


def _context(
    content: bytes,
    *,
    kind: str = "transcribe",
    mode: str = "manual",
    role: str = "source.canonical",
    media_type: str = "audio/wav",
    filename: str = "canonical.wav",
) -> dict:
    manifest = _manifest(
        content,
        role=role,
        media_type=media_type,
        filename=filename,
    )
    context = {
        "context": {
            "job_id": manifest["job_id"],
            "kind": kind,
            "attempt": manifest["attempt"],
            "lease_expires_at": "2026-08-16T18:00:00+00:00",
            "input_fingerprint": manifest["input_fingerprint"],
            "payload": {"mode": mode} if kind == "initialize" else {},
            "preconditions": {},
            "config": {},
            "inputs": manifest["inputs"],
        }
    }
    if kind == "initialize":
        context["context"]["preconditions"] = {
            "project": {"id": "project-1", "name": "Project", "language": "ar"},
            "source": {"id": "source-1", "project_id": "project-1"},
            "annotation": {"source_id": "source-1", "version": 0},
        }
        context["context"]["inputs"][0].update(
            project_id="project-1",
            source_id="source-1",
        )
    return context


def _register(client: TestClient, content: bytes, **values) -> dict:
    response = client.post(
        "/internal/v2/dispatches",
        headers=AUTHORIZATION,
        json=_manifest(content, **values),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, content: bytes, content_range: str) -> object:
    return client.put(
        "/internal/v2/dispatches/dispatch-1/inputs/input-1",
        headers={**AUTHORIZATION, "Content-Range": content_range},
        content=content,
    )


def test_private_routes_require_bearer_authentication(tmp_path) -> None:
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/internal/v2/status")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert (
            client.get("/internal/v2/status", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        ready = client.get("/internal/v2/health/ready", headers=AUTHORIZATION).json()
        assert ready["status"] == "intake_ready"
        assert ready["capabilities"] == {
            "job_kinds": ["initialize", "transcribe"],
            "input_receipt": True,
            "execution": False,
            "callback_outbox": False,
            "functional_check": False,
        }
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_manifest_enforces_protocol_build_idempotency_and_single_capacity(
    tmp_path,
) -> None:
    content = b"audio bytes"
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        wrong_protocol = _manifest(content)
        wrong_protocol["protocol_version"] = "1.0"
        response = client.post(
            "/internal/v2/dispatches", headers=AUTHORIZATION, json=wrong_protocol
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "GPU dispatch protocol mismatch"

        wrong_build = _manifest(content)
        wrong_build["required_build_id"] = "other-build"
        response = client.post("/internal/v2/dispatches", headers=AUTHORIZATION, json=wrong_build)
        assert response.status_code == 409
        assert response.json()["detail"] == "GPU service build mismatch"

        original = _register(client, content)
        assert original["state"] == "receiving"
        duplicate = client.post(
            "/internal/v2/dispatches",
            headers=AUTHORIZATION,
            json=_manifest(content),
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["dispatch_id"] == original["dispatch_id"]

        changed = _manifest(content)
        changed["input_fingerprint"] = "b" * 64
        response = client.post("/internal/v2/dispatches", headers=AUTHORIZATION, json=changed)
        assert response.status_code == 409

        response = client.post(
            "/internal/v2/dispatches",
            headers=AUTHORIZATION,
            json=_manifest(content, dispatch_id="dispatch-2", job_id="job-2"),
        )
        assert response.status_code == 409
        assert "active dispatch" in response.json()["detail"]


def test_resumable_upload_survives_restart_and_accepts_identical_retry(tmp_path) -> None:
    content = b"persistent audio bytes"
    cache = tmp_path / "cache"
    first_app = create_gpu_intake_app(_settings(cache))
    with TestClient(first_app) as client:
        _register(client, content)
        response = _upload(client, content[:7], f"bytes 0-6/{len(content)}")
        assert response.status_code == 200
        assert response.json()["accepted_offset"] == 7
        duplicate = _upload(client, content[:7], f"bytes 0-6/{len(content)}")
        assert duplicate.status_code == 200
        assert duplicate.json()["accepted_offset"] == 7

    second_app = create_gpu_intake_app(_settings(cache))
    with TestClient(second_app) as client:
        head = client.head(
            "/internal/v2/dispatches/dispatch-1/inputs/input-1",
            headers=AUTHORIZATION,
        )
        assert head.status_code == 200
        assert head.headers["x-accepted-offset"] == "7"
        response = _upload(
            client,
            content[7:],
            f"bytes 7-{len(content) - 1}/{len(content)}",
        )
        assert response.status_code == 200
        assert response.json()["state"] == "verified"
        dispatch = client.get("/internal/v2/dispatches/dispatch-1", headers=AUTHORIZATION).json()
        assert dispatch["state"] == "verified"

    digest = hashlib.sha256(content).hexdigest()
    cached = cache / "inputs" / "sha256" / digest[:2] / digest
    assert cached.read_bytes() == content
    assert (cache.stat().st_mode & 0o777) == 0o700
    assert ((cache / "state").stat().st_mode & 0o777) == 0o700
    assert ((cache / "state" / "dispatch.sqlite3").stat().st_mode & 0o777) == 0o600
    assert (cached.stat().st_mode & 0o777) == 0o600


def test_upload_rejects_gaps_conflicts_and_bad_checksum_without_advancing(tmp_path) -> None:
    content = b"expected bytes"
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(client, content)
        gap = _upload(client, content[2:], f"bytes 2-{len(content) - 1}/{len(content)}")
        assert gap.status_code == 409
        assert "resume at byte 0" in gap.json()["detail"]

        short = _upload(client, content[:3], f"bytes 0-4/{len(content)}")
        assert short.status_code == 409
        status_response = client.head(
            "/internal/v2/dispatches/dispatch-1/inputs/input-1",
            headers=AUTHORIZATION,
        )
        assert status_response.headers["x-accepted-offset"] == "0"

        wrong = b"x" * len(content)
        checksum = _upload(client, wrong, f"bytes 0-{len(content) - 1}/{len(content)}")
        assert checksum.status_code == 409
        status_response = client.head(
            "/internal/v2/dispatches/dispatch-1/inputs/input-1",
            headers=AUTHORIZATION,
        )
        assert status_response.headers["x-accepted-offset"] == "0"
        assert status_response.headers["x-input-state"] == "open"


def test_verified_dispatch_can_be_queued_without_exposing_lease_token(tmp_path) -> None:
    content = b"audio bytes"
    lease_token = "lease-" + "x" * 40
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(client, content)
        assert (
            _upload(client, content, f"bytes 0-{len(content) - 1}/{len(content)}").status_code
            == 200
        )
        response = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": lease_token},
            json=_context(content),
        )
        assert response.status_code == 202
        assert response.json()["state"] == "queued"
        assert lease_token not in response.text

        duplicate = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": lease_token},
            json=_context(content),
        )
        assert duplicate.status_code == 200
        changed_lease = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": "lease-" + "y" * 40},
            json=_context(content),
        )
        assert changed_lease.status_code == 409
        status_response = client.get("/internal/v2/status", headers=AUTHORIZATION)
        assert status_response.status_code == 200
        assert status_response.json()["safe_to_stop"] is False
        assert lease_token not in json.dumps(status_response.json())


def test_initialize_original_can_be_received_and_queued(tmp_path) -> None:
    content = b"immutable original media"
    lease_token = "lease-" + "i" * 40
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(
            client,
            content,
            role="source.original",
            media_type="video/mp4",
            filename="episode.mp4",
        )
        assert (
            _upload(client, content, f"bytes 0-{len(content) - 1}/{len(content)}").status_code
            == 200
        )
        response = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": lease_token},
            json=_context(
                content,
                kind="initialize",
                mode="assisted",
                role="source.original",
                media_type="video/mp4",
                filename="episode.mp4",
            ),
        )
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "queued"


def test_initialize_input_role_is_immutable_between_receipt_and_start(tmp_path) -> None:
    content = b"immutable original media"
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(client, content)
        assert (
            _upload(client, content, f"bytes 0-{len(content) - 1}/{len(content)}").status_code
            == 200
        )
        response = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": "lease-" + "i" * 40},
            json=_context(
                content,
                kind="initialize",
                role="source.original",
                media_type="audio/wav",
                filename="canonical.wav",
            ),
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "Execution inputs do not match verified inputs"


def test_queued_initialize_can_be_cancelled(tmp_path) -> None:
    content = b"immutable original media"
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(
            client,
            content,
            role="source.original",
            media_type="video/mp4",
            filename="episode.mp4",
        )
        _upload(client, content, f"bytes 0-{len(content) - 1}/{len(content)}")
        queued = client.post(
            "/internal/v2/dispatches/dispatch-1/start",
            headers={**AUTHORIZATION, "X-Lease-Token": "lease-" + "i" * 40},
            json=_context(
                content,
                kind="initialize",
                role="source.original",
                media_type="video/mp4",
                filename="episode.mp4",
            ),
        )
        assert queued.status_code == 202
        cancelled = client.post(
            "/internal/v2/dispatches/dispatch-1/cancel",
            headers=AUTHORIZATION,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert client.get("/internal/v2/status", headers=AUTHORIZATION).json()[
            "safe_to_stop"
        ] is True


def test_cancel_is_idempotent_and_releases_capacity(tmp_path) -> None:
    content = b"audio bytes"
    app = create_gpu_intake_app(_settings(tmp_path / "cache"))
    with TestClient(app) as client:
        _register(client, content)
        first = client.post("/internal/v2/dispatches/dispatch-1/cancel", headers=AUTHORIZATION)
        assert first.status_code == 200
        assert first.json()["state"] == "cancelled"
        second = client.post("/internal/v2/dispatches/dispatch-1/cancel", headers=AUTHORIZATION)
        assert second.status_code == 200
        status_response = client.get("/internal/v2/status", headers=AUTHORIZATION).json()
        assert status_response["safe_to_stop"] is True
        assert status_response["accepting_dispatches"] is True

        response = client.post(
            "/internal/v2/dispatches",
            headers=AUTHORIZATION,
            json=_manifest(content, dispatch_id="dispatch-2", job_id="job-2"),
        )
        assert response.status_code == 201


def test_functional_check_is_persisted_deduplicated_and_never_returns_text(
    tmp_path, monkeypatch
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    content = b"fake audio bytes"
    audio = fixture_root / "check.wav"
    audio.write_bytes(content)
    reference = "مرحبا بالعالم"
    metadata = fixture_root / "fixture.json"
    metadata.write_text(
        json.dumps(
            {
                "fixture_id": "test-ar-v1",
                "audio_file": audio.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "reference_text": reference,
                "language": "ar",
                "dataset": "test-fixture",
                "dataset_config": "ar",
                "split": "test",
                "row_index": 0,
                "record_id": 1,
                "license": "CC0-1.0",
                "source": "https://example.invalid/test-fixture",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    revision = "a" * 40
    config = fixture_root / "config.yaml"
    config.write_text(
        "\n".join(
            (
                "transcription:",
                "  model: large-v3",
                f"  model_revision: {revision}",
                "  language: ar",
                "  device: auto",
                "  compute_type: float16",
            )
        ),
        encoding="utf-8",
    )

    def fake_transcribe(self, audio_path, transcription_config):
        assert audio_path == audio
        assert transcription_config.device == "cuda"
        return {
            "device": "cuda",
            "model_revision": revision,
            "segments": [{"text": reference}],
        }

    monkeypatch.setattr(
        "moshi_data_pipeline.gpu_self_check.WhisperXTranscriber.transcribe",
        fake_transcribe,
    )
    monkeypatch.setattr("torch.cuda.is_available", lambda: True)
    monkeypatch.setattr("torch.cuda.get_device_name", lambda _: "Test GPU")
    cache = tmp_path / "cache"
    settings = GpuIntakeSettings(
        cache_root=cache,
        build_id="build-a",
        callback_origin="http://web.internal",
        dispatch_token=TOKEN,
        heartbeat_seconds=3600,
        min_free_bytes=1,
        config_path=config,
        self_check_metadata=metadata,
        self_check_manual_cooldown_seconds=3600,
    )

    with TestClient(create_gpu_intake_app(settings)) as client:
        response = client.post(
            "/internal/v2/self-checks",
            headers=AUTHORIZATION,
            json={"trigger": "manual"},
        )
        assert response.status_code == 202
        identifier = response.json()["id"]
        latest = None
        for _ in range(100):
            latest = client.get("/internal/v2/status", headers=AUTHORIZATION).json()[
                "functional_check"
            ]["latest"]
            if latest and latest["status"] == "passed":
                break
            time.sleep(0.01)
        assert latest is not None
        assert latest["status"] == "passed"
        assert latest["id"] == identifier
        assert latest["device"] == "cuda"
        assert latest["gpu_name"] == "Test GPU"
        assert latest["cer"] == 0.0
        serialized = json.dumps(latest, ensure_ascii=False)
        assert reference not in serialized
        assert "reference_text" not in serialized

        cached = client.post(
            "/internal/v2/self-checks",
            headers=AUTHORIZATION,
            json={"trigger": "manual"},
        )
        assert cached.status_code == 200
        assert cached.json()["id"] == identifier
        forced = client.post(
            "/internal/v2/self-checks",
            headers=AUTHORIZATION,
            json={"trigger": "manual", "force": True},
        )
        assert forced.status_code == 429
        assert int(forced.headers["retry-after"]) > 0

    with TestClient(create_gpu_intake_app(settings)) as client:
        ready = client.get("/internal/v2/health/ready", headers=AUTHORIZATION).json()
        assert ready["capabilities"]["functional_check"] is True
        assert ready["functional_check"]["ready"] is True
        assert ready["functional_check"]["latest"]["id"] == identifier
        history = client.get("/internal/v2/self-checks?limit=10", headers=AUTHORIZATION).json()[
            "checks"
        ]
        assert [item["id"] for item in history] == [identifier]
