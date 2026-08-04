from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moshi_data_pipeline.cache import atomic_write_text, load_json


def rebuild_rejected_report(dataset_root: Path) -> int:
    rows: list[dict[str, Any]] = []
    for report_path in sorted((dataset_root / "reports").glob("*_qc.json")):
        try:
            report = load_json(report_path)
        except (OSError, ValueError):
            continue
        source = report.get("source")
        for clip in report.get("clips", []):
            if clip.get("status") == "REJECT":
                rows.append({"source": source, **clip})
    rows.sort(key=lambda item: (str(item.get("source")), str(item.get("clip_id"))))
    atomic_write_text(
        dataset_root / "reports" / "rejected_clips.jsonl",
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
    )
    return len(rows)
