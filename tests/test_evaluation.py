from __future__ import annotations

import pytest

from moshi_data_pipeline.studio.domain import (
    ActivityRegion,
    AnnotationDocument,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.evaluation import (
    edit_metric,
    evaluate_transcript,
    normalize_text,
)


def test_edit_metric_reports_independent_operation_counts() -> None:
    metric = edit_metric(["one", "two", "three"], ["one", "too", "extra"])

    assert metric["distance"] == 2
    assert metric["substitutions"] == 2
    assert metric["insertions"] == 0
    assert metric["deletions"] == 0
    assert metric["reference_units"] == 3
    assert metric["rate"] == pytest.approx(2 / 3)


def test_arabic_normalization_is_versioned_and_conservative() -> None:
    assert normalize_text("إِنَّ الـهُدى!", "arabic-normalized-v1") == "ان الهدي"
    assert normalize_text("إِنَّ الـهُدى!", "strict-v1") == "إِنَّ الـهُدى!"
    with pytest.raises(ValueError, match="Unknown normalization"):
        normalize_text("text", "future-policy")


def test_evaluation_aligns_machine_text_by_time_and_reports_coverage() -> None:
    annotation = AnnotationDocument(
        source_id="source-1",
        transcript=[
            TranscriptUtterance(
                id="u1",
                speaker="A",
                start_sample=0,
                end_sample=24_000,
                text="hello world",
                human_verified=True,
            ),
            TranscriptUtterance(
                id="u2",
                speaker="B",
                start_sample=24_000,
                end_sample=48_000,
                text="not verified",
                human_verified=False,
            ),
        ],
    )
    machine = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "hello word"},
            {"start": 1.0, "end": 2.0, "text": "not verified"},
        ]
    }

    result = evaluate_transcript(annotation, machine, verified_only=True)

    assert result["word_error"]["substitutions"] == 1
    assert result["word_error"]["reference_units"] == 2
    assert result["word_error"]["rate"] == 0.5
    assert result["coverage"] == 0.5
    assert result["diffs"][0]["hypothesis"] == "hello word"


def test_overlap_policy_can_exclude_simultaneous_speech() -> None:
    annotation = AnnotationDocument(
        source_id="source-1",
        activities=[
            ActivityRegion(id="a", speaker="A", start_sample=0, end_sample=48_000),
            ActivityRegion(id="b", speaker="B", start_sample=24_000, end_sample=48_000),
        ],
        transcript=[
            TranscriptUtterance(
                id="clean",
                speaker="A",
                start_sample=0,
                end_sample=24_000,
                text="clean",
                human_verified=True,
            ),
            TranscriptUtterance(
                id="overlap",
                speaker="B",
                start_sample=24_000,
                end_sample=48_000,
                text="overlap",
                human_verified=True,
            ),
        ],
    )
    result = evaluate_transcript(
        annotation,
        {"segments": [{"start": 0, "end": 1, "text": "clean"}]},
        overlap_policy="exclude",
    )

    assert [item["segment_id"] for item in result["diffs"]] == ["clean"]
    assert result["word_error"]["rate"] == 0


def test_long_machine_segment_is_compared_with_all_human_segments_it_spans() -> None:
    annotation = AnnotationDocument(
        source_id="source-1",
        transcript=[
            TranscriptUtterance(
                id="u1", start_sample=0, end_sample=12_000, text="one", human_verified=True
            ),
            TranscriptUtterance(
                id="u2", start_sample=12_000, end_sample=24_000, text="two", human_verified=True
            ),
        ],
    )
    result = evaluate_transcript(
        annotation,
        {"segments": [{"start": 0, "end": 1, "text": "one two"}]},
    )

    assert result["word_error"]["rate"] == 0
    assert result["diffs"][0]["segment_id"] == "u1+u2"


def test_machine_only_range_counts_as_insertion_for_complete_reference() -> None:
    annotation = AnnotationDocument(
        source_id="source-1",
        transcript=[
            TranscriptUtterance(
                id="u1",
                start_sample=0,
                end_sample=24_000,
                text="hello",
                human_verified=True,
            )
        ],
    )
    result = evaluate_transcript(
        annotation,
        {
            "segments": [
                {"id": "matched", "start": 0, "end": 1, "text": "hello"},
                {"id": "hallucination", "start": 2, "end": 3, "text": "extra words"},
            ]
        },
    )

    assert result["word_error"]["insertions"] == 2
    assert result["word_error"]["rate"] == 2
    assert result["diffs"][-1]["reference"] == ""
