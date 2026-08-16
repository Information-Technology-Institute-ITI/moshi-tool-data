from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from hashlib import sha256
from math import isfinite

from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    ClipPlanDocument,
    ClipPlanRequest,
    ClipSpec,
)

MIN_CLIP_SAMPLES = 20 * SAMPLE_RATE
MAX_CLIP_SAMPLES = 100 * SAMPLE_RATE
MIN_SPEAKER_SAMPLES = SAMPLE_RATE
MIN_SPEAKER_SHARE = 0.02
MAX_SILENCE_RATIO = 0.80
CONTEXT_SAMPLES = SAMPLE_RATE // 4
MIN_SAFE_PAUSE_SAMPLES = SAMPLE_RATE // 4
WORD_BOUNDARY_GUARD_SAMPLES = SAMPLE_RATE // 25


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def interval_samples(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merge_intervals(intervals))


def clipped_samples(intervals: Iterable[tuple[int, int]], start: int, end: int) -> int:
    return interval_samples(
        (max(start, left), min(end, right))
        for left, right in intervals
        if right > start and left < end
    )


def derived_overlaps(activities: list[ActivityRegion]) -> list[tuple[int, int]]:
    by_speaker: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for region in activities:
        by_speaker[region.speaker].append((region.start_sample, region.end_sample))
    first = merge_intervals(by_speaker["A"])
    second = merge_intervals(by_speaker["B"])
    overlaps: list[tuple[int, int]] = []
    first_index = second_index = 0
    while first_index < len(first) and second_index < len(second):
        left = max(first[first_index][0], second[second_index][0])
        right = min(first[first_index][1], second[second_index][1])
        if right > left:
            overlaps.append((left, right))
        if first[first_index][1] <= second[second_index][1]:
            first_index += 1
        else:
            second_index += 1
    return merge_intervals(overlaps)


def derived_silences(
    activities: list[ActivityRegion], duration_samples: int
) -> list[tuple[int, int]]:
    speech = merge_intervals(
        (region.start_sample, region.end_sample) for region in activities
    )
    silences: list[tuple[int, int]] = []
    cursor = 0
    for start, end in speech:
        if start > cursor:
            silences.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_samples:
        silences.append((cursor, duration_samples))
    return silences


def validate_annotation(annotation: AnnotationDocument, duration_samples: int) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for collection_name, values in (
        ("activity", annotation.activities),
        ("speaker_reference", annotation.speaker_references),
        ("exclusion", annotation.exclusions),
        ("utterance", annotation.transcript),
    ):
        for value in values:
            if value.id in seen:
                errors.append(f"duplicate_{collection_name}_id:{value.id}")
            seen.add(value.id)
            if value.end_sample > duration_samples:
                errors.append(f"{collection_name}_outside_source:{value.id}")
    for speaker in ("A", "B"):
        own = [
            (value.start_sample, value.end_sample)
            for value in annotation.activities
            if value.speaker == speaker
        ]
        if sum(end - start for start, end in own) != interval_samples(own):
            errors.append(f"same_speaker_regions_overlap:{speaker}")
        references = [
            value
            for value in annotation.speaker_references
            if value.speaker == speaker
        ]
        if len(references) > 1:
            errors.append(f"multiple_speaker_references:{speaker}")
        for reference in references:
            duration = reference.end_sample - reference.start_sample
            if duration < round(1.5 * SAMPLE_RATE):
                errors.append(f"speaker_reference_too_short:{reference.id}")
            if duration > 15 * SAMPLE_RATE:
                errors.append(f"speaker_reference_too_long:{reference.id}")
    return errors


def _conversation_bounds(
    annotation: AnnotationDocument, duration_samples: int
) -> tuple[int, int]:
    if not annotation.activities:
        return 0, duration_samples
    start = max(0, min(region.start_sample for region in annotation.activities) - CONTEXT_SAMPLES)
    end = min(
        duration_samples,
        max(region.end_sample for region in annotation.activities) + CONTEXT_SAMPLES,
    )
    return start, end


def _aligned_word_intervals(
    annotation: AnnotationDocument, duration_samples: int
) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for word in annotation.aligned_words:
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not isfinite(start) or not isfinite(end) or end <= start:
            continue
        first = max(0, min(duration_samples, round(start * SAMPLE_RATE)))
        last = max(0, min(duration_samples, round(end * SAMPLE_RATE)))
        if last > first:
            intervals.append((first, last))
    return merge_intervals(intervals)


def _unsafe_boundary_intervals(
    annotation: AnnotationDocument, duration_samples: int
) -> list[tuple[int, int]]:
    unsafe = [
        (region.start_sample, region.end_sample)
        for region in annotation.exclusions
    ]
    unsafe.extend(derived_overlaps(annotation.activities))
    words = _aligned_word_intervals(annotation, duration_samples)
    if words:
        unsafe.extend(
            (
                max(0, start - WORD_BOUNDARY_GUARD_SAMPLES),
                min(duration_samples, end + WORD_BOUNDARY_GUARD_SAMPLES),
            )
            for start, end in words
        )
    else:
        # Without word alignment, activity regions are the only evidence of
        # active speech. Once alignment exists, pauses inside a long turn are
        # safe candidates even though the activity lane remains continuous.
        unsafe.extend(
            (region.start_sample, region.end_sample)
            for region in annotation.activities
        )
    return unsafe


def _boundary_is_safe(
    annotation: AnnotationDocument, position: int, duration_samples: int
) -> bool:
    if position <= 0 or position >= duration_samples:
        return True
    return not any(
        start < position < end
        for start, end in _unsafe_boundary_intervals(annotation, duration_samples)
    )


def _boundary_candidates(
    annotation: AnnotationDocument, duration_samples: int
) -> list[tuple[int, int]]:
    conversation_start, conversation_end = _conversation_bounds(annotation, duration_samples)
    candidates: dict[int, int] = {conversation_start: 0, conversation_end: 0}
    for start, end in derived_silences(annotation.activities, duration_samples):
        if end - start >= MIN_SAFE_PAUSE_SAMPLES:
            candidates[(start + end) // 2] = 0
    words = _aligned_word_intervals(annotation, duration_samples)
    for (_, previous_end), (next_start, _) in zip(words, words[1:], strict=False):
        if next_start - previous_end >= MIN_SAFE_PAUSE_SAMPLES:
            candidates.setdefault((previous_end + next_start) // 2, 1)
    for utterance in annotation.transcript:
        candidates.setdefault(utterance.end_sample, 1)
    overlaps = derived_overlaps(annotation.activities)
    for region in annotation.activities:
        if not any(start < region.end_sample < end for start, end in overlaps):
            candidates.setdefault(region.end_sample, 2)
    return sorted(
        (position, quality)
        for position, quality in candidates.items()
        if conversation_start <= position <= conversation_end
        and (
            position in {conversation_start, conversation_end}
            or _boundary_is_safe(annotation, position, duration_samples)
        )
    )


def _activity_duration(
    annotation: AnnotationDocument, speaker: str, start: int, end: int
) -> int:
    return clipped_samples(
        (
            (region.start_sample, region.end_sample)
            for region in annotation.activities
            if region.speaker == speaker
        ),
        start,
        end,
    )


def _exchange_count(annotation: AnnotationDocument, start: int, end: int) -> int:
    ordered = sorted(
        (
            (max(start, value.start_sample), min(end, value.end_sample), value.speaker)
            for value in annotation.activities
            if value.end_sample > start and value.start_sample < end
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    switches = 0
    previous: str | None = None
    for _, _, speaker in ordered:
        if previous is not None and speaker != previous:
            switches += 1
        previous = speaker
    return switches


def evaluate_clip(
    annotation: AnnotationDocument, start: int, end: int
) -> tuple[list[str], dict[str, float | int | str]]:
    reasons: list[str] = []
    duration = end - start
    if duration < MIN_CLIP_SAMPLES:
        reasons.append("clip_below_20_seconds")
    if duration > MAX_CLIP_SAMPLES:
        reasons.append("clip_above_100_seconds")
    a_samples = _activity_duration(annotation, "A", start, end)
    b_samples = _activity_duration(annotation, "B", start, end)
    total = a_samples + b_samples
    if a_samples < MIN_SPEAKER_SAMPLES:
        reasons.append("insufficient_speaker_A")
    if b_samples < MIN_SPEAKER_SAMPLES:
        reasons.append("insufficient_speaker_B")
    if total and min(a_samples, b_samples) / total < MIN_SPEAKER_SHARE:
        reasons.append("insufficient_speaker_share")
    speech = clipped_samples(
        ((region.start_sample, region.end_sample) for region in annotation.activities),
        start,
        end,
    )
    silence_ratio = max(0.0, 1.0 - speech / duration) if duration else 1.0
    if silence_ratio > MAX_SILENCE_RATIO:
        reasons.append("excessive_silence")
    exchanges = _exchange_count(annotation, start, end)
    if exchanges < 1:
        reasons.append("missing_speaker_exchange")
    overlap = clipped_samples(derived_overlaps(annotation.activities), start, end)
    excluded = clipped_samples(
        ((region.start_sample, region.end_sample) for region in annotation.exclusions),
        start,
        end,
    )
    metrics: dict[str, float | int | str] = {
        "duration_seconds": duration / SAMPLE_RATE,
        "speaker_A_seconds": a_samples / SAMPLE_RATE,
        "speaker_B_seconds": b_samples / SAMPLE_RATE,
        "silence_ratio": silence_ratio,
        "overlap_ratio": overlap / duration if duration else 0.0,
        "exclusion_ratio": excluded / duration if duration else 0.0,
        "speaker_exchanges": exchanges,
    }
    return sorted(set(reasons)), metrics


def _exact_count_boundaries(
    candidates: list[tuple[int, int]],
    annotation: AnnotationDocument,
    start: int,
    end: int,
    count: int,
) -> list[int] | None:
    quality_by_position = dict(candidates)
    quality_by_position.setdefault(start, 0)
    quality_by_position.setdefault(end, 0)
    ordered = sorted(quality_by_position.items())
    positions = [position for position, _ in ordered]
    start_index = positions.index(start)
    end_index = positions.index(end)
    total = end - start
    # Each state stores a lexicographic cost and the selected boundary path.
    # Pause quality is preferred first, then proximity to equally spaced clips.
    states: dict[int, tuple[tuple[int, int], list[int]]] = {
        start_index: ((0, 0), [start])
    }
    validation_cache: dict[tuple[int, int], bool] = {}

    for segment_number in range(1, count + 1):
        remaining_segments = count - segment_number
        ideal = start + round(total * segment_number / count)
        next_states: dict[int, tuple[tuple[int, int], list[int]]] = {}
        for previous_index, (previous_cost, path) in states.items():
            previous = positions[previous_index]
            if segment_number == count:
                candidate_indices = range(end_index, end_index + 1)
            else:
                minimum = previous + MIN_CLIP_SAMPLES
                maximum = min(previous + MAX_CLIP_SAMPLES, end - MIN_CLIP_SAMPLES)
                first_index = bisect_left(positions, minimum, previous_index + 1)
                last_index = bisect_right(positions, maximum, first_index)
                candidate_indices = range(first_index, last_index)
            for candidate_index in candidate_indices:
                position = positions[candidate_index]
                duration = position - previous
                if not MIN_CLIP_SAMPLES <= duration <= MAX_CLIP_SAMPLES:
                    continue
                remaining_duration = end - position
                if not (
                    remaining_segments * MIN_CLIP_SAMPLES
                    <= remaining_duration
                    <= remaining_segments * MAX_CLIP_SAMPLES
                ):
                    continue
                pair = (previous, position)
                valid = validation_cache.get(pair)
                if valid is None:
                    reasons, _ = evaluate_clip(annotation, previous, position)
                    valid = not reasons
                    validation_cache[pair] = valid
                if not valid:
                    continue
                quality = quality_by_position[position] if position != end else 0
                cost = (
                    previous_cost[0] + quality,
                    previous_cost[1] + abs(position - ideal),
                )
                existing = next_states.get(candidate_index)
                if existing is None or cost < existing[0]:
                    next_states[candidate_index] = (cost, [*path, position])
        states = next_states
        if not states:
            return None
    result = states.get(end_index)
    return result[1] if result is not None else None


def _count_boundaries(
    candidates: list[tuple[int, int]],
    annotation: AnnotationDocument,
    start: int,
    end: int,
    count: int,
) -> tuple[list[int], bool, str]:
    total = end - start
    if total < count * MIN_CLIP_SAMPLES or total > count * MAX_CLIP_SAMPLES:
        nearest = max(1, round(total / (60 * SAMPLE_RATE)))
        return [start, end], False, (
            f"{count} clips cannot cover this conversation within 20–100 seconds; "
            f"try approximately {nearest} clips."
        )
    boundaries = _exact_count_boundaries(
        candidates, annotation, start, end, count
    )
    if boundaries is None:
        return [start, end], False, (
            "The exact clip count has no safe pause combination that gives every clip "
            "both speakers and a completed exchange."
        )
    return boundaries, True, ""


def _target_boundaries(
    candidates: list[tuple[int, int]],
    annotation: AnnotationDocument,
    start: int,
    end: int,
    target_samples: int,
) -> tuple[list[int], bool, str]:
    target_samples = min(MAX_CLIP_SAMPLES, max(MIN_CLIP_SAMPLES, target_samples))
    total = end - start
    minimum_count = max(1, (total + MAX_CLIP_SAMPLES - 1) // MAX_CLIP_SAMPLES)
    maximum_count = max(1, total // MIN_CLIP_SAMPLES)
    counts = sorted(
        range(minimum_count, maximum_count + 1),
        key=lambda count: (abs(total / count - target_samples), count),
    )
    for count in counts:
        boundaries = _exact_count_boundaries(
            candidates, annotation, start, end, count
        )
        if boundaries is not None:
            return boundaries, True, ""
    return [start, end], False, (
        "No safe pause combination can satisfy the target duration while keeping both "
        "speakers and a completed exchange in every clip."
    )


def propose_clip_plan(
    source_id: str,
    annotation: AnnotationDocument,
    duration_samples: int,
    request: ClipPlanRequest,
) -> ClipPlanDocument:
    candidates = _boundary_candidates(annotation, duration_samples)
    start, end = _conversation_bounds(annotation, duration_samples)
    if request.mode == "manual":
        boundaries = sorted(set(request.boundaries_samples))
        if len(boundaries) < 2:
            feasible = False
            message = "Manual mode requires at least two distinct boundaries."
        else:
            feasible = boundaries[0] >= 0 and boundaries[-1] <= duration_samples
            message = "" if feasible else "Manual boundaries must stay within the source."
    elif request.mode == "count":
        boundaries, feasible, message = _count_boundaries(
            candidates, annotation, start, end, int(request.count)
        )
        if not feasible:
            total = end - start
            minimum_count = max(
                1, (total + MAX_CLIP_SAMPLES - 1) // MAX_CLIP_SAMPLES
            )
            maximum_count = max(1, total // MIN_CLIP_SAMPLES)
            requested_count = int(request.count)
            fallback_counts = sorted(
                (
                    count
                    for count in range(minimum_count, maximum_count + 1)
                    if count != requested_count
                ),
                key=lambda count: (
                    abs(count - requested_count),
                    abs(total / count - 60 * SAMPLE_RATE),
                    count,
                ),
            )
            for fallback_count in fallback_counts:
                fallback, fallback_feasible, _ = _count_boundaries(
                    candidates,
                    annotation,
                    start,
                    end,
                    fallback_count,
                )
                if fallback_feasible:
                    boundaries = fallback
                    feasible = True
                    message = (
                        f"{requested_count} clips are not feasible under the 20–100 second "
                        f"conversation constraints. Proposed the closest valid "
                        f"{fallback_count}-clip plan."
                    )
                    break
    else:
        boundaries, feasible, message = _target_boundaries(
            candidates,
            annotation,
            start,
            end,
            round(float(request.target_duration_seconds) * SAMPLE_RATE),
        )
    clips: list[ClipSpec] = []
    for index, (clip_start, clip_end) in enumerate(
        zip(boundaries, boundaries[1:], strict=False)
    ):
        reasons, metrics = evaluate_clip(annotation, clip_start, clip_end)
        if not _boundary_is_safe(annotation, clip_start, duration_samples):
            reasons.append("start_boundary_cuts_active_audio")
        if not _boundary_is_safe(annotation, clip_end, duration_samples):
            reasons.append("end_boundary_cuts_active_audio")
        reasons = sorted(set(reasons))
        digest = sha256(
            f"{source_id}:{annotation.version}:{clip_start}:{clip_end}".encode()
        ).hexdigest()[:12]
        clips.append(
            ClipSpec(
                id=f"clip_{index + 1:03d}_{digest}",
                start_sample=clip_start,
                end_sample=clip_end,
                status="invalid" if reasons else "valid",
                reasons=reasons,
                metrics=metrics,
            )
        )
    if any(clip.status == "invalid" for clip in clips):
        feasible = False
        message = message or "One or more proposed clips violate export constraints."
    return ClipPlanDocument(
        source_id=source_id,
        annotation_version=annotation.version,
        mode=request.mode,
        request=request.model_dump(mode="json"),
        feasible=feasible,
        message=message,
        clips=clips,
    )
