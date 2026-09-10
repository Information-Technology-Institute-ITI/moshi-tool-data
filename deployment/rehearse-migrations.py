from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.migrations import LATEST_SCHEMA_VERSION, apply_migrations


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
        if table != "schema_migrations"
    }


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schema_version": schema_version(connection),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        "tables": table_counts(connection),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up a Studio catalog and replay numbered migrations on the backup."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--replay-current",
        action="store_true",
        help="Clear migration ledger entries in the backup and replay all idempotent migrations.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = args.source.resolve()
    destination_path = args.destination.resolve()
    if not source_path.is_file():
        raise SystemExit(f"Source database does not exist: {source_path}")
    if destination_path.exists():
        raise SystemExit(f"Refusing to overwrite rehearsal database: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        before = snapshot(destination)
        if args.replay_current:
            with destination:
                destination.execute("DELETE FROM schema_migrations")
        applied = apply_migrations(destination)
        after = snapshot(destination)
        integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in destination.execute("PRAGMA foreign_key_check")]
    finally:
        destination.close()
        source.close()

    counts_preserved = before["tables"] == after["tables"]
    valid = (
        integrity == "ok"
        and not foreign_keys
        and counts_preserved
        and after["schema_version"] == LATEST_SCHEMA_VERSION
    )
    report = {
        "source": str(source_path),
        "rehearsal_database": str(destination_path),
        "replay_current": bool(args.replay_current),
        "applied_migrations": applied,
        "before": before,
        "after": after,
        "counts_preserved": counts_preserved,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "valid": valid,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

