from __future__ import annotations

from typing import Any

from moshi_data_pipeline.config import NormalizationConfig
from moshi_data_pipeline.models import Word
from moshi_data_pipeline.transcription.normalization import normalize_word


def build_moshi_payload(
    words: list[Word],
    assistant_speaker: str,
    clip_start: float,
    clip_end: float,
    normalization: NormalizationConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    duration = clip_end - clip_start
    alignments: list[list[Any]] = []
    skipped: list[dict[str, Any]] = []
    transcript_report: list[dict[str, Any]] = []
    for word in sorted(
        words,
        key=lambda item: (
            float("inf") if item.start is None else item.start,
            float("inf") if item.end is None else item.end,
        ),
    ):
        if word.speaker != assistant_speaker:
            continue
        if word.start is None or word.end is None:
            skipped.append({"word": word.word, "reason": "unaligned"})
            continue
        if word.end <= clip_start or word.start >= clip_end:
            continue
        if word.start < clip_start or word.end > clip_end:
            skipped.append({"word": word.word, "reason": "crosses_clip_boundary"})
            continue
        start = max(0.0, word.start - clip_start)
        end = min(duration, word.end - clip_start)
        normalized = normalize_word(word.word, normalization)
        transcript_report.append(
            {
                "original": word.original or word.word,
                "normalized": normalized,
                "start": start,
                "end": end,
                "score": word.score,
            }
        )
        if not normalized:
            skipped.append({"word": word.word, "reason": "empty_after_normalization"})
            continue
        if start < 0 or end > duration or start >= end:
            skipped.append({"word": word.word, "reason": "invalid_timestamp"})
            continue
        alignments.append([normalized, [float(start), float(end)], "SPEAKER_MAIN"])
    return {"alignments": alignments}, {
        "assistant_speaker": assistant_speaker,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "original_and_normalized": transcript_report,
        "skipped_words": skipped,
    }
