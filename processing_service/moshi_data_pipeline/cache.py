from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

STAGES = [
    "inspect",
    "extract",
    "transcribe",
    "align",
    "diarize",
    "select-speaker",
    "segment",
    "render-stereo",
    "generate-json",
    "validate",
    "manifest",
]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any, *, pretty: bool = True) -> None:
    atomic_write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=pretty,
        )
        + "\n",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def input_fingerprint(path: Path, chunk_bytes: int = 1024 * 1024) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        digest.update(stream.read(chunk_bytes))
        if stat.st_size > chunk_bytes:
            stream.seek(max(0, stat.st_size - chunk_bytes))
            digest.update(stream.read(chunk_bytes))
    return {
        "resolved_path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "edge_sha256": digest.hexdigest(),
    }


class StageCache:
    def __init__(self, path: Path, source: Path):
        self.path = path
        self.source_fingerprint = input_fingerprint(source)
        if path.exists():
            try:
                self.state = load_json(path)
            except (OSError, ValueError):
                self.state = {}
        else:
            self.state = {}
        if self.state.get("input") != self.source_fingerprint:
            self.state = {"input": self.source_fingerprint, "stages": {}}

    def should_run(
        self,
        stage: str,
        config_fingerprint: str,
        outputs: list[Path],
        *,
        resume: bool,
        force_stage: str | None,
    ) -> bool:
        if not resume:
            return True
        if force_stage and STAGES.index(stage) >= STAGES.index(force_stage):
            return True
        record = self.state.get("stages", {}).get(stage, {})
        return not (
            record.get("config") == config_fingerprint
            and record.get("complete") is True
            and all(output.exists() for output in outputs)
        )

    def complete(self, stage: str, config_fingerprint: str, outputs: list[Path]) -> None:
        self.state.setdefault("stages", {})[stage] = {
            "complete": True,
            "config": config_fingerprint,
            "outputs": [str(path) for path in outputs],
        }
        atomic_write_json(self.path, self.state)
