from __future__ import annotations

import hashlib

from moshi_data_pipeline.studio.authorization import RequestPrincipal
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import AnnotationDocument, TranscriptUtterance


def _workspace(tmp_path):
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    user = catalog.ensure_local_admin()
    principal = RequestPrincipal.from_catalog_user(user)
    project = catalog.create_project("P5", owner_user_id=user["id"])
    source = catalog.create_source(
        project["id"],
        "source.wav",
        "originals/source.wav",
        "audio/wav",
        "a" * 64,
        1,
    )
    catalog.update_source(source["id"], duration_samples=48_000)
    annotation = AnnotationDocument(
        source_id=source["id"],
        transcript=[
            TranscriptUtterance(
                speaker="A",
                start_sample=0,
                end_sample=24_000,
                text="original",
                human_verified=True,
            )
        ],
    )
    return catalog, principal, project, source, annotation


def test_save_updates_visible_version_and_new_version_is_explicit(tmp_path) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)

    first, first_info = catalog.save_annotation_record(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    assert first.version == 1
    assert first_info["created_new_version"] is True
    assert first_info["corrected_version"]["ordinal"] == 1

    changed = first.model_copy(
        update={"transcript": [first.transcript[0].model_copy(update={"text": "corrected"})]}
    )
    second, second_info = catalog.save_annotation_record(
        source["id"], first.version, changed, principal=principal, origin="human_save"
    )
    assert second.version == 2
    assert second_info["created_new_version"] is False
    assert second_info["corrected_version"]["ordinal"] == 1
    assert second_info["corrected_version"]["generation"] == 2
    revisions = catalog.annotation_revisions(source["id"])
    assert revisions[1]["retention_state"] == "recoverable"
    assert revisions[1]["recoverable_until"]

    unchanged, no_op = catalog.save_annotation_record(
        source["id"], second.version, second, principal=principal, origin="human_save"
    )
    assert unchanged.version == second.version
    assert no_op["no_op"] is True
    assert len(catalog.annotation_revisions(source["id"])) == 2

    third, third_info = catalog.save_annotation_record(
        source["id"],
        second.version,
        second.model_copy(update={"note": "major change"}),
        principal=principal,
        save_mode="new_version",
        origin="human_save",
    )
    assert third.version == 3
    assert third_info["corrected_version"]["ordinal"] == 2
    assert [value["ordinal"] for value in catalog.corrected_versions(source["id"])] == [2, 1]


def test_approved_version_is_frozen_and_next_save_creates_a_version(tmp_path) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)
    saved, info = catalog.save_annotation_record(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    task = catalog.submit_annotation_approval(
        source["id"],
        annotation_version=saved.version,
        content_fingerprint=info["content_fingerprint"],
        principal=principal,
    )
    catalog.decide_annotation_approval(
        task["id"], "approved", "Ready", principal=principal
    )

    next_saved, next_info = catalog.save_annotation_record(
        source["id"],
        saved.version,
        saved.model_copy(update={"note": "post approval edit"}),
        principal=principal,
        origin="human_save",
    )
    assert next_saved.version == 2
    assert next_info["created_new_version"] is True
    assert next_info["corrected_version"]["ordinal"] == 2
    versions = catalog.corrected_versions(source["id"])
    assert versions[1]["status"] == "approved"


def test_editing_pending_submission_supersedes_task_and_freezes_old_version(tmp_path) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)
    saved, info = catalog.save_annotation_record(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    task = catalog.submit_annotation_approval(
        source["id"],
        annotation_version=saved.version,
        content_fingerprint=info["content_fingerprint"],
        principal=principal,
    )

    changed, changed_info = catalog.save_annotation_record(
        source["id"],
        saved.version,
        saved.model_copy(update={"note": "edited after submission"}),
        principal=principal,
        origin="human_save",
    )

    assert changed.version == 2
    assert changed_info["created_new_version"] is True
    assert catalog.annotation_approval_task(task["id"])["status"] == "superseded"
    versions = catalog.corrected_versions(source["id"])
    assert versions[0]["status"] == "unapproved"
    assert versions[1]["status"] == "rejected"
    assert versions[1]["decision_note"] == "Superseded by a newer corrected version"


def test_machine_transcript_snapshots_are_source_local_and_immutable(tmp_path) -> None:
    catalog, principal, project, source, _ = _workspace(tmp_path)
    artifact = tmp_path / "artifacts" / source["id"] / "raw_transcript.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"text":"first"}', encoding="utf-8")

    def register() -> None:
        raw = artifact.read_bytes()
        catalog.register_artifact(
            role="analysis.raw_transcript",
            relative_path=artifact.relative_to(tmp_path).as_posix(),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            media_type="application/json",
            project_id=project["id"],
            source_id=source["id"],
            principal=principal,
        )

    register()
    first = catalog.ensure_machine_transcript_version(source["id"])
    assert first and first["ordinal"] == 1
    assert first["provenance_status"] == "historical_unknown"
    assert first["artifact_manifest"]["raw"]["sha256"] == hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    first_snapshot = tmp_path / first["raw_snapshot_path"]
    assert first_snapshot.read_text(encoding="utf-8") == '{"text":"first"}'

    artifact.write_text('{"text":"second"}', encoding="utf-8")
    register()
    second = catalog.ensure_machine_transcript_version(
        source["id"],
        model_name="test-model",
        model_revision="r1",
        config_fingerprint="b" * 64,
        provenance_status="exact",
    )
    assert second and second["ordinal"] == 2
    assert second["provenance_status"] == "exact"
    assert second["model_name"] == "test-model"
    assert first_snapshot.read_text(encoding="utf-8") == '{"text":"first"}'
    assert (tmp_path / second["raw_snapshot_path"]).read_text(encoding="utf-8") == '{"text":"second"}'


def test_aligned_words_are_stored_once_and_hydrated_without_data_loss(tmp_path) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)
    words = [{"word": "مرحبا", "start": 0.0, "end": 0.5, "speaker": "A"}]
    saved = catalog.save_annotation(
        source["id"],
        0,
        annotation.model_copy(update={"aligned_words": words}),
        principal=principal,
        origin="human_save",
    )
    with catalog.connect() as connection:
        revision = connection.execute(
            """
            SELECT annotation_json,shared_aligned_words_id
            FROM annotation_revisions WHERE source_id=? AND version=?
            """,
            (source["id"], saved.version),
        ).fetchone()
        assert revision["shared_aligned_words_id"]
        assert '"aligned_words":[]' in revision["annotation_json"]
        assert connection.execute("SELECT COUNT(*) FROM annotation_shared_blobs").fetchone()[0] == 1
    assert catalog.latest_annotation(source["id"]).aligned_words == words


def test_expired_generations_ignore_derived_chapters_but_keep_immutable_references(
    tmp_path,
) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)
    first = catalog.save_annotation(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    second = catalog.save_annotation(
        source["id"],
        first.version,
        first.model_copy(update={"note": "second"}),
        principal=principal,
        origin="human_save",
    )
    catalog.save_annotation(
        source["id"],
        second.version,
        second.model_copy(update={"note": "third"}),
        principal=principal,
        origin="human_save",
    )
    with catalog.connect() as connection:
        connection.execute(
            """
            UPDATE annotation_revisions SET recoverable_until='2000-01-01T00:00:00+00:00'
            WHERE source_id=? AND version IN (1,2)
            """,
            (source["id"],),
        )
        connection.execute(
            """
            INSERT INTO review_chapter_sets(
                id,source_id,annotation_version,mode,max_duration_seconds,
                boundary_search_seconds,active,created_at
            ) VALUES('old-chapters',?,1,'max_duration',1800,30,1,'2000-01-01T00:00:00+00:00')
            """,
            (source["id"],),
        )
        connection.execute(
            """
            INSERT INTO annotation_version_references(
                id,source_id,annotation_version,reference_kind,reference_id,created_at
            ) VALUES('training-ref',?,2,'training','export-1','2000-01-01T00:00:00+00:00')
            """,
            (source["id"],),
        )
        connection.commit()

    result = catalog.cleanup_expired_annotation_payloads()

    assert [item["annotation_version"] for item in result["removed"]] == [1]
    assert result["blocked"] == [
        {
            "source_id": source["id"],
            "annotation_version": 2,
            "references": {"immutable_outputs": 1},
        }
    ]
    with catalog.connect() as connection:
        removed = connection.execute(
            """
            SELECT retention_state,payload_removed_at FROM annotation_revisions
            WHERE source_id=? AND version=1
            """,
            (source["id"],),
        ).fetchone()
        assert removed["retention_state"] == "payload_removed"
        assert removed["payload_removed_at"]


def test_keep_all_cancels_a_pending_archive_decision(tmp_path) -> None:
    catalog, principal, _, source, annotation = _workspace(tmp_path)
    first, _ = catalog.save_annotation_record(
        source["id"], 0, annotation, principal=principal, origin="human_save"
    )
    second, second_info = catalog.save_annotation_record(
        source["id"],
        first.version,
        first.model_copy(update={"note": "final"}),
        principal=principal,
        save_mode="new_version",
        origin="human_save",
    )
    task = catalog.submit_annotation_approval(
        source["id"],
        annotation_version=second.version,
        content_fingerprint=second_info["content_fingerprint"],
        principal=principal,
    )
    catalog.decide_annotation_approval(
        task["id"], "approved", "Ready", principal=principal
    )

    archive = catalog.record_retention_decision(
        source["id"], "archive_older", None, principal=principal
    )
    assert archive["status"] == "waiting"
    keep = catalog.record_retention_decision(
        source["id"], "keep_all", None, principal=principal
    )

    assert keep["status"] == "complete"
    with catalog.connect() as connection:
        decisions = connection.execute(
            """
            SELECT mode,status FROM corrected_version_retention_decisions
            WHERE source_id=? ORDER BY created_at,id
            """,
            (source["id"],),
        ).fetchall()
        assert [(row["mode"], row["status"]) for row in decisions] == [
            ("archive_older", "cancelled"),
            ("keep_all", "complete"),
        ]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM corrected_versions
            WHERE source_id=? AND retention_state='archive_pending'
            """,
            (source["id"],),
        ).fetchone()[0] == 0
