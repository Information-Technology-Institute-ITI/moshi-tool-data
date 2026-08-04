import pytest

from moshi_data_pipeline.studio.domain import (
    AnnotationDocument,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.quality_metrics import (
    character_error_rate,
    golden_records,
    source_quality_metrics,
)


def test_character_error_rate_uses_human_text_as_reference() -> None:
    assert character_error_rate("أهلا", "أهلا") == 0.0
    assert character_error_rate("أهلا", "اهلا") == pytest.approx(0.25)


def test_quality_dashboard_builds_review_queue_and_golden_set() -> None:
    annotation = AnnotationDocument(
        source_id="source_test",
        assistant_speaker="A",
        transcript=[
            TranscriptUtterance(
                speaker="A",
                model_speaker="B",
                start_sample=0,
                end_sample=24_000,
                text="النص الصحيح",
                model_text="النص الصخيح",
                quality_flags=["decode_disagreement"],
                alignment_status="aligned",
                human_verified=True,
            ),
            TranscriptUtterance(
                speaker="A",
                model_speaker="A",
                start_sample=24_000,
                end_sample=48_000,
                text="نص مشكوك",
                model_text="نص مشكوك",
                quality_flags=["suspicious_character_sequence"],
                alignment_status="aligned",
            ),
        ],
    )

    metrics = source_quality_metrics(annotation, [])

    assert metrics["golden_examples"] == 1
    assert metrics["assistant_unresolved"] == 1
    assert metrics["speaker_correction_rate"] == 1.0
    assert metrics["review_queue"][0]["utterance_id"] == annotation.transcript[1].id
    assert golden_records(annotation, "source_test")[0]["text"] == "النص الصحيح"
