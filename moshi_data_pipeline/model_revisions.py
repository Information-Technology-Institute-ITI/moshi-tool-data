from __future__ import annotations

import os
import re
from pathlib import Path

from moshi_data_pipeline.exceptions import DependencyError, ModelStageError

IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)


def _cache_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE")
    hf_home = os.environ.get("HF_HOME")
    if explicit:
        roots.append(Path(explicit))
    if hf_home:
        roots.append(Path(hf_home) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def _cached_revision(identifier: str, reference: str) -> str | None:
    directory = f"models--{identifier.replace('/', '--')}"
    for root in _cache_roots():
        value_path = root / directory / "refs" / reference
        if not value_path.is_file():
            continue
        value = value_path.read_text(encoding="utf-8").strip()
        if IMMUTABLE_REVISION.fullmatch(value):
            return value.lower()
    return None


def resolve_model_revision(
    identifier: str,
    requested_revision: str | None,
    *,
    token: str | bool | None = None,
    allow_network: bool = True,
) -> str:
    """Resolve a model branch/tag to an immutable Hugging Face commit SHA."""
    if Path(identifier).exists():
        return "local"
    requested = (requested_revision or "main").strip()
    if IMMUTABLE_REVISION.fullmatch(requested):
        return requested.lower()
    cached = _cached_revision(identifier, requested)
    if cached is not None:
        return cached
    if not allow_network:
        raise ModelStageError(
            f"No immutable cached revision is available for {identifier}@{requested}."
        )
    try:
        from huggingface_hub import model_info
    except ImportError as exc:
        raise DependencyError(
            "Immutable model resolution requires huggingface_hub from the ML dependencies"
        ) from exc
    try:
        info = model_info(identifier, revision=requested, token=token)
    except Exception as exc:
        raise ModelStageError(
            f"Could not resolve immutable revision for {identifier}@{requested}: {exc}"
        ) from exc
    sha = str(getattr(info, "sha", "") or "").strip()
    if not IMMUTABLE_REVISION.fullmatch(sha):
        raise ModelStageError(
            f"Hugging Face did not return an immutable revision for {identifier}@{requested}"
        )
    return sha.lower()


def snapshot_for_revision(
    identifier: str,
    requested_revision: str | None,
    *,
    token: str | bool | None = None,
) -> tuple[Path, str]:
    """Download/reuse a model snapshot by immutable SHA and return its local path."""
    path = Path(identifier)
    if path.exists():
        return path.resolve(), "local"
    revision = resolve_model_revision(
        identifier,
        requested_revision,
        token=token,
    )
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise DependencyError(
            "Pinned model snapshots require huggingface_hub from the ML dependencies"
        ) from exc
    try:
        try:
            snapshot = snapshot_download(
                repo_id=identifier,
                revision=revision,
                token=token,
                local_files_only=True,
            )
        except Exception:
            snapshot = snapshot_download(
                repo_id=identifier,
                revision=revision,
                token=token,
            )
    except Exception as exc:
        raise ModelStageError(
            f"Could not download pinned model {identifier}@{revision}: {exc}"
        ) from exc
    return Path(snapshot).resolve(), revision
