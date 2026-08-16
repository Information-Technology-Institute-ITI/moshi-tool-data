from __future__ import annotations

import json
from typing import Any

from moshi_data_pipeline.studio.execution_contracts import ProcessingPaths, ProcessingState


def clip_artifacts(
    catalog: ProcessingState, paths: ProcessingPaths, source_id: str
) -> dict[str, Any]:
    source = catalog.get_source(source_id)
    annotation = catalog.latest_annotation(source_id)
    stored_path = source.get("clip_artifacts_path")
    path = paths.resolve_relative(stored_path) if stored_path else None
    if source["clips_stale"] or path is None or not path.exists():
        return {
            "source_id": source_id,
            "annotation_version": annotation.version,
            "stale": True,
            "artifacts": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if any(
        "wav_path" not in item or "json_path" not in item
        for item in value.get("artifacts", [])
    ) and hasattr(catalog, "list_artifacts"):
        role_paths = {
            str(item["role"]): str(item["relative_path"])
            for item in catalog.list_artifacts(source_id=source_id)
        }
        for item in value.get("artifacts", []):
            if "wav_path" not in item and item.get("wav_role") in role_paths:
                item["wav_path"] = role_paths[str(item["wav_role"])]
            if "json_path" not in item and item.get("json_role") in role_paths:
                item["json_path"] = role_paths[str(item["json_role"])]
    value["stale"] = False
    decisions = catalog.clip_decisions(source_id)
    for artifact in value["artifacts"]:
        artifact["decision"] = decisions.get(artifact["clip"]["id"])
    return value
