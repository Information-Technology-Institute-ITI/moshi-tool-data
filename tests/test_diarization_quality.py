from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.speakers.diarization import smooth_exclusive_segments


def test_micro_turn_between_same_speaker_is_merged() -> None:
    segments = [
        SpeakerSegment(0.0, 2.0, "A"),
        SpeakerSegment(2.0, 2.2, "B"),
        SpeakerSegment(2.2, 4.0, "A"),
    ]
    smoothed, ambiguous = smooth_exclusive_segments(segments, 0.35)
    assert smoothed == [SpeakerSegment(0.0, 4.0, "A")]
    assert ambiguous == []


def test_micro_turn_at_real_change_remains_for_review() -> None:
    segments = [
        SpeakerSegment(0.0, 2.0, "A"),
        SpeakerSegment(2.0, 2.2, "B"),
        SpeakerSegment(2.2, 4.0, "B"),
    ]
    _, ambiguous = smooth_exclusive_segments(segments, 0.35)
    assert ambiguous == [SpeakerSegment(2.0, 2.2, "B")]
