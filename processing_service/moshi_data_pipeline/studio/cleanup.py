from __future__ import annotations

import shutil
from pathlib import Path

LEGACY_ARTIFACTS = (
    "data",
    "data_balanced",
    "sample_dataset",
    "dist",
    "moshi_data_pipeline.egg-info",
    ".pytest_cache",
    ".ruff_cache",
)


def remove_legacy_artifacts(workspace: Path) -> list[Path]:
    """Permanently remove only the explicitly approved v1 generated targets."""
    root = workspace.resolve()
    removed: list[Path] = []
    for name in LEGACY_ARTIFACTS:
        target = (root / name).resolve()
        if target.parent != root:
            raise ValueError(f"Refusing legacy cleanup target outside workspace: {target}")
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(target)
        elif target.exists():
            target.unlink()
            removed.append(target)
    return removed
