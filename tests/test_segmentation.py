from moshi_data_pipeline.config import SegmentationConfig
from moshi_data_pipeline.models import QCStatus, SpeakerSegment
from moshi_data_pipeline.segmentation.conversation import segment_conversation


def test_segmentation_requires_both_speakers_and_preserves_turn_boundaries() -> None:
    segments = [
        SpeakerSegment(0.5, 2.0, "A"),
        SpeakerSegment(2.5, 4.0, "B"),
        SpeakerSegment(4.5, 6.0, "A"),
        SpeakerSegment(6.5, 8.0, "B"),
        SpeakerSegment(8.5, 10.0, "A"),
        SpeakerSegment(10.5, 12.0, "B"),
    ]
    config = SegmentationConfig(
        min_duration=4,
        target_duration=6,
        max_duration=8,
        max_silence_ratio=0.8,
        min_speaker_duration=0.5,
    )
    ids = iter(["conversation_001", "conversation_002"])
    plans = segment_conversation(segments, "A", 12.5, config, lambda: next(ids))
    assert len(plans) == 2
    assert all(plan.status == QCStatus.PASS for plan in plans)
    assert all(plan.metrics["speaker_count"] == 2 for plan in plans)


def test_excessive_overlap_is_rejected() -> None:
    from moshi_data_pipeline.segmentation.policies import evaluate_window

    segments = [
        SpeakerSegment(0, 10, "A"),
        SpeakerSegment(1, 9, "B"),
    ]
    config = SegmentationConfig(
        min_duration=1,
        target_duration=5,
        max_duration=10,
        min_speaker_duration=0.5,
        max_overlap_ratio=0.05,
        overlap_warning_ratio=0.02,
    )
    status, reasons, _ = evaluate_window(0, 10, segments, "A", config)
    assert status == QCStatus.REJECT
    assert "excessive_overlap" in reasons


def test_overlap_aware_timeline_is_separate_from_exclusive_assignment() -> None:
    from moshi_data_pipeline.segmentation.policies import evaluate_window

    exclusive = [
        SpeakerSegment(0, 5, "A"),
        SpeakerSegment(5, 10, "B"),
    ]
    overlapping = [
        SpeakerSegment(0, 6, "A"),
        SpeakerSegment(4, 10, "B"),
    ]
    config = SegmentationConfig(
        min_duration=1,
        target_duration=5,
        max_duration=10,
        min_speaker_duration=0.5,
        max_overlap_ratio=0.05,
        overlap_warning_ratio=0.02,
    )
    status, reasons, metrics = evaluate_window(
        0, 10, exclusive, "A", config, overlapping
    )
    assert status == QCStatus.REJECT
    assert "excessive_overlap" in reasons
    assert metrics["overlap_ratio"] == 0.2
