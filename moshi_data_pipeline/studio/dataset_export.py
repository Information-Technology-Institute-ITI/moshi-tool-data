"""Builds an administrator's download of a finished dataset.

The archive holds the audio the annotation timestamps actually refer to, plus
one CSV describing every transcript segment. It is meant to be fed straight to a
splitting script: each row carries the audio file it belongs to and the exact
range to cut, in samples, milliseconds and seconds.

Nothing here starts processing or touches the GPU. It reads the newest saved
annotation for each source, which is the reviewer's final text.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.domain import SAMPLE_RATE, AnnotationDocument
from moshi_data_pipeline.studio.media import safe_filename

READY_STATUSES = {"ready", "clips_ready"}

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
    """One finished source: its audio on disk and the annotation to describe it."""

    source_id: str
    original_name: str
    audio_path: Path
    annotation: AnnotationDocument


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


def write_csv(rows: list[dict[str, Any]]) -> bytes:
    """UTF-8 with a BOM, so Excel opens the Arabic text correctly."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def readme(project_name: str, sources: list[ExportSource], rows: list[dict[str, Any]]) -> bytes:
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


def collect_sources(catalog: Any, paths: Any, project_id: str) -> list[ExportSource]:
    """The finished sources of a dataset whose audio is actually on disk."""
    collected: list[ExportSource] = []
    for source in catalog.list_sources(project_id):
        if str(source["status"]) not in READY_STATUSES:
            continue
        audio = paths.canonical_audio(str(source["id"]))
        if not audio.is_file():
            continue
        collected.append(
            ExportSource(
                source_id=str(source["id"]),
                original_name=str(source["original_name"]),
                audio_path=audio,
                annotation=catalog.latest_annotation(str(source["id"])),
            )
        )
    return collected


def build_dataset_archive(catalog: Any, paths: Any, project_id: str, destination: Path) -> Path:
    """Writes the dataset's audio and transcript CSV to a zip file.

    Raises NothingToExportError when no source in the dataset is finished, so
    the caller can say why instead of handing over an empty archive.
    """
    project = catalog.get_project(project_id)
    sources = collect_sources(catalog, paths, project_id)
    if not sources:
        raise NothingToExportError(
            "This dataset has no prepared source to export yet."
        )

    rows = transcript_rows(sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("transcriptions.csv", write_csv(rows))
        archive.writestr("README.txt", readme(str(project["name"]), sources, rows))
        for source, name in zip(sources, archive_audio_names(sources), strict=True):
            archive.write(source.audio_path, name)
    return destination


def archive_filename(project_name: str) -> str:
    stem = Path(safe_filename(project_name) or "dataset").stem or "dataset"
    return f"{stem}.zip"
