from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from moshi_data_pipeline.audio.io import audio_info
from moshi_data_pipeline.audio.validation import load_schema, validate_alignment_payload
from moshi_data_pipeline.cache import atomic_write_json, atomic_write_text, load_json
from moshi_data_pipeline.models import QCStatus


def _qc_index(dataset_root: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for report_path in sorted((dataset_root / "reports").glob("*_qc.json")):
        try:
            report = load_json(report_path)
        except (OSError, ValueError):
            continue
        for clip in report.get("clips", []):
            if clip.get("path") and clip.get("status"):
                index[str(clip["path"]).replace("\\", "/")] = str(clip["status"])
    return index


def rebuild_manifest(
    dataset_root: Path,
    *,
    include_approved_reviews: bool = False,
) -> list[dict[str, Any]]:
    stereo_root = dataset_root / "data_stereo"
    qc_index = _qc_index(dataset_root)
    approval_path = dataset_root / "reports" / "manual_approvals.json"
    approvals = set()
    if include_approved_reviews and approval_path.exists():
        approvals = set(load_json(approval_path).get("approved_paths", []))
    records: dict[str, dict[str, Any]] = {}
    for wav_path in sorted(stereo_root.glob("*.wav"), key=lambda item: item.name.casefold()):
        relative = wav_path.relative_to(dataset_root).as_posix()
        status = qc_index.get(relative)
        if status != QCStatus.PASS.value and not (
            status == QCStatus.REVIEW.value and relative in approvals
        ):
            continue
        json_path = wav_path.with_suffix(".json")
        if not json_path.exists():
            continue
        info = audio_info(wav_path)
        if info["channels"] != 2 or info["sample_rate"] != 24_000:
            continue
        try:
            payload = load_json(json_path)
        except (OSError, ValueError):
            continue
        if validate_alignment_payload(payload, float(info["duration"])):
            continue
        record = {"path": relative, "duration": float(info["duration"])}
        jsonschema.validate(record, load_schema("manifest_record.schema.json"))
        records[relative] = record
    ordered = [records[key] for key in sorted(records)]
    contents = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in ordered
    )
    atomic_write_text(dataset_root / "train.jsonl", contents)
    return ordered


def approve_review_path(dataset_root: Path, relative_path: str) -> None:
    normalized = Path(relative_path).as_posix()
    qc_index = _qc_index(dataset_root)
    if qc_index.get(normalized) != QCStatus.REVIEW.value:
        raise ValueError(f"{normalized} is not a REVIEW clip")
    approval_path = dataset_root / "reports" / "manual_approvals.json"
    current = load_json(approval_path) if approval_path.exists() else {}
    approved = set(current.get("approved_paths", []))
    approved.add(normalized)
    atomic_write_json(approval_path, {"approved_paths": sorted(approved)})
