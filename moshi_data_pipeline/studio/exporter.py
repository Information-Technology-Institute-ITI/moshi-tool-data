from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from typing import Any

from moshi_data_pipeline.audio.io import audio_info
from moshi_data_pipeline.audio.validation import validate_alignment_payload
from moshi_data_pipeline.cache import atomic_write_json, atomic_write_text
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.processing import clip_artifacts
from moshi_data_pipeline.studio.quality_metrics import (
    golden_records,
    source_quality_metrics,
)
from moshi_data_pipeline.studio.reproducibility import reproducibility_snapshot
from moshi_data_pipeline.transcription.quality import HIGH_RISK_TRANSCRIPT_FLAGS

Progress = Callable[[float, str], None]


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return cleaned[:80] or "dataset"


def _source_assignments(source_ids: list[str]) -> tuple[set[str], set[str], list[str]]:
    if len(source_ids) < 2:
        return set(source_ids), set(), [
            "A leakage-free evaluation manifest requires at least two approved sources."
        ]
    ordered = sorted(
        source_ids,
        key=lambda source_id: hashlib.sha256(source_id.encode()).hexdigest(),
    )
    eval_count = max(1, min(len(ordered) - 1, round(len(ordered) * 0.10)))
    evaluation = set(ordered[:eval_count])
    return set(ordered) - evaluation, evaluation, []


def _assistant_transcript_blocker(
    catalog: StudioCatalog,
    source_id: str,
    label: str,
    approved: list[dict[str, Any]],
) -> str | None:
    annotation = catalog.latest_annotation(source_id)
    assistant = annotation.assistant_speaker
    if assistant is None:
        return f"{label}: choose the Moshi speaker before export"
    clip_bounds = [
        (int(item["clip"]["start_sample"]), int(item["clip"]["end_sample"]))
        for item in approved
    ]
    utterances = [
        utterance
        for utterance in annotation.transcript
        if utterance.speaker == assistant
        and any(
            utterance.end_sample > start and utterance.start_sample < end
            for start, end in clip_bounds
        )
    ]
    if not utterances:
        return f"{label}: approved clips have no Moshi-speaker transcript"
    invalid_alignment = [
        value
        for value in utterances
        if not value.text.strip() or value.alignment_status != "aligned"
    ]
    if invalid_alignment:
        return (
            f"{label}: {len(invalid_alignment)} Moshi utterance(s) need valid "
            "text and word alignment"
        )
    suspicious = [
        value
        for value in utterances
        if HIGH_RISK_TRANSCRIPT_FLAGS.intersection(value.quality_flags)
        and not value.human_verified
    ]
    if suspicious:
        return (
            f"{label}: {len(suspicious)} suspicious Moshi utterance(s) must be "
            "corrected, realigned, and verified against the audio"
        )
    return None


def _collect_export_sources(
    catalog: StudioCatalog,
    paths: StudioPaths,
    project_id: str,
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
    list[str],
    list[str],
    list[dict[str, Any]],
]:
    included: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    source_reports: list[dict[str, Any]] = []
    for source in catalog.list_sources(project_id):
        label = str(source["original_name"])
        artifacts = clip_artifacts(catalog, paths, source["id"])
        report: dict[str, Any] = {
            "source_id": source["id"],
            "original_name": label,
            "status": "skipped",
            "generated_clips": len(artifacts.get("artifacts", [])),
            "approved_clips": 0,
            "messages": [],
        }
        if artifacts.get("stale") or not artifacts.get("artifacts"):
            message = f"{label}: no current generated clips; source will be skipped"
            warnings.append(message)
            report["messages"].append(message)
            source_reports.append(report)
            continue
        decisions = catalog.clip_decisions(source["id"])
        missing = [
            item["clip"]["id"]
            for item in artifacts["artifacts"]
            if item["clip"]["id"] not in decisions
            or decisions[item["clip"]["id"]]["decision"] == "needs_work"
        ]
        if missing:
            message = f"{label}: every generated clip needs a review decision"
            blockers.append(message)
            report["status"] = "blocked"
            report["messages"].append(message)
            source_reports.append(report)
            continue
        pending_overlaps = [
            value["region_id"]
            for value in catalog.overlap_recoveries(source["id"])
            if value["status"] == "recovered"
            and (
                value["decision"] not in {"approve", "reject"}
                or not value["auditioned"]
            )
        ]
        if pending_overlaps:
            message = (
                f"{label}: every successfully recovered overlap needs an "
                "auditioned approve/reject decision"
            )
            blockers.append(message)
            report["status"] = "blocked"
            report["messages"].append(message)
            source_reports.append(report)
            continue
        approved = [
            item
            for item in artifacts["artifacts"]
            if decisions[item["clip"]["id"]]["decision"] == "approve"
            and decisions[item["clip"]["id"]]["auditioned"]
        ]
        report["approved_clips"] = len(approved)
        rejected_qc = [
            item["clip"]["id"]
            for item in approved
            if item["qc"]["status"] == "REJECT"
        ]
        if rejected_qc:
            message = f"{label}: rejected-QC clips cannot be approved"
            blockers.append(message)
            report["status"] = "blocked"
            report["messages"].append(message)
            source_reports.append(report)
            continue
        if not approved:
            message = f"{label}: no clips were approved; source will be skipped"
            warnings.append(message)
            report["messages"].append(message)
            source_reports.append(report)
            continue
        transcript_blocker = _assistant_transcript_blocker(
            catalog, source["id"], label, approved
        )
        if transcript_blocker:
            blockers.append(transcript_blocker)
            report["status"] = "blocked"
            report["messages"].append(transcript_blocker)
            source_reports.append(report)
            continue
        if (
            not source["rights_confirmed"]
            or not source["origin"].strip()
            or not source["rights_basis"]
        ):
            message = f"{label}: source rights declaration is incomplete"
            blockers.append(message)
            report["status"] = "blocked"
            report["messages"].append(message)
            source_reports.append(report)
            continue
        report["status"] = "ready"
        source_reports.append(report)
        included.append((source, artifacts, approved))
    return included, blockers, warnings, source_reports


def validate_project_export(
    catalog: StudioCatalog,
    paths: StudioPaths,
    project_id: str,
) -> dict[str, Any]:
    included, blockers, warnings, source_reports = _collect_export_sources(
        catalog, paths, project_id
    )
    if not included:
        blockers.append("No fully reviewed and approved clips are ready for export")
    else:
        _, _, split_warnings = _source_assignments(
            [source["id"] for source, _, _ in included]
        )
        warnings.extend(split_warnings)
    return {
        "valid": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "ready_sources": len(included),
        "approved_clips": sum(len(approved) for _, _, approved in included),
        "sources": source_reports,
    }


def build_project_export(
    catalog: StudioCatalog,
    paths: StudioPaths,
    export_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    export = catalog.get_export(export_id)
    project = catalog.get_project(export["project_id"])
    included, blockers, validation_warnings, _ = _collect_export_sources(
        catalog, paths, project["id"]
    )
    if blockers:
        raise ValueError("; ".join(blockers))
    if not included:
        raise ValueError("No fully reviewed and approved clips are ready for export")
    train_sources, eval_sources, split_warnings = _source_assignments(
        [source["id"] for source, _, _ in included]
    )
    warnings = [*validation_warnings, *split_warnings]
    final_name = f"{_slug(export['name'])}_v{int(export['version']):03d}"
    final_root = paths.exports / final_name
    temporary = paths.exports / f".{export_id}.tmp"
    if final_root.exists():
        raise ValueError(f"Immutable export already exists: {final_name}")
    if temporary.exists():
        shutil.rmtree(temporary)
    stereo_root = temporary / "data_stereo"
    stereo_root.mkdir(parents=True, exist_ok=True)
    train_records: list[dict[str, Any]] = []
    eval_records: list[dict[str, Any]] = []
    qc_clips: list[dict[str, Any]] = []
    provenance_sources: list[dict[str, Any]] = []
    golden_examples: list[dict[str, Any]] = []
    source_quality: list[dict[str, Any]] = []
    total_clips = sum(len(approved) for _, _, approved in included)
    completed = 0
    try:
        for source, artifacts, approved in included:
            assignment = "eval" if source["id"] in eval_sources else "train"
            annotation = catalog.latest_annotation(source["id"])
            golden_examples.extend(golden_records(annotation, source["id"]))
            source_quality.append(
                {
                    "source_id": source["id"],
                    **source_quality_metrics(
                        annotation,
                        catalog.overlap_recoveries(source["id"]),
                    ),
                }
            )
            provenance_sources.append(
                {
                    "source_id": source["id"],
                    "original_name": source["original_name"],
                    "source_sha256": source["sha256"],
                    "origin": source["origin"],
                    "rights_basis": source["rights_basis"],
                    "rights_notes": source["rights_notes"],
                    "rights_confirmed": source["rights_confirmed"],
                    "assignment": assignment,
                    "annotation_version": artifacts["annotation_version"],
                    "speaker_references": [
                        value.model_dump(mode="json")
                        for value in annotation.speaker_references
                    ],
                }
            )
            for artifact in approved:
                completed += 1
                progress(
                    0.05 + 0.88 * completed / total_clips,
                    f"Copying approved clip {completed} of {total_clips}",
                )
                clip_id = artifact["clip"]["id"]
                output_stem = f"{source['id']}_{clip_id}"
                input_wav = paths.resolve_relative(artifact["wav_path"])
                input_json = paths.resolve_relative(artifact["json_path"])
                output_wav = stereo_root / f"{output_stem}.wav"
                output_json = stereo_root / f"{output_stem}.json"
                shutil.copy2(input_wav, output_wav)
                shutil.copy2(input_json, output_json)
                info = audio_info(output_wav)
                payload = json.loads(output_json.read_text(encoding="utf-8"))
                alignment_errors = validate_alignment_payload(
                    payload, float(info["duration"])
                )
                if alignment_errors:
                    raise ValueError(
                        f"{output_stem} failed alignment validation: {alignment_errors}"
                    )
                record = {
                    "path": f"data_stereo/{output_wav.name}",
                    "duration": float(info["duration"]),
                }
                if assignment == "eval":
                    eval_records.append(record)
                else:
                    train_records.append(record)
                qc_clips.append(
                    {
                        "source_id": source["id"],
                        "clip_id": clip_id,
                        "assignment": assignment,
                        "qc": artifact["qc"],
                        "raw_overlap_ratio": artifact["raw_overlap_ratio"],
                        "separation_used": artifact["separation_used"],
                        "recovery_method": artifact.get("recovery_method"),
                        "routing_method": artifact.get("routing_method"),
                    }
                )
        train_records.sort(key=lambda item: item["path"])
        eval_records.sort(key=lambda item: item["path"])
        atomic_write_text(
            temporary / "train.jsonl",
            "".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                for value in train_records
            ),
        )
        atomic_write_text(
            temporary / "eval.jsonl",
            "".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                for value in eval_records
            ),
        )
        golden_examples.sort(
            key=lambda value: (
                str(value["source_id"]),
                int(value["start_sample"]),
                str(value["utterance_id"]),
            )
        )
        atomic_write_text(
            temporary / "golden_regression.jsonl",
            "".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                for value in golden_examples
            ),
        )
        if len(golden_examples) < 20:
            warnings.append(
                "The golden regression set has fewer than 20 human-verified utterances."
            )
        report = {
            "project_id": project["id"],
            "project_name": project["name"],
            "export_id": export_id,
            "version": export["version"],
            "train_sources": sorted(train_sources),
            "eval_sources": sorted(eval_sources),
            "train_clips": len(train_records),
            "eval_clips": len(eval_records),
            "warnings": warnings,
            "clips": qc_clips,
            "source_quality": source_quality,
            "golden_examples": len(golden_examples),
        }
        atomic_write_json(temporary / "qc_summary.json", report)
        atomic_write_json(
            temporary / "provenance.json",
            {
                "project": project,
                "sources": provenance_sources,
                "split_policy": "deterministic source-level 90/10",
            },
        )
        atomic_write_json(
            temporary / "config.snapshot.json",
            config.model_dump(mode="json"),
        )
        atomic_write_json(
            temporary / "reproducibility.json",
            reproducibility_snapshot(
                config,
                paths.root,
                [path for path in temporary.rglob("*") if path.is_file()],
            ),
        )
        os.replace(temporary, final_root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    progress(1.0, "Immutable dataset export is complete")
    return {
        "path": paths.relative(final_root),
        "report": report,
    }
