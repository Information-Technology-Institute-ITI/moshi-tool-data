from __future__ import annotations

from math import isfinite
from typing import Any

from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    AnnotationDocument,
)


def _bounded_sample(value: int, duration_samples: int) -> int:
    return max(0, min(duration_samples, int(value)))


def _bounded_seconds(value: Any, duration_seconds: float) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result):
        return None
    return max(0.0, min(duration_seconds, result))


def normalize_annotation_bounds(
    annotation: AnnotationDocument, duration_samples: int
) -> AnnotationDocument:
    """Clamp model and browser timestamps to the canonical 24 kHz source."""
    if duration_samples <= 0:
        return annotation

    activities = []
    for region in annotation.activities:
        start = _bounded_sample(region.start_sample, duration_samples)
        end = _bounded_sample(region.end_sample, duration_samples)
        if end > start:
            activities.append(
                region.model_copy(update={"start_sample": start, "end_sample": end})
            )

    speaker_references = []
    for region in annotation.speaker_references:
        start = _bounded_sample(region.start_sample, duration_samples)
        end = _bounded_sample(region.end_sample, duration_samples)
        if end > start:
            speaker_references.append(
                region.model_copy(update={"start_sample": start, "end_sample": end})
            )

    exclusions = []
    for region in annotation.exclusions:
        start = _bounded_sample(region.start_sample, duration_samples)
        end = _bounded_sample(region.end_sample, duration_samples)
        if end > start:
            exclusions.append(
                region.model_copy(update={"start_sample": start, "end_sample": end})
            )

    transcript = []
    for utterance in annotation.transcript:
        start = _bounded_sample(utterance.start_sample, duration_samples)
        end = _bounded_sample(utterance.end_sample, duration_samples)
        if end > start:
            transcript.append(
                utterance.model_copy(
                    update={"start_sample": start, "end_sample": end}
                )
            )

    duration_seconds = duration_samples / SAMPLE_RATE
    aligned_words: list[dict[str, Any]] = []
    for original in annotation.aligned_words:
        word = dict(original)
        start = _bounded_seconds(word.get("start"), duration_seconds)
        end = _bounded_seconds(word.get("end"), duration_seconds)
        if start is None or end is None or end <= start:
            continue
        word["start"] = start
        word["end"] = end
        aligned_words.append(word)

    return annotation.model_copy(
        update={
            "activities": activities,
            "speaker_references": speaker_references,
            "exclusions": exclusions,
            "transcript": transcript,
            "aligned_words": aligned_words,
        }
    )
