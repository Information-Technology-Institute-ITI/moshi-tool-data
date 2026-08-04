from moshi_data_pipeline.models import SpeakerSegment, Word
from moshi_data_pipeline.speakers.assignment import apply_speaker_overrides


def test_speaker_override_updates_timeline_and_words() -> None:
    segments = [
        SpeakerSegment(0.0, 2.0, "A"),
        SpeakerSegment(2.0, 4.0, "B"),
    ]
    words = [Word("word", 0.5, 1.0, "A", assignment_confidence=1.0)]
    updated_words, updated_segments = apply_speaker_overrides(
        words,
        segments,
        [{"start": 0.0, "end": 1.5, "speaker": "B"}],
    )
    assert updated_words[0].speaker == "B"
    assert updated_words[0].assignment_confidence == 1.0
    assert updated_segments[0] == SpeakerSegment(0.0, 1.5, "B")
