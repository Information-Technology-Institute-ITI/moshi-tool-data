"""Deterministic, generated media fixtures with no production recordings."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    TranscriptUtterance,
)


def _pcm16(value: float) -> bytes:
    bounded = max(-1.0, min(1.0, value))
    return struct.pack("<h", round(bounded * 32767))


def write_mono_tone(
    path: Path,
    *,
    duration_seconds: float = 1.0,
    frequency_hz: float = 440.0,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Write a small deterministic PCM WAV suitable for browser/audio tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(
            b"".join(
                _pcm16(0.2 * math.sin(2 * math.pi * frequency_hz * index / sample_rate))
                for index in range(frame_count)
            )
        )
    return path


def write_independent_stereo(
    path: Path,
    *,
    duration_seconds: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Write isolated left/right turns with a central overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(duration_seconds * sample_rate)
    first_end = round(frame_count * 0.6)
    second_start = round(frame_count * 0.4)
    frames = bytearray()
    for index in range(frame_count):
        left = 0.2 * math.sin(2 * math.pi * 330 * index / sample_rate) if index < first_end else 0
        right = (
            0.2 * math.sin(2 * math.pi * 550 * index / sample_rate)
            if index >= second_start
            else 0
        )
        frames.extend(_pcm16(left))
        frames.extend(_pcm16(right))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(frames)
    return path


def write_video_with_audio(
    path: Path,
    *,
    duration_seconds: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Generate a tiny H.264/AAC MP4 with an audible track using ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to generate the video fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x123b42:s=160x90:r=25:d={duration_seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={sample_rate}:duration={duration_seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"ffmpeg fixture generation failed: {result.stderr}")
    return path


def long_timeline_annotation(
    *,
    hours: float = 3,
    segment_seconds: float = 15,
    source_id: str = "source_long_fixture",
) -> AnnotationDocument:
    """Build a long logical timeline without allocating a multi-hour media file."""
    total_samples = round(hours * 60 * 60 * SAMPLE_RATE)
    segment_samples = round(segment_seconds * SAMPLE_RATE)
    transcript: list[TranscriptUtterance] = []
    activities: list[ActivityRegion] = []
    for index, start in enumerate(range(0, total_samples, segment_samples)):
        end = min(total_samples, start + segment_samples)
        speaker = "A" if index % 2 == 0 else "B"
        transcript.append(
            TranscriptUtterance(
                id=f"utterance_{index:06d}",
                speaker=speaker,
                start_sample=start,
                end_sample=end,
                text=f"fixture segment {index}",
                model_text=f"fixture segment {index}",
            )
        )
        activities.append(
            ActivityRegion(
                id=f"activity_{index:06d}",
                speaker=speaker,
                start_sample=start,
                end_sample=end,
                origin="model",
            )
        )
    return AnnotationDocument(
        source_id=source_id,
        transcript=transcript,
        activities=activities,
    )

