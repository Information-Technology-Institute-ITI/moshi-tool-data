from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from moshi_data_pipeline.studio.domain import SAMPLE_RATE, AnnotationDocument
from moshi_data_pipeline.studio.planning import derived_overlaps, merge_intervals

_SPACE_RE = re.compile(r"\s+")
_ARABIC_MARKS_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_PUNCTUATION_RE = re.compile(r"[^\w\s\u0600-\u06ff]", re.UNICODE)
_ARABIC_TRANSLATION = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي"})


@dataclass(frozen=True)
class TimedText:
    id: str
    start_sample: int
    end_sample: int
    text: str
    speaker: str | None = None
    verified: bool = True


def normalize_text(text: str, policy: str) -> str:
    value = unicodedata.normalize("NFC", text).casefold()
    if policy == "arabic-normalized-v1":
        value = _ARABIC_MARKS_RE.sub("", value).replace("ـ", "").translate(
            _ARABIC_TRANSLATION
        )
        value = _PUNCTUATION_RE.sub(" ", value)
    elif policy != "strict-v1":
        raise ValueError(f"Unknown normalization policy: {policy}")
    return _SPACE_RE.sub(" ", value).strip()


def _tokens(text: str, policy: str, unit: str) -> list[str]:
    normalized = normalize_text(text, policy)
    if unit == "word":
        return normalized.split() if normalized else []
    return [character for character in normalized if not character.isspace()]


def edit_metric(reference: list[str], hypothesis: list[str]) -> dict[str, Any]:
    rows = len(reference) + 1
    columns = len(hypothesis) + 1
    costs = [[0] * columns for _ in range(rows)]
    steps = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        costs[row][0] = row
        steps[row][0] = "delete"
    for column in range(1, columns):
        costs[0][column] = column
        steps[0][column] = "insert"
    for row in range(1, rows):
        for column in range(1, columns):
            if reference[row - 1] == hypothesis[column - 1]:
                costs[row][column] = costs[row - 1][column - 1]
                steps[row][column] = "equal"
                continue
            choices = (
                (costs[row - 1][column - 1] + 1, "substitute"),
                (costs[row - 1][column] + 1, "delete"),
                (costs[row][column - 1] + 1, "insert"),
            )
            costs[row][column], steps[row][column] = min(choices, key=lambda item: item[0])
    row = len(reference)
    column = len(hypothesis)
    operations: list[dict[str, str | None]] = []
    counts = {"substitutions": 0, "insertions": 0, "deletions": 0}
    while row or column:
        step = steps[row][column]
        if step == "equal":
            operations.append(
                {"operation": step, "reference": reference[row - 1], "hypothesis": hypothesis[column - 1]}
            )
            row -= 1
            column -= 1
        elif step == "substitute":
            counts["substitutions"] += 1
            operations.append(
                {"operation": step, "reference": reference[row - 1], "hypothesis": hypothesis[column - 1]}
            )
            row -= 1
            column -= 1
        elif step == "delete":
            counts["deletions"] += 1
            operations.append(
                {"operation": step, "reference": reference[row - 1], "hypothesis": None}
            )
            row -= 1
        else:
            counts["insertions"] += 1
            operations.append(
                {"operation": "insert", "reference": None, "hypothesis": hypothesis[column - 1]}
            )
            column -= 1
    operations.reverse()
    distance = sum(counts.values())
    return {
        "distance": distance,
        **counts,
        "reference_units": len(reference),
        "hypothesis_units": len(hypothesis),
        "rate": distance / len(reference) if reference else (0.0 if not hypothesis else 1.0),
        "operations": operations,
    }


def _aggregate(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "distance",
        "substitutions",
        "insertions",
        "deletions",
        "reference_units",
        "hypothesis_units",
    )
    value = {key: sum(int(metric[key]) for metric in metrics) for key in keys}
    denominator = value["reference_units"]
    value["rate"] = value["distance"] / denominator if denominator else (
        0.0 if value["hypothesis_units"] == 0 else 1.0
    )
    return value


def machine_segments(payload: dict[str, Any]) -> list[TimedText]:
    raw = payload.get("segments") or payload.get("transcript") or []
    values: list[TimedText] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        start = item.get("start_sample")
        end = item.get("end_sample")
        if start is None:
            start = round(float(item.get("start", 0)) * SAMPLE_RATE)
        if end is None:
            end = round(float(item.get("end", 0)) * SAMPLE_RATE)
        if int(end) <= int(start):
            continue
        values.append(
            TimedText(
                id=str(item.get("id") or f"machine-{index + 1}"),
                start_sample=int(start),
                end_sample=int(end),
                text=str(item.get("text") or ""),
                speaker=item.get("speaker"),
            )
        )
    if not values and payload.get("text"):
        values.append(TimedText("machine-1", 0, 2**63 - 1, str(payload["text"])))
    return sorted(values, key=lambda item: (item.start_sample, item.end_sample))


def _intersects(intervals: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(left < end and right > start for left, right in intervals)


def evaluate_transcript(
    annotation: AnnotationDocument,
    machine_payload: dict[str, Any],
    *,
    normalization_policy: str = "strict-v1",
    overlap_policy: str = "deduplicate",
    speakers: list[str] | None = None,
    verified_only: bool = True,
) -> dict[str, Any]:
    if overlap_policy not in {"deduplicate", "include", "exclude"}:
        raise ValueError("Invalid overlap policy")
    speaker_filter = set(speakers or [])
    overlaps = derived_overlaps(annotation.activities)
    eligible = [
        TimedText(
            item.id,
            item.start_sample,
            item.end_sample,
            item.text,
            item.speaker,
            item.human_verified,
        )
        for item in annotation.transcript
        if item.text.strip()
        and (not speaker_filter or item.speaker in speaker_filter)
        and not (overlap_policy == "exclude" and _intersects(overlaps, item.start_sample, item.end_sample))
    ]
    reference = [item for item in eligible if item.verified or not verified_only]
    if overlap_policy == "deduplicate":
        seen: set[tuple[int, int, str]] = set()
        unique: list[TimedText] = []
        for item in reference:
            key = (
                item.start_sample,
                item.end_sample,
                normalize_text(item.text, normalization_policy),
            )
            if key not in seen:
                unique.append(item)
                seen.add(key)
        reference = unique
    hypotheses = [
        item
        for item in machine_segments(machine_payload)
        if (not speaker_filter or item.speaker is None or item.speaker in speaker_filter)
        and not (
            overlap_policy == "exclude"
            and _intersects(overlaps, item.start_sample, item.end_sample)
        )
    ]
    parents = list(range(len(reference)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    hypothesis_matches: list[tuple[TimedText, list[int]]] = []
    unmatched_hypotheses: list[TimedText] = []
    for hypothesis in hypotheses:
        matches = [
            index
            for index, item in enumerate(reference)
            if hypothesis.start_sample < item.end_sample and hypothesis.end_sample > item.start_sample
        ]
        for index in matches[1:]:
            union(matches[0], index)
        if matches:
            hypothesis_matches.append((hypothesis, matches))
        else:
            unmatched_hypotheses.append(hypothesis)

    grouped_references: dict[int, list[TimedText]] = {}
    for index, item in enumerate(reference):
        grouped_references.setdefault(find(index), []).append(item)
    grouped_hypotheses: dict[int, list[TimedText]] = {
        root: [] for root in grouped_references
    }
    for hypothesis, matches in hypothesis_matches:
        grouped_hypotheses[find(matches[0])].append(hypothesis)

    strict_word: list[dict[str, Any]] = []
    strict_character: list[dict[str, Any]] = []
    normalized_word: list[dict[str, Any]] = []
    normalized_character: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    for root, items in sorted(
        grouped_references.items(), key=lambda value: value[1][0].start_sample
    ):
        items.sort(key=lambda item: (item.start_sample, item.end_sample))
        assigned = sorted(
            grouped_hypotheses[root],
            key=lambda item: (item.start_sample, item.end_sample),
        )
        reference_text = " ".join(item.text.strip() for item in items).strip()
        hypothesis_text = " ".join(value.text.strip() for value in assigned).strip()
        sw = edit_metric(_tokens(reference_text, "strict-v1", "word"), _tokens(hypothesis_text, "strict-v1", "word"))
        sc = edit_metric(_tokens(reference_text, "strict-v1", "character"), _tokens(hypothesis_text, "strict-v1", "character"))
        nw = edit_metric(_tokens(reference_text, normalization_policy, "word"), _tokens(hypothesis_text, normalization_policy, "word"))
        nc = edit_metric(_tokens(reference_text, normalization_policy, "character"), _tokens(hypothesis_text, normalization_policy, "character"))
        strict_word.append(sw)
        strict_character.append(sc)
        normalized_word.append(nw)
        normalized_character.append(nc)
        diffs.append(
            {
                "segment_id": "+".join(item.id for item in items),
                "speaker": items[0].speaker
                if all(item.speaker == items[0].speaker for item in items)
                else None,
                "start_sample": min(item.start_sample for item in items),
                "end_sample": max(item.end_sample for item in items),
                "reference": reference_text,
                "hypothesis": hypothesis_text,
                "word_operations": nw["operations"],
            }
        )

    # With a fully reviewed scope, machine-only time ranges are hallucinations
    # and must contribute insertions. For a partial verified-only scope they are
    # outside the evaluated evidence and are deliberately excluded.
    complete_scope = not verified_only or len(reference) == len(eligible)
    if complete_scope:
        for hypothesis in unmatched_hypotheses:
            hypothesis_text = hypothesis.text.strip()
            sw = edit_metric([], _tokens(hypothesis_text, "strict-v1", "word"))
            sc = edit_metric([], _tokens(hypothesis_text, "strict-v1", "character"))
            nw = edit_metric([], _tokens(hypothesis_text, normalization_policy, "word"))
            nc = edit_metric([], _tokens(hypothesis_text, normalization_policy, "character"))
            strict_word.append(sw)
            strict_character.append(sc)
            normalized_word.append(nw)
            normalized_character.append(nc)
            diffs.append(
                {
                    "segment_id": hypothesis.id,
                    "speaker": hypothesis.speaker,
                    "start_sample": hypothesis.start_sample,
                    "end_sample": hypothesis.end_sample,
                    "reference": "",
                    "hypothesis": hypothesis_text,
                    "word_operations": nw["operations"],
                }
            )

    diffs.sort(key=lambda item: (item["start_sample"], item["end_sample"]))

    eligible_ranges = merge_intervals((item.start_sample, item.end_sample) for item in eligible)
    evaluated_ranges = merge_intervals((item.start_sample, item.end_sample) for item in reference)
    def duration(values: Iterable[tuple[int, int]]) -> int:
        return sum(end - start for start, end in values)

    eligible_duration = duration(eligible_ranges)
    evaluated_duration = duration(evaluated_ranges)
    return {
        "word_error": _aggregate(strict_word),
        "character_error": _aggregate(strict_character),
        "normalized_word_error": _aggregate(normalized_word),
        "normalized_character_error": _aggregate(normalized_character),
        "evaluated_duration_samples": evaluated_duration,
        "eligible_duration_samples": eligible_duration,
        "coverage": evaluated_duration / eligible_duration if eligible_duration else 0.0,
        "diffs": diffs,
        "policies": {
            "normalization": normalization_policy,
            "overlap": overlap_policy,
            "character_units": "Unicode characters excluding whitespace",
            "temporal_alignment": "connected time-overlap groups",
        },
    }
