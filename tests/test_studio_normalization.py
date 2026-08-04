from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    ExclusionRegion,
    SpeakerReferenceRegion,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.normalization import normalize_annotation_bounds


def test_annotation_timestamps_are_clamped_to_canonical_audio() -> None:
    duration = 2 * SAMPLE_RATE
    annotation = AnnotationDocument(
        source_id="source_test",
        activities=[
            ActivityRegion(
                speaker="A",
                start_sample=0,
                end_sample=duration + 4_979,
            ),
            ActivityRegion(
                speaker="B",
                start_sample=duration + 1,
                end_sample=duration + 20,
            ),
        ],
        exclusions=[
            ExclusionRegion(
                kind="noise",
                start_sample=duration - 10,
                end_sample=duration + 10,
            )
        ],
        speaker_references=[
            SpeakerReferenceRegion(
                speaker="A",
                start_sample=duration - 1_000,
                end_sample=duration + 1_000,
            )
        ],
        transcript=[
            TranscriptUtterance(
                speaker="A",
                start_sample=0,
                end_sample=duration + 525,
                text="اهلا",
            )
        ],
        aligned_words=[
            {"word": "اهلا", "start": 1.9, "end": 2.02},
            {"word": "خارج", "start": 2.01, "end": 2.02},
        ],
    )

    normalized = normalize_annotation_bounds(annotation, duration)

    assert [(value.start_sample, value.end_sample) for value in normalized.activities] == [
        (0, duration)
    ]
    assert normalized.exclusions[0].end_sample == duration
    assert normalized.speaker_references[0].end_sample == duration
    assert normalized.transcript[0].end_sample == duration
    assert normalized.aligned_words == [{"word": "اهلا", "start": 1.9, "end": 2.0}]
