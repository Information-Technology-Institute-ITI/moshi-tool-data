"""Media helpers required by isolated GPU job execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moshi_data_pipeline.cache import atomic_write_json


def create_waveform_peaks(
    audio_path: Path,
    destination: Path,
    *,
    points: int = 4_000,
) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(audio_path) as stream:
        sample_rate = int(stream.samplerate)
        frames = int(stream.frames)
        block = max(1, int(np.ceil(frames / points)))
        values: list[list[float]] = []
        while True:
            audio = stream.read(block, dtype="float32", always_2d=True)
            if not len(audio):
                break
            mono = audio[:, 0]
            values.append([round(float(mono.min()), 6), round(float(mono.max()), 6)])
    payload = {
        "sample_rate": sample_rate,
        "duration_samples": frames,
        "points": values,
    }
    atomic_write_json(destination, payload)
    return payload


def load_json_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
