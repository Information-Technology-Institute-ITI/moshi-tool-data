from __future__ import annotations

import ast
from pathlib import Path

from moshi_data_pipeline.studio.job_contracts import validate_job_result

ROOT = Path(__file__).parents[1]


def test_processing_algorithms_do_not_import_web_state_modules() -> None:
    forbidden = {
        "moshi_data_pipeline.studio.catalog",
        "moshi_data_pipeline.studio.server",
        "moshi_data_pipeline.studio.service",
        "moshi_data_pipeline.studio.media.StudioPaths",
    }
    for relative in (
        "moshi_data_pipeline/studio/processing.py",
        "moshi_data_pipeline/studio/exporter.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not imports & forbidden, f"{relative} imports web state: {imports & forbidden}"


def test_all_nine_job_results_have_typed_contracts() -> None:
    source_id = "source_fixture"
    annotation = {"source_id": source_id}
    fixtures = {
        "initialize": {
            "kind": "initialize",
            "source_id": source_id,
            "annotation": annotation,
            "inspection": {},
            "duration_samples": 1,
            "source_updates": {"status": "ready"},
        },
        "transcribe": {
            "kind": "transcribe",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "annotation": annotation,
        },
        "review_transcript": {
            "kind": "review_transcript",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "annotation": annotation,
        },
        "rediarize": {
            "kind": "rediarize",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "annotation": annotation,
        },
        "realign": {
            "kind": "realign",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "annotation": annotation,
        },
        "recover_overlap": {
            "kind": "recover_overlap",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "recoveries": [],
        },
        "transcribe_overlap": {
            "kind": "transcribe_overlap",
            "source_id": source_id,
            "region_id": "overlap_1",
            "expected_annotation_version": 1,
            "stem_transcripts": {},
        },
        "generate": {
            "kind": "generate",
            "source_id": source_id,
            "expected_annotation_version": 1,
            "clip_manifest": {"artifacts": []},
        },
        "export": {
            "kind": "export",
            "export_id": "export_1",
            "report": {},
            "export_manifest": {"files": []},
        },
    }
    assert {kind for kind, value in fixtures.items() if validate_job_result(kind, value)} == set(
        fixtures
    )
