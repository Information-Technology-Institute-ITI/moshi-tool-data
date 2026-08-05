from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import soundfile as sf

from moshi_data_pipeline.cache import atomic_write_json

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(value: str) -> str:
    name = Path(value).name.strip()
    cleaned = SAFE_NAME.sub("_", name).strip("._")
    return cleaned[:180] or "media.bin"


class StudioPaths:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.database = self.root / "catalog.sqlite3"
        self.originals = self.root / "originals"
        self.sources = self.root / "sources"
        self.exports = self.root / "exports"
        self.incoming = self.root / ".incoming"
        for directory in (self.root, self.originals, self.sources, self.exports, self.incoming):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_relative(self, value: str) -> Path:
        path = (self.root / value).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("Path escapes the studio workspace")
        return path

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError("Path is outside the studio workspace")
        return resolved.relative_to(self.root).as_posix()

    def source_root(self, source_id: str) -> Path:
        if not re.fullmatch(r"source_[a-f0-9]{32}", source_id):
            raise ValueError("Invalid source id")
        return self.sources / source_id

    def canonical_audio(self, source_id: str) -> Path:
        return self.source_root(source_id) / "canonical.wav"

    def canonical_channels(self, source_id: str) -> Path:
        return self.source_root(source_id) / "canonical_channels.wav"

    def video_proxy(self, source_id: str) -> Path:
        return self.source_root(source_id) / "proxy.mp4"

    def peaks(self, source_id: str) -> Path:
        return self.source_root(source_id) / "peaks.json"

    def artifact(self, source_id: str, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("Invalid artifact name")
        return self.source_root(source_id) / name


async def store_upload(
    paths: StudioPaths,
    filename: str,
    stream: AsyncIterator[bytes],
) -> tuple[Path, str, int]:
    clean_name = safe_filename(filename)
    temporary = paths.incoming / f"{uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            async for chunk in stream:
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise ValueError("Uploaded file is empty")
        destination = paths.originals / f"{uuid4().hex}_{clean_name}"
        os.replace(temporary, destination)
        return destination, digest.hexdigest(), size
    finally:
        if temporary.exists():
            temporary.unlink()


def create_waveform_peaks(
    audio_path: Path,
    destination: Path,
    *,
    points: int = 4_000,
) -> dict[str, Any]:
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
