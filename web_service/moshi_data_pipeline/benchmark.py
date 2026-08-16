from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, TypeVar

from moshi_data_pipeline.audio.ffmpeg import extract_working_wav, inspect_media
from moshi_data_pipeline.cache import load_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.pipeline import media_key
from moshi_data_pipeline.transcription.quality import normalized_tokens
from moshi_data_pipeline.transcription.whisperx_backend import WhisperXTranscriber

T = TypeVar("T")


def _distance(first: list[T], second: list[T]) -> int:
    previous = list(range(len(second) + 1))
    for left_index, left in enumerate(first, start=1):
        current = [left_index]
        for right_index, right in enumerate(second, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left != right),
                )
            )
        previous = current
    return previous[-1]


def _speaker_at(timestamp: float, segments: list[dict[str, Any]]) -> str | None:
    return next(
        (
            str(segment["speaker"])
            for segment in segments
            if float(segment["start"]) <= timestamp < float(segment["end"])
        ),
        None,
    )


def _text_error_metrics(prediction_text: str, gold_text: str) -> dict[str, float]:
    gold_tokens = normalized_tokens(gold_text)
    prediction_tokens = normalized_tokens(prediction_text)
    gold_characters = list("".join(gold_tokens))
    prediction_characters = list("".join(prediction_tokens))
    return {
        "word_error_rate": _distance(prediction_tokens, gold_tokens)
        / max(1, len(gold_tokens)),
        "character_error_rate": _distance(
            prediction_characters, gold_characters
        )
        / max(1, len(gold_characters)),
    }


def benchmark_profiles(
    input_path: Path,
    gold_path: Path,
    config: PipelineConfig,
    compute_types: tuple[str, ...] = ("int8", "float16"),
) -> dict[str, Any]:
    """Compare transcription compute types without writing production artifacts."""
    gold = load_json(gold_path)
    gold_segments = list(gold.get("segments", []))
    if not gold_segments:
        raise ValueError("Gold JSON must contain a non-empty segments list")
    gold_text = " ".join(str(segment.get("text", "")) for segment in gold_segments)
    inspection = inspect_media(input_path)
    source_duration = float(inspection["duration"])
    profiles: dict[str, dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix="moshi-benchmark-") as temporary:
        working_wav = Path(temporary) / "source_mono.wav"
        extract_working_wav(input_path, working_wav, config.audio.sample_rate)
        for compute_type in compute_types:
            transcription = config.transcription.model_copy(deep=True)
            transcription.compute_type = compute_type
            started = time.perf_counter()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.reset_peak_memory_stats()
                result = WhisperXTranscriber().transcribe(working_wav, transcription)
                elapsed = time.perf_counter() - started
                prediction_text = " ".join(
                    str(segment.get("text", "")) for segment in result.get("segments", [])
                )
                metrics = _text_error_metrics(prediction_text, gold_text)
                peak_memory = (
                    int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else 0
                )
                profiles[compute_type] = {
                    "status": "ok",
                    **metrics,
                    "duration_seconds": elapsed,
                    "realtime_factor": elapsed / source_duration,
                    "peak_gpu_memory_bytes": peak_memory,
                    "segment_count": len(result.get("segments", [])),
                    "suspect_segment_count": int(
                        result.get("quality", {}).get("suspect_segment_count", 0)
                    ),
                }
            except Exception as exc:
                profiles[compute_type] = {
                    "status": "failed",
                    "error": str(exc),
                    "duration_seconds": time.perf_counter() - started,
                }

    int8 = profiles.get("int8", {})
    float16 = profiles.get("float16", {})
    relative_cer_improvement = None
    if int8.get("status") == float16.get("status") == "ok":
        int8_cer = float(int8["character_error_rate"])
        float16_cer = float(float16["character_error_rate"])
        relative_cer_improvement = (
            (int8_cer - float16_cer) / int8_cer
            if int8_cer > 0
            else 0.0
        )
    float16_promotion_eligible = bool(
        relative_cer_improvement is not None
        and relative_cer_improvement >= 0.05
        and float(float16["realtime_factor"]) <= 2.0
        and int(float16["peak_gpu_memory_bytes"]) <= int(5.5 * 1024**3)
    )
    return {
        "input": str(input_path.resolve()),
        "gold": str(gold_path.resolve()),
        "source_duration_seconds": source_duration,
        "model": config.transcription.model,
        "language": config.transcription.language,
        "profiles": profiles,
        "relative_float16_cer_improvement": relative_cer_improvement,
        "float16_promotion_eligible": float16_promotion_eligible,
        "promotion_requirements": {
            "minimum_relative_cer_improvement": 0.05,
            "maximum_realtime_factor": 2.0,
            "maximum_peak_gpu_memory_bytes": int(5.5 * 1024**3),
        },
    }


def benchmark_dataset(
    input_path: Path,
    gold_path: Path,
    dataset_root: Path,
) -> dict[str, Any]:
    key = media_key(input_path.resolve())
    gold = load_json(gold_path)
    gold_segments = list(gold.get("segments", []))
    if not gold_segments:
        raise ValueError("Gold JSON must contain a non-empty segments list")
    prediction_path = dataset_root / "working" / key / "assigned_words.json"
    if not prediction_path.exists():
        raise ValueError(f"Prediction not found: {prediction_path}")
    predicted_words = load_json(prediction_path)
    gold_text = " ".join(str(segment.get("text", "")) for segment in gold_segments)
    prediction_text = " ".join(
        str(word.get("original") or word.get("word") or "") for word in predicted_words
    )
    text_metrics = _text_error_metrics(prediction_text, gold_text)

    gold_speakers = sorted(
        {str(segment["speaker"]) for segment in gold_segments if segment.get("speaker")}
    )
    predicted_speakers = sorted(
        {str(word["speaker"]) for word in predicted_words if word.get("speaker")}
    )
    mappings: list[dict[str, str]] = []
    if len(gold_speakers) == len(predicted_speakers) == 2:
        mappings = [
            dict(zip(predicted_speakers, gold_speakers, strict=True)),
            dict(zip(predicted_speakers, reversed(gold_speakers), strict=True)),
        ]
    elif predicted_speakers:
        mappings = [{speaker: speaker for speaker in predicted_speakers}]
    best_speaker_errors = None
    evaluated_words = 0
    for mapping in mappings:
        errors = 0
        evaluated = 0
        for word in predicted_words:
            if word.get("start") is None or word.get("end") is None or not word.get("speaker"):
                continue
            timestamp = (float(word["start"]) + float(word["end"])) / 2
            expected = _speaker_at(timestamp, gold_segments)
            if expected is None:
                continue
            evaluated += 1
            errors += mapping.get(str(word["speaker"])) != expected
        if best_speaker_errors is None or errors < best_speaker_errors:
            best_speaker_errors = errors
            evaluated_words = evaluated

    qc_path = dataset_root / "reports" / f"{key}_qc.json"
    performance_path = dataset_root / "reports" / f"{key}_performance.json"
    qc = load_json(qc_path) if qc_path.exists() else {}
    performance = load_json(performance_path) if performance_path.exists() else {}
    clips = qc.get("clips", [])
    accepted = [clip for clip in clips if clip.get("status") == "PASS"]
    balance_failures = [
        clip["clip_id"]
        for clip in accepted
        if min(
            float(clip.get("metrics", {}).get("assistant_speech_share", 1.0)),
            float(clip.get("metrics", {}).get("user_speech_share", 1.0)),
        )
        < 0.12
    ]
    unresolved = int(
        qc.get("transcription_quality", {}).get("unresolved_hallucination_count", 0)
    )
    speaker_error_rate = (
        best_speaker_errors / evaluated_words
        if best_speaker_errors is not None and evaluated_words
        else None
    )
    return {
        "input": str(input_path.resolve()),
        "gold": str(gold_path.resolve()),
        "prediction": str(prediction_path.resolve()),
        **text_metrics,
        "speaker_assignment_error_rate": speaker_error_rate,
        "speaker_words_evaluated": evaluated_words,
        "unresolved_hallucinations": unresolved,
        "accepted_clip_count": len(accepted),
        "accepted_balance_failures": balance_failures,
        "realtime_factor": performance.get("realtime_factor"),
        "peak_gpu_memory_bytes": performance.get("peak_gpu_memory_bytes"),
        "acceptance": {
            "no_unflagged_hallucination": unresolved == 0,
            "zero_accepted_speaker_swaps": speaker_error_rate in {None, 0.0},
            "all_accepted_clips_balanced": not balance_failures,
            "runtime_at_most_2x": (
                performance.get("realtime_factor") is not None
                and float(performance["realtime_factor"]) <= 2.0
            ),
            "gpu_memory_below_5_5_gib": (
                performance.get("peak_gpu_memory_bytes") is not None
                and int(performance["peak_gpu_memory_bytes"]) <= int(5.5 * 1024**3)
            ),
        },
    }
