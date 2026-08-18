from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import ActivityRegion, AnnotationDocument
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.server import create_studio_app


def test_state_changes_reject_untrusted_browser_origins(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    payload = {"name": "Origin test", "language": "ar"}
    with TestClient(app) as client:
        rejected = client.post(
            "/api/projects",
            json=payload,
            headers={"origin": "https://attacker.invalid"},
        )
        assert rejected.status_code == 403
        accepted = client.post(
            "/api/projects",
            json=payload,
            headers={"origin": "http://testserver"},
        )
        assert accepted.status_code == 201


def test_studio_project_upload_rights_and_delete(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "Arabic podcast set", "language": "ar"}
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        settings = client.put(
            f"/api/projects/{project_id}",
            json={"name": "Cairo podcast set", "language": "ar-EG"},
        )
        assert settings.status_code == 200
        assert settings.json()["name"] == "Cairo podcast set"
        assert settings.json()["language"] == "ar-EG"
        validation = client.get(f"/api/projects/{project_id}/validate")
        assert validation.status_code == 200
        assert validation.json()["valid"] is False
        upload = client.post(
            f"/api/projects/{project_id}/sources",
            content=b"RIFF-not-yet-processed",
            headers={"x-filename": "episode.wav", "content-type": "audio/wav"},
        )
        assert upload.status_code == 201
        source_id = upload.json()["id"]
        rights = client.put(
            f"/api/sources/{source_id}/rights",
            json={
                "origin": "Owned studio recording",
                "rights_basis": "owned",
                "rights_notes": "Participant releases recorded",
                "rights_confirmed": True,
            },
        )
        assert rights.status_code == 200
        assert rights.json()["rights_confirmed"] is True
        assert client.get(f"/api/sources/{source_id}").status_code == 200
        missing_confirmation = client.delete(f"/api/sources/{source_id}")
        assert missing_confirmation.status_code == 422
        deleted = client.delete(
            f"/api/sources/{source_id}",
            headers={"x-confirm-delete": source_id},
        )
        assert deleted.status_code == 200
        assert client.get(f"/api/sources/{source_id}").status_code == 404


def test_annotation_version_conflict_is_http_409(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    project = service.catalog.create_project(
        "Version test", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "version.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "version.wav",
        service.paths.relative(original),
        "audio/wav",
        "b" * 64,
        len(b"placeholder"),
    )
    service.catalog.update_source(source["id"], duration_samples=48_000, status="ready")
    payload = {
        "expected_version": 0,
        "annotation": {
            "source_id": source["id"],
            "version": 0,
            "assistant_speaker": "A",
            "activities": [
                {
                    "speaker": "A",
                    "start_sample": 0,
                    "end_sample": 24_000,
                    "origin": "manual",
                },
                {
                    "speaker": "B",
                    "start_sample": 24_000,
                    "end_sample": 48_000,
                    "origin": "manual",
                },
            ],
        },
    }
    with TestClient(app) as client:
        assert client.put(
            f"/api/sources/{source['id']}/annotations", json=payload
        ).status_code == 200
        assert client.put(
            f"/api/sources/{source['id']}/annotations", json=payload
        ).status_code == 409


def test_annotation_save_clamps_small_model_overrun(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    project = service.catalog.create_project(
        "Clamp test", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "clamp.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "clamp.wav",
        service.paths.relative(original),
        "audio/wav",
        "e" * 64,
        len(b"placeholder"),
    )
    service.catalog.update_source(source["id"], duration_samples=48_000, status="ready")
    payload = {
        "expected_version": 0,
        "annotation": {
            "source_id": source["id"],
            "activities": [
                {
                    "speaker": "A",
                    "start_sample": 0,
                    "end_sample": 48_200,
                    "origin": "model",
                }
            ],
            "transcript": [
                {
                    "speaker": "A",
                    "start_sample": 0,
                    "end_sample": 48_050,
                    "text": "اهلا",
                }
            ],
        },
    }
    with TestClient(app) as client:
        response = client.put(
            f"/api/sources/{source['id']}/annotations", json=payload
        )
    assert response.status_code == 200
    annotation = response.json()
    assert annotation["activities"][0]["end_sample"] == 48_000
    assert annotation["transcript"][0]["end_sample"] == 48_000


def test_startup_repairs_legacy_annotation_past_source_end(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    paths = StudioPaths(workspace)
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project(
        "Legacy bounds", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    original = paths.originals / "legacy.wav"
    original.write_bytes(b"placeholder")
    source = catalog.create_source(
        project["id"],
        "legacy.wav",
        paths.relative(original),
        "audio/wav",
        "f" * 64,
        len(b"placeholder"),
    )
    catalog.update_source(source["id"], duration_samples=48_000, status="ready")
    catalog.save_annotation(
        source["id"],
        0,
        AnnotationDocument(
            source_id=source["id"],
            activities=[
                ActivityRegion(
                    speaker="A",
                    start_sample=0,
                    end_sample=48_100,
                    origin="model",
                )
            ],
        ),
    )

    app = create_studio_app(workspace, start_worker=False)
    repaired = app.state.studio.catalog.latest_annotation(source["id"])

    assert repaired.version == 2
    assert repaired.activities[0].end_sample == 48_000


def test_overlap_job_is_not_queued_until_regions_are_finalized(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    project = service.catalog.create_project(
        "Overlap gate", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "overlap.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "overlap.wav",
        service.paths.relative(original),
        "audio/wav",
        "a" * 64,
        len(b"placeholder"),
    )
    service.catalog.update_source(source["id"], duration_samples=48_000, status="ready")
    annotation = AnnotationDocument(
        source_id=source["id"],
        assistant_speaker="A",
        activities=[
            ActivityRegion(
                speaker="A",
                start_sample=0,
                end_sample=30_000,
            ),
            ActivityRegion(
                speaker="B",
                start_sample=20_000,
                end_sample=48_000,
            ),
        ],
    )
    saved = service.catalog.save_annotation(source["id"], 0, annotation)

    with TestClient(app) as client:
        blocked = client.post(f"/api/sources/{source['id']}/recover-overlap")
        assert blocked.status_code == 400
        assert "Finalize and save" in blocked.json()["detail"]
        assert not service.catalog.list_jobs(project["id"])

        service.catalog.save_annotation(
            source["id"],
            saved.version,
            saved.model_copy(update={"activities_finalized": True}),
        )
        accepted = client.post(f"/api/sources/{source['id']}/recover-overlap")
        assert accepted.status_code == 202
        assert accepted.json()["kind"] == "recover_overlap"
