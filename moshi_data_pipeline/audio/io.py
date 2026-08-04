from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf

from moshi_data_pipeline.exceptions import InputValidationError


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    except (OSError, RuntimeError) as exc:
        raise InputValidationError(f"Could not read audio {path}: {exc}") from exc
    return audio, int(sample_rate)


def read_audio_segment(
    path: Path, start: float, end: float, expected_sample_rate: int = 24_000
) -> tuple[np.ndarray, int]:
    if start < 0 or end <= start:
        raise InputValidationError(f"Invalid audio interval [{start}, {end}]")
    try:
        with sf.SoundFile(path) as stream:
            sample_rate = int(stream.samplerate)
            if sample_rate != expected_sample_rate:
                raise InputValidationError(
                    f"Expected {expected_sample_rate} Hz in {path}, got {sample_rate}"
                )
            start_frame = round(start * sample_rate)
            frame_count = round(end * sample_rate) - start_frame
            stream.seek(start_frame)
            audio = stream.read(frame_count, dtype="float32", always_2d=True)
    except (OSError, RuntimeError) as exc:
        raise InputValidationError(f"Could not read interval from {path}: {exc}") from exc
    if len(audio) != frame_count:
        raise InputValidationError(
            f"Requested {frame_count} samples from {path}, decoded {len(audio)}"
        )
    return audio, sample_rate


def write_pcm16(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    if audio.ndim != 2:
        raise ValueError("audio must be shaped [samples, channels]")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{os.getpid()}.tmp.wav")
    try:
        sf.write(temporary, audio, sample_rate, format="WAV", subtype="PCM_16")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def audio_info(path: Path) -> dict[str, int | float | str]:
    try:
        info = sf.info(path)
    except (OSError, RuntimeError) as exc:
        raise InputValidationError(f"Could not inspect WAV {path}: {exc}") from exc
    return {
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "samples": int(info.frames),
        "duration": float(info.frames / info.samplerate),
        "subtype": str(info.subtype),
        "format": str(info.format),
    }
