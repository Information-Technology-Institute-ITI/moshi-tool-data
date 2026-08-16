from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from moshi_data_pipeline.audio.channels import render_stereo
from moshi_data_pipeline.audio.ffmpeg import extract_working_wav, inspect_media
from moshi_data_pipeline.audio.io import read_audio_segment, write_pcm16
from moshi_data_pipeline.audio.validation import validate_clip, validate_qc_payload
from moshi_data_pipeline.cache import (
    STAGES,
    StageCache,
    atomic_write_json,
    input_fingerprint,
    load_json,
)
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.exceptions import ConfigurationError
from moshi_data_pipeline.models import ClipPlan, QCResult, QCStatus, SpeakerSegment, Word
from moshi_data_pipeline.output.manifest import rebuild_manifest
from moshi_data_pipeline.output.moshi_json import build_moshi_payload
from moshi_data_pipeline.output.reports import rebuild_rejected_report
from moshi_data_pipeline.segmentation.conversation import segment_conversation
from moshi_data_pipeline.speakers.assignment import (
    apply_speaker_overrides,
    assign_speakers_to_words,
    read_mapping,
    select_assistant_speaker,
)
from moshi_data_pipeline.speakers.diarization import WhisperXDiarizer
from moshi_data_pipeline.speakers.separation import build_overlap_separator
from moshi_data_pipeline.transcription.alignment import WhisperXAlignmentBackend
from moshi_data_pipeline.transcription.whisperx_backend import (
    WhisperXTranscriber,
    release_model,
    resolve_device,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessOptions:
    assistant_speaker: str | None = None
    speaker_mapping: Path | None = None
    interactive_speaker: bool | None = None
    resume: bool = False
    force_stage: str | None = None
    keep_working_files: bool = False
    experimental_separation: bool = False
    separate_overlap: bool | None = None
    manual_transcript: Path | None = None
    include_approved_reviews: bool = True


@dataclass(slots=True)
class ProcessSummary:
    source: str
    processed: int
    accepted: int
    review: int
    rejected: int
    failed: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "source": self.source,
            "processed": self.processed,
            "accepted": self.accepted,
            "review": self.review,
            "rejected": self.rejected,
            "failed": self.failed,
        }


def media_key(path: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9_.\-\u0600-\u06ff]+", "_", path.stem).strip("._")
    return value or hashlib.sha256(str(path).encode()).hexdigest()[:12]


def _fingerprint(*values: Any) -> str:
    raw = json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_words(path: Path) -> list[Word]:
    return [Word.from_dict(value) for value in load_json(path)]


def _load_segments(path: Path) -> list[SpeakerSegment]:
    return [SpeakerSegment.from_dict(value) for value in load_json(path)]


def _next_clip_id(dataset_root: Path, reserved: set[str]):
    existing = {path.stem for path in (dataset_root / "data_stereo").glob("conversation_*.wav")}
    existing.update(reserved)
    numbers = [
        int(match.group(1))
        for name in existing
        if (match := re.fullmatch(r"conversation_(\d+)", name))
    ]
    counter = max(numbers, default=0) + 1
    while True:
        candidate = f"conversation_{counter:03d}"
        counter += 1
        if candidate not in existing:
            existing.add(candidate)
            yield candidate


def _install_file_logger(path: Path) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


def _combine_status(first: QCStatus, second: QCStatus) -> QCStatus:
    rank = {QCStatus.PASS: 0, QCStatus.REVIEW: 1, QCStatus.REJECT: 2}
    return first if rank[first] >= rank[second] else second


def process_file(
    source: Path,
    dataset_root: Path,
    config: PipelineConfig,
    options: ProcessOptions,
) -> ProcessSummary:
    source = source.resolve()
    dataset_root = dataset_root.resolve()
    if options.force_stage and options.force_stage not in STAGES:
        raise ConfigurationError(
            f"Unknown --force-stage {options.force_stage!r}; choose from {', '.join(STAGES)}"
        )
    if options.separate_overlap is not None:
        config.separation.enabled = options.separate_overlap
    elif options.experimental_separation:
        config.separation.enabled = True
    key = media_key(source)
    reports_root = dataset_root / "reports"
    working_root = dataset_root / "working" / key
    stereo_root = dataset_root / "data_stereo"
    for directory in (reports_root, working_root, stereo_root):
        directory.mkdir(parents=True, exist_ok=True)
    log_handler = _install_file_logger(reports_root / "logs" / f"{key}.log")
    try:
        LOGGER.info("Processing %s", source)
        pipeline_started = time.perf_counter()
        performance: dict[str, Any] = {"stages": {}}
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
        except (ImportError, RuntimeError):
            pass
        cache = StageCache(working_root / "state.json", source)
        inspection_path = reports_root / f"{key}_inspection.json"
        mono_path = working_root / "source_mono.wav"
        raw_path = working_root / "raw_transcript.json"
        aligned_path = working_root / "aligned_transcript.json"
        words_path = working_root / "aligned_words.json"
        diarization_path = reports_root / f"{key}_diarization.json"
        segments_path = working_root / "diarization_segments.json"
        overlap_segments_path = working_root / "overlap_diarization_segments.json"
        assigned_words_path = working_root / "assigned_words.json"
        selection_path = working_root / "assistant_selection.json"
        plans_path = working_root / "clip_plans.json"
        rendering_path = working_root / "rendered_clips.json"
        generation_path = working_root / "generated_json.json"
        qc_path = reports_root / f"{key}_qc.json"
        transcript_report_path = reports_root / f"{key}_transcript.json"
        performance_path = reports_root / f"{key}_performance.json"
        config_snapshot_path = working_root / "config.snapshot.json"
        atomic_write_json(config_snapshot_path, config.model_dump(mode="json"))
        correction_payload = (
            load_json(options.manual_transcript)
            if options.manual_transcript is not None
            else None
        )
        correction_fingerprint = (
            input_fingerprint(options.manual_transcript)
            if options.manual_transcript is not None
            else None
        )

        def record_stage(name: str, started: float, ran: bool) -> None:
            performance["stages"][name] = {
                "duration_seconds": round(time.perf_counter() - started, 3),
                "cache_hit": not ran,
            }

        stage = "inspect"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] input inspection", stage)
        fingerprint = config.fingerprint("audio")
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [inspection_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            inspection = inspect_media(source)
            atomic_write_json(inspection_path, inspection)
            cache.complete(stage, fingerprint, [inspection_path])
        else:
            inspection = load_json(inspection_path)
        record_stage(stage, stage_started, run_stage)

        stage = "extract"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] lossless mono extraction", stage)
        fingerprint = config.fingerprint("audio")
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [mono_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            extract_working_wav(source, mono_path, config.audio.sample_rate)
            cache.complete(stage, fingerprint, [mono_path])
        record_stage(stage, stage_started, run_stage)

        stage = "transcribe"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] WhisperX transcription", stage)
        fingerprint = config.fingerprint("transcription")
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [raw_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            raw = WhisperXTranscriber().transcribe(mono_path, config.transcription)
            # Raw model output is persisted before alignment or normalization.
            atomic_write_json(raw_path, raw)
            cache.complete(stage, fingerprint, [raw_path])
        else:
            raw = load_json(raw_path)
        record_stage(stage, stage_started, run_stage)

        stage = "align"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] word alignment", stage)
        fingerprint = _fingerprint(
            config.fingerprint("alignment"),
            config.fingerprint("transcription"),
            correction_fingerprint,
        )
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [aligned_path, words_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            aligned, words = WhisperXAlignmentBackend().align(
                mono_path,
                raw,
                config.transcription,
                config.alignment,
                correction_payload,
            )
            atomic_write_json(aligned_path, aligned)
            atomic_write_json(words_path, [word.to_dict() for word in words])
            cache.complete(stage, fingerprint, [aligned_path, words_path])
        else:
            aligned = load_json(aligned_path)
            words = _load_words(words_path)
        record_stage(stage, stage_started, run_stage)

        stage = "diarize"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] speaker diarization and word assignment", stage)
        fingerprint = _fingerprint(
            config.fingerprint("diarization"),
            config.fingerprint("transcription"),
            correction_fingerprint,
        )
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [
                diarization_path,
                segments_path,
                overlap_segments_path,
                assigned_words_path,
            ],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            segments, diarization_report = WhisperXDiarizer().diarize(
                mono_path, config.diarization, config.transcription
            )
            words, uncertain_count = assign_speakers_to_words(
                words, segments, config.diarization.min_assignment_overlap
            )
            if isinstance(correction_payload, dict):
                words, segments = apply_speaker_overrides(
                    words,
                    segments,
                    list(correction_payload.get("speaker_overrides", [])),
                )
            diarization_report["uncertain_word_assignments"] = uncertain_count
            overlap_segments = [
                SpeakerSegment.from_dict(value)
                for value in diarization_report.get("segments", [])
            ]
            atomic_write_json(diarization_path, diarization_report)
            atomic_write_json(segments_path, [segment.to_dict() for segment in segments])
            atomic_write_json(
                overlap_segments_path,
                [segment.to_dict() for segment in overlap_segments],
            )
            atomic_write_json(assigned_words_path, [word.to_dict() for word in words])
            cache.complete(
                stage,
                fingerprint,
                [
                    diarization_path,
                    segments_path,
                    overlap_segments_path,
                    assigned_words_path,
                ],
            )
        else:
            diarization_report = load_json(diarization_path)
            segments = _load_segments(segments_path)
            overlap_segments = _load_segments(overlap_segments_path)
            words = _load_words(assigned_words_path)
        record_stage(stage, stage_started, run_stage)

        stage = "select-speaker"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] assistant identity", stage)
        mapping = read_mapping(options.speaker_mapping)
        fingerprint = _fingerprint(options.assistant_speaker, mapping.get(key))
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [selection_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            assistant = select_assistant_speaker(
                key,
                segments,
                options.assistant_speaker,
                mapping,
                options.interactive_speaker,
            )
            atomic_write_json(
                selection_path,
                {
                    "assistant_speaker": assistant,
                    "selection_mode": (
                        "explicit"
                        if options.assistant_speaker
                        else "mapping"
                        if key in mapping
                        else "interactive"
                    ),
                },
            )
            cache.complete(stage, fingerprint, [selection_path])
        else:
            assistant = str(load_json(selection_path)["assistant_speaker"])
        record_stage(stage, stage_started, run_stage)

        stage = "segment"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] conversation windows", stage)
        fingerprint = _fingerprint(
            config.fingerprint("segmentation"),
            config.fingerprint("separation"),
            assistant,
            inspection["duration"],
        )
        previous_plans = (
            [ClipPlan.from_dict(value) for value in load_json(plans_path)]
            if plans_path.exists()
            else []
        )
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [plans_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            previous_ids = iter(plan.clip_id for plan in previous_plans)
            allocator = _next_clip_id(dataset_root, {plan.clip_id for plan in previous_plans})

            def clip_id_factory() -> str:
                try:
                    return next(previous_ids)
                except StopIteration:
                    return next(allocator)

            plans = segment_conversation(
                segments,
                assistant,
                float(inspection["duration"]),
                config.segmentation,
                clip_id_factory,
                overlap_segments,
            )
            for plan in plans:
                if (
                    config.separation.enabled
                    and plan.status == QCStatus.REJECT
                    and set(plan.reasons) == {"excessive_overlap"}
                ):
                    plan.status = QCStatus.REVIEW
                    plan.reasons = ["overlap_separation_required"]
                plan.start = round(plan.start * config.audio.sample_rate) / config.audio.sample_rate
                plan.end = round(plan.end * config.audio.sample_rate) / config.audio.sample_rate
                plan.metrics["duration"] = plan.duration
            atomic_write_json(plans_path, [plan.to_dict() for plan in plans])
            cache.complete(stage, fingerprint, [plans_path])
        else:
            plans = previous_plans
        record_stage(stage, stage_started, run_stage)

        stage = "render-stereo"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] timeline-preserving stereo reconstruction", stage)
        renderable = [plan for plan in plans if plan.status != QCStatus.REJECT]
        rendered_wavs = [stereo_root / f"{plan.clip_id}.wav" for plan in renderable]
        fingerprint = _fingerprint(
            config.fingerprint("audio"),
            config.fingerprint("separation"),
            assistant,
        )
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [rendering_path, *rendered_wavs],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        separator = None
        if run_stage:
            separator = build_overlap_separator(
                mono_path,
                segments,
                overlap_segments,
                assistant,
                config.separation,
                resolve_device(config.transcription.device),
            )
            rendered: list[dict[str, Any]] = []
            for plan in renderable:
                audio, sample_rate = read_audio_segment(
                    mono_path,
                    plan.start,
                    plan.end,
                    config.audio.sample_rate,
                )
                mono = audio[:, 0]
                recovery = (
                    separator.recover_clip(
                        mono,
                        sample_rate,
                        plan.start,
                        plan.end,
                    )
                    if separator is not None
                    else None
                )
                result = render_stereo(
                    mono,
                    sample_rate,
                    plan.start,
                    plan.end,
                    segments,
                    assistant,
                    config.audio.fade_ms,
                    overlap_segments,
                    recovery,
                )
                wav_path = stereo_root / f"{plan.clip_id}.wav"
                write_pcm16(wav_path, result.stereo, result.sample_rate)
                expected_overlap_samples = round(
                    float(plan.metrics.get("overlap_ratio", 0.0))
                    * plan.duration
                    * result.sample_rate
                )
                recovered_samples = int(recovery.mask.sum()) if recovery is not None else 0
                coverage = (
                    min(1.0, recovered_samples / expected_overlap_samples)
                    if expected_overlap_samples
                    else 0.0
                )
                rendered.append(
                    {
                        "clip_id": plan.clip_id,
                        "wav": str(wav_path),
                        "overlap_samples_omitted": int(result.overlap_mask.sum()),
                        "separation_requested": config.separation.enabled,
                        "separation_used": bool(recovery and recovery.used),
                        "separation_coverage": coverage,
                        "separation_failures": recovery.failures if recovery is not None else [],
                        "recovered_intervals": (
                            recovery.recovered_intervals if recovery is not None else []
                        ),
                        "manual_review_required_for_separation": bool(
                            recovery and recovery.used
                        ),
                    }
                )
            atomic_write_json(rendering_path, rendered)
            cache.complete(stage, fingerprint, [rendering_path, *rendered_wavs])
            del separator
            release_model()
        rendered = load_json(rendering_path)
        rendered_by_id = {value["clip_id"]: value for value in rendered}
        record_stage(stage, stage_started, run_stage)

        stage = "generate-json"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] official Moshi sidecars", stage)
        sidecars = [stereo_root / f"{plan.clip_id}.json" for plan in renderable]
        fingerprint = _fingerprint(config.fingerprint("normalization"), assistant)
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [generation_path, transcript_report_path, *sidecars],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            transcript_clips: list[dict[str, Any]] = []
            generated: list[dict[str, str]] = []
            for plan in renderable:
                payload, report = build_moshi_payload(
                    words,
                    assistant,
                    plan.start,
                    plan.end,
                    config.normalization,
                )
                json_path = stereo_root / f"{plan.clip_id}.json"
                atomic_write_json(json_path, payload)
                transcript_clips.append({"clip_id": plan.clip_id, **report})
                generated.append({"clip_id": plan.clip_id, "json": str(json_path)})
            unaligned = [word.to_dict() for word in words if word.start is None or word.end is None]
            atomic_write_json(
                transcript_report_path,
                {
                    "source": str(source),
                    "language": aligned.get("language", config.transcription.language),
                    "raw_transcript_path": str(raw_path),
                    "aligned_transcript_path": str(aligned_path),
                    "transcription_quality": raw.get("quality", {}),
                    "low_confidence_latin_words": aligned.get(
                        "low_confidence_latin_words", []
                    ),
                    "unaligned_words": unaligned,
                    "clips": transcript_clips,
                },
            )
            atomic_write_json(generation_path, generated)
            cache.complete(stage, fingerprint, [generation_path, transcript_report_path, *sidecars])
        record_stage(stage, stage_started, run_stage)

        stage = "validate"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] clip quality control", stage)
        fingerprint = _fingerprint(
            config.fingerprint("qc"),
            config.fingerprint("segmentation"),
            config.fingerprint("audio"),
            config.fingerprint("separation"),
        )
        run_stage = cache.should_run(
            stage,
            fingerprint,
            [qc_path],
            resume=options.resume,
            force_stage=options.force_stage,
        )
        if run_stage:
            qc_results: list[QCResult] = []
            for plan in plans:
                relative = f"data_stereo/{plan.clip_id}.wav"
                if plan.status == QCStatus.REJECT:
                    qc = QCResult(
                        QCStatus.REJECT,
                        plan.reasons,
                        plan.metrics,
                        plan.clip_id,
                        relative,
                    )
                else:
                    expected_samples = round(plan.duration * config.audio.sample_rate)
                    expected = render_stereo(
                        np.zeros(expected_samples, dtype=np.float32),
                        config.audio.sample_rate,
                        plan.start,
                        plan.end,
                        segments,
                        assistant,
                        config.audio.fade_ms,
                        overlap_segments,
                    )
                    clip_words = [
                        word
                        for word in words
                        if word.start is not None
                        and word.end is not None
                        and word.start >= plan.start
                        and word.end <= plan.end
                    ]
                    low_confidence = sum(
                        1
                        for word in clip_words
                        if word.score is not None
                        and word.score < config.alignment.low_confidence_score
                    )
                    uncertain = sum(
                        word.speaker is None
                        or word.assignment_confidence is None
                        or word.assignment_confidence
                        < config.diarization.min_assignment_overlap
                        for word in clip_words
                    )
                    suspicious_segments = [
                        segment
                        for segment in raw.get("segments", [])
                        if segment.get("quality_flags")
                        and float(segment.get("end", 0.0)) > plan.start
                        and float(segment.get("start", 0.0)) < plan.end
                    ]
                    render_metadata = rendered_by_id.get(plan.clip_id, {})
                    qc = validate_clip(
                        stereo_root / f"{plan.clip_id}.wav",
                        stereo_root / f"{plan.clip_id}.json",
                        config,
                        assistant_mask=expected.assistant_mask,
                        user_mask=expected.user_mask,
                        overlap_ratio=float(plan.metrics.get("overlap_ratio", 0.0)),
                        low_confidence_words=low_confidence,
                        total_words=len(clip_words),
                        uncertain_word_assignments=uncertain,
                        suspect_transcript_segments=len(suspicious_segments),
                        unresolved_hallucinations=sum(
                            "repeated_ngram" in segment.get("quality_flags", [])
                            for segment in suspicious_segments
                        ),
                        separation_coverage=float(
                            render_metadata.get("separation_coverage", 0.0)
                        ),
                        separation_used=bool(render_metadata.get("separation_used", False)),
                        expected_duration=plan.duration,
                    )
                    qc.clip_id = plan.clip_id
                    qc.path = relative
                    qc.metrics = {**plan.metrics, **qc.metrics}
                    qc.status = _combine_status(qc.status, plan.status)
                    plan_reasons = [
                        reason
                        for reason in plan.reasons
                        if reason != "overlap_separation_required"
                    ]
                    qc.reasons = sorted(set(qc.reasons + plan_reasons))
                qc_results.append(qc)
            counts = {
                status.value: sum(result.status == status for result in qc_results)
                for status in QCStatus
            }
            qc_report = {
                "source": str(source),
                "assistant_speaker": assistant,
                "status_counts": counts,
                "global_unaligned_words": len(
                    [word for word in words if word.start is None or word.end is None]
                ),
                "uncertain_word_assignments": diarization_report.get(
                    "uncertain_word_assignments", 0
                ),
                "transcription_quality": raw.get("quality", {}),
                "clips": [result.to_dict() for result in qc_results],
            }
            validate_qc_payload(qc_report)
            atomic_write_json(qc_path, qc_report)
            cache.complete(stage, fingerprint, [qc_path])
        else:
            qc_report = load_json(qc_path)
            qc_results = [
                QCResult(
                    status=QCStatus(value["status"]),
                    reasons=list(value.get("reasons", [])),
                    metrics=dict(value.get("metrics", {})),
                    clip_id=value.get("clip_id"),
                    path=value.get("path"),
                )
                for value in qc_report.get("clips", [])
            ]
        record_stage(stage, stage_started, run_stage)

        stage = "manifest"
        stage_started = time.perf_counter()
        LOGGER.info("[%s] deterministic manifest", stage)
        rebuild_manifest(dataset_root, include_approved_reviews=options.include_approved_reviews)
        rebuild_rejected_report(dataset_root)
        cache.complete(
            stage,
            _fingerprint(options.include_approved_reviews),
            [dataset_root / "train.jsonl", reports_root / "rejected_clips.jsonl"],
        )
        record_stage(stage, stage_started, True)

        elapsed = time.perf_counter() - pipeline_started
        performance.update(
            {
                "source": str(source),
                "source_duration_seconds": float(inspection["duration"]),
                "total_duration_seconds": round(elapsed, 3),
                "realtime_factor": round(elapsed / float(inspection["duration"]), 4),
            }
        )
        try:
            import torch

            performance["peak_gpu_memory_bytes"] = (
                int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else 0
            )
        except (ImportError, RuntimeError):
            performance["peak_gpu_memory_bytes"] = 0
        atomic_write_json(performance_path, performance)

        summary = ProcessSummary(
            source=str(source),
            processed=len(qc_results),
            accepted=sum(result.status == QCStatus.PASS for result in qc_results),
            review=sum(result.status == QCStatus.REVIEW for result in qc_results),
            rejected=sum(result.status == QCStatus.REJECT for result in qc_results),
        )
        LOGGER.info(
            "Summary processed=%d accepted=%d review=%d rejected=%d failed=0",
            summary.processed,
            summary.accepted,
            summary.review,
            summary.rejected,
        )
        return summary
    finally:
        logging.getLogger().removeHandler(log_handler)
        log_handler.close()
