from __future__ import annotations

import shutil
import struct
import wave

import pytest

from moshi_data_pipeline.audio.ffmpeg import inspect_media
from moshi_data_pipeline.studio.domain import SAMPLE_RATE
from tests.support.media_fixtures import (
    long_timeline_annotation,
    write_independent_stereo,
    write_mono_tone,
    write_video_with_audio,
)


def test_generated_mono_and_stereo_fixtures_have_expected_headers(tmp_path) -> None:
    mono = write_mono_tone(tmp_path / "mono.wav", duration_seconds=0.2)
    stereo = write_independent_stereo(tmp_path / "stereo.wav", duration_seconds=0.2)
    with wave.open(str(mono), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getframerate() == SAMPLE_RATE
        assert stream.getnframes() == round(0.2 * SAMPLE_RATE)
    with wave.open(str(stereo), "rb") as stream:
        assert stream.getnchannels() == 2
        assert stream.getnframes() == round(0.2 * SAMPLE_RATE)
        frame_count = stream.getnframes()
        samples = struct.unpack(f"<{frame_count * 2}h", stream.readframes(frame_count))

    left = samples[0::2]
    right = samples[1::2]
    first = slice(0, round(frame_count * 0.3))
    overlap = slice(round(frame_count * 0.45), round(frame_count * 0.55))
    last = slice(round(frame_count * 0.7), frame_count)
    assert any(left[first]) and not any(right[first])
    assert any(left[overlap]) and any(right[overlap])
    assert not any(left[last]) and any(right[last])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_generated_video_fixture_has_audio_and_video(tmp_path) -> None:
    video = write_video_with_audio(tmp_path / "review.mp4", duration_seconds=0.4)
    report = inspect_media(video)
    assert report["has_video"] is True
    assert report["sample_rate"] == SAMPLE_RATE
    assert 0.3 <= report["duration"] <= 0.5


def test_long_timeline_fixture_needs_no_large_media_file() -> None:
    annotation = long_timeline_annotation(hours=3, segment_seconds=15)
    assert len(annotation.transcript) == 720
    assert annotation.transcript[-1].end_sample == 3 * 60 * 60 * SAMPLE_RATE
