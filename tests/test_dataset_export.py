"""The administrator's dataset download: audio plus one CSV of every segment."""
from __future__ import annotations

import csv
import io
import json
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
    final_alignment_document,
    source_folders,
    transcript_rows,
)
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
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


def _word(word: str, start: float, end: float, **extra) -> dict:
    """One aligned word. Alignment timings are seconds, not samples."""
    return {"word": word, "start": start, "end": end, "score": 0.9, **extra}


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
                # Left exactly as the model transcribed it.
                _utterance(
                    4.0,
                    6.5,
                    "B",
                    "الحمد لله",
                    model_text="الحمد لله",
                    quality_flags=["repeated_ngram"],
                ),
                _utterance(0.0, 2.5, "A", "أهلا بيك", model_text="اهلا بيك"),
            ],
            activities=[
                ActivityRegion(
                    speaker="A",
                    start_sample=0,
                    end_sample=int(2.5 * SAMPLE_RATE),
                    origin="model",
                ),
                ActivityRegion(
                    speaker="B",
                    start_sample=int(4.0 * SAMPLE_RATE),
                    end_sample=int(6.5 * SAMPLE_RATE),
                    origin="model",
                ),
            ],
            aligned_words=[
                _word("أهلا", 0.5, 1.0),
                _word("بيك", 1.2, 2.0),
                # Between the two segments: no final segment holds it.
                _word("خارج", 3.0, 3.4),
                _word("الحمد", 4.2, 4.8),
                _word("لله", 6.0, 6.4),
                # The aligner could not place this one.
                {"word": "مجهول", "start": None, "end": None, "speaker": None},
            ],
        ),
    )
    return app, service, str(project["id"]), source_id


def _gpu_prepared_source(service, project_id: str, name: str, *, audio: bool = True):
    """A source prepared on the GPU host, as production actually stores one.

    Its canonical audio is committed under worker_artifacts/ and registered in
    the catalog; nothing ever writes it to the workspace's default name.
    """
    original = service.paths.originals / name
    _write_wav(original, 8)
    source = service.catalog.create_source(
        project_id,
        name,
        service.paths.relative(original),
        "audio/wav",
        "c" * 64,
        original.stat().st_size,
    )
    source_id = str(source["id"])
    if audio:
        committed = (
            service.paths.worker_artifacts / f"job_{source_id}" / "attempt_1" / "canonical.wav"
        )
        committed.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(committed, 8)
        service.catalog.register_artifact(
            role="source.canonical",
            relative_path=service.paths.relative(committed),
            sha256="d" * 64,
            size_bytes=committed.stat().st_size,
            media_type="audio/wav",
            source_id=source_id,
        )
    service.catalog.update_source(
        source_id, duration_samples=8 * SAMPLE_RATE, status="ready"
    )
    service.catalog.save_annotation(
        source_id,
        0,
        AnnotationDocument(
            source_id=source_id,
            transcript=[_utterance(0.0, 3.0, "A", "من الجهاز")],
        ),
    )
    return source_id


def _analysis(service, source_id: str, **documents) -> None:
    """Write pipeline artifacts for a source, as a finished GPU run would."""
    for name, document in documents.items():
        path = service.paths.artifact(source_id, f"{name}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _json_member(archive: zipfile.ZipFile, name: str) -> dict:
    return json.loads(archive.read(name).decode("utf-8"))


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


def test_a_source_prepared_on_the_gpu_host_can_be_exported(tmp_path) -> None:
    """The audio of a GPU-prepared source never reaches the default path.

    It is committed under worker_artifacts/ and registered instead. Reading only
    the default name made every such dataset report that nothing was prepared.
    """
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    admin = service.catalog.ensure_local_admin()
    project = service.catalog.create_project("Remote", owner_user_id=str(admin["id"]))
    project_id = str(project["id"])
    source_id = _gpu_prepared_source(service, project_id, "remote-episode.wav")
    # The premise of the fault: nothing is at the name the export used to read.
    assert not service.paths.canonical_audio(source_id).exists()

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        rows = _rows(archive)

    assert "audio/remote-episode.wav" in names
    assert "sources/remote-episode/final_user_edited_transcript.json" in names
    assert [row["text"] for row in rows] == ["من الجهاز"]


def test_a_dataset_whose_audio_is_missing_does_not_claim_nothing_is_prepared(
    tmp_path,
) -> None:
    app = create_studio_app(tmp_path / "workspace", start_worker=False)
    service = app.state.studio
    admin = service.catalog.ensure_local_admin()
    project = service.catalog.create_project("Lost", owner_user_id=str(admin["id"]))
    project_id = str(project["id"])
    _gpu_prepared_source(service, project_id, "gone.wav", audio=False)

    try:
        build_dataset_archive(
            service.catalog, service.paths, project_id, tmp_path / "out.zip"
        )
    except NothingToExportError as exc:
        # The old message sent the search for this fault the wrong way entirely.
        assert "1 prepared source" in str(exc)
        assert "audio could be found" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected NothingToExportError")


def test_a_dataset_exports_every_source_it_has(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    _gpu_prepared_source(service, project_id, "second-episode.wav")

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        rows = _rows(archive)

    # Both audio files, and a JSON folder each, whichever way they were prepared.
    assert {"audio/episode_one.wav", "audio/second-episode.wav"} <= names
    assert "sources/episode_one/final_aligned_transcript.json" in names
    assert "sources/second-episode/final_aligned_transcript.json" in names
    # One running number across the export, restarting the index per source.
    assert [row["sequential_id"] for row in rows] == ["1", "2", "3"]
    assert [row["segment_index"] for row in rows] == ["1", "2", "1"]
    assert [row["audio_file"] for row in rows] == [
        "audio/episode_one.wav",
        "audio/episode_one.wav",
        "audio/second-episode.wav",
    ]
    with zipfile.ZipFile(destination) as archive:
        second = _json_member(
            archive, "sources/second-episode/final_user_edited_transcript.json"
        )
    # Each folder names the audio it belongs to, so nothing has to be guessed.
    assert second["audio_file"] == "audio/second-episode.wav"


def test_one_source_without_audio_does_not_cost_the_others(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    _gpu_prepared_source(service, project_id, "missing-episode.wav", audio=False)

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        audio = [name for name in archive.namelist() if name.startswith("audio/")]
        notes = archive.read("README.txt").decode("utf-8")

    assert audio == ["audio/episode_one.wav"]
    # Named rather than silently dropped.
    assert "missing-episode.wav" in notes


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


def test_archive_carries_a_json_folder_for_every_source(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())

    # The folder is named after the audio file it describes.
    assert "sources/episode_one/final_user_edited_transcript.json" in names
    assert "sources/episode_one/final_aligned_transcript.json" in names
    # This source was never processed, so there is no diarization to copy.
    assert "sources/episode_one/diarization.json" not in names


def test_diarization_is_copied_out_untouched(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    diarization = {"model": "pyannote/x", "segments": [{"start": 0.0, "end": 1.0}]}
    _analysis(service, source_id, diarization=diarization)

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        copied = _json_member(archive, "sources/episode_one/diarization.json")

    # Nothing in the studio edits diarization, so it travels as the model left it.
    assert copied == diarization


def test_final_transcript_holds_the_reviewers_text_and_what_changed(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    latest = service.catalog.latest_annotation(source_id)
    edited = latest.model_copy(deep=True)
    edited.transcript[1].text = "نص بعد التعديل"
    service.catalog.save_annotation(source_id, latest.version, edited)

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_user_edited_transcript.json"
        )

    assert document["annotation_version"] == latest.version + 1
    assert document["audio_file"] == "audio/episode_one.wav"
    utterances = document["utterances"]
    # Chronological, whatever order the segments were stored in.
    assert [item["start_seconds"] for item in utterances] == [0.0, 4.0]
    assert utterances[0]["text"] == "نص بعد التعديل"
    assert utterances[0]["model_text"] == "اهلا بيك"
    assert utterances[0]["text_changed_from_model"] is True
    assert utterances[1]["text_changed_from_model"] is False
    assert utterances[1]["quality_flags"] == ["repeated_ngram"]


def test_final_transcript_carries_the_speaker_lanes(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_user_edited_transcript.json"
        )

    # The lanes as the reviewer left them, alongside the transcript.
    assert [region["speaker"] for region in document["speaker_activity"]] == ["A", "B"]
    assert document["speaker_activity"][1]["start_seconds"] == 4.0


def test_final_alignment_puts_the_words_under_the_final_segments(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_aligned_transcript.json"
        )

    segments = document["segments"]
    assert [segment["speaker"] for segment in segments] == ["A", "B"]
    assert [word["word"] for word in segments[0]["words"]] == ["أهلا", "بيك"]
    assert [word["word"] for word in segments[1]["words"]] == ["الحمد", "لله"]
    # Seconds in, samples out as well, so the words share the transcript's units.
    assert segments[0]["words"][0]["start_sample"] == int(0.5 * SAMPLE_RATE)


def test_every_aligned_word_is_listed_once_with_the_segments_holding_it(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_aligned_transcript.json"
        )

    words = document["word_segments"]
    assert [word["word"] for word in words] == [
        "أهلا",
        "بيك",
        "خارج",
        "الحمد",
        "لله",
        # A word the aligner could not place sorts last rather than vanishing.
        "مجهول",
    ]
    held = {word["word"]: word["segment_ids"] for word in words}
    assert len(held["أهلا"]) == 1
    # No final segment covers these, so nothing claims them.
    assert held["خارج"] == []
    assert held["مجهول"] == []


def test_a_word_spoken_over_belongs_to_both_segments() -> None:
    """Overlapped speech is one segment per speaker, and both really said it."""
    annotation = AnnotationDocument(
        source_id="source_1",
        transcript=[
            _utterance(0.0, 4.0, "A", "الأول"),
            _utterance(3.0, 6.0, "B", "الثاني"),
        ],
        aligned_words=[_word("مشترك", 3.2, 3.6)],
    )
    document = final_alignment_document(
        ExportSource("source_1", "one.wav", Path("a"), annotation),
        audio_file="audio/one.wav",
    )

    assert [len(segment["words"]) for segment in document["segments"]] == [1, 1]
    assert len(document["word_segments"][0]["segment_ids"]) == 2
    assert document["word_segments"][0]["speakers"] == ["A", "B"]


def test_alignment_metadata_comes_from_the_pipeline_artifact(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    _analysis(
        service,
        source_id,
        aligned_transcript={
            "alignment_model": "jonatasgrosman/wav2vec2-large-xlsr-53-arabic",
            "alignment_model_revision": "af46c2d",
            "language": "ar",
            "segments": [],
        },
    )

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_aligned_transcript.json"
        )

    assert document["alignment_model"] == "jonatasgrosman/wav2vec2-large-xlsr-53-arabic"
    assert document["language"] == "ar"


def test_a_damaged_artifact_does_not_cost_the_download(tmp_path) -> None:
    app, service, project_id, source_id = _fixture(tmp_path)
    path = service.paths.artifact(source_id, "aligned_transcript.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    destination = tmp_path / "out.zip"
    build_dataset_archive(service.catalog, service.paths, project_id, destination)
    with zipfile.ZipFile(destination) as archive:
        document = _json_member(
            archive, "sources/episode_one/final_aligned_transcript.json"
        )

    # The alignment we build ourselves survives; only the model's name is lost.
    assert document["alignment_model"] is None
    assert len(document["segments"]) == 2


def test_two_sources_named_alike_get_distinct_folders() -> None:
    sources = [
        ExportSource("source_1", "episode.wav", Path("a"), AnnotationDocument(source_id="s1")),
        ExportSource("source_2", "episode.wav", Path("b"), AnnotationDocument(source_id="s2")),
    ]
    # A folder always matches the audio file it sits beside.
    assert source_folders(sources) == ["sources/episode", "sources/episode_2"]


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


def test_an_export_can_leave_the_audio_out(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path)
    destination = tmp_path / "out.zip"
    build_dataset_archive(
        service.catalog, service.paths, project_id, destination, include_media=False
    )

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        rows = _rows(archive)
        document = _json_member(
            archive, "sources/episode_one/final_user_edited_transcript.json"
        )
        notes = archive.read("README.txt").decode("utf-8")

    assert not [name for name in names if name.startswith("audio/")]
    # Everything else is still here, and still says which audio it describes,
    # so a text-only archive matches up with a later one that has the media.
    assert "transcriptions.csv" in names
    assert "sources/episode_one/final_aligned_transcript.json" in names
    assert {row["audio_file"] for row in rows} == {"audio/episode_one.wav"}
    assert document["audio_file"] == "audio/episode_one.wav"
    assert "without the audio" in notes


def test_export_route_leaves_the_audio_out_when_asked(tmp_path) -> None:
    app, service, project_id, _ = _fixture(tmp_path, require_sign_in=True)
    _make_user(service, "admin@example.test", "admin")
    with TestClient(app) as client:
        assert _sign_in(client, "admin@example.test").status_code == 200
        response = client.get(
            f"/api/admin/projects/{project_id}/export", params={"include_media": "false"}
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert "audio/episode_one.wav" not in names
    assert "transcriptions.csv" in names


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
