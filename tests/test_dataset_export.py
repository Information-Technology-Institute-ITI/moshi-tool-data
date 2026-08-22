"""The administrator's dataset download: audio plus one CSV of every segment."""
from __future__ import annotations

import csv
import io
import wave
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from moshi_data_pipeline.studio.auth import AuthSettings, hash_password
from moshi_data_pipeline.studio.dataset_export import (
    CSV_COLUMNS,
    ExportSource,
    NothingToExportError,
    archive_audio_names,
    build_dataset_archive,
    transcript_rows,
)
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    AnnotationDocument,
    TranscriptUtterance,
)
from moshi_data_pipeline.studio.server import create_studio_app

PASSWORD = "TestPassword123!"
ORIGIN = {"Origin": "http://testserver"}


def _write_wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * int(seconds * SAMPLE_RATE))


def _utterance(start: float, end: float, speaker: str, text: str, **extra):
    return TranscriptUtterance(
        speaker=speaker,
        start_sample=int(start * SAMPLE_RATE),
        end_sample=int(end * SAMPLE_RATE),
        text=text,
        **extra,
    )


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        public_origin="http://testserver",
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="",
        require_sign_in=True,
    )


def _fixture(tmp_path: Path, *, require_sign_in: bool = False):
    """An app with one admin, one dataset, and one prepared source."""
    app = create_studio_app(
        tmp_path / "workspace",
        start_worker=False,
        start_lifecycle=False,
        start_dispatcher=False,
        auth_settings=_auth_settings() if require_sign_in else None,
    )
    service = app.state.studio
    admin = service.catalog.ensure_local_admin()
    project = service.catalog.create_project(
        "Cairo conversations", owner_user_id=str(admin["id"])
    )
    original = service.paths.originals / "episode one.wav"
    _write_wav(original, 10)
    source = service.catalog.create_source(
        str(project["id"]),
        "episode one.wav",
        service.paths.relative(original),
        "audio/wav",
        "a" * 64,
        original.stat().st_size,
    )
    source_id = str(source["id"])
    _write_wav(service.paths.canonical_audio(source_id), 10)
    service.catalog.update_source(
        source_id, duration_samples=10 * SAMPLE_RATE, status="ready"
    )
    service.catalog.save_annotation(
        source_id,
        0,
        AnnotationDocument(
            source_id=source_id,
            transcript=[
                _utterance(4.0, 6.5, "B", "الحمد لله", quality_flags=["repeated_ngram"]),
                _utterance(0.0, 2.5, "A", "أهلا بيك", model_text="اهلا بيك"),
            ],
        ),
    )
    return app, service, str(project["id"]), source_id


def _rows(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    text = archive.read("transcriptions.csv").decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_archive_holds_the_audio_and_one_row_per_segment(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert "transcriptions.csv" in names
        assert "README.txt" in names
        assert "audio/episode_one.wav" in names
        rows = _rows(archive)

    assert [row["text"] for row in rows] == ["أهلا بيك", "الحمد لله"]
    assert list(rows[0]) == CSV_COLUMNS


def test_rows_are_ordered_and_numbered_for_splitting(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        rows = _rows(archive)

    # Chronological regardless of the order the segments were stored in.
    assert [int(row["start_ms"]) for row in rows] == [0, 4000]
    assert [row["sequential_id"] for row in rows] == ["1", "2"]
    assert [row["segment_index"] for row in rows] == ["1", "2"]


def test_every_row_states_the_same_range_three_ways(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        first = _rows(archive)[0]

    assert first["start_sample"] == "0"
    assert first["end_sample"] == str(int(2.5 * SAMPLE_RATE))
    assert first["start_ms"] == "0"
    assert first["end_ms"] == "2500"
    assert first["duration_ms"] == "2500"
    assert first["start_seconds"] == "0.000"
    assert first["end_seconds"] == "2.500"


def test_row_carries_the_rest_of_the_segment_data(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        rows = _rows(archive)

    assert rows[0]["speaker"] == "A"
    assert rows[0]["model_text"] == "اهلا بيك"
    assert rows[0]["source_id"] == source_id
    assert rows[0]["segment_id"].startswith("utterance_")
    assert rows[0]["human_verified"] == "false"
    assert rows[1]["quality_flags"] == "repeated_ngram"
    assert rows[1]["audio_file"] == "audio/episode_one.wav"


def test_csv_text_is_the_reviewers_latest_revision(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    latest = service.catalog.latest_annotation(source_id)
    edited = latest.model_copy(deep=True)
    edited.transcript[0].text = "نص بعد التعديل"
    service.catalog.save_annotation(source_id, latest.version, edited)

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        rows = _rows(archive)

    # The export must never fall back to the model's original wording.
    assert "نص بعد التعديل" in [row["text"] for row in rows]


def test_cutting_by_the_csv_produces_the_promised_audio(tmp_path) -> None:
    """The whole point: the CSV alone is enough to split the audio correctly."""
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(destination) as archive:
        archive.extractall(extracted)
    rows = list(
        csv.DictReader((extracted / "transcriptions.csv").open(encoding="utf-8-sig"))
    )

    for row in rows:
        with wave.open(str(extracted / row["audio_file"]), "rb") as handle:
            assert handle.getframerate() == SAMPLE_RATE
            handle.setpos(int(row["start_sample"]))
            frames = handle.readframes(
                int(row["end_sample"]) - int(row["start_sample"])
            )
        cut_ms = round(len(frames) / 2 / SAMPLE_RATE * 1000)
        assert cut_ms == int(row["duration_ms"])


def test_two_sources_named_alike_get_distinct_audio_files(tmp_path) -> None:
    sources = [
        ExportSource("source_1", "episode.wav", Path("a"), AnnotationDocument(source_id="s1")),
        ExportSource("source_2", "episode.wav", Path("b"), AnnotationDocument(source_id="s2")),
    ]
    assert archive_audio_names(sources) == ["audio/episode.wav", "audio/episode_2.wav"]


def test_rows_span_every_source_with_one_running_number(tmp_path) -> None:
    sources = [
        ExportSource(
            "source_1",
            "one.wav",
            Path("a"),
            AnnotationDocument(
                source_id="source_1",
                transcript=[_utterance(0, 1, "A", "first"), _utterance(1, 2, "B", "second")],
            ),
        ),
        ExportSource(
            "source_2",
            "two.wav",
            Path("b"),
            AnnotationDocument(
                source_id="source_2", transcript=[_utterance(0, 1, "A", "third")]
            ),
        ),
    ]
    rows = transcript_rows(sources)
    assert [row["sequential_id"] for row in rows] == [1, 2, 3]
    # Numbering restarts per audio file so a row can be found inside its own source.
    assert [row["segment_index"] for row in rows] == [1, 2, 1]
    assert [row["audio_file"] for row in rows] == [
        "audio/one.wav",
        "audio/one.wav",
        "audio/two.wav",
    ]


def test_unprepared_sources_are_left_out(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    original = service.paths.originals / "not-started.wav"
    _write_wav(original, 3)
    service.catalog.create_source(
        project_id,
        "not-started.wav",
        service.paths.relative(original),
        "audio/wav",
        "b" * 64,
        original.stat().st_size,
    )
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        audio = [name for name in archive.namelist() if name.startswith("audio/")]
    assert audio == ["audio/episode_one.wav"]


def test_a_dataset_with_nothing_prepared_says_so(tmp_path) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    admin = service.catalog.ensure_local_admin()
    project = service.catalog.create_project("Empty", owner_user_id=str(admin["id"]))
    try:
        build_dataset_archive(
            service.catalog, service.paths, str(project["id"]), tmp_path / "out.zip"
        )
    except NothingToExportError as exc:
        assert "no prepared source" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected NothingToExportError")


def _make_user(service, email: str, role: str) -> dict:
    user, _ = service.catalog.create_or_refresh_pending_user(
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    with service.catalog.connect() as connection:
        connection.execute(
            "UPDATE users SET status='active',email_verified_at=?,role=? WHERE id=?",
            (service.catalog._now(), role, user["id"]),
        )
    return service.catalog.get_user(str(user["id"]))


def _sign_in(client: TestClient, email: str):
    return client.post(
        "/api/auth/signin", headers=ORIGIN, json={"email": email, "password": PASSWORD}
    )


def test_export_route_serves_a_zip_to_an_administrator(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path, require_sign_in=True)
    _make_user(service, "admin@example.test", "admin")
    with TestClient(app) as client:
        assert _sign_in(client, "admin@example.test").status_code == 200
        response = client.get(f"/api/admin/projects/{project_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "Cairo_conversations.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "transcriptions.csv" in archive.namelist()
        assert "audio/episode_one.wav" in archive.namelist()


def test_export_route_refuses_a_regular_user(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path, require_sign_in=True)
    _make_user(service, "editor@example.test", "user")
    with TestClient(app) as client:
        assert _sign_in(client, "editor@example.test").status_code == 200
        response = client.get(f"/api/admin/projects/{project_id}/export")
    # The dataset download is administrators only.
    assert response.status_code == 403


def test_export_route_refuses_a_signed_out_visitor(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path, require_sign_in=True)
    with TestClient(app) as client:
        response = client.get(f"/api/admin/projects/{project_id}/export")
    assert response.status_code == 401


def test_export_route_reports_a_dataset_with_nothing_prepared(tmp_path) -> None:
    app, service, _, _ = _fixture(tmp_path, require_sign_in=True)
    admin = _make_user(service, "admin@example.test", "admin")
    empty = service.catalog.create_project("Empty", owner_user_id=str(admin["id"]))
    with TestClient(app) as client:
        assert _sign_in(client, "admin@example.test").status_code == 200
        response = client.get(f"/api/admin/projects/{empty['id']}/export")
    assert response.status_code == 409
    assert "no prepared source" in response.json()["detail"]


def test_export_leaves_no_archive_behind_on_the_server(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path, require_sign_in=True)
    _make_user(service, "admin@example.test", "admin")
    with TestClient(app) as client:
        assert _sign_in(client, "admin@example.test").status_code == 200
        assert client.get(f"/api/admin/projects/{project_id}/export").status_code == 200
    # The download is built on demand; nothing accumulates in the workspace.
    assert list(service.paths.exports.glob("*.zip")) == []
