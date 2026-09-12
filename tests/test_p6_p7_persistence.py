from __future__ import annotations

import hashlib

from moshi_data_pipeline.studio.authorization import RequestPrincipal
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import (
    ActivityRegion,
    AnnotationDocument,
    OverlapReview,
    TranscriptUtterance,
)


def test_overlap_audit_and_evaluation_report_preserve_exact_inputs(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    user = catalog.ensure_local_admin()
    principal = RequestPrincipal.from_catalog_user(user)
    project = catalog.create_project("Quality", owner_user_id=user["id"])
    source = catalog.create_source(
        project["id"], "source.wav", "source.wav", "audio/wav", "a" * 64, 1
    )
    catalog.update_source(source["id"], duration_samples=48_000)
    annotation = AnnotationDocument(
        source_id=source["id"],
        activities=[
            ActivityRegion(id="a", speaker="A", start_sample=0, end_sample=40_000),
            ActivityRegion(id="b", speaker="B", start_sample=20_000, end_sample=48_000),
        ],
        transcript=[
            TranscriptUtterance(
                speaker="A",
                start_sample=0,
                end_sample=40_000,
                text="verified text",
                human_verified=True,
            )
        ],
        overlap_reviews=[
            OverlapReview(
                id="logical-review",
                speaker_a_activity_id="a",
                speaker_b_activity_id="b",
                start_sample=20_000,
                end_sample=40_000,
                classification="confirmed",
                training_decision="raw",
            )
        ],
    )
    saved, save_info = catalog.save_annotation_record(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    with catalog.connect() as connection:
        review = connection.execute("SELECT * FROM overlap_reviews").fetchone()
        event = connection.execute("SELECT * FROM overlap_review_events").fetchone()
    assert review["annotation_version"] == saved.version
    assert review["reviewer_user_id"] == user["id"]
    assert event["event_type"] == "saved"

    artifact_path = tmp_path / "raw.json"
    artifact_path.write_text('{"segments": []}', encoding="utf-8")
    raw = artifact_path.read_bytes()
    catalog.register_artifact(
        role="analysis.raw_transcript",
        relative_path="raw.json",
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type="application/json",
        project_id=project["id"],
        source_id=source["id"],
        principal=principal,
    )
    machine = catalog.ensure_machine_transcript_version(source["id"])
    task = catalog.submit_annotation_approval(
        source["id"],
        annotation_version=saved.version,
        content_fingerprint=save_info["content_fingerprint"],
        principal=principal,
    )
    catalog.decide_annotation_approval(task["id"], "approved", "Ready", principal=principal)
    corrected = catalog.current_corrected_version(source["id"])
    report = catalog.create_evaluation_report(
        source_id=source["id"],
        machine_transcript_version_id=machine["id"],
        corrected_version_id=corrected["id"],
        annotation_version=saved.version,
        content_fingerprint=save_info["content_fingerprint"],
        normalization_policy="strict-v1",
        overlap_policy="deduplicate",
        scope={"source_ids": [source["id"]]},
        result={"word_error": {"rate": 0.0}},
        principal=principal,
    )

    assert report["annotation_version"] == saved.version
    assert report["machine_transcript_version_id"] == machine["id"]
    with catalog.connect() as connection:
        reference = connection.execute(
            "SELECT * FROM annotation_version_references WHERE reference_id=?",
            (report["id"],),
        ).fetchone()
    assert reference["reference_kind"] == "evaluation_report"
    assert reference["annotation_version"] == saved.version
