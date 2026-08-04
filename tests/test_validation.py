import numpy as np

from moshi_data_pipeline.audio.io import write_pcm16
from moshi_data_pipeline.audio.validation import validate_clip
from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.models import QCStatus


def test_validation_rejects_timestamp_beyond_wav(tmp_path) -> None:
    wav_path = tmp_path / "clip.wav"
    json_path = tmp_path / "clip.json"
    audio = np.ones((24_000, 2), dtype=np.float32) * 0.02
    write_pcm16(wav_path, audio, 24_000)
    atomic_write_json(
        json_path,
        {"alignments": [["bad", [0.8, 1.2], "SPEAKER_MAIN"]]},
    )
    result = validate_clip(wav_path, json_path, PipelineConfig())
    assert result.status == QCStatus.REJECT
    assert "alignment_timestamp_out_of_bounds" in result.reasons


def test_validation_rejects_channel_over_ninety_percent_silent(tmp_path) -> None:
    wav_path = tmp_path / "clip.wav"
    json_path = tmp_path / "clip.json"
    audio = np.zeros((240_000, 2), dtype=np.float32)
    audio[:12_000, 0] = 0.05
    audio[:, 1] = 0.05
    write_pcm16(wav_path, audio, 24_000)
    atomic_write_json(
        json_path,
        {"alignments": [["word", [0.1, 0.2], "SPEAKER_MAIN"]]},
    )
    config = PipelineConfig()
    config.qc.max_channel_silence_ratio = 0.90
    result = validate_clip(wav_path, json_path, config)
    assert result.status == QCStatus.REJECT
    assert "channel_silence_ratio_above_reject_threshold" in result.reasons


def test_separated_overlap_always_requires_review(tmp_path) -> None:
    wav_path = tmp_path / "clip.wav"
    json_path = tmp_path / "clip.json"
    audio = np.ones((24_000, 2), dtype=np.float32) * 0.02
    write_pcm16(wav_path, audio, 24_000)
    atomic_write_json(
        json_path,
        {"alignments": [["word", [0.1, 0.2], "SPEAKER_MAIN"]]},
    )
    result = validate_clip(
        wav_path,
        json_path,
        PipelineConfig(),
        overlap_ratio=0.10,
        separation_coverage=1.0,
        separation_used=True,
    )
    assert result.status == QCStatus.REVIEW
    assert "separated_overlap_requires_review" in result.reasons
