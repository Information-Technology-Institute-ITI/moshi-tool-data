from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from moshi_data_pipeline.audio.ffmpeg import (
    SUPPORTED_EXTENSIONS,
    extract_working_wav,
    inspect_media,
)
from moshi_data_pipeline.benchmark import benchmark_profiles
from moshi_data_pipeline.cache import STAGES, atomic_write_json
from moshi_data_pipeline.config import load_config
from moshi_data_pipeline.exceptions import PipelineError
from moshi_data_pipeline.logging_utils import configure_logging
from moshi_data_pipeline.output.manifest import (
    approve_review_path,
    rebuild_manifest,
)
from moshi_data_pipeline.pipeline import ProcessOptions, ProcessSummary, process_file

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Prepare two-speaker media for official Kyutai Moshi fine-tuning.",
)
console = Console()


def _config_overrides(
    *,
    quality_profile: str | None,
    model: str | None,
    language: str | None,
    device: str | None,
    compute_type: str | None,
    batch_size: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
    min_clip_duration: float | None,
    target_clip_duration: float | None,
    max_clip_duration: float | None,
    max_overlap_ratio: float | None,
    max_silence_ratio: float | None,
    min_speaker_duration: float | None,
) -> dict[str, Any]:
    return {
        "transcription": {
            "quality_profile": quality_profile,
            "model": model,
            "language": language,
            "device": device,
            "compute_type": compute_type,
            "batch_size": batch_size,
        },
        "diarization": {
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
        },
        "segmentation": {
            "min_duration": min_clip_duration,
            "target_duration": target_clip_duration,
            "max_duration": max_clip_duration,
            "max_overlap_ratio": max_overlap_ratio,
            "max_silence_ratio": max_silence_ratio,
            "min_speaker_duration": min_speaker_duration,
        },
    }


def _run_process(
    input_path: Path,
    output: Path,
    config_path: Path | None,
    assistant_speaker: str | None,
    speaker_mapping: Path | None,
    interactive_speaker: bool | None,
    resume: bool,
    force_stage: str | None,
    keep_working_files: bool,
    experimental_separation: bool,
    separate_overlap: bool | None,
    manual_transcript: Path | None,
    overrides: dict[str, Any],
) -> ProcessSummary:
    config = load_config(config_path, overrides)
    return process_file(
        input_path,
        output,
        config,
        ProcessOptions(
            assistant_speaker=assistant_speaker,
            speaker_mapping=speaker_mapping,
            interactive_speaker=interactive_speaker,
            resume=resume,
            force_stage=force_stage,
            keep_working_files=keep_working_files,
            experimental_separation=experimental_separation,
            separate_overlap=separate_overlap,
            manual_transcript=manual_transcript,
        ),
    )


@app.command()
def process(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    output: Path = typer.Option(Path("data"), "--output"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    quality_profile: str | None = typer.Option(None, "--quality-profile"),
    language: str | None = typer.Option(None, "--language"),
    model: str | None = typer.Option(None, "--model"),
    device: str | None = typer.Option(None, "--device"),
    compute_type: str | None = typer.Option(None, "--compute-type"),
    batch_size: int | None = typer.Option(None, "--batch-size", min=1),
    min_speakers: int | None = typer.Option(None, "--min-speakers", min=1),
    max_speakers: int | None = typer.Option(None, "--max-speakers", min=1),
    assistant_speaker: str | None = typer.Option(None, "--assistant-speaker"),
    speaker_mapping: Path | None = typer.Option(
        None, "--speaker-mapping", exists=True, dir_okay=False
    ),
    interactive_speaker: bool | None = typer.Option(
        None, "--interactive-speaker/--no-interactive-speaker"
    ),
    min_clip_duration: float | None = typer.Option(None, "--min-clip-duration", min=0.1),
    target_clip_duration: float | None = typer.Option(None, "--target-clip-duration", min=0.1),
    max_clip_duration: float | None = typer.Option(None, "--max-clip-duration", min=0.1),
    max_overlap_ratio: float | None = typer.Option(None, "--max-overlap-ratio", min=0, max=1),
    max_silence_ratio: float | None = typer.Option(None, "--max-silence-ratio", min=0, max=1),
    min_speaker_duration: float | None = typer.Option(None, "--min-speaker-duration", min=0),
    resume: bool = typer.Option(False, "--resume"),
    force_stage: str | None = typer.Option(None, "--force-stage"),
    keep_working_files: bool = typer.Option(False, "--keep-working-files"),
    experimental_separation: bool = typer.Option(False, "--experimental-separation"),
    separate_overlap: bool | None = typer.Option(
        None, "--separate-overlap/--no-separate-overlap"
    ),
    manual_transcript: Path | None = typer.Option(
        None, "--manual-transcript", exists=True, dir_okay=False
    ),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Process one downloaded audio or video file."""
    configure_logging(verbose=verbose)
    overrides = _config_overrides(
        quality_profile=quality_profile,
        model=model,
        language=language,
        device=device,
        compute_type=compute_type,
        batch_size=batch_size,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        min_clip_duration=min_clip_duration,
        target_clip_duration=target_clip_duration,
        max_clip_duration=max_clip_duration,
        max_overlap_ratio=max_overlap_ratio,
        max_silence_ratio=max_silence_ratio,
        min_speaker_duration=min_speaker_duration,
    )
    try:
        summary = _run_process(
            input_path,
            output,
            config,
            assistant_speaker,
            speaker_mapping,
            interactive_speaker,
            resume,
            force_stage,
            keep_working_files,
            experimental_separation,
            separate_overlap,
            manual_transcript,
            overrides,
        )
    except PipelineError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(summary.to_dict(), ensure_ascii=False))


@app.command()
def batch(
    input_dir: Path = typer.Option(..., "--input-dir", exists=True, file_okay=False),
    output: Path = typer.Option(Path("data"), "--output"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    quality_profile: str | None = typer.Option(None, "--quality-profile"),
    language: str | None = typer.Option(None, "--language"),
    model: str | None = typer.Option(None, "--model"),
    device: str | None = typer.Option(None, "--device"),
    compute_type: str | None = typer.Option(None, "--compute-type"),
    batch_size: int | None = typer.Option(None, "--batch-size", min=1),
    min_speakers: int | None = typer.Option(None, "--min-speakers", min=1),
    max_speakers: int | None = typer.Option(None, "--max-speakers", min=1),
    assistant_speaker: str | None = typer.Option(None, "--assistant-speaker"),
    speaker_mapping: Path | None = typer.Option(
        None, "--speaker-mapping", exists=True, dir_okay=False
    ),
    min_clip_duration: float | None = typer.Option(None, "--min-clip-duration", min=0.1),
    target_clip_duration: float | None = typer.Option(None, "--target-clip-duration", min=0.1),
    max_clip_duration: float | None = typer.Option(None, "--max-clip-duration", min=0.1),
    max_overlap_ratio: float | None = typer.Option(None, "--max-overlap-ratio", min=0, max=1),
    max_silence_ratio: float | None = typer.Option(None, "--max-silence-ratio", min=0, max=1),
    min_speaker_duration: float | None = typer.Option(None, "--min-speaker-duration", min=0),
    resume: bool = typer.Option(False, "--resume"),
    force_stage: str | None = typer.Option(None, "--force-stage"),
    keep_working_files: bool = typer.Option(False, "--keep-working-files"),
    experimental_separation: bool = typer.Option(False, "--experimental-separation"),
    separate_overlap: bool | None = typer.Option(
        None, "--separate-overlap/--no-separate-overlap"
    ),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Process supported files sequentially, one input and one model stage at a time."""
    configure_logging(verbose=verbose)
    overrides = _config_overrides(
        quality_profile=quality_profile,
        model=model,
        language=language,
        device=device,
        compute_type=compute_type,
        batch_size=batch_size,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        min_clip_duration=min_clip_duration,
        target_clip_duration=target_clip_duration,
        max_clip_duration=max_clip_duration,
        max_overlap_ratio=max_overlap_ratio,
        max_silence_ratio=max_silence_ratio,
        min_speaker_duration=min_speaker_duration,
    )
    files = sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )
    total = {"processed": 0, "accepted": 0, "review": 0, "rejected": 0, "failed": 0}
    for path in files:
        try:
            summary = _run_process(
                path,
                output,
                config,
                assistant_speaker,
                speaker_mapping,
                None,
                resume,
                force_stage,
                keep_working_files,
                experimental_separation,
                separate_overlap,
                None,
                overrides,
            )
        except Exception as exc:
            logging.exception("Failed to process %s", path)
            console.print(f"[red]FAILED[/red] {path.name}: {exc}")
            total["failed"] += 1
            continue
        for key in ("processed", "accepted", "review", "rejected", "failed"):
            total[key] += int(getattr(summary, key))
    console.print_json(json.dumps({"input_files": len(files), **total}))


@app.command("inspect")
def inspect_command(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    output_json: Path | None = typer.Option(None, "--output-json"),
) -> None:
    """Run ffprobe plus a full decode validation without model loading."""
    configure_logging()
    try:
        report = inspect_media(input_path)
    except PipelineError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc
    if output_json:
        atomic_write_json(output_json, report)
    console.print_json(json.dumps(report, ensure_ascii=False))


@app.command("extract")
def extract_command(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    output_wav: Path = typer.Option(..., "--output-wav"),
    sample_rate: int = typer.Option(24_000, "--sample-rate", min=8_000),
) -> None:
    """Extract a mono PCM working WAV without silence trimming."""
    configure_logging()
    try:
        extract_working_wav(input_path, output_wav, sample_rate)
    except PipelineError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(str(output_wav.resolve()))


@app.command("rebuild-manifest")
def rebuild_manifest_command(
    output: Path = typer.Option(Path("data"), "--output"),
    include_approved_reviews: bool = typer.Option(False, "--include-approved-reviews"),
) -> None:
    """Atomically rebuild train.jsonl from current QC statuses and validated artifacts."""
    records = rebuild_manifest(output.resolve(), include_approved_reviews=include_approved_reviews)
    console.print(f"Wrote {len(records)} records to {(output / 'train.jsonl').resolve()}")


@app.command("approve-review")
def approve_review_command(
    path: str = typer.Option(..., "--path"),
    output: Path = typer.Option(Path("data"), "--output"),
) -> None:
    """Explicitly approve one REVIEW clip, then rebuild the manifest."""
    try:
        approve_review_path(output.resolve(), path)
        records = rebuild_manifest(output.resolve(), include_approved_reviews=True)
    except (OSError, ValueError) as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"Approved {Path(path).as_posix()}; manifest now has {len(records)} records")


@app.command("review")
def review_command(
    output: Path = typer.Option(Path("data"), "--output", exists=True, file_okay=False),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65_535),
    allow_remote: bool = typer.Option(False, "--allow-remote"),
) -> None:
    """Start the local transcript, speaker, and clip review page."""
    from moshi_data_pipeline.review.server import serve_review

    try:
        serve_review(output, host, port, allow_remote=allow_remote)
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("web")
def web_command(
    workspace: Path = typer.Option(Path("studio_workspace"), "--workspace"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65_535),
    allow_remote: bool = typer.Option(False, "--allow-remote"),
) -> None:
    """Start the local v2 upload, annotation, review, and export studio."""
    from moshi_data_pipeline.studio.server import serve_studio

    try:
        serve_studio(
            workspace,
            host,
            port,
            config_path=config,
            allow_remote=allow_remote,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("benchmark")
def benchmark_command(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    gold: Path = typer.Option(..., "--gold", exists=True, dir_okay=False),
    config_path: Path | None = typer.Option(
        None, "--config", exists=True, dir_okay=False
    ),
    compute_types: str = typer.Option("int8,float16", "--compute-types"),
    model: str | None = typer.Option(None, "--model"),
    language: str | None = typer.Option(None, "--language"),
    device: str | None = typer.Option(None, "--device"),
    output_json: Path | None = typer.Option(None, "--output-json"),
) -> None:
    """Compare balanced decoder profiles in an isolated temporary workspace."""
    configure_logging()
    try:
        config = load_config(config_path)
        if model is not None:
            config.transcription.model = model
        if language is not None:
            config.transcription.language = language
        if device is not None:
            config.transcription.device = device
        requested_types = tuple(
            value.strip() for value in compute_types.split(",") if value.strip()
        )
        if not requested_types:
            raise ValueError("--compute-types must include at least one value")
        result = benchmark_profiles(input_path, gold, config, requested_types)
    except (OSError, PipelineError, ValueError) as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(1) from exc
    if output_json is not None:
        atomic_write_json(output_json, result)
    console.print_json(json.dumps(result, ensure_ascii=False))


@app.command("stages")
def stages_command() -> None:
    """List valid values for --force-stage."""
    for stage in STAGES:
        console.print(stage)
