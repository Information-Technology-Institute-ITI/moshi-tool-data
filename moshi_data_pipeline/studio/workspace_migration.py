from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION

CATALOG_TABLES = (
    "projects",
    "sources",
    "annotation_revisions",
    "jobs",
    "clip_plans",
    "clip_decisions",
    "overlap_recoveries",
    "exports",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return dict.fromkeys(CATALOG_TABLES, 0)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return {
            table: (
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in existing
                else 0
            )
            for table in CATALOG_TABLES
        }
    finally:
        connection.close()


def inspect_workspace(workspace: Path) -> dict[str, Any]:
    root = workspace.resolve()
    database = root / "catalog.sqlite3"
    files = sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []
    return {
        "workspace": str(root),
        "database": str(database),
        "database_exists": database.is_file(),
        "schema_counts": _database_counts(database),
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
    }


def backup_database(database: Path, destination: Path) -> Path:
    if not database.is_file():
        raise FileNotFoundError(database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(database)
    backup_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()
    return destination


def _role_for_source_path(source_id: str, relative: str) -> str:
    name = Path(relative).name
    exact = {
        "canonical.wav": "source.canonical",
        "canonical_channels.wav": "source.channels",
        "proxy.mp4": "source.proxy",
        "peaks.json": "source.peaks",
        "raw_transcript.json": "analysis.raw_transcript",
        "aligned_transcript.json": "analysis.aligned_transcript",
        "diarization.json": "analysis.diarization",
        "clip_artifacts.json": "clips.manifest",
    }
    if name in exact:
        return exact[name]
    digest = hashlib.sha256(f"{source_id}\0{relative}".encode()).hexdigest()[:24]
    return f"legacy.source_file.{digest}"


def _registered_candidates(
    catalog: StudioCatalog,
    paths: StudioPaths,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for project in catalog.list_projects(viewer_id="system", is_admin=True):
        project_id = str(project["id"])
        for source in catalog.list_sources(project_id):
            source_id = str(source["id"])
            original = paths.resolve_relative(str(source["stored_path"]))
            if original.is_file():
                candidates[paths.relative(original)] = {
                    "role": "source.original",
                    "path": original,
                    "project_id": project_id,
                    "source_id": source_id,
                    "known_sha256": str(source["sha256"]),
                    "known_size": int(source["size_bytes"]),
                }
            source_root = paths.source_root(source_id)
            if source_root.exists():
                for file in sorted(path for path in source_root.rglob("*") if path.is_file()):
                    relative = paths.relative(file)
                    candidates[relative] = {
                        "role": _role_for_source_path(source_id, relative),
                        "path": file,
                        "project_id": project_id,
                        "source_id": source_id,
                    }
            for record in catalog.overlap_recoveries(source_id):
                for field, suffix in (
                    ("original_path", "original"),
                    ("assistant_path", "assistant"),
                    ("user_path", "user"),
                ):
                    if not record.get(field):
                        continue
                    file = paths.resolve_relative(str(record[field]))
                    if file.is_file():
                        relative = paths.relative(file)
                        candidates[relative] = {
                            "role": f"overlap.{record['region_id']}.{suffix}",
                            "path": file,
                            "project_id": project_id,
                            "source_id": source_id,
                        }
            manifest_path_value = source.get("clip_artifacts_path")
            if manifest_path_value:
                manifest_path = paths.resolve_relative(str(manifest_path_value))
                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    for item in manifest.get("artifacts", []):
                        clip_id = str(item.get("clip", {}).get("id", "unknown"))
                        for field, suffix in (
                            ("wav_path", "audio"),
                            ("json_path", "alignment"),
                        ):
                            if not item.get(field):
                                continue
                            file = paths.resolve_relative(str(item[field]))
                            if file.is_file():
                                relative = paths.relative(file)
                                candidates[relative] = {
                                    "role": f"clip.{clip_id}.{suffix}",
                                    "path": file,
                                    "project_id": project_id,
                                    "source_id": source_id,
                                }
    for export in paths.exports.rglob("*") if paths.exports.exists() else ():
        if not export.is_file():
            continue
        relative = paths.relative(export)
        digest = hashlib.sha256(relative.encode()).hexdigest()[:24]
        candidates[relative] = {
            "role": f"legacy.export_file.{digest}",
            "path": export,
            "project_id": None,
            "source_id": None,
        }
    return list(candidates.values())


def register_workspace_artifacts(
    catalog: StudioCatalog,
    paths: StudioPaths,
) -> dict[str, Any]:
    registered = 0
    bytes_hashed = 0
    for candidate in _registered_candidates(catalog, paths):
        path = candidate["path"]
        digest = candidate.get("known_sha256") or sha256_file(path)
        size = int(candidate.get("known_size", path.stat().st_size))
        catalog.register_artifact(
            role=str(candidate["role"]),
            relative_path=paths.relative(path),
            sha256=str(digest),
            size_bytes=size,
            media_type=mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            project_id=candidate.get("project_id"),
            source_id=candidate.get("source_id"),
        )
        registered += 1
        bytes_hashed += size
    return {"registered": registered, "bytes": bytes_hashed}


def verify_workspace(catalog: StudioCatalog, paths: StudioPaths) -> dict[str, Any]:
    missing: list[str] = []
    mismatched: list[str] = []
    artifacts = catalog.list_artifacts()
    for artifact in artifacts:
        try:
            path = paths.resolve_relative(str(artifact["relative_path"]))
        except ValueError:
            mismatched.append(str(artifact["id"]))
            continue
        if not path.is_file():
            missing.append(str(artifact["id"]))
            continue
        if path.stat().st_size != int(artifact["size_bytes"]):
            mismatched.append(str(artifact["id"]))
            continue
        if sha256_file(path) != artifact["sha256"]:
            mismatched.append(str(artifact["id"]))
    return {
        "valid": not missing and not mismatched,
        "artifact_count": len(artifacts),
        "missing": missing,
        "mismatched": mismatched,
        "schema_counts": _database_counts(paths.database),
    }


def migrate_workspace(
    workspace: Path,
    *,
    apply: bool = False,
    verify_only: bool = False,
    backup_directory: Path | None = None,
) -> dict[str, Any]:
    before = inspect_workspace(workspace)
    if not apply and not verify_only:
        return {"mode": "dry-run", "before": before, "changes_applied": False}
    if not before["database_exists"]:
        raise FileNotFoundError(before["database"])
    root = workspace.resolve()
    backup_path: Path | None = None
    if apply:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_root = (backup_directory or (root / "backups")).resolve()
        backup_path = backup_database(
            root / "catalog.sqlite3",
            backup_root / f"catalog.pre-migration.{timestamp}.sqlite3",
        )
    paths = StudioPaths(root)
    catalog = StudioCatalog(paths.database)
    registration = (
        register_workspace_artifacts(catalog, paths)
        if apply
        else {"registered": 0, "bytes": 0}
    )
    verification = verify_workspace(catalog, paths)
    with catalog.connect() as connection:
        schema_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
            ).fetchone()[0]
        )
    return {
        "mode": "apply" if apply else "verify",
        "before": before,
        "after": inspect_workspace(root),
        "backup": str(backup_path) if backup_path else None,
        "registration": registration,
        "schema_version": schema_version,
        "expected_schema_version": LATEST_SCHEMA_VERSION,
        "verification": verification,
        "changes_applied": apply,
    }
