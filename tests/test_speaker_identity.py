import pytest

from moshi_data_pipeline.speakers.identity import choose_identity_mapping


def test_identity_mapping_keeps_confirmed_speakers_stable() -> None:
    similarities = {
        "SPEAKER_00": {"A": 0.82, "B": 0.31},
        "SPEAKER_01": {"A": 0.29, "B": 0.86},
    }
    assert choose_identity_mapping(similarities, 0.65, 0.10) == {
        "SPEAKER_00": "A",
        "SPEAKER_01": "B",
    }


def test_identity_mapping_handles_reversed_model_labels() -> None:
    similarities = {
        "SPEAKER_00": {"A": 0.25, "B": 0.88},
        "SPEAKER_01": {"A": 0.84, "B": 0.30},
    }
    assert choose_identity_mapping(similarities, 0.65, 0.10) == {
        "SPEAKER_00": "B",
        "SPEAKER_01": "A",
    }


def test_identity_mapping_rejects_ambiguous_voice_match() -> None:
    similarities = {
        "SPEAKER_00": {"A": 0.70, "B": 0.66},
        "SPEAKER_01": {"A": 0.67, "B": 0.71},
    }
    with pytest.raises(ValueError, match="ambiguous"):
        choose_identity_mapping(similarities, 0.65, 0.10)
