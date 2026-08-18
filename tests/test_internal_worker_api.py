from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.domain import AnnotationDocument
from moshi_data_pipeline.studio.server import create_studio_app

TOKEN = "test-worker-token-with-enough-entropy"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _app_with_job(tmp_path, kind: str = "transcribe"):
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        worker_token=TOKEN,
    )
    service = app.state.studio
    project = service.catalog.create_project(
        "Worker API", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    job = service.enqueue(project["id"], kind)
    return app, service, job


def _claim(client: TestClient, kinds: list[str] | None = None) -> dict:
    response = client.post(
        "/internal/v1/jobs/claim",
        headers=AUTH,
        json={
            "protocol_version": "1.0",
            "worker_id": "worker-1",
            "boot_id": "boot-1",
            "build_id": "build-a",
            "supported_kinds": kinds or ["transcribe"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_internal_api_requires_bearer_token_and_exposes_compatibility(tmp_path) -> None:
    app, service, job = _app_with_job(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/internal/v1/jobs/claim",
                json={
                    "protocol_version": "1.0",
                    "worker_id": "worker-1",
                    "boot_id": "boot-1",
                    "build_id": "build-a",
                    "supported_kinds": ["transcribe"],
                },
            ).status_code
            == 401
        )
        incompatible = client.post(
            "/internal/v1/jobs/claim",
            headers=AUTH,
            json={
                "protocol_version": "0.9",
                "worker_id": "old-worker",
                "boot_id": "old-boot",
                "build_id": "old-build",
                "supported_kinds": ["transcribe"],
            },
        )
        assert incompatible.status_code == 409
        assert service.catalog.get_job(job["id"])["status"] == "queued"
        status = client.get("/api/system/worker")
        assert status.status_code == 200
        assert status.json()["worker"]["status"] == "incompatible"


def test_claim_heartbeat_and_deterministic_complete_over_http(tmp_path) -> None:
    app, service, created = _app_with_job(tmp_path)
    with TestClient(app) as client:
        claimed = _claim(client)
        assert claimed["job"]["job_id"] == created["id"]
        lease_headers = {
            **AUTH,
            "x-lease-token": claimed["lease_token"],
        }
        heartbeat = client.post(
            f"/internal/v1/jobs/{created['id']}/heartbeat",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "progress": 0.5,
                "message": "Working",
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["cancel_requested"] is False

        context = claimed["job"]
        complete = client.post(
            f"/internal/v1/jobs/{created['id']}/complete",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "input_fingerprint": context["input_fingerprint"],
                "kind": "transcribe",
                "result": {
                    "kind": "transcribe",
                    "source_id": "source_fake",
                    "expected_annotation_version": 0,
                    "annotation": {"source_id": "source_fake"},
                    "metrics": {},
                },
                "artifacts": [],
            },
        )
        # There is deliberately no source on this project: typed commit rejects the
        # mutation rather than exposing a generic catalog operation.
        assert complete.status_code == 400
        assert service.catalog.get_job(created["id"])["status"] == "running"

        failed = client.post(
            f"/internal/v1/jobs/{created['id']}/fail",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "failure_class": "invalid_input",
                "error": "fixture has no source",
                "retryable": False,
            },
        )
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"
        duplicate_failure = client.post(
            f"/internal/v1/jobs/{created['id']}/fail",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "failure_class": "invalid_input",
                "error": "fixture has no source",
                "retryable": False,
            },
        )
        assert duplicate_failure.status_code == 200
        assert duplicate_failure.json()["status"] == "failed"


def test_resumable_upload_duplicate_chunk_checksum_and_range_download(tmp_path) -> None:
    app, service, created = _app_with_job(tmp_path)
    content = b"worker-output-bytes"
    digest = hashlib.sha256(content).hexdigest()
    with TestClient(app) as client:
        claimed = _claim(client)
        lease_headers = {
            **AUTH,
            "x-lease-token": claimed["lease_token"],
        }
        created_upload = client.post(
            f"/internal/v1/jobs/{created['id']}/uploads",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "role": "analysis.test",
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": "application/octet-stream",
                "filename": "result.bin",
            },
        )
        assert created_upload.status_code == 201, created_upload.text
        upload_id = created_upload.json()["id"]
        first = content[:7]
        chunk = client.put(
            f"/internal/v1/uploads/{upload_id}?worker_id=worker-1",
            headers={
                **lease_headers,
                "content-range": f"bytes 0-6/{len(content)}",
            },
            content=first,
        )
        assert chunk.status_code == 200
        assert chunk.json()["accepted_offset"] == 7
        repeated = client.put(
            f"/internal/v1/uploads/{upload_id}?worker_id=worker-1",
            headers={
                **lease_headers,
                "content-range": f"bytes 0-6/{len(content)}",
            },
            content=first,
        )
        assert repeated.status_code == 200
        rest = client.put(
            f"/internal/v1/uploads/{upload_id}?worker_id=worker-1",
            headers={
                **lease_headers,
                "content-range": f"bytes 7-{len(content) - 1}/{len(content)}",
            },
            content=content[7:],
        )
        assert rest.status_code == 200
        assert rest.json()["state"] == "verified"
        status = client.head(
            f"/internal/v1/uploads/{upload_id}?worker_id=worker-1",
            headers=lease_headers,
        )
        assert status.headers["upload-offset"] == str(len(content))

        upload = service.catalog.get_artifact_upload(upload_id)
        artifact = service.catalog.register_artifact(
            role="fixture.download",
            relative_path=upload["staging_path"],
            sha256=digest,
            size_bytes=len(content),
            media_type="application/octet-stream",
            project_id=created["project_id"],
        )
        partial = client.get(
            f"/internal/v1/artifacts/{artifact['id']}/content",
            headers={**AUTH, "range": "bytes=2-7"},
        )
        assert partial.status_code == 206
        assert partial.content == content[2:8]
        assert partial.headers["content-range"] == f"bytes 2-7/{len(content)}"
        rejected = client.get(
            f"/internal/v1/artifacts/{artifact['id']}/content",
            headers={**AUTH, "range": "bytes=999-1000"},
        )
        assert rejected.status_code == 416


def test_expired_upload_is_discarded(tmp_path) -> None:
    app, service, created = _app_with_job(tmp_path)
    with TestClient(app) as client:
        claimed = _claim(client)
        lease_headers = {**AUTH, "x-lease-token": claimed["lease_token"]}
        upload = client.post(
            f"/internal/v1/jobs/{created['id']}/uploads",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "role": "analysis.fixture",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
                "media_type": "application/octet-stream",
                "filename": "fixture.bin",
            },
        ).json()
        staging = service.paths.resolve_relative(upload["staging_path"])
        assert staging.is_file()
        with service.catalog.connect() as connection:
            connection.execute(
                "UPDATE artifact_uploads SET expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", upload["id"]),
            )
        expired = client.head(
            f"/internal/v1/uploads/{upload['id']}?worker_id=worker-1",
            headers=lease_headers,
        )
        assert expired.status_code == 409
        assert service.catalog.get_artifact_upload(upload["id"])["state"] == "discarded"
        assert not staging.exists()


def test_stale_authoritative_context_supersedes_completion(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "workspace", start_worker=False, worker_token=TOKEN
    )
    service = app.state.studio
    project = service.catalog.create_project(
        "Stale context", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "episode.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "episode.wav",
        service.paths.relative(original),
        "audio/wav",
        hashlib.sha256(b"placeholder").hexdigest(),
        len(b"placeholder"),
    )
    job = service.enqueue(project["id"], "initialize", source["id"], {"mode": "manual"})
    with TestClient(app) as client:
        claimed = _claim(client, ["initialize"])
        service.catalog.update_source(source["id"], init_mode="assisted")
        completed = client.post(
            f"/internal/v1/jobs/{job['id']}/complete",
            headers={
                **AUTH,
                "x-lease-token": claimed["lease_token"],
            },
            json={
                "worker_id": "worker-1",
                "input_fingerprint": claimed["job"]["input_fingerprint"],
                "kind": "initialize",
                "result": {
                    "kind": "initialize",
                    "source_id": source["id"],
                    "annotation": {"source_id": source["id"]},
                    "inspection": {},
                    "duration_samples": 1,
                    "source_updates": {"status": "ready", "duration_samples": 1},
                },
                "artifacts": [],
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "superseded"


def test_artifact_and_typed_annotation_commit_are_atomic(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "workspace", start_worker=False, worker_token=TOKEN
    )
    service = app.state.studio
    project = service.catalog.create_project(
        "Atomic result", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "episode.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "episode.wav",
        service.paths.relative(original),
        "audio/wav",
        hashlib.sha256(b"placeholder").hexdigest(),
        len(b"placeholder"),
    )
    service.paths.source_root(source["id"]).mkdir(parents=True)
    service.paths.canonical_audio(source["id"]).write_bytes(b"canonical")
    service.catalog.update_source(source["id"], duration_samples=24_000, status="ready")
    saved = service.catalog.save_annotation(
        source["id"],
        0,
        AnnotationDocument(source_id=source["id"]),
    )
    job = service.enqueue(project["id"], "rediarize", source["id"])
    content = b'{"segments":[]}'
    digest = hashlib.sha256(content).hexdigest()
    original_commit = service.catalog.commit_leased_job_result
    observed_staging: dict[str, object] = {}

    def inspect_atomic_boundary(*args, **kwargs):
        observed_staging["produced_visible"] = any(
            item["producing_job_id"] == job["id"]
            for item in service.catalog.list_artifacts(source_id=source["id"])
        )
        with service.catalog.connect() as connection:
            observed_staging["artifact_state"] = connection.execute(
                "SELECT state FROM artifacts WHERE producing_job_id=?", (job["id"],)
            ).fetchone()["state"]
            observed_staging["upload_state"] = connection.execute(
                "SELECT state FROM artifact_uploads WHERE job_id=?", (job["id"],)
            ).fetchone()["state"]
        return original_commit(*args, **kwargs)

    service.catalog.commit_leased_job_result = inspect_atomic_boundary
    with TestClient(app) as client:
        claimed = _claim(client, ["rediarize"])
        lease_headers = {**AUTH, "x-lease-token": claimed["lease_token"]}
        upload = client.post(
            f"/internal/v1/jobs/{job['id']}/uploads",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "role": "analysis.diarization",
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": "application/json",
                "filename": "diarization.json",
            },
        ).json()
        sent = client.put(
            f"/internal/v1/uploads/{upload['id']}?worker_id=worker-1",
            headers={
                **lease_headers,
                "content-range": f"bytes 0-{len(content) - 1}/{len(content)}",
            },
            content=content,
        )
        assert sent.status_code == 200
        completed = client.post(
            f"/internal/v1/jobs/{job['id']}/complete",
            headers=lease_headers,
            json={
                "worker_id": "worker-1",
                "input_fingerprint": claimed["job"]["input_fingerprint"],
                "kind": "rediarize",
                "result": {
                    "kind": "rediarize",
                    "source_id": source["id"],
                    "expected_annotation_version": saved.version,
                    "annotation": saved.model_dump(mode="json"),
                    "metrics": {},
                },
                "artifacts": [
                    {
                        "upload_id": upload["id"],
                        "role": "analysis.diarization",
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/json",
                    }
                ],
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "complete"
        duplicate = client.post(
            f"/internal/v1/jobs/{job['id']}/complete",
            headers={**lease_headers, "content-type": "application/json"},
            content=completed.request.content,
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["status"] == "complete"

    assert service.catalog.latest_annotation(source["id"]).version == saved.version + 1
    assert observed_staging == {
        "produced_visible": False,
        "artifact_state": "missing",
        "upload_state": "verified",
    }
    artifact = next(
        item
        for item in service.catalog.list_artifacts(source_id=source["id"])
        if item["role"] == "analysis.diarization"
    )
    assert service.paths.resolve_relative(artifact["relative_path"]).read_bytes() == content
    with service.catalog.connect() as connection:
        journal_state = connection.execute(
            "SELECT state FROM artifact_commits WHERE job_id=?", (job["id"],)
        ).fetchone()["state"]
    assert journal_state == "committed"
