from __future__ import annotations

import json
import math
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np

from moshi_data_pipeline.audio.io import audio_info, read_audio
from moshi_data_pipeline.audio.metrics import channel_metrics, leakage_indicators
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.models import QCResult, QCStatus


def load_schema(name: str) -> dict[str, Any]:
    schema_path = files("moshi_data_pipeline").joinpath("schemas", name)
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_alignment_payload(payload: dict[str, Any], duration: float) -> list[str]:
    reasons: list[str] = []
    try:
        jsonschema.validate(payload, load_schema("moshi_alignment.schema.json"))
    except jsonschema.ValidationError:
        return ["invalid_alignment_schema"]
    last_start = -1.0
    for _, timestamp, speaker in payload["alignments"]:
        start, end = timestamp
        if speaker != "SPEAKER_MAIN":
            reasons.append("unexpected_alignment_speaker")
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end > duration + 1e-6
            or start >= end
        ):
            reasons.append("alignment_timestamp_out_of_bounds")
        if start < last_start:
            reasons.append("alignment_timestamps_not_ordered")
        last_start = start
    return sorted(set(reasons))


def validate_qc_payload(payload: dict[str, Any]) -> None:
    jsonschema.validate(payload, load_schema("qc_report.schema.json"))


def validate_clip(
    wav_path: Path,
    json_path: Path,
    config: PipelineConfig,
    *,
    assistant_mask: np.ndarray | None = None,
    user_mask: np.ndarray | None = None,
    overlap_ratio: float = 0.0,
    unaligned_words: int = 0,
    low_confidence_words: int = 0,
    total_words: int = 0,
    uncertain_word_assignments: int = 0,
    suspect_transcript_segments: int = 0,
    unresolved_hallucinations: int = 0,
    separation_coverage: float = 0.0,
    separation_used: bool = False,
    expected_duration: float | None = None,
) -> QCResult:
    reasons: list[str] = []
    status = QCStatus.PASS
    if not wav_path.exists():
        return QCResult(QCStatus.REJECT, ["wav_missing"], {}, path=str(wav_path))
    if not json_path.exists():
        return QCResult(QCStatus.REJECT, ["json_missing"], {}, path=str(wav_path))
    try:
        info = audio_info(wav_path)
        stereo, sample_rate = read_audio(wav_path)
    except Exception:
        return QCResult(QCStatus.REJECT, ["wav_unreadable"], {}, path=str(wav_path))
    if info["channels"] != 2:
        reasons.append("wav_not_stereo")
    if sample_rate != config.audio.sample_rate:
        reasons.append("wrong_sample_rate")
    if info["samples"] <= 0 or info["duration"] <= 0:
        reasons.append("invalid_duration")
    if stereo.shape[1] != 2:
        reasons.append("channel_count_mismatch")

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        reasons.extend(validate_alignment_payload(payload, float(info["duration"])))
    except (OSError, json.JSONDecodeError):
        payload = {}
        reasons.append("json_unreadable")

    metrics: dict[str, Any] = {
        "duration": float(info["duration"]),
        "sample_rate": sample_rate,
        "channels": int(info["channels"]),
        "samples_per_channel": int(info["samples"]),
        "overlap_ratio": float(overlap_ratio),
        "unaligned_words": int(unaligned_words),
        "low_confidence_words": int(low_confidence_words),
        "total_words": int(total_words),
        "uncertain_word_assignments": int(uncertain_word_assignments),
        "suspect_transcript_segments": int(suspect_transcript_segments),
        "unresolved_hallucinations": int(unresolved_hallucinations),
        "separation_coverage": float(separation_coverage),
        "separation_used": bool(separation_used),
    }
    low_confidence_ratio = low_confidence_words / total_words if total_words else 0.0
    uncertain_assignment_ratio = (
        uncertain_word_assignments / total_words if total_words else 0.0
    )
    metrics["low_confidence_word_ratio"] = low_confidence_ratio
    metrics["uncertain_assignment_ratio"] = uncertain_assignment_ratio
    if expected_duration is not None:
        metrics["expected_duration"] = float(expected_duration)
        metrics["duration_delta"] = abs(float(info["duration"]) - expected_duration)
        if metrics["duration_delta"] > 1.0 / config.audio.sample_rate + 1e-9:
            reasons.append("transcript_to_audio_duration_inconsistency")
    if stereo.shape[1] == 2:
        metrics.update(channel_metrics(stereo, config.qc.silence_threshold_db))
        if metrics["left_rms"] == 0:
            reasons.append("left_channel_silent")
        if metrics["right_rms"] == 0:
            reasons.append("right_channel_silent")
        if max(metrics["left_silence_ratio"], metrics["right_silence_ratio"]) > (
            config.qc.max_channel_silence_ratio
        ):
            reasons.append("channel_silence_ratio_above_reject_threshold")
        maximum_clipping = max(metrics["left_clipping_ratio"], metrics["right_clipping_ratio"])
        if maximum_clipping > config.qc.clipping_reject_ratio:
            reasons.append("clipping_ratio_above_reject_threshold")
        elif maximum_clipping > config.qc.clipping_review_ratio:
            reasons.append("clipping_ratio_above_warning_threshold")
        if assistant_mask is not None and user_mask is not None:
            leakage = leakage_indicators(stereo, assistant_mask, user_mask)
            metrics.update(leakage)
            if leakage["right_leakage_in_assistant_intervals"] > config.qc.leakage_review_ratio:
                reasons.append("possible_right_channel_leakage")
            if leakage["left_leakage_in_user_intervals"] > config.qc.leakage_review_ratio:
                reasons.append("possible_left_channel_leakage")
            if leakage["possible_swapped_channels"]:
                reasons.append("possible_swapped_channels")
            if leakage["left_expected_energy"] < leakage["right_expected_energy"] * 1e-4:
                reasons.append("possible_swapped_or_missing_left_channel")
    unrecovered_overlap_ratio = overlap_ratio * max(0.0, 1.0 - separation_coverage)
    metrics["unrecovered_overlap_ratio"] = unrecovered_overlap_ratio
    if separation_used:
        reasons.append("separated_overlap_requires_review")
    if overlap_ratio > config.segmentation.max_overlap_ratio and separation_coverage < 0.95:
        reasons.append("overlap_separation_incomplete")
    elif unrecovered_overlap_ratio > config.segmentation.max_overlap_ratio:
        reasons.append("overlap_ratio_above_reject_threshold")
    elif unrecovered_overlap_ratio > config.segmentation.overlap_warning_ratio:
        reasons.append("overlap_ratio_above_warning_threshold")
    if unaligned_words:
        reasons.append("unaligned_words_present")
    if low_confidence_ratio > config.qc.low_confidence_reject_ratio:
        reasons.append("low_confidence_ratio_above_reject_threshold")
    elif low_confidence_ratio > config.qc.low_confidence_review_ratio:
        reasons.append("low_confidence_words_present")
    if uncertain_assignment_ratio > config.qc.uncertain_assignment_reject_ratio:
        reasons.append("uncertain_assignment_ratio_above_reject_threshold")
    elif uncertain_assignment_ratio > config.qc.uncertain_assignment_review_ratio:
        reasons.append("uncertain_word_assignments_present")
    if suspect_transcript_segments:
        reasons.append("suspect_transcript_segments_present")
    if unresolved_hallucinations:
        reasons.append("unresolved_transcript_hallucination")

    reject_reasons = {
        "wav_not_stereo",
        "wrong_sample_rate",
        "invalid_duration",
        "channel_count_mismatch",
        "invalid_alignment_schema",
        "alignment_timestamp_out_of_bounds",
        "alignment_timestamps_not_ordered",
        "json_unreadable",
        "left_channel_silent",
        "right_channel_silent",
        "channel_silence_ratio_above_reject_threshold",
        "clipping_ratio_above_reject_threshold",
        "overlap_ratio_above_reject_threshold",
        "overlap_separation_incomplete",
        "low_confidence_ratio_above_reject_threshold",
        "uncertain_assignment_ratio_above_reject_threshold",
        "unresolved_transcript_hallucination",
    }
    if reject_reasons.intersection(reasons):
        status = QCStatus.REJECT
    elif reasons:
        status = QCStatus.REVIEW
    return QCResult(status, sorted(set(reasons)), metrics, path=str(wav_path))
