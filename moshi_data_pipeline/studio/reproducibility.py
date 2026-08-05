from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.model_revisions import IMMUTABLE_REVISION
from moshi_data_pipeline.transcription.whisperx_backend import model_repository

_PACKAGES = (
    "moshi-data-pipeline",
    "torch",
    "torchaudio",
    "torchcodec",
    "faster-whisper",
    "whisperx",
    "pyannote.audio",
    "speechbrain",
    "numpy",
    "soundfile",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_versions() -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = None
    return values


def _hf_revision(
    identifier: str,
    workspace: Path,
    requested_revision: str | None,
) -> str | None:
    if Path(identifier).exists():
        return "local"
    if "/" not in identifier:
        return None
    requested = requested_revision or "main"
    if IMMUTABLE_REVISION.fullmatch(requested):
        return requested.lower()
    repo_directory = f"models--{identifier.replace('/', '--')}"
    candidates: list[Path] = []
    explicit_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
    hf_home = os.environ.get("HF_HOME")
    if explicit_cache:
        candidates.append(Path(explicit_cache))
    if hf_home:
        candidates.append(Path(hf_home) / "hub")
    candidates.extend(
        [
            workspace.parent / ".downloads" / "huggingface" / "hub",
            Path.home() / ".cache" / "huggingface" / "hub",
        ]
    )
    for cache in candidates:
        reference = cache / repo_directory / "refs" / requested
        if reference.is_file():
            value = reference.read_text(encoding="utf-8").strip()
            if value:
                return value
    return None


def _cache_identifier(role: str, identifier: str) -> str:
    if role.startswith("transcription") and "/" not in identifier:
        return f"Systran/faster-whisper-{identifier}"
    return identifier


def _ffmpeg_version() -> str | None:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.splitlines()[0] if completed.stdout else None


def reproducibility_snapshot(
    config: PipelineConfig,
    workspace: Path,
    output_files: list[Path],
) -> dict[str, Any]:
    packages = _package_versions()
    review = config.transcription.model_copy(
        update={
            "model": config.transcription.review_model or config.transcription.model,
            "model_repository": (
                config.transcription.review_model_repository
                if config.transcription.review_model
                else config.transcription.model_repository
            ),
        }
    )
    model_identifiers = {
        "transcription": (
            config.transcription.model,
            model_repository(config.transcription),
            config.transcription.model_revision,
        ),
        "transcription_review": (
            review.model,
            model_repository(review),
            config.transcription.review_model_revision
            if config.transcription.review_model
            else config.transcription.model_revision,
        ),
        "alignment": (
            config.alignment.model or "whisperx-language-default",
            config.alignment.model or "whisperx-language-default",
            config.alignment.model_revision,
        ),
        "diarization": (
            config.diarization.model,
            config.diarization.model,
            config.diarization.model_revision,
        ),
        "separation": (
            config.separation.model,
            config.separation.model,
            config.separation.model_revision,
        ),
        "speaker_embedding": (
            config.separation.embedding_model,
            config.separation.embedding_model,
            config.separation.embedding_model_revision,
        ),
    }
    models = {}
    for role, (identifier, repository, requested_revision) in model_identifiers.items():
        cache_identifier = _cache_identifier(role, repository)
        revision = _hf_revision(cache_identifier, workspace, requested_revision)
        material = f"{role}\0{cache_identifier}\0{revision or 'unknown'}".encode()
        models[role] = {
            "identifier": identifier,
            "cache_identifier": cache_identifier,
            "requested_revision": requested_revision or "main",
            "immutable_revision": revision,
            "revision_status": (
                "local" if revision == "local" else "resolved" if revision else "not_discoverable"
            ),
            "descriptor_sha256": hashlib.sha256(material).hexdigest(),
        }
    return {
        "config_sha256": config.fingerprint(),
        "models": models,
        "dependencies": packages,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ffmpeg": _ffmpeg_version(),
        },
        "files": {
            path.name: {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(output_files)
            if path.is_file()
        },
    }
