from __future__ import annotations

import argparse
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moshi_data_pipeline.studio.auth import (  # noqa: E402
    ActivationMailer,
    AuthenticationService,
    AuthSettings,
    normalize_email,
    validate_display_name,
    validate_password,
)
from moshi_data_pipeline.studio.catalog import StudioCatalog  # noqa: E402

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
EXPECTED_HEADERS = ("name", "email", "password")


@dataclass(frozen=True)
class UserRecord:
    display_name: str
    email: str
    password: str


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(f"{{{MAIN_NS}}}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))
    return values


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find(f".//{{{MAIN_NS}}}sheet")
    if sheet is None:
        raise ValueError("The workbook has no worksheets")
    relationship_id = sheet.attrib.get(f"{{{REL_NS}}}id")
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relationship in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib["Target"].replace("\\", "/")
            return target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
    raise ValueError("The first worksheet relationship is missing")


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
    value = cell.find(f"{{{MAIN_NS}}}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        return shared[int(value.text)]
    return value.text


def read_users(path: Path) -> list[UserRecord]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet = ElementTree.fromstring(archive.read(_first_sheet_path(archive)))
    rows = sheet.findall(f".//{{{MAIN_NS}}}row")
    if not rows:
        raise ValueError("The workbook is empty")
    values = [
        [_cell_value(cell, shared).strip() for cell in row.findall(f"{{{MAIN_NS}}}c")]
        for row in rows
    ]
    headers = tuple(value.casefold() for value in values[0])
    if headers != EXPECTED_HEADERS:
        raise ValueError("The workbook headers must be Name, Email, Password")

    records: list[UserRecord] = []
    seen: set[str] = set()
    for row_number, row in enumerate(values[1:], start=2):
        if not any(row):
            continue
        if len(row) != 3:
            raise ValueError(f"Workbook row {row_number} must contain three values")
        display_name = validate_display_name(row[0])
        email = normalize_email(row[1])
        password = validate_password(row[2])
        if email in seen:
            raise ValueError(f"Workbook row {row_number} contains a duplicate email")
        seen.add(email)
        records.append(UserRecord(display_name, email, password))
    if not records:
        raise ValueError("The workbook has no user rows")
    return records


def backup_database(database: Path) -> Path | None:
    if not database.exists():
        return None
    backup_directory = database.parent / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_directory / f"catalog-pre-user-import-{timestamp}.sqlite3"
    source = sqlite3.connect(database)
    target = sqlite3.connect(backup)
    try:
        with target:
            source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError("The SQLite backup failed its integrity check")
    finally:
        target.close()
        source.close()
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import pending web users from an XLSX workbook.")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--xlsx", type=Path, required=True)
    parser.add_argument("--group", default="Alexandria Persona")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = read_users(args.xlsx.resolve())
    settings = AuthSettings.from_environment()
    if not settings.email_configured:
        raise RuntimeError("SMTP and MOSHI_PUBLIC_ORIGIN must be configured before importing users")
    if args.dry_run:
        print(f"Validated {len(records)} user rows; no database changes made.")
        return 0

    workspace = args.workspace.resolve()
    database = workspace / "catalog.sqlite3"
    mailer = ActivationMailer(settings)
    mailer.check_connection()
    backup = backup_database(database)
    catalog = StudioCatalog(database)
    authentication = AuthenticationService(catalog, settings, mailer=mailer)
    issued = 0
    existing = 0
    for record in records:
        _, activation_issued = authentication.signup(
            email=record.email,
            password=record.password,
            display_name=record.display_name,
            group_name=args.group,
            send_email=True,
        )
        if activation_issued:
            issued += 1
        else:
            existing += 1
    print(f"Imported {len(records)} users; activation emails sent: {issued}.")
    if existing:
        print(f"Accounts unchanged or awaiting resend cooldown: {existing}.")
    if backup is not None:
        print(f"Verified database backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
