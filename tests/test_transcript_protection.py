from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import AnnotationDocument, TranscriptUtterance
from moshi_data_pipeline.studio.transcript_protection import (
    SNAPSHOT_FORMAT,
    create_transcript_snapshot,
    verify_transcript_snapshot,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace_with_transcripts(root: Path) -> Path:
    workspace = root / "workspace"
    catalog = StudioCatalog(workspace / "catalog.sqlite3")
    owner = catalog.ensure_local_admin()
    project = catalog.create_project("Protected Arabic", owner_user_id=owner["id"])
    source = catalog.create_source(
        project["id"],
        "episode.wav",
        "originals/episode.wav",
        "audio/wav",
        "a" * 64,
        100,
    )
    first = AnnotationDocument(
        source_id=source["id"],
        transcript=[
            TranscriptUtterance(
                speaker="A",
                start_sample=0,
                end_sample=24_000,
                text="النص الأصلي",
                model_text="النص الأصلي",
            )
        ],
    )
    saved = catalog.save_annotation(source["id"], 0, first)
    corrected = saved.model_copy(deep=True)
    corrected.transcript[0].text = "النص المصحح"
    catalog.save_annotation(source["id"], 1, corrected)

    for role, filename, content in (
        ("analysis.raw_transcript", "raw_transcript.json", b'{"text":"raw"}\n'),
        ("analysis.aligned_transcript", "aligned_transcript.json", b'{"text":"aligned"}\n'),
        ("analysis.diarization", "diarization.json", b'{"speaker":"A"}\n'),
    ):
        relative = Path("sources") / source["id"] / filename
        artifact = workspace / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)
        catalog.register_artifact(
            role=role,
            relative_path=relative.as_posix(),
            sha256=_sha256(artifact),
            size_bytes=artifact.stat().st_size,
            media_type="application/json",
            project_id=project["id"],
            source_id=source["id"],
        )
    return workspace


def test_snapshot_preserves_all_revisions_and_transcript_artifacts(tmp_path: Path) -> None:
    workspace = _workspace_with_transcripts(tmp_path)
    snapshot = tmp_path / "protected" / "snapshot-1"
    manifest = create_transcript_snapshot(workspace, snapshot)

    assert manifest["format"] == SNAPSHOT_FORMAT
    assert manifest["complete"] is True
    assert manifest["annotations"]["revision_count"] == 2
    assert len(manifest["artifacts"]) == 3
    assert verify_transcript_snapshot(snapshot)["valid"] is True
    assert not (snapshot / "catalog.sqlite3-wal").exists()
    assert not (snapshot / "catalog.sqlite3-shm").exists()

    with gzip.open(snapshot / "annotation_revisions.jsonl.gz", "rt", encoding="utf-8") as stream:
        revisions = [json.loads(line) for line in stream]
    assert revisions[0]["annotation"]["transcript"][0]["text"] == "النص الأصلي"
    assert revisions[1]["annotation"]["transcript"][0]["text"] == "النص المصحح"


def test_snapshot_refuses_overwrite_and_detects_tampering(tmp_path: Path) -> None:
    workspace = _workspace_with_transcripts(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_transcript_snapshot(workspace, snapshot)

    try:
        create_transcript_snapshot(workspace, snapshot)
    except FileExistsError:
        pass
    else:
        raise AssertionError("snapshot overwrite should be refused")

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    raw = next(item for item in manifest["artifacts"] if item["role"] == "analysis.raw_transcript")
    artifact = snapshot / raw["snapshot_relative_path"]
    artifact.write_text("tampered", encoding="utf-8")
    report = verify_transcript_snapshot(snapshot)
    assert report["valid"] is False
    assert any("checksum mismatch" in problem for problem in report["problems"])
