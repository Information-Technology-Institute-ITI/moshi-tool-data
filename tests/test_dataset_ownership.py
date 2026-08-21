from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.auth import AuthSettings, hash_password, token_hash
from moshi_data_pipeline.studio.authorization import RequestPrincipal
from moshi_data_pipeline.studio.catalog import (
    LOCAL_ADMIN_USER_ID,
    LeaseConflictError,
    ProjectDeletionConflictError,
    StudioCatalog,
)
from moshi_data_pipeline.studio.domain import (
    ActivityRegion,
    AnnotationDocument,
    ClipPlanDocument,
    ClipSpec,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION
from moshi_data_pipeline.studio.server import create_studio_app

ORIGIN = "http://testserver"
PASSWORD = "secure-password"
WORKER_TOKEN = "ownership-worker-token"

PROJECT_STATE_TABLES = (
    "projects",
    "sources",
    "annotation_revisions",
    "jobs",
    "job_attempts",
    "worker_state",
    "clip_plans",
    "clip_decisions",
    "overlap_recoveries",
    "exports",
    "artifacts",
    "artifact_uploads",
    "artifact_commits",
    "gpu_dispatches",
    "gpu_dispatch_inputs",
    "project_ownership_audit",
    "project_deletion_cleanup",
)


def _auth_settings(*, require_sign_in: bool = True) -> AuthSettings:
    return AuthSettings(
        public_origin=ORIGIN,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="",
        require_sign_in=require_sign_in,
    )


def _create_user(
    catalog: StudioCatalog,
    email: str,
    *,
    role: str = "user",
    status: str = "active",
) -> dict[str, object]:
    user, _ = catalog.create_or_refresh_pending_user(
        email=email,
        display_name=email.split("@", 1)[0].title(),
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    if status != "pending":
        with catalog.connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET status=?,email_verified_at=?,updated_at=?
                WHERE id=?
                """,
                (status, catalog._now(), catalog._now(), user["id"]),
            )
    return catalog.get_user(str(user["id"]))


def _signin(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/auth/signin",
        headers={"origin": ORIGIN},
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _private_app(tmp_path: Path):
    return create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(),
        worker_token=WORKER_TOKEN,
    )


def _principal(user: dict[str, object]) -> RequestPrincipal:
    return RequestPrincipal.from_catalog_user(user)


def _snapshot_project_state(catalog: StudioCatalog, workspace: Path) -> dict[str, object]:
    with catalog.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        database = {
            table: [
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
            ]
            for table in PROJECT_STATE_TABLES
            if table in tables
        }
    files = {}
    for path in sorted(
        (candidate for candidate in workspace.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.as_posix(),
    ):
        if path.name in {"catalog.sqlite3", "catalog.sqlite3-wal", "catalog.sqlite3-shm"}:
            continue
        content = path.read_bytes()
        files[path.relative_to(workspace).as_posix()] = (
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
    return {"database": database, "files": files}


def test_migration_adds_owner_constraints_and_hides_legacy_ownerless_projects(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = StudioCatalog(path)
    owner = _create_user(catalog, "owner@example.test")
    admin = _create_user(catalog, "legacy-admin@example.test", role="admin")
    pending = _create_user(catalog, "pending-owner@example.test", status="pending")
    project = catalog.create_project("Owned", owner_user_id=str(owner["id"]))
    with pytest.raises(ValueError, match="active user"):
        catalog.create_project("Pending owner", owner_user_id=str(pending["id"]))
    with pytest.raises(ValueError, match="active user"):
        catalog.create_project("Missing owner", owner_user_id="user_missing")

    with catalog.connect() as connection:
        connection.execute("DROP TRIGGER projects_owner_required_insert")
        connection.execute("DROP TRIGGER projects_owner_required_update")
        connection.execute(
            """
            INSERT INTO projects(id,name,language,owner_user_id,created_at,updated_at)
            VALUES('project_legacy','Legacy','ar',NULL,?,?)
            """,
            (catalog._now(), catalog._now()),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version>=6")

    upgraded = StudioCatalog(path)
    assert [value["id"] for value in upgraded.list_projects(str(owner["id"]), False)] == [
        project["id"]
    ]
    assert {value["id"] for value in upgraded.list_projects("admin", True)} == {
        project["id"],
        "project_legacy",
    }
    with upgraded.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        index_names = {
            row[1] for row in connection.execute("PRAGMA index_list(projects)").fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_list(projects)").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO projects(id,name,language,created_at,updated_at)
                VALUES('project_invalid','Invalid','ar',?,?)
                """,
                (catalog._now(), catalog._now()),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE projects SET name='Still legacy' WHERE id='project_legacy'"
            )
    assert version == LATEST_SCHEMA_VERSION
    assert "projects_owner_updated_idx" in index_names
    assert any(
        row[2] == "users" and row[3] == "owner_user_id" and row[6] == "RESTRICT"
        for row in foreign_keys
    )
    assert {
        "projects_owner_required_insert",
        "projects_owner_required_update",
        "projects_deletion_state_insert",
        "projects_deletion_state_update",
    } <= triggers
    reservation = upgraded.reserve_project_deletion(
        "project_legacy",
        principal=_principal(admin),
    )
    assert reservation["deletion_state"] == "deleting"
    upgraded.abort_project_deletion(str(reservation["deletion_reservation_id"]))


def test_project_payload_cannot_inject_owner_or_role(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "payload-workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    with TestClient(app) as client:
        for field, value in (
            ("owner_user_id", "user_injected"),
            ("role", "admin"),
        ):
            created = client.post(
                "/api/projects",
                json={"name": "No injection", "language": "ar", field: value},
            )
            assert created.status_code == 422

        project = client.post(
            "/api/projects", json={"name": "Owned by principal", "language": "ar"}
        ).json()
        for field, value in (
            ("owner_user_id", "user_injected"),
            ("role", "admin"),
        ):
            updated = client.put(
                f"/api/projects/{project['id']}",
                json={"name": "No injection", "language": "ar", field: value},
            )
            assert updated.status_code == 422


def test_users_list_only_their_projects_admin_transfers_and_system_is_admin_only(
    tmp_path,
) -> None:
    app = _private_app(tmp_path)
    catalog = app.state.studio.catalog
    alice = _create_user(catalog, "alice@example.test")
    bob = _create_user(catalog, "bob@example.test")
    admin = _create_user(catalog, "admin@example.test", role="admin")
    pending = _create_user(catalog, "pending@example.test", status="pending")
    disabled = _create_user(catalog, "disabled@example.test", status="disabled")

    with (
        TestClient(app) as alice_client,
        TestClient(app) as bob_client,
        TestClient(app) as admin_client,
    ):
        _signin(alice_client, "alice@example.test")
        _signin(bob_client, "bob@example.test")
        _signin(admin_client, "admin@example.test")
        alice_project = alice_client.post(
            "/api/projects", json={"name": "Alice data", "language": "ar"}
        )
        bob_project = bob_client.post(
            "/api/projects", json={"name": "Bob data", "language": "ar"}
        )
        assert alice_project.status_code == bob_project.status_code == 201
        alice_id = alice_project.json()["id"]
        bob_id = bob_project.json()["id"]
        assert alice_project.json()["owner_user_id"] == alice["id"]
        assert bob_project.json()["owner_user_id"] == bob["id"]

        alice_upload = alice_client.post(
            f"/api/projects/{alice_id}/sources",
            content=b"alice-source",
            headers={"x-filename": "alice.wav", "content-type": "audio/wav"},
        )
        assert alice_upload.status_code == 201
        alice_source_id = alice_upload.json()["id"]
        for number in range(2):
            assert bob_client.post(
                f"/api/projects/{bob_id}/sources",
                content=f"bob-{number}".encode(),
                headers={
                    "x-filename": f"bob-{number}.wav",
                    "content-type": "audio/wav",
                },
            ).status_code == 201

        alice_list = alice_client.get("/api/projects").json()["projects"]
        bob_list = bob_client.get("/api/projects").json()["projects"]
        admin_list = admin_client.get("/api/projects").json()["projects"]
        assert [(value["id"], value["source_count"]) for value in alice_list] == [
            (alice_id, 1)
        ]
        assert [(value["id"], value["source_count"]) for value in bob_list] == [
            (bob_id, 2)
        ]
        assert {value["id"] for value in admin_list} == {alice_id, bob_id}
        assert all(set(value["owner"]) == {"id", "display_name", "email"} for value in admin_list)
        assert admin_client.get(f"/api/projects/{alice_id}").status_code == 200
        assert admin_client.get(f"/api/projects/{bob_id}").status_code == 200
        assert admin_client.get(f"/api/sources/{alice_source_id}").status_code == 200

        assert alice_client.get("/api/admin/users").status_code == 403
        assert alice_client.patch(
            f"/api/admin/projects/{alice_id}/owner",
            json={"owner_user_id": str(bob["id"])},
        ).status_code == 403
        for method, path in (
            ("get", "/api/system/worker"),
            ("post", "/api/system/worker/retry-startup"),
            ("get", "/api/system/gpu"),
            ("get", "/api/system/gpu/checks"),
            ("post", "/api/system/gpu/checks"),
        ):
            response = getattr(alice_client, method)(path, headers={"origin": ORIGIN})
            assert response.status_code == 403

        users = admin_client.get("/api/admin/users").json()["users"]
        assert all(set(user) == {"id", "display_name", "email"} for user in users)
        assert {user["id"] for user in users} == {alice["id"], bob["id"], admin["id"]}
        assert admin_client.get("/api/system/worker").status_code == 200
        assert admin_client.get("/api/system/gpu").status_code == 200
        assert admin_client.get("/api/system/gpu/checks").status_code == 200

        for destination in (pending, disabled):
            rejected = admin_client.patch(
                f"/api/admin/projects/{alice_id}/owner",
                json={"owner_user_id": str(destination["id"])},
            )
            assert rejected.status_code == 400
            assert catalog.get_project_owner_id(alice_id) == alice["id"]

        transferred = admin_client.patch(
            f"/api/admin/projects/{alice_id}/owner",
            json={"owner_user_id": str(bob["id"])},
        )
        assert transferred.status_code == 200
        assert alice_client.get(f"/api/projects/{alice_id}").status_code == 404
        assert alice_client.get(f"/api/sources/{alice_source_id}").status_code == 404
        assert bob_client.get(f"/api/projects/{alice_id}").status_code == 200
        assert bob_client.get(f"/api/sources/{alice_source_id}").status_code == 200
        audit = catalog.list_project_ownership_audit(alice_id)
        assert len(audit) == 1
        assert audit[0]["actor_user_id"] == admin["id"]
        assert audit[0]["previous_owner_user_id"] == alice["id"]
        assert audit[0]["new_owner_user_id"] == bob["id"]

    with pytest.raises(ValueError, match="Transfer or delete"):
        catalog.delete_user(str(bob["id"]))
    assert catalog.delete_user(str(alice["id"]))["id"] == alice["id"]


def test_cross_user_authorization_precedes_validation_and_causes_no_side_effects(
    tmp_path,
) -> None:
    app = _private_app(tmp_path)
    service = app.state.studio
    catalog = service.catalog
    _create_user(catalog, "alice@example.test")
    bob = _create_user(catalog, "bob@example.test")
    _create_user(catalog, "privacy-admin@example.test", role="admin")
    project = catalog.create_project("Bob private", owner_user_id=str(bob["id"]))
    original = service.paths.originals / "bob.wav"
    original.write_bytes(b"private-media")
    source = catalog.create_source(
        str(project["id"]),
        "bob.wav",
        service.paths.relative(original),
        "audio/wav",
        "b" * 64,
        original.stat().st_size,
    )
    source_id = str(source["id"])
    source_root = service.paths.source_root(source_id)
    source_root.mkdir(parents=True, exist_ok=True)
    for path, content in (
        (service.paths.canonical_audio(source_id), b"canonical"),
        (service.paths.canonical_channels(source_id), b"channels"),
        (service.paths.video_proxy(source_id), b"proxy"),
        (service.paths.peaks(source_id), b"{}"),
    ):
        path.write_bytes(content)
    catalog.update_source(source_id, duration_samples=4_800_000, status="ready")
    annotation = catalog.save_annotation(
        source_id,
        0,
        AnnotationDocument(
            source_id=source_id,
            assistant_speaker="A",
            activities_finalized=True,
            activities=[
                ActivityRegion(speaker="A", start_sample=0, end_sample=2_400_000),
                ActivityRegion(speaker="B", start_sample=2_000_000, end_sample=4_800_000),
            ],
            transcript=[
                TranscriptUtterance(
                    speaker="A",
                    start_sample=0,
                    end_sample=1_200_000,
                    text="private annotation",
                )
            ],
        ),
    )
    overlap_root = source_root / "recovery" / "private"
    overlap_root.mkdir(parents=True)
    overlap_paths = {}
    for channel in ("original", "assistant", "user"):
        path = overlap_root / f"{channel}.wav"
        path.write_bytes(channel.encode())
        overlap_paths[channel] = service.paths.relative(path)
    catalog.replace_overlap_recoveries(
        source_id,
        annotation.version,
        [
            {
                "region_id": "region_private",
                "start_sample": 2_000_000,
                "end_sample": 2_400_000,
                "status": "recovered",
                "original_path": overlap_paths["original"],
                "assistant_path": overlap_paths["assistant"],
                "user_path": overlap_paths["user"],
            }
        ],
    )
    clip_id = "clip_private"
    catalog.save_clip_plan(
        ClipPlanDocument(
            source_id=source_id,
            annotation_version=annotation.version,
            mode="manual",
            request={"boundaries_samples": [0, 2_400_000]},
            feasible=True,
            clips=[
                ClipSpec(
                    id=clip_id,
                    start_sample=0,
                    end_sample=2_400_000,
                    status="valid",
                )
            ],
        )
    )
    clip_root = source_root / "clips" / "private"
    clip_root.mkdir(parents=True)
    clip_audio = clip_root / "clip.wav"
    clip_alignment = clip_root / "clip.json"
    clip_audio.write_bytes(b"clip-audio")
    clip_alignment.write_text('{"alignments": []}', encoding="utf-8")
    clip_manifest = clip_root / "clip_artifacts.json"
    clip_manifest.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "annotation_version": annotation.version,
                "artifacts": [
                    {
                        "clip": {"id": clip_id},
                        "wav_path": service.paths.relative(clip_audio),
                        "json_path": service.paths.relative(clip_alignment),
                        "qc": {"status": "PASS"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog.update_source(
        source_id,
        status="clips_ready",
        clips_stale=False,
        clip_artifacts_path=service.paths.relative(clip_manifest),
    )
    job = catalog.create_job(str(project["id"]), "transcribe", str(source["id"]))
    export = catalog.create_export(str(project["id"]), "Bob bundle", 1)
    bundle = service.paths.exports / "bob-bundle.zip"
    bundle.write_bytes(b"private bundle")
    export = catalog.update_export(
        str(export["id"]),
        status="complete",
        path=service.paths.relative(bundle),
    )

    routes: list[tuple[str, str, dict[str, object]]] = [
        ("get", f"/api/projects/{project['id']}", {}),
        ("put", f"/api/projects/{project['id']}", {"json": {}}),
        (
            "delete",
            f"/api/projects/{project['id']}",
            {},
        ),
        (
            "post",
            f"/api/projects/{project['id']}/sources",
            {"content": b"attack", "headers": {"x-filename": "attack.wav"}},
        ),
        ("get", f"/api/sources/{source['id']}", {}),
        (
            "delete",
            f"/api/sources/{source['id']}",
            {},
        ),
        ("post", f"/api/sources/{source['id']}/initialize", {"json": {}}),
        ("get", f"/api/sources/{source['id']}/annotations", {}),
        ("get", f"/api/sources/{source['id']}/annotations/{annotation.version}", {}),
        ("put", f"/api/sources/{source['id']}/annotations", {"json": {}}),
        ("get", f"/api/jobs/{job['id']}", {}),
        ("post", f"/api/jobs/{job['id']}/retry", {}),
        ("get", f"/api/jobs/{job['id']}/events", {}),
        ("get", f"/media/{source['id']}/original", {}),
        ("get", f"/media/{source['id']}/canonical", {}),
        ("get", f"/media/{source['id']}/channels", {}),
        ("get", f"/media/{source['id']}/proxy", {}),
        ("get", f"/media/{source['id']}/peaks", {}),
    ]
    protected_reads = [
        f"/api/projects/{project['id']}",
        f"/api/sources/{source_id}",
        f"/api/sources/{source_id}/annotations",
        f"/api/sources/{source_id}/annotations/{annotation.version}",
        f"/api/jobs/{job['id']}",
        f"/media/{source_id}/original",
        f"/media/{source_id}/canonical",
        f"/media/{source_id}/channels",
        f"/media/{source_id}/proxy",
        f"/media/{source_id}/peaks",
    ]

    with (
        TestClient(app) as client,
        TestClient(app) as bob_client,
        TestClient(app) as admin_client,
    ):
        _signin(client, "alice@example.test")
        _signin(bob_client, "bob@example.test")
        _signin(admin_client, "privacy-admin@example.test")
        before = _snapshot_project_state(catalog, service.paths.root)
        missing = client.get("/api/projects/project_missing")
        assert missing.status_code == 404
        for method, path, options in routes:
            response = getattr(client, method)(path, **options)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json() == missing.json()
        assert _snapshot_project_state(catalog, service.paths.root) == before

        for authorized_client in (bob_client, admin_client):
            for path in protected_reads:
                response = authorized_client.get(path)
                assert response.status_code == 200, (path, response.text)
                if path.startswith("/media/"):
                    assert response.headers["cache-control"] == "private, no-store"

    assert _snapshot_project_state(catalog, service.paths.root) == before
    assert not [path for path in service.paths.incoming.rglob("*") if path.is_file()]


def test_open_job_event_stream_stops_after_ownership_transfer(tmp_path) -> None:
    app = _private_app(tmp_path)
    catalog = app.state.studio.catalog
    authorization = app.state.authorization
    alice = _create_user(catalog, "stream-alice@example.test")
    bob = _create_user(catalog, "stream-bob@example.test")
    admin = _create_user(catalog, "stream-admin@example.test", role="admin")
    project = catalog.create_project("Streaming", owner_user_id=str(alice["id"]))
    job = catalog.create_job(str(project["id"]), "transcribe", None)

    stream_open = threading.Event()
    transferred = threading.Event()
    call_count = 0
    call_lock = threading.Lock()
    original_authorize_job = authorization.authorize_job

    def observed_authorize_job(principal, job_id):
        nonlocal call_count
        value = original_authorize_job(principal, job_id)
        with call_lock:
            call_count += 1
            if call_count >= 3:
                stream_open.set()
        return value

    authorization.authorize_job = observed_authorize_job

    def transfer_after_open() -> None:
        assert stream_open.wait(timeout=5)
        catalog.transfer_project_owner(
            str(project["id"]),
            owner_user_id=str(bob["id"]),
            principal=RequestPrincipal.from_catalog_user(admin),
        )
        transferred.set()

    transfer_thread = threading.Thread(target=transfer_after_open, daemon=True)
    transfer_thread.start()
    try:
        with TestClient(app) as alice_client:
            _signin(alice_client, "stream-alice@example.test")
            with alice_client.stream("GET", f"/api/jobs/{job['id']}/events") as response:
                assert response.status_code == 200
                body = "".join(response.iter_text())
            assert transferred.wait(timeout=5)
            assert f'"id": "{job["id"]}"' in body
            assert 'event: access_revoked\ndata: {"detail":"Not found"}' in body
            assert alice_client.get(f"/api/jobs/{job['id']}").status_code == 404
        with TestClient(app) as bob_client:
            _signin(bob_client, "stream-bob@example.test")
            assert bob_client.get(f"/api/jobs/{job['id']}").status_code == 200
    finally:
        authorization.authorize_job = original_authorize_job
        transfer_thread.join(timeout=5)


@pytest.mark.parametrize("revocation", ["disabled", "session", "demoted_admin"])
def test_open_job_event_stream_rechecks_live_principal(tmp_path, revocation: str) -> None:
    app = _private_app(tmp_path)
    catalog = app.state.studio.catalog
    authentication = app.state.authentication
    authorization = app.state.authorization
    owner = _create_user(catalog, f"sse-owner-{revocation}@example.test")
    viewer = (
        _create_user(catalog, f"sse-admin-{revocation}@example.test", role="admin")
        if revocation == "demoted_admin"
        else owner
    )
    project = catalog.create_project("Live SSE", owner_user_id=str(owner["id"]))
    job = catalog.create_job(str(project["id"]), "transcribe", None)
    polled = threading.Event()
    changed = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    original_authorize_job = authorization.authorize_job

    def observed_authorize_job(principal, job_id):
        nonlocal calls
        job_value = original_authorize_job(principal, job_id)
        with calls_lock:
            calls += 1
            if calls >= 3:
                polled.set()
        return job_value

    authorization.authorize_job = observed_authorize_job
    try:
        with TestClient(app) as client:
            _signin(client, str(viewer["email"]))
            raw_token = client.cookies.get(authentication.settings.cookie_name)
            assert raw_token

            def revoke_after_first_poll() -> None:
                assert polled.wait(timeout=5)
                if revocation == "session":
                    assert catalog.revoke_user_session(token_hash(raw_token))
                else:
                    with catalog.connect() as connection:
                        if revocation == "disabled":
                            connection.execute(
                                "UPDATE users SET status='disabled' WHERE id=?",
                                (viewer["id"],),
                            )
                        else:
                            connection.execute(
                                "UPDATE users SET role='user' WHERE id=?",
                                (viewer["id"],),
                            )
                changed.set()

            thread = threading.Thread(target=revoke_after_first_poll, daemon=True)
            thread.start()
            with client.stream("GET", f"/api/jobs/{job['id']}/events") as response:
                assert response.status_code == 200
                body = "".join(response.iter_text())
            assert changed.wait(timeout=5)
            assert f'"id": "{job["id"]}"' in body
            assert 'event: access_revoked\ndata: {"detail":"Not found"}' in body
            thread.join(timeout=5)
    finally:
        authorization.authorize_job = original_authorize_job


def test_transfer_during_upload_and_enqueue_cannot_commit_for_former_owner(
    tmp_path,
) -> None:
    app = _private_app(tmp_path)
    service = app.state.studio
    catalog = service.catalog
    owner = _create_user(catalog, "atomic-owner@example.test")
    destination = _create_user(catalog, "atomic-destination@example.test")
    _create_user(catalog, "atomic-admin@example.test", role="admin")
    project = catalog.create_project("Atomic", owner_user_id=str(owner["id"]))

    with TestClient(app) as owner_client, TestClient(app) as admin_client:
        _signin(owner_client, "atomic-owner@example.test")
        _signin(admin_client, "atomic-admin@example.test")

        upload_paused = threading.Event()
        finish_upload = threading.Event()
        original_create_source = catalog.create_source

        def paused_create_source(*args, **kwargs):
            upload_paused.set()
            assert finish_upload.wait(timeout=5)
            return original_create_source(*args, **kwargs)

        catalog.create_source = paused_create_source
        upload_result = {}

        def upload() -> None:
            upload_result["response"] = owner_client.post(
                f"/api/projects/{project['id']}/sources",
                content=b"must-not-commit",
                headers={"x-filename": "paused.wav", "content-type": "audio/wav"},
            )

        upload_thread = threading.Thread(target=upload, daemon=True)
        upload_thread.start()
        assert upload_paused.wait(timeout=5)
        transferred = admin_client.patch(
            f"/api/admin/projects/{project['id']}/owner",
            json={"owner_user_id": destination["id"]},
        )
        assert transferred.status_code == 200
        finish_upload.set()
        upload_thread.join(timeout=5)
        catalog.create_source = original_create_source
        assert upload_result["response"].status_code == 404
        assert catalog.list_sources(str(project["id"])) == []
        assert not [path for path in service.paths.originals.iterdir() if path.is_file()]

        assert admin_client.patch(
            f"/api/admin/projects/{project['id']}/owner",
            json={"owner_user_id": owner["id"]},
        ).status_code == 200
        source = owner_client.post(
            f"/api/projects/{project['id']}/sources",
            content=b"owned",
            headers={"x-filename": "owned.wav", "content-type": "audio/wav"},
        ).json()

        enqueue_paused = threading.Event()
        finish_enqueue = threading.Event()
        original_create_job = catalog.create_job

        def paused_create_job(*args, **kwargs):
            enqueue_paused.set()
            assert finish_enqueue.wait(timeout=5)
            return original_create_job(*args, **kwargs)

        catalog.create_job = paused_create_job
        enqueue_result = {}

        def enqueue() -> None:
            enqueue_result["response"] = owner_client.post(
                f"/api/sources/{source['id']}/initialize",
                json={"mode": "assisted"},
            )

        enqueue_thread = threading.Thread(target=enqueue, daemon=True)
        enqueue_thread.start()
        assert enqueue_paused.wait(timeout=5)
        assert admin_client.patch(
            f"/api/admin/projects/{project['id']}/owner",
            json={"owner_user_id": destination["id"]},
        ).status_code == 200
        finish_enqueue.set()
        enqueue_thread.join(timeout=5)
        catalog.create_job = original_create_job
        assert enqueue_result["response"].status_code == 404
        assert catalog.list_jobs(str(project["id"])) == []
        assert owner_client.delete(
            f"/api/projects/{project['id']}",
            headers={"x-confirm-delete": project["id"]},
        ).status_code == 404
        assert catalog.get_project(str(project["id"]))["owner_user_id"] == destination["id"]


def test_deletion_reservation_blocks_transfer_until_quarantine_finishes(tmp_path) -> None:
    app = _private_app(tmp_path)
    service = app.state.studio
    catalog = service.catalog
    owner = _create_user(catalog, "delete-owner@example.test")
    destination = _create_user(catalog, "delete-destination@example.test")
    _create_user(catalog, "delete-admin@example.test", role="admin")
    project = catalog.create_project("Reserved deletion", owner_user_id=str(owner["id"]))
    paused = threading.Event()
    resume = threading.Event()
    original_targets = service._project_deletion_targets

    def paused_targets(project_id: str):
        paused.set()
        assert resume.wait(timeout=5)
        return original_targets(project_id)

    service._project_deletion_targets = paused_targets
    try:
        with TestClient(app) as owner_client, TestClient(app) as admin_client:
            _signin(owner_client, "delete-owner@example.test")
            _signin(admin_client, "delete-admin@example.test")
            result = {}

            def delete() -> None:
                result["response"] = owner_client.delete(
                    f"/api/projects/{project['id']}",
                    headers={"x-confirm-delete": project["id"]},
                )

            thread = threading.Thread(target=delete, daemon=True)
            thread.start()
            assert paused.wait(timeout=5)
            reserved = catalog.get_project(str(project["id"]))
            assert reserved["deletion_state"] == "deleting"
            transfer = admin_client.patch(
                f"/api/admin/projects/{project['id']}/owner",
                json={"owner_user_id": destination["id"]},
            )
            assert transfer.status_code == 409
            resume.set()
            thread.join(timeout=5)
            assert result["response"].status_code == 200
            assert result["response"].json()["cleanup_state"] == "complete"
    finally:
        service._project_deletion_targets = original_targets


def test_reserved_deletion_blocks_new_project_mutations_and_worker_artifacts(
    tmp_path,
) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    owner = _create_user(catalog, "reserved-owner@example.test")
    admin = _create_user(catalog, "reserved-admin@example.test", role="admin")
    principal = _principal(owner)
    project = catalog.create_project("Reserved", owner_user_id=str(owner["id"]))
    source = catalog.create_source(
        str(project["id"]),
        "source.wav",
        "originals/source.wav",
        "audio/wav",
        "a" * 64,
        1,
    )
    job = catalog.create_job(str(project["id"]), "transcribe", str(source["id"]))
    claimed = catalog.claim_leased_job(
        "worker-reserved",
        protocol_version="1.0",
        worker_build_id="build-reserved",
        supported_kinds=["transcribe"],
    )
    assert claimed is not None
    reservation = catalog.reserve_project_deletion(
        str(project["id"]),
        principal=principal,
    )
    baseline = _snapshot_project_state(catalog, tmp_path)

    with pytest.raises(ProjectDeletionConflictError):
        catalog.transfer_project_owner(
            str(project["id"]),
            owner_user_id=str(owner["id"]),
            principal=_principal(admin),
        )
    with pytest.raises(ProjectDeletionConflictError):
        catalog.create_source(
            str(project["id"]),
            "new.wav",
            "originals/new.wav",
            "audio/wav",
            "b" * 64,
            1,
            principal=principal,
        )
    with pytest.raises(ProjectDeletionConflictError):
        catalog.create_job(
            str(project["id"]),
            "transcribe",
            str(source["id"]),
            principal=principal,
        )
    with pytest.raises(ProjectDeletionConflictError):
        catalog.create_export(
            str(project["id"]),
            "blocked",
            1,
            principal=principal,
        )
    with pytest.raises(ProjectDeletionConflictError):
        catalog.update_source(str(source["id"]), principal=principal, status="ready")
    with pytest.raises(LeaseConflictError):
        catalog.create_artifact_upload(
            str(job["id"]),
            "worker-reserved",
            str(claimed["lease_token"]),
            role="source.canonical",
            staging_path=".incoming/blocked.part",
            expected_sha256="c" * 64,
            expected_size=1,
            media_type="audio/wav",
            filename="blocked.wav",
            expires_at="2999-01-01T00:00:00+00:00",
        )
    with pytest.raises(ProjectDeletionConflictError):
        catalog.create_artifact_commit(str(job["id"]), int(claimed["attempt"]), [])
    with pytest.raises(ProjectDeletionConflictError):
        catalog.register_artifact(
            role="blocked",
            relative_path="blocked.bin",
            sha256="d" * 64,
            size_bytes=1,
            media_type="application/octet-stream",
            project_id=str(project["id"]),
        )
    assert _snapshot_project_state(catalog, tmp_path) == baseline
    catalog.abort_project_deletion(str(reservation["deletion_reservation_id"]))


def test_database_failure_after_quarantine_restores_files(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "restore-workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    service = app.state.studio
    catalog = service.catalog
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Restore"}).json()
        source = client.post(
            f"/api/projects/{project['id']}/sources",
            content=b"restore-me",
            headers={"x-filename": "restore.wav", "content-type": "audio/wav"},
        ).json()
    original = service.paths.resolve_relative(str(source["stored_path"]))
    before_files = _snapshot_project_state(catalog, service.paths.root)["files"]
    principal = _principal(catalog.get_user(LOCAL_ADMIN_USER_ID))
    original_finalize = catalog.finalize_project_deletion

    def fail_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("injected deletion commit failure")

    catalog.finalize_project_deletion = fail_finalize
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            service.delete_project(str(project["id"]), principal=principal)
    finally:
        catalog.finalize_project_deletion = original_finalize
    assert original.read_bytes() == b"restore-me"
    assert _snapshot_project_state(catalog, service.paths.root)["files"] == before_files
    assert catalog.get_project(str(project["id"]))["deletion_state"] == "active"
    cleanup = catalog.list_project_deletion_cleanup(str(project["id"]))
    assert cleanup[-1]["state"] == "aborted"
    assert catalog.list_project_ownership_audit(str(project["id"])) == []


def test_purge_failure_leaves_durable_audit_and_cleanup_state(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "purge-workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    service = app.state.studio
    catalog = service.catalog
    original_purge = service._purge_project_quarantine

    def fail_purge(_: Path) -> None:
        raise OSError("injected purge failure")

    service._purge_project_quarantine = fail_purge
    try:
        with TestClient(app) as client:
            project = client.post("/api/projects", json={"name": "Purge"}).json()
            source = client.post(
                f"/api/projects/{project['id']}/sources",
                content=b"quarantined",
                headers={"x-filename": "purge.wav", "content-type": "audio/wav"},
            ).json()
            deleted = client.delete(
                f"/api/projects/{project['id']}",
                headers={"x-confirm-delete": project["id"]},
            )
        assert deleted.status_code == 200
        assert deleted.json() == {
            "deleted": project["id"],
            "recoverable": False,
            "cleanup_state": "purge_failed",
        }
    finally:
        service._purge_project_quarantine = original_purge
    with pytest.raises(KeyError):
        catalog.get_project(str(project["id"]))
    cleanup = catalog.list_project_deletion_cleanup(str(project["id"]))[-1]
    audit = catalog.list_project_ownership_audit(str(project["id"]))
    assert cleanup["state"] == "purge_failed"
    assert cleanup["audit_id"] == audit[-1]["id"]
    assert cleanup["error"] == "injected purge failure"
    quarantine = service.paths.resolve_relative(str(cleanup["quarantine_path"]))
    assert quarantine.is_dir()
    assert any(path.is_file() for path in quarantine.rglob("*"))
    assert not service.paths.resolve_relative(str(source["stored_path"])).exists()


def test_project_deletion_validates_all_paths_and_records_audit(tmp_path) -> None:
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    catalog = app.state.studio.catalog
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Delete me"}).json()
        uploaded = client.post(
            f"/api/projects/{project['id']}/sources",
            content=b"owned",
            headers={"x-filename": "owned.wav", "content-type": "audio/wav"},
        ).json()
        original = app.state.studio.paths.resolve_relative(uploaded["stored_path"])
        with catalog.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id,role,relative_path,sha256,size_bytes,media_type,
                    project_id,state,created_at
                ) VALUES('artifact_escape','test.escape','../outside.bin',?,1,
                    'application/octet-stream',?,'active',?)
                """,
                ("a" * 64, project["id"], catalog._now()),
            )
        rejected = client.delete(
            f"/api/projects/{project['id']}",
            headers={"x-confirm-delete": project["id"]},
        )
        assert rejected.status_code == 400
        assert original.exists()
        assert catalog.get_project(project["id"])["id"] == project["id"]
        assert catalog.list_project_ownership_audit(project["id"]) == []

        with catalog.connect() as connection:
            connection.execute("DELETE FROM artifacts WHERE id='artifact_escape'")
        deleted = client.delete(
            f"/api/projects/{project['id']}",
            headers={"x-confirm-delete": project["id"]},
        )
        assert deleted.status_code == 200
        assert not original.exists()
        assert client.get(f"/api/projects/{project['id']}").status_code == 404

    audit = catalog.list_project_ownership_audit(project["id"])
    assert len(audit) == 1
    assert audit[0]["action"] == "delete"
    assert audit[0]["actor_user_id"] == LOCAL_ADMIN_USER_ID
    assert audit[0]["previous_owner_user_id"] == LOCAL_ADMIN_USER_ID
    assert audit[0]["new_owner_user_id"] is None
    cleanup = catalog.list_project_deletion_cleanup(project["id"])
    assert cleanup[-1]["state"] == "complete"
    assert cleanup[-1]["audit_id"] == audit[0]["id"]
    assert not app.state.studio.paths.resolve_relative(cleanup[-1]["quarantine_path"]).exists()


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_project_deletion_rejects_linked_paths_that_escape_workspace(
    tmp_path,
    link_kind: str,
) -> None:
    if link_kind == "junction" and os.name != "nt":
        pytest.skip("Directory junctions are Windows-specific")
    app = create_studio_app(
        tmp_path / f"workspace-{link_kind}",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    service = app.state.studio
    catalog = service.catalog
    outside = tmp_path / f"outside-{link_kind}"
    outside.mkdir()
    outside_file = outside / "must-survive.bin"
    outside_file.write_bytes(b"outside")
    link = service.paths.worker_artifacts / f"escape-{link_kind}"
    if link_kind == "symlink":
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable")
            raise
    else:
        created = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr or created.stdout

    try:
        with TestClient(app) as client:
            project = client.post("/api/projects", json={"name": link_kind}).json()
            source = client.post(
                f"/api/projects/{project['id']}/sources",
                content=b"inside",
                headers={"x-filename": "inside.wav", "content-type": "audio/wav"},
            ).json()
            original = service.paths.resolve_relative(source["stored_path"])
            with catalog.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        id,role,relative_path,sha256,size_bytes,media_type,
                        project_id,state,created_at
                    ) VALUES(?,?,?,?,1,'application/octet-stream',?,'active',?)
                    """,
                    (
                        f"artifact_{link_kind}",
                        f"test.{link_kind}",
                        f"worker_artifacts/{link.name}/{outside_file.name}",
                        "d" * 64,
                        project["id"],
                        catalog._now(),
                    ),
                )
            rejected = client.delete(
                f"/api/projects/{project['id']}",
                headers={"x-confirm-delete": project["id"]},
            )
            assert rejected.status_code == 400
            assert original.exists()
            assert outside_file.read_bytes() == b"outside"
            assert catalog.get_project(project["id"])["id"] == project["id"]
            assert catalog.list_project_ownership_audit(project["id"]) == []
    finally:
        if link_kind == "junction" and link.exists():
            os.rmdir(link)
        elif link.is_symlink():
            link.unlink()


def test_worker_credentials_ignore_browser_ownership_and_local_mode_has_real_owner(
    tmp_path,
) -> None:
    local_app = create_studio_app(
        tmp_path / "local",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings(require_sign_in=False),
    )
    with TestClient(local_app) as client:
        me = client.get("/api/auth/me").json()["user"]
        project = client.post("/api/projects", json={"name": "Local data"}).json()
    assert me["id"] == LOCAL_ADMIN_USER_ID
    assert me["role"] == "admin"
    assert project["owner_user_id"] == LOCAL_ADMIN_USER_ID

    app = _private_app(tmp_path)
    service = app.state.studio
    owner = _create_user(service.catalog, "worker-owner@example.test")
    project = service.catalog.create_project(
        "Worker-owned", owner_user_id=str(owner["id"])
    )
    service.enqueue(str(project["id"]), "transcribe")
    with TestClient(app) as client:
        claimed = client.post(
            "/internal/v1/jobs/claim",
            headers={"authorization": f"Bearer {WORKER_TOKEN}"},
            json={
                "worker_id": "worker-ownership",
                "boot_id": "boot-ownership",
                "protocol_version": "1.0",
                "build_id": "build-ownership",
                "supported_kinds": ["transcribe"],
            },
        )
    assert claimed.status_code == 200
    assert claimed.json()["job"]["kind"] == "transcribe"
    assert claimed.json()["lease_token"]
