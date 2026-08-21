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


def test_studio_project_upload_and_delete(tmp_path) -> None:
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
        upload = client.post(
            f"/api/projects/{project_id}/sources",
            content=b"RIFF-not-yet-processed",
            headers={"x-filename": "episode.wav", "content-type": "audio/wav"},
        )
        assert upload.status_code == 201
        source_id = upload.json()["id"]
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


def test_annotation_save_keeps_both_speakers_over_an_overlap(tmp_path) -> None:
    """Overlapped speech is saved twice: the full turn, and the short overlap.

    Diarisation gives an overlapped stretch to whichever speaker dominates, so
    the reviewer adds a segment for the other one on top of it. Saving must keep
    both ranges rather than collapsing them into a single speaker.
    """
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    project = service.catalog.create_project(
        "Overlap test", owner_user_id=service.catalog.ensure_local_admin()["id"]
    )
    original = service.paths.originals / "overlap.wav"
    original.write_bytes(b"placeholder")
    source = service.catalog.create_source(
        project["id"],
        "overlap.wav",
        service.paths.relative(original),
        "audio/wav",
        "f" * 64,
        len(b"placeholder"),
    )
    service.catalog.update_source(source["id"], duration_samples=30 * 24_000, status="ready")
    payload = {
        "expected_version": 0,
        "annotation": {
            "source_id": source["id"],
            "activities": [
                {
                    "speaker": "A",
                    "start_sample": 10 * 24_000,
                    "end_sample": 20 * 24_000,
                    "origin": "model",
                },
                {
                    "speaker": "B",
                    "start_sample": 16 * 24_000,
                    "end_sample": 18 * 24_000,
                    "origin": "model",
                },
            ],
            "transcript": [
                {
                    "speaker": "A",
                    "start_sample": 10 * 24_000,
                    "end_sample": 20 * 24_000,
                    "text": "واحد اتنين تلاتة",
                },
                {
                    "speaker": "B",
                    "start_sample": 16 * 24_000,
                    "end_sample": 18 * 24_000,
                    "text": "اتنين",
                    "quality_flags": ["overlapping_speech"],
                },
            ],
        },
    }
    with TestClient(app) as client:
        response = client.put(f"/api/sources/{source['id']}/annotations", json=payload)
        assert response.status_code == 200
        reloaded = client.get(f"/api/sources/{source['id']}").json()["annotation"]

    ranges = [
        (item["speaker"], item["start_sample"], item["end_sample"])
        for item in reloaded["transcript"]
    ]
    assert ("A", 10 * 24_000, 20 * 24_000) in ranges
    assert ("B", 16 * 24_000, 18 * 24_000) in ranges
    # The dominant speaker keeps every word; the overlap is a copy, not a move.
    dominant = next(item for item in reloaded["transcript"] if item["speaker"] == "A")
    assert dominant["text"] == "واحد اتنين تلاتة"


def test_enqueue_fingerprints_the_state_the_job_will_run_against(tmp_path) -> None:
    """A job must not be superseded for a change enqueuing it made itself.

    Enqueuing an initialization sets the source to processing and records the
    chosen mode. The fingerprint was taken before those landed, so it described a
    row state that never existed once the job started; every completion was then
    rejected with "Authoritative inputs changed" and the result thrown away.
    """
    from dataclasses import dataclass

    @dataclass
    class Principal:
        user_id: str

    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    admin = service.catalog.ensure_local_admin()
    principal = Principal(user_id=str(admin["id"]))
    project = service.catalog.create_project("Fingerprint", owner_user_id=str(admin["id"]))

    def enqueue(name: str, digest: str) -> dict:
        original = service.paths.originals / name
        original.write_bytes(b"placeholder-" + digest.encode() * 4)
        source = service.catalog.create_source(
            project["id"],
            name,
            service.paths.relative(original),
            "audio/wav",
            digest * 64,
            original.stat().st_size,
        )
        return service.enqueue(
            project["id"],
            "initialize",
            str(source["id"]),
            {"mode": "assisted"},
            principal=principal,
            source_updates={"status": "processing", "init_mode": "assisted"},
        )

    # Every source behaves the same, not just the first one in a project.
    for name, digest in (("first.wav", "a"), ("second.wav", "b"), ("third.wav", "c")):
        job = enqueue(name, digest)
        assert service.contexts.current_fingerprint(job) == job["input_fingerprint"], name

    # The mode the reviewer chose is still part of the frozen inputs.
    job = enqueue("fourth.wav", "d")
    assert job["preconditions"]["source"]["init_mode"] == "assisted"
    # Status is the source's own lifecycle, not an input, so it is not frozen.
    assert "status" not in job["preconditions"]["source"]

    # A real change to the inputs is still caught.
    service.catalog.update_source(str(job["source_id"]), duration_samples=999)
    assert service.contexts.current_fingerprint(job) != job["input_fingerprint"]


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


def test_removed_processing_surface_is_not_served(tmp_path) -> None:
    """The product is one Assisted pass followed by editing and saving.

    Every regeneration, overlap, clip, export, and rights route is gone from the
    user API, so no browser action can start a second pass on a source.
    """
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "One pass", "language": "ar"}
        ).json()
        upload = client.post(
            f"/api/projects/{project['id']}/sources",
            content=b"RIFF-not-yet-processed",
            headers={"x-filename": "episode.wav", "content-type": "audio/wav"},
        )
        source_id = upload.json()["id"]

        removed_posts = [
            f"/api/sources/{source_id}/transcribe",
            f"/api/sources/{source_id}/review-transcript",
            f"/api/sources/{source_id}/rediarize",
            f"/api/sources/{source_id}/realign",
            f"/api/sources/{source_id}/recover-overlap",
            f"/api/sources/{source_id}/overlaps/region_1/transcribe",
            f"/api/sources/{source_id}/overlaps/region_1/decision",
            f"/api/sources/{source_id}/clip-plan",
            f"/api/sources/{source_id}/generate",
            f"/api/sources/{source_id}/clips/clip_1/decision",
            f"/api/projects/{project['id']}/exports",
        ]
        # 404 or 405 both mean the route is not served; the SPA catch-all claims
        # the path for GET, so an unrouted POST surfaces as 405.
        for path in removed_posts:
            assert client.post(path, json={}).status_code in {404, 405}, path

        removed_gets = [
            f"/api/sources/{source_id}/overlaps",
            f"/api/sources/{source_id}/clip-plan",
            f"/api/sources/{source_id}/clips",
            f"/api/projects/{project['id']}/validate",
            f"/api/projects/{project['id']}/exports",
            f"/media/{source_id}/overlap/region_1/assistant",
            f"/media/{source_id}/clips/clip_1/stereo",
            "/media/exports/export_1/bundle",
        ]
        for path in removed_gets:
            assert client.get(path).status_code == 404, path

        assert client.put(
            f"/api/sources/{source_id}/rights", json={}
        ).status_code in {404, 405}

        # The single allowed pass is still reachable, and no job existed before it.
        assert not client.get(f"/api/projects/{project['id']}").json()["jobs"]
        started = client.post(
            f"/api/sources/{source_id}/initialize", json={"mode": "assisted"}
        )
        assert started.status_code == 202
        assert started.json()["kind"] == "initialize"
