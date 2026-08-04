from moshi_data_pipeline.config import TranscriptionConfig
from moshi_data_pipeline.transcription.quality import (
    analyze_segment,
    normalized_disagreement,
    quality_rank,
    quality_summary,
)
from moshi_data_pipeline.transcription.whisperx_backend import (
    _aggregate_average_log_probability,
)


def test_repeated_phrase_is_flagged_as_hallucination() -> None:
    config = TranscriptionConfig()
    segment = {
        "start": 0.0,
        "end": 20.0,
        "text": "من بورسعيد " * 11,
        "avg_logprob": -0.2,
    }
    flags = analyze_segment(segment, config)
    assert "repeated_ngram" in flags
    segment["quality_flags"] = flags
    summary = quality_summary([segment])
    assert summary["unresolved_hallucination_count"] == 1


def test_decode_disagreement_is_normalized() -> None:
    assert normalized_disagreement("أهلا بالعالم", "أهلا بالعالم") == 0.0
    assert normalized_disagreement("أهلا بالعالم", "نص مختلف") > 0.5


def test_suspicious_arabic_character_run_is_flagged() -> None:
    config = TranscriptionConfig()
    flags = analyze_segment(
        {
            "start": 186.77,
            "end": 203.97,
            "text": "آأ آؤ آا آء آإ آئ آ؞ آة آث آت آ؜ آؠ آ؝",
            "avg_logprob": -1.14,
        },
        config,
    )
    assert "suspicious_character_sequence" in flags
    assert "low_average_log_probability" in flags


def test_retry_quality_prefers_plausible_text_over_corrupted_decode() -> None:
    assert quality_rank(
        ["suspicious_character_sequence", "low_average_log_probability"],
        -1.14,
    ) > quality_rank(["low_average_log_probability"], -0.72)


def test_retry_average_log_probability_is_duration_weighted() -> None:
    value = _aggregate_average_log_probability(
        [
            {"start": 0.0, "end": 1.0, "avg_logprob": -1.0},
            {"start": 1.0, "end": 4.0, "avg_logprob": -0.2},
        ]
    )
    assert value == -0.4
