from __future__ import annotations

import hashlib
from pathlib import Path

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION
from moshi_data_pipeline.studio.workspace_migration import migrate_workspace


def _workspace(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "workspace"
    paths = StudioPaths(root)
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project(
        "Migration fixture", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    content = b"original"
    original = paths.originals / "episode.wav"
    original.write_bytes(content)
    source = catalog.create_source(
        project["id"],
        "episode.wav",
        paths.relative(original),
        "audio/wav",
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    paths.source_root(source["id"]).mkdir(parents=True)
    paths.canonical_audio(source["id"]).write_bytes(b"canonical")
    return root, source["id"]


def _filesystem_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    values = {}
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.as_posix(),
    ):
        content = path.read_bytes()
        values[path.relative_to(root).as_posix()] = (
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
    return values


def test_workspace_migration_dry_run_does_not_create_backup(tmp_path) -> None:
    root, _ = _workspace(tmp_path)
    report = migrate_workspace(root)
    assert report["mode"] == "dry-run"
    assert report["changes_applied"] is False
    assert not (root / "backups").exists()


def test_workspace_migration_is_idempotent_and_verifies_checksums(tmp_path) -> None:
    root, source_id = _workspace(tmp_path)
    first = migrate_workspace(root, apply=True)
    assert first["schema_version"] == LATEST_SCHEMA_VERSION
    assert first["verification"]["valid"] is True
    assert Path(first["backup"]).is_file()
    first_count = first["verification"]["artifact_count"]
    assert first_count >= 2

    second = migrate_workspace(root, apply=True)
    assert second["backup"] != first["backup"]
    assert second["verification"]["artifact_count"] == first_count
    verification = migrate_workspace(root, verify_only=True)
    assert verification["verification"]["valid"] is True

    paths = StudioPaths(root)
    paths.canonical_audio(source_id).write_bytes(b"corrupted")
    broken = migrate_workspace(root, verify_only=True)
    assert broken["verification"]["valid"] is False
    assert broken["verification"]["mismatched"]


def test_workspace_verify_is_read_only_and_reports_outdated_schema(tmp_path) -> None:
    root, _ = _workspace(tmp_path)
    database = root / "catalog.sqlite3"
    with StudioCatalog(database).connect() as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (LATEST_SCHEMA_VERSION,),
        )
    before = _filesystem_snapshot(root)
    before_size = database.stat().st_size
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    report = migrate_workspace(root, verify_only=True)

    assert report["mode"] == "verify"
    assert report["changes_applied"] is False
    assert report["schema_version"] == LATEST_SCHEMA_VERSION - 1
    assert report["verification"]["schema_current"] is False
    assert report["verification"]["valid"] is False
    assert "outdated" in report["verification"]["schema_error"]
    assert database.stat().st_size == before_size
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert _filesystem_snapshot(root) == before
