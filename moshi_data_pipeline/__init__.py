"""Dataset preparation for the official Kyutai Moshi fine-tuning format."""

from __future__ import annotations

import os
from pathlib import Path

_DLL_DIRECTORY_HANDLES: list[object] = []


def _load_local_hf_token() -> None:
    """Load HF_TOKEN from the ignored project .env unless already configured."""

    if os.environ.get("HF_TOKEN"):
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "HF_TOKEN":
            os.environ["HF_TOKEN"] = value.strip().strip("\"'")
            return


def _configure_private_ffmpeg_runtime() -> None:
    """Prefer the project-local FFmpeg shared build when one is installed.

    TorchCodec needs FFmpeg shared DLLs on Windows. The normal FFmpeg executable
    may be a static build, so the setup keeps a compatible shared build isolated
    under ``.runtime`` and registers its DLL directory for this Python process.
    """

    configured = os.environ.get("MOSHI_FFMPEG_BIN")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    project_root = Path(__file__).resolve().parent.parent
    candidates.extend(sorted((project_root / ".runtime" / "ffmpeg-7-shared").glob("*/bin")))
    for candidate in candidates:
        if not (candidate / "ffmpeg.exe").is_file():
            continue
        candidate_text = str(candidate.resolve())
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if candidate_text not in path_parts:
            os.environ["PATH"] = candidate_text + os.pathsep + os.environ.get("PATH", "")
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(candidate_text))
        break


_load_local_hf_token()
_configure_private_ffmpeg_runtime()

__version__ = "0.3.0"
