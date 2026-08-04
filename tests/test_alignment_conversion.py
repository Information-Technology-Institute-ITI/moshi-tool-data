from moshi_data_pipeline.audio.validation import validate_alignment_payload
from moshi_data_pipeline.config import NormalizationConfig
from moshi_data_pipeline.models import Word
from moshi_data_pipeline.output.moshi_json import build_moshi_payload


def test_moshi_payload_is_assistant_only_and_clip_relative() -> None:
    words = [
        Word("قبل", 9.0, 9.5, "SPEAKER_00"),
        Word("إزّيك", 10.5, 10.9, "SPEAKER_00"),
        Word("okay", 11.0, 11.4, "SPEAKER_01"),
        Word("AI", 12.0, 12.3, "SPEAKER_00"),
        Word("مفقود", None, None, "SPEAKER_00"),
    ]
    payload, report = build_moshi_payload(words, "SPEAKER_00", 10.0, 13.0, NormalizationConfig())
    assert payload == {
        "alignments": [
            ["ازيك", [0.5, 0.9000000000000004], "SPEAKER_MAIN"],
            ["AI", [2.0, 2.3000000000000007], "SPEAKER_MAIN"],
        ]
    }
    assert report["skipped_words"] == [{"word": "مفقود", "reason": "unaligned"}]
    assert validate_alignment_payload(payload, 3.0) == []


def test_invalid_timestamp_is_rejected() -> None:
    payload = {"alignments": [["word", [1.0, 1.0], "SPEAKER_MAIN"]]}
    assert "alignment_timestamp_out_of_bounds" in validate_alignment_payload(payload, 2.0)
