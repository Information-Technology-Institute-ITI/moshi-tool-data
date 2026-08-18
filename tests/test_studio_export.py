import json

import numpy as np

from moshi_data_pipeline.audio.io import read_audio, write_pcm16
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.models import Word
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    ClipPlanRequest,
    ExclusionRegion,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.exporter import (
    _assistant_transcript_blocker,
    _source_assignments,
    build_project_export,
)
from moshi_data_pipeline.studio.media import StudioPaths
from moshi_data_pipeline.studio.planning import propose_clip_plan
from moshi_data_pipeline.studio.processing import clip_artifacts, render_source_clips


def test_source_level_split_is_deterministic_and_disjoint() -> None:
    source_ids = [f"source_{value:032x}" for value in range(12)]
    first = _source_assignments(source_ids)
    second = _source_assignments(list(reversed(source_ids)))
    assert first == second
    train, evaluation, warnings = first
    assert not warnings
    assert train.isdisjoint(evaluation)
    assert train | evaluation == set(source_ids)
    assert len(evaluation) == 1


def test_unapproved_overlap_is_muted_and_single_source_exports_train_only(tmp_path) -> None:
    paths = StudioPaths(tmp_path / "workspace")
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project(
        "Stereo routing", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    original = paths.originals / "source.wav"
    original.write_bytes(b"immutable-placeholder")
    source = catalog.create_source(
        project["id"],
        "source.wav",
        paths.relative(original),
        "audio/wav",
        "c" * 64,
        original.stat().st_size,
    )
    sample_count = 20 * SAMPLE_RATE
    time = np.arange(sample_count, dtype=np.float32) / SAMPLE_RATE
    mono = (0.12 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
    paths.source_root(source["id"]).mkdir(parents=True)
    write_pcm16(paths.canonical_audio(source["id"]), mono[:, None], SAMPLE_RATE)
    catalog.update_source(
        source["id"],
        status="ready",
        duration_samples=sample_count,
        origin="Owned recording",
        rights_basis="owned",
        rights_confirmed=True,
    )
    annotation = AnnotationDocument(
        source_id=source["id"],
        assistant_speaker="A",
        activities_finalized=True,
        activities=[
            ActivityRegion(
                speaker="A",
                start_sample=0,
                end_sample=11 * SAMPLE_RATE,
            ),
            ActivityRegion(
                speaker="B",
                start_sample=10 * SAMPLE_RATE,
                end_sample=20 * SAMPLE_RATE,
            ),
        ],
        exclusions=[
            ExclusionRegion(
                kind="noise",
                start_sample=5 * SAMPLE_RATE,
                end_sample=6 * SAMPLE_RATE,
            )
        ],
        aligned_words=[
            Word("اهلا", 2.0, 2.5, speaker="A", original="اهلا").to_dict()
        ],
        transcript=[
            TranscriptUtterance(
                speaker="A",
                start_sample=2 * SAMPLE_RATE,
                end_sample=3 * SAMPLE_RATE,
                text="اهلا",
                alignment_status="aligned",
            )
        ],
    )
    saved = catalog.save_annotation(source["id"], 0, annotation)
    plan = propose_clip_plan(
        source["id"],
        saved,
        sample_count,
        ClipPlanRequest(
            mode="manual",
            boundaries_samples=[0, sample_count],
        ),
    )
    assert plan.feasible
    catalog.save_clip_plan(plan)
    config = PipelineConfig()
    render_source_clips(catalog, paths, source["id"], config, lambda *_: None)
    first_artifact_path = catalog.get_source(source["id"])["clip_artifacts_path"]
    render_source_clips(catalog, paths, source["id"], config, lambda *_: None)
    second_artifact_path = catalog.get_source(source["id"])["clip_artifacts_path"]
    assert first_artifact_path != second_artifact_path
    assert paths.resolve_relative(first_artifact_path).exists()
    artifacts = clip_artifacts(catalog, paths, source["id"])
    artifact = artifacts["artifacts"][0]
    rendered, _ = read_audio(paths.resolve_relative(artifact["wav_path"]))
    assert np.max(np.abs(rendered[int(10.25 * SAMPLE_RATE) : int(10.75 * SAMPLE_RATE)])) == 0
    assert np.max(np.abs(rendered[int(5.25 * SAMPLE_RATE) : int(5.75 * SAMPLE_RATE)])) == 0
    assert np.max(np.abs(rendered[int(2 * SAMPLE_RATE) : int(3 * SAMPLE_RATE), 0])) > 0
    assert np.max(np.abs(rendered[int(15 * SAMPLE_RATE) : int(16 * SAMPLE_RATE), 1])) > 0

    clip_id = artifact["clip"]["id"]
    catalog.save_clip_decision(source["id"], clip_id, "approve", True)
    export = catalog.create_export(
        project["id"], "routing dataset", catalog.next_export_version(project["id"])
    )
    result = build_project_export(
        catalog, paths, export["id"], config, lambda *_: None
    )
    export_root = paths.resolve_relative(result["path"])
    train = [
        json.loads(line)
        for line in (export_root / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(train) == 1
    assert (export_root / "eval.jsonl").read_text(encoding="utf-8") == ""
    sidecar = next((export_root / "data_stereo").glob("*.json"))
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["alignments"][0][2] == "SPEAKER_MAIN"
    assert (export_root / "golden_regression.jsonl").exists()
    assert (export_root / "reproducibility.json").exists()
    reproducibility = json.loads(
        (export_root / "reproducibility.json").read_text(encoding="utf-8")
    )
    assert len(reproducibility["config_sha256"]) == 64
    assert "whisperx" in reproducibility["dependencies"]
    assert result["report"]["warnings"]


def test_suspicious_assistant_transcript_requires_human_verification(tmp_path) -> None:
    paths = StudioPaths(tmp_path / "workspace")
    catalog = StudioCatalog(paths.database)
    project = catalog.create_project(
        "Verification", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    original = paths.originals / "source.wav"
    original.write_bytes(b"placeholder")
    source = catalog.create_source(
        project["id"],
        "source.wav",
        paths.relative(original),
        "audio/wav",
        "d" * 64,
        original.stat().st_size,
    )
    utterance = TranscriptUtterance(
        speaker="A",
        start_sample=0,
        end_sample=SAMPLE_RATE,
        text="نص مشكوك فيه",
        quality_flags=["decode_disagreement"],
        alignment_status="aligned",
    )
    saved = catalog.save_annotation(
        source["id"],
        0,
        AnnotationDocument(
            source_id=source["id"],
            assistant_speaker="A",
            transcript=[utterance],
        ),
    )
    approved = [{"clip": {"start_sample": 0, "end_sample": SAMPLE_RATE}}]
    blocker = _assistant_transcript_blocker(
        catalog, source["id"], "source.wav", approved
    )
    assert blocker is not None
    assert "verified against the audio" in blocker

    catalog.save_annotation(
        source["id"],
        saved.version,
        saved.model_copy(
            update={
                "transcript": [
                    utterance.model_copy(update={"human_verified": True})
                ]
            }
        ),
    )
    assert (
        _assistant_transcript_blocker(
            catalog, source["id"], "source.wav", approved
        )
        is None
    )
