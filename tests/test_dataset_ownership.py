from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.auth import AuthSettings, hash_password
from moshi_data_pipeline.studio.catalog import LOCAL_ADMIN_USER_ID, StudioCatalog
from moshi_data_pipeline.studio.server import create_studio_app

ORIGIN = "http://testserver"
PASSWORD = "secure-password"
WORKER_TOKEN = "ownership-worker-token"


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


def test_migration_adds_owner_constraints_and_hides_legacy_ownerless_projects(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = StudioCatalog(path)
    owner = _create_user(catalog, "owner@example.test")
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
        connection.execute("DELETE FROM schema_migrations WHERE version=6")

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
    assert version == 6
    assert "projects_owner_updated_idx" in index_names
    assert any(
        row[2] == "users" and row[3] == "owner_user_id" and row[6] == "RESTRICT"
        for row in foreign_keys
    )
    assert {
        "projects_owner_required_insert",
        "projects_owner_required_update",
    } <= triggers


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
        ("post", f"/api/projects/{project['id']}/exports", {"json": {}}),
        ("get", f"/api/projects/{project['id']}/validate", {}),
        ("get", f"/api/projects/{project['id']}/exports", {}),
        ("get", f"/api/sources/{source['id']}", {}),
        (
            "delete",
            f"/api/sources/{source['id']}",
            {},
        ),
        ("put", f"/api/sources/{source['id']}/rights", {"json": {}}),
        ("post", f"/api/sources/{source['id']}/initialize", {"json": {}}),
        ("post", f"/api/sources/{source['id']}/transcribe", {}),
        ("post", f"/api/sources/{source['id']}/review-transcript", {}),
        ("post", f"/api/sources/{source['id']}/rediarize", {}),
        ("post", f"/api/sources/{source['id']}/realign", {}),
        ("post", f"/api/sources/{source['id']}/recover-overlap", {}),
        ("post", f"/api/sources/{source['id']}/overlaps/region_x/transcribe", {}),
        ("get", f"/api/sources/{source['id']}/annotations", {}),
        ("get", f"/api/sources/{source['id']}/annotations/1", {}),
        ("put", f"/api/sources/{source['id']}/annotations", {"json": {}}),
        ("get", f"/api/sources/{source['id']}/overlaps", {}),
        (
            "post",
            f"/api/sources/{source['id']}/overlaps/region_x/decision",
            {"json": {}},
        ),
        ("post", f"/api/sources/{source['id']}/clip-plan", {"json": {}}),
        ("get", f"/api/sources/{source['id']}/clip-plan", {}),
        ("post", f"/api/sources/{source['id']}/generate", {}),
        ("get", f"/api/sources/{source['id']}/clips", {}),
        (
            "post",
            f"/api/sources/{source['id']}/clips/clip_x/decision",
            {"json": {}},
        ),
        ("get", f"/api/jobs/{job['id']}", {}),
        ("post", f"/api/jobs/{job['id']}/retry", {}),
        ("get", f"/api/jobs/{job['id']}/events", {}),
        ("get", f"/media/{source['id']}/original", {}),
        ("get", f"/media/{source['id']}/canonical", {}),
        ("get", f"/media/{source['id']}/channels", {}),
        ("get", f"/media/{source['id']}/proxy", {}),
        ("get", f"/media/{source['id']}/peaks", {}),
        ("get", f"/media/{source['id']}/overlap/region_x/original", {}),
        ("get", f"/media/{source['id']}/clips/clip_x/audio", {}),
        ("get", f"/media/{source['id']}/clips/clip_x/alignment", {}),
        ("get", f"/media/exports/{export['id']}/bundle", {}),
    ]
    before_jobs = len(catalog.list_jobs(str(project["id"])))
    before_audit = catalog.list_project_ownership_audit()
    before_originals = {path for path in service.paths.originals.rglob("*") if path.is_file()}

    with TestClient(app) as client:
        _signin(client, "alice@example.test")
        missing = client.get("/api/projects/project_missing")
        assert missing.status_code == 404
        for method, path, options in routes:
            response = getattr(client, method)(path, **options)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json() == missing.json()

    with TestClient(app) as bob_client:
        _signin(bob_client, "bob@example.test")
        downloaded = bob_client.get(f"/media/exports/{export['id']}/bundle")
        assert downloaded.status_code == 200
        assert downloaded.content == b"private bundle"

    assert len(catalog.list_jobs(str(project["id"]))) == before_jobs
    assert catalog.list_project_ownership_audit() == before_audit
    assert {path for path in service.paths.originals.rglob("*") if path.is_file()} == before_originals
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
            actor_user_id=str(admin["id"]),
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
        link.symlink_to(outside, target_is_directory=True)
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
