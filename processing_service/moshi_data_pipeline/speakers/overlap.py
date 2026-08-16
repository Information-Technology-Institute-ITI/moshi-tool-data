from __future__ import annotations

from moshi_data_pipeline.models import SpeakerSegment


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def interval_duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in merge_intervals(intervals))


def overlap_intervals(segments: list[SpeakerSegment]) -> list[tuple[float, float]]:
    events: list[tuple[float, int, str]] = []
    for segment in segments:
        if segment.end > segment.start:
            events.append((segment.start, 1, segment.speaker))
            events.append((segment.end, -1, segment.speaker))
    # End events precede start events at identical times.
    events.sort(key=lambda event: (event[0], event[1]))
    active: dict[str, int] = {}
    overlap_start: float | None = None
    intervals: list[tuple[float, float]] = []
    for timestamp, direction, speaker in events:
        was_overlap = sum(value > 0 for value in active.values()) > 1
        active[speaker] = active.get(speaker, 0) + direction
        if active[speaker] <= 0:
            active.pop(speaker, None)
        is_overlap = sum(value > 0 for value in active.values()) > 1
        if not was_overlap and is_overlap:
            overlap_start = timestamp
        elif was_overlap and not is_overlap and overlap_start is not None:
            if timestamp > overlap_start:
                intervals.append((overlap_start, timestamp))
            overlap_start = None
    return merge_intervals(intervals)


def clipped_duration(intervals: list[tuple[float, float]], start: float, end: float) -> float:
    return interval_duration([(max(first, start), min(last, end)) for first, last in intervals])
