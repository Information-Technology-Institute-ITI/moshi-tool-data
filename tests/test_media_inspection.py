import numpy as np

from moshi_data_pipeline.audio.ffmpeg import inspect_media
from moshi_data_pipeline.audio.io import write_pcm16


def test_dual_mono_input_is_detected(tmp_path) -> None:
    sample_rate = 24_000
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    signal = 0.1 * np.sin(2 * np.pi * 440 * time)
    path = tmp_path / "dual_mono.wav"
    write_pcm16(path, np.column_stack((signal, signal)), sample_rate)
    report = inspect_media(path)
    assert report["source_is_stereo"] is True
    assert report["source_is_dual_mono"] is True
    assert report["source_channel_difference_relative_db"] <= -30.0
