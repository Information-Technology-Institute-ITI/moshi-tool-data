from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from moshi_data_pipeline.studio.reproducibility import sha256_file

SNAPSHOT_FORMAT = "moshi.transcript-protection/v1"
TRANSCRIPT_ARTIFACT_ROLES = (
    "analysis.raw_transcript",
    "analysis.aligned_transcript",
    "analysis.diarization",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _inside(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or relative in {Path(""), Path("."), Path("..")}:
        raise ValueError(f"Unsafe workspace-relative path: {relative_path}")
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {relative_path}")
    return resolved


def _copy_catalog(source_path: Path, destination_path: Path) -> None:
    source = _read_only(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode = DELETE")
        destination.commit()
    finally:
        destination.close()
        source.close()


def _write_annotation_export(
    connection: sqlite3.Connection,
    destination: Path,
) -> dict[str, int]:
    revision_count = 0
    source_ids: set[str] = set()
    with gzip.open(destination, "wt", encoding="utf-8", newline="\n", compresslevel=6) as stream:
        has_shared_blobs = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='annotation_shared_blobs'
            """
        ).fetchone() is not None
        rows = connection.execute(
            """
            SELECT r.source_id,r.version,r.created_at,r.annotation_json,
                   b.payload_json AS shared_aligned_words_json
            FROM annotation_revisions r
            LEFT JOIN annotation_shared_blobs b ON b.id=r.shared_aligned_words_id
            ORDER BY r.source_id,r.version
            """
            if has_shared_blobs
            else """
            SELECT source_id,version,created_at,annotation_json,
                   NULL AS shared_aligned_words_json
            FROM annotation_revisions
            ORDER BY source_id,version
            """
        )
        for row in rows:
            source_id = str(row["source_id"])
            annotation = json.loads(str(row["annotation_json"]))
            if row["shared_aligned_words_json"]:
                annotation["aligned_words"] = json.loads(
                    str(row["shared_aligned_words_json"])
                )
            payload = {
                "source_id": source_id,
                "version": int(row["version"]),
                "created_at": str(row["created_at"]),
                "annotation": annotation,
            }
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
            revision_count += 1
            source_ids.add(source_id)
    return {"revision_count": revision_count, "source_count": len(source_ids)}


def _snapshot_artifacts(
    connection: sqlite3.Connection,
    workspace: Path,
    temporary: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    placeholders = ",".join("?" for _ in TRANSCRIPT_ARTIFACT_ROLES)
    rows = connection.execute(
        f"""
        SELECT id,role,relative_path,sha256,size_bytes,source_id,producing_job_id,state,created_at
        FROM artifacts
        WHERE role IN ({placeholders})
        ORDER BY source_id,role,created_at,id
        """,
        TRANSCRIPT_ARTIFACT_ROLES,
    ).fetchall()
    copied: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        relative_path = str(row["relative_path"])
        source = _inside(workspace, relative_path)
        suffix = Path(relative_path).suffix.lower()
        backup_relative = Path("a") / f"{index:06d}{suffix}"
        destination = temporary / backup_relative
        problem: str | None = None
        if str(row["state"]) == "missing":
            problem = "catalog state is missing"
        elif not source.is_file():
            problem = "artifact file is absent"
        if problem is not None:
            problems.append(
                {
                    "artifact_id": str(row["id"]),
                    "relative_path": relative_path,
                    "problem": problem,
                }
            )
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        digest = sha256_file(destination)
        size_bytes = destination.stat().st_size
        if digest != str(row["sha256"]) or size_bytes != int(row["size_bytes"]):
            problems.append(
                {
                    "artifact_id": str(row["id"]),
                    "relative_path": relative_path,
                    "problem": "copied file does not match registered checksum or size",
                }
            )
        copied.append(
            {
                "artifact_id": str(row["id"]),
                "role": str(row["role"]),
                "source_id": str(row["source_id"]) if row["source_id"] is not None else None,
                "producing_job_id": (
                    str(row["producing_job_id"])
                    if row["producing_job_id"] is not None
                    else None
                ),
                "source_relative_path": relative_path,
                "snapshot_relative_path": backup_relative.as_posix(),
                "sha256": digest,
                "size_bytes": size_bytes,
                "created_at": str(row["created_at"]),
            }
        )
    return copied, problems


def _write_checksums(root: Path) -> None:
    lines: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "SHA256SUMS":
            continue
        relative = path.relative_to(root).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_transcript_snapshot(workspace: Path, destination: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    destination = destination.resolve()
    database = workspace / "catalog.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Studio catalog does not exist: {database}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite transcript snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.partial-{uuid4().hex}"
    temporary.mkdir()
    try:
        catalog_copy = temporary / "catalog.sqlite3"
        _copy_catalog(database, catalog_copy)
        connection = _read_only(catalog_copy)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_errors = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
            annotation = _write_annotation_export(
                connection,
                temporary / "annotation_revisions.jsonl.gz",
            )
            artifacts, artifact_problems = _snapshot_artifacts(
                connection,
                workspace,
                temporary,
            )
            source_count = int(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
            project_count = int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        finally:
            connection.close()

        complete = integrity == "ok" and not foreign_key_errors and not artifact_problems
        manifest = {
            "format": SNAPSHOT_FORMAT,
            "created_at": _utc_now(),
            "complete": complete,
            "workspace": str(workspace),
            "catalog": {
                "relative_path": "catalog.sqlite3",
                "sha256": sha256_file(catalog_copy),
                "size_bytes": catalog_copy.stat().st_size,
                "integrity_check": integrity,
                "foreign_key_errors": foreign_key_errors,
            },
            "annotations": {
                **annotation,
                "relative_path": "annotation_revisions.jsonl.gz",
                "sha256": sha256_file(temporary / "annotation_revisions.jsonl.gz"),
                "size_bytes": (temporary / "annotation_revisions.jsonl.gz").stat().st_size,
            },
            "project_count": project_count,
            "source_count": source_count,
            "artifact_roles": list(TRANSCRIPT_ARTIFACT_ROLES),
            "artifacts": artifacts,
            "artifact_problems": artifact_problems,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_checksums(temporary)
        os.replace(temporary, destination)
        return manifest
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def verify_transcript_snapshot(snapshot: Path) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    manifest_path = snapshot / "manifest.json"
    checksums_path = snapshot / "SHA256SUMS"
    if not manifest_path.is_file() or not checksums_path.is_file():
        return {"valid": False, "problems": ["manifest.json or SHA256SUMS is missing"]}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest.get("format") != SNAPSHOT_FORMAT:
        problems.append("unsupported snapshot format")

    expected_files: set[str] = set()
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        try:
            expected, relative_path = line.split("  ", 1)
            path = _inside(snapshot, relative_path)
        except ValueError as error:
            problems.append(str(error))
            continue
        expected_files.add(relative_path)
        if not path.is_file():
            problems.append(f"missing file: {relative_path}")
        elif sha256_file(path) != expected:
            problems.append(f"checksum mismatch: {relative_path}")

    actual_files = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    for relative_path in sorted(actual_files - expected_files):
        problems.append(f"unlisted file: {relative_path}")

    database = snapshot / "catalog.sqlite3"
    if database.is_file():
        connection = _read_only(database)
        try:
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                problems.append("catalog integrity check failed")
            if list(connection.execute("PRAGMA foreign_key_check")):
                problems.append("catalog foreign-key check failed")
        finally:
            connection.close()

    export = snapshot / "annotation_revisions.jsonl.gz"
    exported_count = 0
    if export.is_file():
        try:
            with gzip.open(export, "rt", encoding="utf-8") as stream:
                for line in stream:
                    json.loads(line)
                    exported_count += 1
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            problems.append(f"annotation export is unreadable: {error}")
    if exported_count != int(manifest.get("annotations", {}).get("revision_count", -1)):
        problems.append("annotation revision count does not match manifest")
    if not manifest.get("complete", False):
        problems.append("snapshot manifest is marked incomplete")
    return {
        "valid": not problems,
        "format": manifest.get("format"),
        "created_at": manifest.get("created_at"),
        "revision_count": exported_count,
        "artifact_count": len(manifest.get("artifacts", [])),
        "problems": problems,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify a protected Studio transcript snapshot."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a new append-only snapshot directory.")
    create.add_argument("workspace", type=Path)
    create.add_argument("destination", type=Path)
    verify = commands.add_parser("verify", help="Verify checksums and readable snapshot content.")
    verify.add_argument("snapshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create":
        report = create_transcript_snapshot(args.workspace, args.destination)
        summary = {
            "destination": str(args.destination.resolve()),
            "complete": report["complete"],
            "revision_count": report["annotations"]["revision_count"],
            "artifact_count": len(report["artifacts"]),
            "artifact_problem_count": len(report["artifact_problems"]),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if report["complete"] else 1
    report = verify_transcript_snapshot(args.snapshot)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
