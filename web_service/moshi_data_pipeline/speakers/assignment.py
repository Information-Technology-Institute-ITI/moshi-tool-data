from __future__ import annotations

import json
import sys
from pathlib import Path

from moshi_data_pipeline.exceptions import ConfigurationError
from moshi_data_pipeline.models import SpeakerSegment, Word


def assign_speakers_to_words(
    words: list[Word],
    segments: list[SpeakerSegment],
    minimum_overlap: float = 0.5,
) -> tuple[list[Word], int]:
    uncertain = 0
    for word in words:
        if word.start is None or word.end is None or word.end <= word.start:
            uncertain += 1
            continue
        overlaps: dict[str, float] = {}
        for segment in segments:
            overlap = max(0.0, min(word.end, segment.end) - max(word.start, segment.start))
            if overlap:
                overlaps[segment.speaker] = overlaps.get(segment.speaker, 0.0) + overlap
        if not overlaps:
            uncertain += 1
            continue
        speaker, overlap = max(overlaps.items(), key=lambda item: (item[1], item[0]))
        confidence = overlap / (word.end - word.start)
        word.assignment_confidence = confidence
        if confidence >= minimum_overlap:
            word.speaker = speaker
        else:
            uncertain += 1
    return words, uncertain


def apply_speaker_overrides(
    words: list[Word],
    segments: list[SpeakerSegment],
    overrides: list[dict],
) -> tuple[list[Word], list[SpeakerSegment]]:
    cleaned: list[tuple[float, float, str]] = []
    known_speakers = {segment.speaker for segment in segments}
    for index, value in enumerate(overrides):
        try:
            start = float(value["start"])
            end = float(value["end"])
            speaker = str(value["speaker"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid speaker override at index {index}") from exc
        if start < 0 or end <= start or speaker not in known_speakers:
            raise ConfigurationError(f"Invalid speaker override at index {index}")
        cleaned.append((start, end, speaker))
    if not cleaned:
        return words, segments
    boundaries = sorted(
        {
            value
            for segment in segments
            for value in (segment.start, segment.end)
        }
        | {value for start, end, _ in cleaned for value in (start, end)}
    )
    rebuilt: list[SpeakerSegment] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        if end <= start:
            continue
        midpoint = (start + end) / 2
        speaker = next(
            (
                override_speaker
                for override_start, override_end, override_speaker in reversed(cleaned)
                if override_start <= midpoint < override_end
            ),
            None,
        )
        if speaker is None:
            speaker = next(
                (
                    segment.speaker
                    for segment in segments
                    if segment.start <= midpoint < segment.end
                ),
                None,
            )
        if speaker is None:
            continue
        if rebuilt and rebuilt[-1].speaker == speaker and start <= rebuilt[-1].end + 1e-6:
            previous = rebuilt[-1]
            rebuilt[-1] = SpeakerSegment(previous.start, end, speaker)
        else:
            rebuilt.append(SpeakerSegment(start, end, speaker))
    for word in words:
        if word.start is None or word.end is None:
            continue
        midpoint = (word.start + word.end) / 2
        for start, end, speaker in reversed(cleaned):
            if start <= midpoint < end:
                word.speaker = speaker
                word.assignment_confidence = 1.0
                break
    return words, rebuilt


def read_mapping(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read speaker mapping {path}: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ConfigurationError("Speaker mapping must be a JSON object of string keys and values")
    return value


def select_assistant_speaker(
    media_key: str,
    segments: list[SpeakerSegment],
    explicit: str | None,
    mapping: dict[str, str],
    interactive: bool | None = None,
) -> str:
    speakers = sorted({segment.speaker for segment in segments})
    selected = explicit or mapping.get(media_key)
    if selected:
        if selected not in speakers:
            raise ConfigurationError(
                f"Assistant speaker {selected!r} is not present; detected: {', '.join(speakers)}"
            )
        return selected
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise ConfigurationError(
            "Assistant identity is ambiguous. Use --assistant-speaker, --speaker-mapping, "
            "or run interactively. SPEAKER_00 is never assumed automatically."
        )
    print("Detected speakers and sample timestamps:")
    for speaker in speakers:
        samples = [
            f"{segment.start:.2f}-{segment.end:.2f}s"
            for segment in segments
            if segment.speaker == speaker
        ][:5]
        print(f"  {speaker}: {', '.join(samples)}")
    selected = input("Assistant/Moshi speaker label: ").strip()
    if selected not in speakers:
        raise ConfigurationError(f"Unknown speaker {selected!r}; detected: {', '.join(speakers)}")
    return selected
