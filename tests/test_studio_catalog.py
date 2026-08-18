import pytest

from moshi_data_pipeline.studio.catalog import StudioCatalog, VersionConflictError
from moshi_data_pipeline.studio.domain import ActivityRegion, AnnotationDocument


def test_catalog_versions_annotations_and_recovers_jobs(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    owner = catalog.ensure_local_admin()
    project = catalog.create_project("Cairo conversations", owner_user_id=owner["id"])
    source = catalog.create_source(
        project["id"],
        "episode.wav",
        "originals/episode.wav",
        "audio/wav",
        "a" * 64,
        1234,
    )
    catalog.update_source(source["id"], duration_samples=48_000)
    annotation = AnnotationDocument(
        source_id=source["id"],
        activities=[
            ActivityRegion(speaker="A", start_sample=0, end_sample=24_000),
            ActivityRegion(speaker="B", start_sample=24_000, end_sample=48_000),
        ],
    )
    saved = catalog.save_annotation(source["id"], 0, annotation)
    assert saved.version == 1
    with pytest.raises(VersionConflictError):
        catalog.save_annotation(source["id"], 0, annotation)

    catalog.replace_overlap_recoveries(
        source["id"],
        saved.version,
        [
            {
                "region_id": "overlap_1",
                "start_sample": 1_000,
                "end_sample": 2_000,
                "status": "recovered",
            }
        ],
    )
    with pytest.raises(ValueError, match="requires audition"):
        catalog.decide_overlap(source["id"], "overlap_1", "reject", False)

    job = catalog.create_job(project["id"], "initialize", source["id"], {"mode": "manual"})
    claimed = catalog.claim_job()
    assert claimed and claimed["id"] == job["id"]
    restarted = StudioCatalog(tmp_path / "catalog.sqlite3")
    assert restarted.get_job(job["id"])["status"] == "queued"
    restarted.update_source(source["id"], status="clips_ready")
    assert restarted.list_projects(viewer_id=owner["id"], is_admin=False)[0][
        "ready_sources"
    ] == 1


def test_catalog_repairs_orphaned_processing_source(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    project = catalog.create_project(
        "Repair", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    source = catalog.create_source(
        project["id"],
        "episode.wav",
        "originals/episode.wav",
        "audio/wav",
        "a" * 64,
        1234,
    )
    catalog.update_source(source["id"], status="processing", init_mode="assisted")
    job = catalog.create_job(
        project["id"],
        "initialize",
        source["id"],
        {"mode": "assisted"},
    )
    assert catalog.repair_orphaned_processing_sources() == 0

    catalog.supersede_job(job["id"], "Authoritative inputs changed")

    assert catalog.repair_orphaned_processing_sources() == 1
    repaired = catalog.get_source(source["id"])
    assert repaired["status"] == "uploaded"
    assert repaired["init_mode"] == "assisted"


def test_catalog_fails_unsupported_queued_jobs(tmp_path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    project = catalog.create_project(
        "Unsupported", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    supported = catalog.create_job(project["id"], "initialize", None)
    unsupported = catalog.create_job(project["id"], "realign", None, {"annotation_version": 2})

    assert (
        catalog.fail_unsupported_queued_jobs(
            ["initialize", "transcribe"],
            reason="Not supported by GPU",
        )
        == 1
    )
    assert catalog.get_job(supported["id"])["status"] == "queued"
    failed = catalog.get_job(unsupported["id"])
    assert failed["status"] == "failed"
    assert failed["failure_class"] == "unsupported_job_kind"
