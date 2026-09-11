from pathlib import Path

from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.authorization import RequestPrincipal
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.chapters import generate_chapter_set
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    AnnotationDocument,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.media import atomic_write_json
from moshi_data_pipeline.studio.product_contracts import ReviewChapterConfig
from moshi_data_pipeline.studio.server import create_studio_app


def _annotation(hours: int = 3) -> AnnotationDocument:
    step = 10 * 60 * SAMPLE_RATE
    duration = hours * 60 * 60 * SAMPLE_RATE
    return AnnotationDocument(
        source_id="source_long",
        version=4,
        transcript=[
            TranscriptUtterance(
                id=f"utterance_{index}",
                speaker="A" if index % 2 == 0 else "B",
                start_sample=index * step,
                end_sample=min((index + 1) * step, duration),
                text=f"segment {index}",
            )
            for index in range(duration // step)
        ],
    )


def test_default_chapters_are_at_most_thirty_minutes_and_global() -> None:
    annotation = _annotation()
    duration = 3 * 60 * 60 * SAMPLE_RATE
    result = generate_chapter_set(
        annotation.source_id,
        annotation,
        duration,
        ReviewChapterConfig(),
    )
    assert len(result.chapters) == 6
    assert result.chapters[0].start_sample == 0
    assert result.chapters[-1].end_sample == duration
    assert all(
        chapter.end_sample - chapter.start_sample <= 30 * 60 * SAMPLE_RATE
        for chapter in result.chapters
    )
    assert all(
        left.end_sample == right.start_sample
        for left, right in zip(result.chapters, result.chapters[1:], strict=False)
    )


def test_count_mode_produces_exact_count_without_cutting_segments() -> None:
    annotation = _annotation()
    duration = 3 * 60 * 60 * SAMPLE_RATE
    result = generate_chapter_set(
        annotation.source_id,
        annotation,
        duration,
        ReviewChapterConfig(mode="count", count=5),
    )
    assert len(result.chapters) == 5
    transcript = annotation.transcript
    for chapter in result.chapters[:-1]:
        boundary = chapter.end_sample
        assert not any(item.start_sample < boundary < item.end_sample for item in transcript)


def test_empty_long_source_uses_safe_regular_boundaries() -> None:
    duration = 2 * 60 * 60 * SAMPLE_RATE
    annotation = AnnotationDocument(source_id="source_empty", version=1)
    result = generate_chapter_set(
        annotation.source_id,
        annotation,
        duration,
        ReviewChapterConfig(),
        silences=[(0, duration)],
    )
    assert len(result.chapters) == 4
    assert all(chapter.boundary_reason == "silence" for chapter in result.chapters[:-1])


def test_silence_is_preferred_and_boundary_reason_is_persistable() -> None:
    annotation = AnnotationDocument(
        source_id="source_pause",
        version=2,
        transcript=[
            TranscriptUtterance(
                speaker="A", start_sample=0, end_sample=29 * 60 * SAMPLE_RATE
            ),
            TranscriptUtterance(
                speaker="B",
                start_sample=31 * 60 * SAMPLE_RATE,
                end_sample=60 * 60 * SAMPLE_RATE,
            ),
        ],
    )
    result = generate_chapter_set(
        annotation.source_id,
        annotation,
        60 * 60 * SAMPLE_RATE,
        ReviewChapterConfig(),
        silences=[(29 * 60 * SAMPLE_RATE, 31 * 60 * SAMPLE_RATE)],
    )
    assert result.chapters[0].end_sample == 30 * 60 * SAMPLE_RATE
    assert result.chapters[0].boundary_reason == "silence"


def test_catalog_replaces_active_set_atomically(tmp_path: Path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    owner = catalog.ensure_local_admin()["id"]
    project = catalog.create_project("Chapters", owner_user_id=owner)
    original = tmp_path / "source.wav"
    original.write_bytes(b"audio")
    source = catalog.create_source(
        project["id"], "source.wav", str(original), "audio/wav", "a" * 64, 5
    )
    duration = 3 * 60 * 60 * SAMPLE_RATE
    catalog.update_source(source["id"], duration_samples=duration, status="ready")
    annotation = _annotation().model_copy(update={"source_id": source["id"]})
    first = generate_chapter_set(source["id"], annotation, duration, ReviewChapterConfig())
    catalog.replace_chapter_set(first)
    second = generate_chapter_set(
        source["id"],
        annotation,
        duration,
        ReviewChapterConfig(mode="count", count=4),
    )
    catalog.replace_chapter_set(second)
    active = catalog.active_chapter_set(source["id"])
    assert active is not None
    assert active.id == second.id
    assert len(active.chapters) == 4
    with catalog.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_chapter_sets WHERE source_id=?",
            (source["id"],),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM review_chapter_sets WHERE source_id=? AND active=1",
            (source["id"],),
        ).fetchone()[0] == 1


def test_chapter_completion_is_reviewer_scoped_and_becomes_stale(tmp_path: Path) -> None:
    catalog = StudioCatalog(tmp_path / "catalog.sqlite3")
    owner = catalog.ensure_local_admin()
    principal = RequestPrincipal.from_catalog_user(owner)
    project = catalog.create_project("P4 reviews", owner_user_id=owner["id"])
    original = tmp_path / "source.wav"
    original.write_bytes(b"audio")
    source = catalog.create_source(
        project["id"], "source.wav", str(original), "audio/wav", "c" * 64, 5
    )
    duration = 20 * 60 * SAMPLE_RATE
    catalog.update_source(source["id"], duration_samples=duration, status="ready")
    first_annotation = AnnotationDocument(source_id=source["id"], version=0)
    first = generate_chapter_set(
        source["id"], first_annotation, duration, ReviewChapterConfig()
    )
    catalog.replace_chapter_set(first, principal=principal)
    catalog.save_chapter_review(
        first.chapters[0].id, 0, "complete", principal=principal
    )

    current = catalog.chapter_reviews(source["id"], principal=principal)
    assert current[0]["status"] == "complete"
    assert current[0]["stale"] is False

    second = generate_chapter_set(
        source["id"],
        first_annotation.model_copy(update={"version": 1}),
        duration,
        ReviewChapterConfig(),
    )
    catalog.replace_chapter_set(second, principal=principal)
    stale = catalog.chapter_reviews(source["id"], principal=principal)
    assert stale[0]["chapter_id"] == second.chapters[0].id
    assert stale[0]["annotation_version"] == 0
    assert stale[0]["status"] == "complete"
    assert stale[0]["stale"] is True


def test_chapter_configuration_api_and_peak_windows(tmp_path: Path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    owner = service.catalog.ensure_local_admin()["id"]
    project = service.catalog.create_project("P3 API", owner_user_id=owner)
    original = service.paths.originals / "chapter-api.wav"
    original.write_bytes(b"audio")
    source = service.catalog.create_source(
        project["id"],
        original.name,
        service.paths.relative(original),
        "audio/wav",
        "b" * 64,
        5,
    )
    duration = 2 * 60 * 60 * SAMPLE_RATE
    service.catalog.update_source(source["id"], duration_samples=duration, status="ready")
    peaks_path = service.paths.peaks(source["id"])
    atomic_write_json(
        peaks_path,
        {
            "sample_rate": SAMPLE_RATE,
            "duration_samples": duration,
            "points": [[-index / 100, index / 100] for index in range(1, 101)],
        },
    )

    with TestClient(app) as client:
        default = client.get(f"/api/sources/{source['id']}/chapters")
        assert default.status_code == 200
        assert len(default.json()["chapters"]) == 4
        configured = client.put(
            f"/api/sources/{source['id']}/chapters",
            json={
                "contract_version": "studio.review/v1",
                "mode": "count",
                "max_duration_seconds": 1_800,
                "count": 7,
                "manual_boundaries_samples": [],
                "boundary_search_seconds": 30,
            },
        )
        assert configured.status_code == 200
        assert len(configured.json()["chapters"]) == 7
        chapter_id = configured.json()["chapters"][0]["id"]
        review = client.put(
            f"/api/review-chapters/{chapter_id}/review",
            json={"annotation_version": 0, "status": "complete"},
        )
        assert review.status_code == 200
        assert review.json()["status"] == "complete"
        window = client.get(
            f"/api/sources/{source['id']}/waveform-peaks",
            params={"start_sample": duration // 4, "end_sample": duration // 2, "max_points": 5},
        )
        assert window.status_code == 200
        assert len(window.json()["points"]) <= 5
        assert window.json()["start_sample"] == duration // 4
