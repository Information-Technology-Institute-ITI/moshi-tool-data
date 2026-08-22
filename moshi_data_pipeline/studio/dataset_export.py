"""Builds an administrator's download of a finished dataset.

The archive holds the audio the annotation timestamps actually refer to, one CSV
describing every transcript segment, and a folder per source carrying the same
material as JSON. The CSV is meant to be fed straight to a splitting script:
each row carries the audio file it belongs to and the exact range to cut, in
samples, milliseconds and seconds.

Two of the JSON documents are built here from the newest saved annotation, so
they describe what the reviewer finished with rather than what the model first
produced: `final_user_edited_transcript.json` is the transcript itself, and
`final_aligned_transcript.json` puts the word timings back under the reviewer's
final segments and speakers. The third, `diarization.json`, is the pipeline's
own artifact copied out untouched, since nothing in the studio edits it.

Nothing here starts processing or touches the GPU.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.domain import SAMPLE_RATE, AnnotationDocument
from moshi_data_pipeline.studio.media import safe_filename

READY_STATUSES = {"ready", "clips_ready"}

CANONICAL_AUDIO_ROLE = "source.canonical"

# Analysis artifacts worth carrying into the archive, and the name each one is
# written under in the workspace when it was never re-registered elsewhere.
ANALYSIS_ARTIFACTS = {
    "analysis.aligned_transcript": "aligned_transcript.json",
    "analysis.diarization": "diarization.json",
}

CSV_COLUMNS = [
    "sequential_id",
    "audio_file",
    "segment_index",
    "start_ms",
    "end_ms",
    "duration_ms",
    "start_seconds",
    "end_seconds",
    "start_sample",
    "end_sample",
    "speaker",
    "text",
    "model_text",
    "model_speaker",
    "quality_flags",
    "alignment_status",
    "human_verified",
    "source_id",
    "segment_id",
]


class NothingToExportError(RuntimeError):
    """Raised when a dataset has no finished source to export."""


@dataclass(frozen=True)
class ExportSource:
    """One finished source: its audio on disk and the annotation to describe it.

    `analysis` maps an artifact role to the file the pipeline left behind, and
    only holds roles whose file is actually present. A source prepared before
    those artifacts were kept, or one seeded by hand, simply has fewer of them.
    """

    source_id: str
    original_name: str
    audio_path: Path
    annotation: AnnotationDocument
    analysis: dict[str, Path] = field(default_factory=dict)


def archive_audio_names(sources: list[ExportSource]) -> list[str]:
    """Where each source's audio lands in the archive, one name per source.

    Computed once and shared, so the audio_file column and the file actually
    written can never disagree. Two sources uploaded under the same name get
    distinct names here.
    """
    names: list[str] = []
    taken: set[str] = set()
    for source in sources:
        stem = Path(safe_filename(source.original_name) or "audio").stem or "audio"
        candidate = f"{stem}.wav"
        counter = 2
        while candidate.lower() in taken:
            candidate = f"{stem}_{counter}.wav"
            counter += 1
        taken.add(candidate.lower())
        names.append(f"audio/{candidate}")
    return names


def _milliseconds(sample: int) -> int:
    return round(sample * 1000 / SAMPLE_RATE)


def transcript_rows(sources: list[ExportSource]) -> list[dict[str, Any]]:
    """One row per transcript segment, ordered so splits come out in sequence.

    Rows are grouped by audio file and ordered by start time within it, and
    `sequential_id` counts across the whole export. A split named after it sorts
    the same way the conversation runs.
    """
    rows: list[dict[str, Any]] = []
    for source, audio_file in zip(sources, archive_audio_names(sources), strict=True):
        segments = sorted(
            source.annotation.transcript,
            key=lambda item: (item.start_sample, item.end_sample),
        )
        for index, segment in enumerate(segments, start=1):
            rows.append(
                {
                    "sequential_id": len(rows) + 1,
                    "audio_file": audio_file,
                    "segment_index": index,
                    "start_ms": _milliseconds(segment.start_sample),
                    "end_ms": _milliseconds(segment.end_sample),
                    "duration_ms": _milliseconds(segment.end_sample - segment.start_sample),
                    "start_seconds": f"{segment.start_sample / SAMPLE_RATE:.3f}",
                    "end_seconds": f"{segment.end_sample / SAMPLE_RATE:.3f}",
                    "start_sample": segment.start_sample,
                    "end_sample": segment.end_sample,
                    "speaker": segment.speaker or "",
                    "text": segment.text,
                    "model_text": segment.model_text,
                    "model_speaker": segment.model_speaker or "",
                    # Pipe-separated so the column survives a spreadsheet import.
                    "quality_flags": "|".join(segment.quality_flags),
                    "alignment_status": segment.alignment_status,
                    "human_verified": "true" if segment.human_verified else "false",
                    "source_id": source.source_id,
                    "segment_id": segment.id,
                }
            )
    return rows


def source_folders(sources: list[ExportSource]) -> list[str]:
    """The folder each source's JSON documents land in, one per source.

    Derived from the audio names so a source's folder is always named after the
    audio file it describes, and two sources can never share one.
    """
    return [f"sources/{Path(name).stem}" for name in archive_audio_names(sources)]


def _seconds(sample: int) -> float:
    return round(sample / SAMPLE_RATE, 3)


def _word_sample(value: Any) -> int | None:
    """An aligned word's boundary in samples.

    Word alignments carry seconds and both ends are nullable, while everything
    else in an annotation is samples at 24 kHz. Words the aligner could not
    place are left out rather than guessed at.
    """
    if value is None:
        return None
    try:
        return round(float(value) * SAMPLE_RATE)
    except (TypeError, ValueError):
        return None


def _overlapping_indices(
    aligned_words: list[dict[str, Any]],
    start_sample: int,
    end_sample: int,
) -> list[int]:
    chosen: list[tuple[int, int]] = []
    for index, word in enumerate(aligned_words):
        start = _word_sample(word.get("start"))
        end = _word_sample(word.get("end"))
        if start is None or end is None:
            continue
        if start < end_sample and end > start_sample:
            chosen.append((start, index))
    chosen.sort()
    return [index for _, index in chosen]


def words_in_range(
    aligned_words: list[dict[str, Any]],
    start_sample: int,
    end_sample: int,
) -> list[dict[str, Any]]:
    """The aligned words a range covers, in spoken order.

    Matched by overlap, the same rule the review screen uses, so a word belongs
    to the segment it actually falls inside. Where two people talk at once a
    word is claimed by both segments, which is what happened.
    """
    return [aligned_words[index] for index in _overlapping_indices(
        aligned_words, start_sample, end_sample
    )]


def _exported_word(word: dict[str, Any]) -> dict[str, Any]:
    start = _word_sample(word.get("start"))
    end = _word_sample(word.get("end"))
    return {
        "word": word.get("word", ""),
        "start_seconds": word.get("start"),
        "end_seconds": word.get("end"),
        "start_sample": start,
        "end_sample": end,
        "score": word.get("score"),
        "model_speaker": word.get("speaker"),
    }


def _text_changed(segment: Any) -> bool:
    return segment.text.strip() != segment.model_text.strip()


def final_transcript_document(
    source: ExportSource,
    *,
    project_name: str,
    owner_email: str | None,
    audio_file: str,
) -> dict[str, Any]:
    """The reviewer's finished transcript, in chronological order.

    `text` is what to train on. `model_text` and `model_speaker` travel with it
    only so a reader can see what the reviewer changed, which the two
    `*_changed_from_model` flags state outright.
    """
    annotation = source.annotation
    segments = sorted(
        annotation.transcript, key=lambda item: (item.start_sample, item.end_sample)
    )
    return {
        "format": "studio.final-user-edited-transcript/v1",
        "project_name": project_name,
        "owner_email": owner_email,
        "source_id": source.source_id,
        "original_name": source.original_name,
        "audio_file": audio_file,
        "annotation_version": annotation.version,
        "sample_rate_hz": SAMPLE_RATE,
        "assistant_speaker": annotation.assistant_speaker,
        "note": annotation.note,
        "utterances": [
            {
                "utterance_id": segment.id,
                "speaker": segment.speaker,
                "start_sample": segment.start_sample,
                "end_sample": segment.end_sample,
                "start_seconds": _seconds(segment.start_sample),
                "end_seconds": _seconds(segment.end_sample),
                "text": segment.text,
                "model_text": segment.model_text,
                "model_speaker": segment.model_speaker,
                "text_changed_from_model": _text_changed(segment),
                "speaker_changed_from_model": segment.speaker != segment.model_speaker,
                "alignment_status": segment.alignment_status,
                "human_verified": segment.human_verified,
                "quality_flags": list(segment.quality_flags),
            }
            for segment in segments
        ],
        # The speaker lanes as the reviewer left them. diarization.json holds
        # the model's original view of the same thing.
        "speaker_activity": [
            {
                "id": region.id,
                "speaker": region.speaker,
                "start_sample": region.start_sample,
                "end_sample": region.end_sample,
                "start_seconds": _seconds(region.start_sample),
                "end_seconds": _seconds(region.end_sample),
                "origin": region.origin,
                "confidence": region.confidence,
            }
            for region in sorted(
                annotation.activities,
                key=lambda item: (item.start_sample, item.end_sample),
            )
        ],
        "exclusions": [
            {
                "id": region.id,
                "kind": region.kind,
                "start_sample": region.start_sample,
                "end_sample": region.end_sample,
                "note": region.note,
            }
            for region in sorted(
                annotation.exclusions,
                key=lambda item: (item.start_sample, item.end_sample),
            )
        ],
    }


def final_alignment_document(
    source: ExportSource,
    *,
    audio_file: str,
    alignment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Word timings placed under the reviewer's final segments.

    The timings themselves are still the aligner's — nothing re-runs alignment
    when a reviewer retypes a line — but which segment and which speaker a word
    belongs to follows the finished transcript, not the model's first pass.
    Where the reviewer rewrote a line the words no longer spell it, and
    `text_changed_from_model` on that segment says so.
    """
    annotation = source.annotation
    metadata = alignment_metadata or {}
    segments = sorted(
        annotation.transcript, key=lambda item: (item.start_sample, item.end_sample)
    )
    exported_segments = []
    holders: dict[int, list[tuple[str, str | None]]] = {}
    for segment in segments:
        indices = _overlapping_indices(
            annotation.aligned_words, segment.start_sample, segment.end_sample
        )
        words = [annotation.aligned_words[index] for index in indices]
        for index in indices:
            holders.setdefault(index, []).append((segment.id, segment.speaker))
        exported_segments.append(
            {
                "segment_id": segment.id,
                "speaker": segment.speaker,
                "start_sample": segment.start_sample,
                "end_sample": segment.end_sample,
                "start_seconds": _seconds(segment.start_sample),
                "end_seconds": _seconds(segment.end_sample),
                "text": segment.text,
                "text_changed_from_model": _text_changed(segment),
                "alignment_status": segment.alignment_status,
                "words": [_exported_word(word) for word in words],
            }
        )

    # Every aligned word once, in time order, saying which final segments now
    # hold it and who they belong to. An empty list means the reviewer deleted
    # the segment that word was in; two entries mean it was spoken over.
    ordered = sorted(
        enumerate(annotation.aligned_words),
        key=lambda item: (
            _word_sample(item[1].get("start")) is None,
            _word_sample(item[1].get("start")) or 0,
            item[0],
        ),
    )
    word_segments = [
        {
            **_exported_word(word),
            "segment_ids": [segment_id for segment_id, _ in holders.get(index, [])],
            "speakers": [speaker for _, speaker in holders.get(index, [])],
        }
        for index, word in ordered
    ]

    return {
        "format": "studio.final-aligned-transcript/v1",
        "source_id": source.source_id,
        "original_name": source.original_name,
        "audio_file": audio_file,
        "annotation_version": annotation.version,
        "sample_rate_hz": SAMPLE_RATE,
        "alignment_model": metadata.get("alignment_model"),
        "alignment_model_revision": metadata.get("alignment_model_revision"),
        "language": metadata.get("language"),
        "segments": exported_segments,
        "word_segments": word_segments,
    }


def write_json(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def read_json_file(path: Path) -> dict[str, Any] | None:
    """A JSON artifact, or None when it is missing or unreadable.

    A damaged artifact must not cost an administrator the whole download, so it
    is dropped from the archive and the README says which sources have it.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def write_csv(rows: list[dict[str, Any]]) -> bytes:
    """UTF-8 with a BOM, so Excel opens the Arabic text correctly."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def readme(
    project_name: str,
    sources: list[ExportSource],
    rows: list[dict[str, Any]],
    without_audio: list[str] | None = None,
) -> bytes:
    lines = [
        f"Dataset: {project_name}",
        "",
        f"Audio files : {len(sources)}",
        f"Segments    : {len(rows)}",
        f"Sample rate : {SAMPLE_RATE} Hz, mono",
        "",
        "transcriptions.csv describes every segment. The text column is the",
        "reviewer's final wording, taken from the newest saved revision.",
        "",
        "The audio here is the canonical 24 kHz mono conversion the timestamps",
        "were made against, not the uploaded original. Cut from these files or",
        "the ranges will not line up.",
        "",
        "Each row carries the same range three ways: samples at 24 kHz for exact",
        "work, and milliseconds and seconds for tools that want them. Rows are",
        "ordered by audio file and then by start time, and sequential_id counts",
        "across the whole export, so splits named after it stay in order.",
        "",
        "Every source also has a folder under sources/, named after its audio",
        "file, holding the same material as JSON:",
        "",
        "  final_user_edited_transcript.json",
        "      The finished transcript in chronological order. Train on each",
        "      utterance's text; model_text and model_speaker are there only to",
        "      show what the reviewer changed. The speaker lanes as the reviewer",
        "      left them are under speaker_activity.",
        "",
        "  final_aligned_transcript.json",
        "      The same segments with the word timings underneath them. The",
        "      timings are the aligner's - nothing re-runs alignment when a line",
        "      is retyped - but which segment and speaker a word belongs to",
        "      follows the finished transcript. Where a segment has",
        "      text_changed_from_model set, its words no longer spell its text.",
        "      word_segments lists every aligned word once, with the segments",
        "      that now hold it and who is speaking there. An empty segment_ids",
        "      means the reviewer deleted the segment that word was in; two",
        "      entries mean it was spoken over, and both speakers said it.",
        "",
        "  diarization.json",
        "      The pipeline's own speaker diarization, copied out untouched.",
        "      Nothing in the studio edits it, so it is the model's view, not",
        "      the reviewer's. Present only for sources processed with it.",
        "",
    ]
    if without_audio:
        lines += [
            "Left out of this archive, because no audio for them could be found",
            "in the workspace:",
            "",
            *(f"  {name}" for name in without_audio),
            "",
        ]
    lines += [
        "Splitting every segment with ffmpeg:",
        "",
        "    tail -n +2 transcriptions.csv | while IFS=, read -r id file rest; do",
        '      : # see start_seconds and end_seconds in the row',
        "    done",
        "",
        "or in Python:",
        "",
        "    import csv, subprocess",
        '    with open("transcriptions.csv", encoding="utf-8-sig") as handle:',
        "        for row in csv.DictReader(handle):",
        "            subprocess.run([",
        '                "ffmpeg", "-i", row["audio_file"],',
        '                "-ss", row["start_seconds"], "-to", row["end_seconds"],',
        '                "-c", "copy", f"split_{row[\'sequential_id\']}.wav",',
        "            ], check=True)",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def registered_artifacts(catalog: Any, paths: Any, source_id: str) -> dict[str, Path]:
    """Every active artifact recorded for a source, by role.

    A source prepared on the GPU host has its files committed under
    worker_artifacts/ and registered here; nothing copies them to the
    workspace's default names. The registered record is therefore the only
    place some sources' audio can be found, and it is what the rest of the
    service reads.
    """
    found: dict[str, Path] = {}
    for item in catalog.list_artifacts(source_id=source_id):
        found[str(item["role"])] = paths.resolve_relative(str(item["relative_path"]))
    return found


def _first_existing(*candidates: Path | None) -> Path | None:
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def source_audio_path(catalog: Any, paths: Any, source_id: str) -> Path | None:
    """A source's canonical audio, wherever it actually lives.

    Reading only the workspace's default name made every GPU-prepared dataset
    look like it had nothing to export, since that file only exists for sources
    the local worker handled. The original upload is never a fallback: the
    timestamps were made against the canonical conversion, so cutting the
    upload would not line up.
    """
    return _first_existing(
        registered_artifacts(catalog, paths, source_id).get(CANONICAL_AUDIO_ROLE),
        paths.canonical_audio(source_id),
    )


def analysis_artifacts(catalog: Any, paths: Any, source_id: str) -> dict[str, Path]:
    """Where a source's analysis artifacts are, skipping any that are missing.

    A registered artifact wins over the default workspace name. Anything not on
    disk is left out rather than reported as an empty file.
    """
    registered = registered_artifacts(catalog, paths, source_id)
    found: dict[str, Path] = {}
    for role, name in ANALYSIS_ARTIFACTS.items():
        candidate = _first_existing(registered.get(role), paths.artifact(source_id, name))
        if candidate is not None:
            found[role] = candidate
    return found


@dataclass(frozen=True)
class ExportPlan:
    """What a dataset can produce: the sources to write, and what was left out.

    `without_audio` names finished sources whose audio could not be found at
    all. They are reported rather than silently dropped, so an administrator
    can see the difference between a dataset that exported everything and one
    that quietly lost a file.
    """

    sources: list[ExportSource]
    without_audio: list[str]


def plan_export(catalog: Any, paths: Any, project_id: str) -> ExportPlan:
    """The finished sources of a dataset, and which of them have no audio."""
    collected: list[ExportSource] = []
    without_audio: list[str] = []
    for source in catalog.list_sources(project_id):
        if str(source["status"]) not in READY_STATUSES:
            continue
        source_id = str(source["id"])
        audio = source_audio_path(catalog, paths, source_id)
        if audio is None:
            without_audio.append(str(source["original_name"]))
            continue
        collected.append(
            ExportSource(
                source_id=source_id,
                original_name=str(source["original_name"]),
                audio_path=audio,
                annotation=catalog.latest_annotation(source_id),
                analysis=analysis_artifacts(catalog, paths, source_id),
            )
        )
    return ExportPlan(collected, without_audio)


def build_dataset_archive(catalog: Any, paths: Any, project_id: str, destination: Path) -> Path:
    """Writes the dataset's audio, transcript CSV and per-source JSON to a zip.

    Raises NothingToExportError when no source in the dataset is finished, so
    the caller can say why instead of handing over an empty archive.
    """
    project = catalog.get_project(project_id)
    owner = project.get("owner") or {}
    project_name = str(project["name"])
    plan = plan_export(catalog, paths, project_id)
    sources = plan.sources
    if not sources:
        if plan.without_audio:
            # Saying "nothing is prepared" here would be a lie, and it sent the
            # last search for this fault in entirely the wrong direction.
            raise NothingToExportError(
                f"This dataset has {len(plan.without_audio)} prepared "
                f"{'source' if len(plan.without_audio) == 1 else 'sources'}, but none "
                "of their audio could be found in the workspace."
            )
        raise NothingToExportError(
            "This dataset has no prepared source to export yet."
        )

    rows = transcript_rows(sources)
    audio_names = archive_audio_names(sources)
    folders = source_folders(sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("transcriptions.csv", write_csv(rows))
        archive.writestr(
            "README.txt", readme(project_name, sources, rows, plan.without_audio)
        )
        for source, audio_file, folder in zip(sources, audio_names, folders, strict=True):
            archive.write(source.audio_path, audio_file)
            archive.writestr(
                f"{folder}/final_user_edited_transcript.json",
                write_json(
                    final_transcript_document(
                        source,
                        project_name=project_name,
                        owner_email=owner.get("email"),
                        audio_file=audio_file,
                    )
                ),
            )
            aligned = source.analysis.get("analysis.aligned_transcript")
            archive.writestr(
                f"{folder}/final_aligned_transcript.json",
                write_json(
                    final_alignment_document(
                        source,
                        audio_file=audio_file,
                        alignment_metadata=read_json_file(aligned) if aligned else None,
                    )
                ),
            )
            diarization = source.analysis.get("analysis.diarization")
            if diarization is not None:
                archive.write(diarization, f"{folder}/diarization.json")
    return destination


def archive_filename(project_name: str) -> str:
    stem = Path(safe_filename(project_name) or "dataset").stem or "dataset"
    return f"{stem}.zip"
