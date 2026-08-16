from __future__ import annotations

from typing import Any

from moshi_data_pipeline.models import Word
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    ExclusionRegion,
)


def _overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return min(end, other_end) > max(start, other_start)


def close_word_supported_activity_gaps(
    activities: list[ActivityRegion],
    words: list[Word],
    exclusions: list[ExclusionRegion],
    maximum_gap_seconds: float,
) -> tuple[list[ActivityRegion], list[dict[str, Any]]]:
    """Bridge a model dropout only when aligned speech supports the same speaker.

    Natural pauses remain gaps. Opposite-speaker activity and exclusions always block
    a bridge so overlap and intentionally muted audio retain their exact boundaries.
    """
    maximum_gap_samples = max(0, round(maximum_gap_seconds * SAMPLE_RATE))
    if maximum_gap_samples == 0 or len(activities) < 2:
        return sorted(
            activities,
            key=lambda value: (value.start_sample, value.end_sample, value.speaker),
        ), []

    word_intervals: dict[str, list[tuple[int, int]]] = {"A": [], "B": []}
    for word in words:
        if (
            word.speaker not in word_intervals
            or word.start is None
            or word.end is None
            or word.end <= word.start
            or not word.word.strip()
        ):
            continue
        word_intervals[word.speaker].append(
            (round(word.start * SAMPLE_RATE), round(word.end * SAMPLE_RATE))
        )

    repaired: list[ActivityRegion] = []
    repairs: list[dict[str, Any]] = []
    for speaker in ("A", "B"):
        speaker_regions = sorted(
            (value for value in activities if value.speaker == speaker),
            key=lambda value: (value.start_sample, value.end_sample),
        )
        merged: list[ActivityRegion] = []
        for region in speaker_regions:
            if not merged:
                merged.append(region)
                continue
            previous = merged[-1]
            gap_start = previous.end_sample
            gap_end = region.start_sample
            gap_samples = gap_end - gap_start
            if gap_samples <= 0:
                merged[-1] = previous.model_copy(
                    update={"end_sample": max(previous.end_sample, region.end_sample)}
                )
                continue
            if gap_samples > maximum_gap_samples:
                merged.append(region)
                continue
            opposite_speaker_present = any(
                value.speaker != speaker
                and _overlaps(
                    gap_start,
                    gap_end,
                    value.start_sample,
                    value.end_sample,
                )
                for value in activities
            )
            exclusion_present = any(
                _overlaps(
                    gap_start,
                    gap_end,
                    value.start_sample,
                    value.end_sample,
                )
                for value in exclusions
            )
            aligned_speech_present = any(
                _overlaps(gap_start, gap_end, start, end)
                for start, end in word_intervals[speaker]
            )
            if opposite_speaker_present or exclusion_present or not aligned_speech_present:
                merged.append(region)
                continue
            merged[-1] = previous.model_copy(update={"end_sample": region.end_sample})
            repairs.append(
                {
                    "speaker": speaker,
                    "start_sample": gap_start,
                    "end_sample": gap_end,
                    "gap_seconds": gap_samples / SAMPLE_RATE,
                }
            )
        repaired.extend(merged)
    return sorted(
        repaired,
        key=lambda value: (value.start_sample, value.end_sample, value.speaker),
    ), repairs
