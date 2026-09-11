from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass

from moshi_data_pipeline.studio.domain import SAMPLE_RATE, AnnotationDocument, new_id
from moshi_data_pipeline.studio.product_contracts import (
    ReviewChapter,
    ReviewChapterConfig,
    ReviewChapterSet,
)


@dataclass(frozen=True)
class BoundaryCandidate:
    sample: int
    reason: str


def _word_bounds(annotation: AnnotationDocument) -> Iterable[tuple[int, int]]:
    for word in annotation.aligned_words:
        start_value = word.get("start", word.get("start_seconds"))
        end_value = word.get("end", word.get("end_seconds"))
        if start_value is None or end_value is None:
            continue
        start = round(float(start_value) * SAMPLE_RATE)
        end = round(float(end_value) * SAMPLE_RATE)
        if end > start:
            yield start, end


def _safe_checker(annotation: AnnotationDocument):
    ranges = sorted([
        *((item.start_sample, item.end_sample) for item in annotation.transcript),
        *_word_bounds(annotation),
    ])
    starts: list[int] = []
    maximum_ends: list[int] = []
    maximum = -1
    for start, end in ranges:
        starts.append(start)
        maximum = max(maximum, end)
        maximum_ends.append(maximum)

    def safe(sample: int) -> bool:
        previous = bisect_left(starts, sample) - 1
        return previous < 0 or maximum_ends[previous] <= sample

    return safe


def boundary_candidates(
    annotation: AnnotationDocument,
    duration_samples: int,
    silences: Iterable[tuple[int, int]],
) -> list[BoundaryCandidate]:
    """Return safe global-sample boundaries, preferring silence over segment edges."""
    safe = _safe_checker(annotation)
    reasons: dict[int, str] = {0: "source_edge", duration_samples: "source_edge"}
    for item in annotation.transcript:
        for sample in (item.start_sample, item.end_sample):
            if 0 < sample < duration_samples and safe(sample):
                reasons.setdefault(sample, "segment")
    for start, end in silences:
        sample = max(0, min(duration_samples, round((start + end) / 2)))
        if 0 < sample < duration_samples and safe(sample):
            reasons[sample] = "silence"
    return [BoundaryCandidate(sample, reasons[sample]) for sample in sorted(reasons)]


def _max_duration_boundaries(
    candidates: list[BoundaryCandidate],
    duration_samples: int,
    maximum: int,
    search: int,
) -> list[BoundaryCandidate]:
    result = [candidates[0]]
    start = 0
    while duration_samples - start > maximum:
        limit = start + maximum
        eligible = [candidate for candidate in candidates if start < candidate.sample <= limit]
        if not eligible:
            raise ValueError(
                "No safe chapter boundary exists within the maximum duration; "
                "a transcript segment or aligned word is too long"
            )
        nearby_silences = [
            candidate
            for candidate in eligible
            if candidate.reason == "silence" and candidate.sample >= limit - search
        ]
        chosen = nearby_silences[-1] if nearby_silences else eligible[-1]
        if chosen.sample == duration_samples:
            break
        result.append(chosen)
        start = chosen.sample
    if result[-1].sample != duration_samples:
        result.append(candidates[-1])
    return result


def _count_boundaries(
    candidates: list[BoundaryCandidate],
    duration_samples: int,
    count: int,
    search: int,
) -> list[BoundaryCandidate]:
    if count == 1:
        return [candidates[0], candidates[-1]]
    interior = candidates[1:-1]
    if len(interior) < count - 1:
        raise ValueError("The requested chapter count has too few safe boundaries")
    chosen: list[BoundaryCandidate] = []
    remaining = list(interior)
    for ordinal in range(1, count):
        target = round(duration_samples * ordinal / count)
        nearby_silences = [
            candidate
            for candidate in remaining
            if candidate.reason == "silence" and abs(candidate.sample - target) <= search
        ]
        pool = nearby_silences or remaining
        candidate = min(pool, key=lambda value: (abs(value.sample - target), value.sample))
        chosen.append(candidate)
        remaining.remove(candidate)
    ordered = sorted(chosen, key=lambda value: value.sample)
    return [candidates[0], *ordered, candidates[-1]]


def generate_chapter_set(
    source_id: str,
    annotation: AnnotationDocument,
    duration_samples: int,
    config: ReviewChapterConfig,
    *,
    silences: Iterable[tuple[int, int]] = (),
) -> ReviewChapterSet:
    if duration_samples <= 0:
        raise ValueError("Source duration must be positive")
    silence_ranges = list(silences)
    candidates = boundary_candidates(annotation, duration_samples, silence_ranges)
    safe = _safe_checker(annotation)
    targets: list[int] = []
    if config.mode == "max_duration":
        # A small safe grid lets a silence-preferred boundary shift earlier
        # without making the following chapter exceed the configured maximum.
        grid_seconds = min(30, config.boundary_search_seconds or 30)
        targets = list(range(grid_seconds * SAMPLE_RATE, duration_samples, grid_seconds * SAMPLE_RATE))
    elif config.mode == "count" and config.count:
        targets = [
            round(duration_samples * ordinal / config.count)
            for ordinal in range(1, config.count)
        ]
    by_sample = {candidate.sample: candidate for candidate in candidates}
    for sample in targets:
        if sample not in by_sample and safe(sample):
            reason = (
                "silence"
                if any(start <= sample <= end for start, end in silence_ranges)
                else "segment"
            )
            by_sample[sample] = BoundaryCandidate(sample, reason)
    candidates = [by_sample[sample] for sample in sorted(by_sample)]
    if config.mode == "max_duration":
        boundaries = _max_duration_boundaries(
            candidates,
            duration_samples,
            config.max_duration_seconds * SAMPLE_RATE,
            config.boundary_search_seconds * SAMPLE_RATE,
        )
    elif config.mode == "count":
        boundaries = _count_boundaries(
            candidates,
            duration_samples,
            int(config.count or 0),
            config.boundary_search_seconds * SAMPLE_RATE,
        )
    else:
        manual = [0, *config.manual_boundaries_samples, duration_samples]
        samples = sorted(set(manual))
        if samples[0] != 0 or samples[-1] != duration_samples:
            raise ValueError("Manual boundaries must stay within the source")
        lookup = {candidate.sample: candidate for candidate in candidates}
        if unsafe := [sample for sample in samples if sample not in lookup]:
            raise ValueError(f"Manual boundaries cut a word or segment: {unsafe}")
        boundaries = [BoundaryCandidate(sample, "manual") for sample in samples]
        boundaries[0] = BoundaryCandidate(0, "source_edge")
        boundaries[-1] = BoundaryCandidate(duration_samples, "source_edge")

    chapters = [
        ReviewChapter(
            id=new_id("chapter"),
            ordinal=index + 1,
            start_sample=left.sample,
            end_sample=right.sample,
            boundary_reason=right.reason,
        )
        for index, (left, right) in enumerate(zip(boundaries, boundaries[1:], strict=False))
    ]
    return ReviewChapterSet(
        id=new_id("chapter_set"),
        source_id=source_id,
        annotation_version=annotation.version,
        config=config,
        chapters=chapters,
    )


def chapter_segment_count(chapter: ReviewChapter, annotation: AnnotationDocument) -> int:
    return sum(
        1
        for item in annotation.transcript
        if item.start_sample < chapter.end_sample and item.end_sample > chapter.start_sample
    )
