from moshi_data_pipeline.models import Word
from moshi_data_pipeline.studio.activity import close_word_supported_activity_gaps
from moshi_data_pipeline.studio.domain import SAMPLE_RATE, ActivityRegion, ExclusionRegion


def _sample(seconds: float) -> int:
    return round(seconds * SAMPLE_RATE)


def _region(speaker: str, start: float, end: float) -> ActivityRegion:
    return ActivityRegion(
        speaker=speaker,
        start_sample=_sample(start),
        end_sample=_sample(end),
        origin="model",
    )


def test_word_supported_same_speaker_gap_is_closed() -> None:
    activities = [_region("A", 0.0, 1.0), _region("A", 1.3, 2.0)]
    words = [Word("hello", 0.8, 1.5, speaker="A")]

    repaired, repairs = close_word_supported_activity_gaps(
        activities, words, [], maximum_gap_seconds=0.5
    )

    assert [(value.start_sample, value.end_sample) for value in repaired] == [
        (_sample(0.0), _sample(2.0))
    ]
    assert repairs == [
        {
            "speaker": "A",
            "start_sample": _sample(1.0),
            "end_sample": _sample(1.3),
            "gap_seconds": 0.3,
        }
    ]


def test_natural_pause_without_aligned_speech_remains_open() -> None:
    activities = [_region("A", 0.0, 1.0), _region("A", 1.3, 2.0)]

    repaired, repairs = close_word_supported_activity_gaps(
        activities, [], [], maximum_gap_seconds=0.5
    )

    assert len(repaired) == 2
    assert repairs == []


def test_opposite_speaker_blocks_a_word_supported_bridge() -> None:
    activities = [
        _region("A", 0.0, 1.0),
        _region("B", 1.1, 1.2),
        _region("A", 1.3, 2.0),
    ]
    words = [Word("hello", 0.8, 1.5, speaker="A")]

    repaired, repairs = close_word_supported_activity_gaps(
        activities, words, [], maximum_gap_seconds=0.5
    )

    assert len(repaired) == 3
    assert repairs == []


def test_exclusion_blocks_a_word_supported_bridge() -> None:
    activities = [_region("A", 0.0, 1.0), _region("A", 1.3, 2.0)]
    words = [Word("hello", 0.8, 1.5, speaker="A")]
    exclusions = [
        ExclusionRegion(
            kind="noise",
            start_sample=_sample(1.1),
            end_sample=_sample(1.2),
        )
    ]

    repaired, repairs = close_word_supported_activity_gaps(
        activities, words, exclusions, maximum_gap_seconds=0.5
    )

    assert len(repaired) == 2
    assert repairs == []
