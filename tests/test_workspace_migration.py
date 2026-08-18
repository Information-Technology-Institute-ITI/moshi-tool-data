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
