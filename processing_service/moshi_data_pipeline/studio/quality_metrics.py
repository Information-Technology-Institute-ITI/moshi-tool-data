from __future__ import annotations

import re
from typing import Any

from moshi_data_pipeline.studio.domain import AnnotationDocument
from moshi_data_pipeline.transcription.quality import HIGH_RISK_TRANSCRIPT_FLAGS

_SPACE_RE = re.compile(r"\s+")


def _normalized_characters(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip().casefold())


def character_error_rate(reference: str, hypothesis: str) -> float:
    expected = _normalized_characters(reference)
    actual = _normalized_characters(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for index, expected_character in enumerate(expected, start=1):
        current = [index]
        for other_index, actual_character in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[other_index] + 1,
                    previous[other_index - 1]
                    + (expected_character != actual_character),
                )
            )
        previous = current
    return previous[-1] / len(expected)


def review_priority(utterance: Any, assistant_speaker: str | None) -> int:
    weights = {
        "suspicious_character_sequence": 100,
        "repeated_ngram": 90,
        "overlapping_speech": 70,
        "decode_disagreement": 55,
        "low_average_log_probability": 45,
        "abnormally_high_word_rate": 40,
        "unaligned_words": 35,
        "low_confidence_alignment": 25,
    }
    score = sum(weights.get(flag, 10) for flag in set(utterance.quality_flags))
    if utterance.speaker == assistant_speaker:
        score += 20
    if utterance.alignment_status != "aligned":
        score += 30
    if utterance.human_verified:
        score -= 1_000
    return score


def source_quality_metrics(
    annotation: AnnotationDocument,
    overlap_recoveries: list[dict[str, Any]],
) -> dict[str, Any]:
    transcript = annotation.transcript
    flagged = [
        value
        for value in transcript
        if HIGH_RISK_TRANSCRIPT_FLAGS.intersection(value.quality_flags)
    ]
    unresolved = [value for value in flagged if not value.human_verified]
    assistant = [
        value
        for value in transcript
        if value.speaker == annotation.assistant_speaker and value.text.strip()
    ]
    aligned_assistant = [
        value for value in assistant if value.alignment_status == "aligned"
    ]
    golden = [
        value
        for value in transcript
        if value.human_verified and value.text.strip()
    ]
    cer_values = [
        character_error_rate(value.text, value.model_text)
        for value in golden
        if value.model_text.strip()
    ]
    speaker_values = [
        value
        for value in golden
        if value.model_speaker is not None and value.speaker is not None
    ]
    recovered = [
        value for value in overlap_recoveries if value["status"] == "recovered"
    ]
    approved = [
        value
        for value in recovered
        if value["decision"] == "approve" and value["auditioned"]
    ]
    queue = sorted(
        (
            {
                "utterance_id": value.id,
                "priority": review_priority(value, annotation.assistant_speaker),
                "start_sample": value.start_sample,
                "end_sample": value.end_sample,
                "speaker": value.speaker,
                "flags": value.quality_flags,
            }
            for value in transcript
            if review_priority(value, annotation.assistant_speaker) > 0
        ),
        key=lambda value: (-int(value["priority"]), int(value["start_sample"])),
    )
    return {
        "utterances": len(transcript),
        "flagged_utterances": len(flagged),
        "unresolved_flagged_utterances": len(unresolved),
        "assistant_unresolved": sum(
            value.speaker == annotation.assistant_speaker for value in unresolved
        ),
        "assistant_alignment_coverage": (
            len(aligned_assistant) / len(assistant) if assistant else 0.0
        ),
        "golden_examples": len(golden),
        "golden_target": 20,
        "model_character_error_rate": (
            sum(cer_values) / len(cer_values) if cer_values else None
        ),
        "speaker_correction_rate": (
            sum(value.model_speaker != value.speaker for value in speaker_values)
            / len(speaker_values)
            if speaker_values
            else None
        ),
        "recovered_overlap_approval_rate": (
            len(approved) / len(recovered) if recovered else None
        ),
        "review_queue": queue,
    }


def golden_records(
    annotation: AnnotationDocument,
    source_id: str,
) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source_id,
            "utterance_id": value.id,
            "speaker": value.speaker,
            "start_sample": value.start_sample,
            "end_sample": value.end_sample,
            "text": value.text,
            "model_text": value.model_text,
            "model_speaker": value.model_speaker,
            "quality_flags": value.quality_flags,
            "alignment_status": value.alignment_status,
        }
        for value in annotation.transcript
        if value.human_verified and value.text.strip()
    ]
