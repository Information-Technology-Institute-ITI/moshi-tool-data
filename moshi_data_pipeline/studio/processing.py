from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import numpy as np

from moshi_data_pipeline.audio.channels import render_stereo
from moshi_data_pipeline.audio.ffmpeg import (
    create_video_proxy,
    extract_working_wav,
    inspect_media,
)
from moshi_data_pipeline.audio.io import audio_info, read_audio_segment, write_pcm16
from moshi_data_pipeline.audio.validation import validate_clip
from moshi_data_pipeline.cache import atomic_write_json
from moshi_data_pipeline.config import PipelineConfig
from moshi_data_pipeline.models import QCStatus, SpeakerSegment, Word
from moshi_data_pipeline.output.moshi_json import build_moshi_payload
from moshi_data_pipeline.speakers.assignment import assign_speakers_to_words
from moshi_data_pipeline.speakers.diarization import WhisperXDiarizer
from moshi_data_pipeline.speakers.identity import SpeakerReferenceMatcher
from moshi_data_pipeline.speakers.separation import (
    OverlapRecovery,
    build_overlap_separator,
)
from moshi_data_pipeline.studio.activity import close_word_supported_activity_gaps
from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    TranscriptCandidate,
    TranscriptUtterance,
    new_id,
)
from moshi_data_pipeline.studio.media import (
    StudioPaths,
    create_waveform_peaks,
    load_json_file,
)
from moshi_data_pipeline.studio.normalization import normalize_annotation_bounds
from moshi_data_pipeline.studio.planning import derived_overlaps
from moshi_data_pipeline.transcription.alignment import WhisperXAlignmentBackend
from moshi_data_pipeline.transcription.quality import HIGH_RISK_TRANSCRIPT_FLAGS
from moshi_data_pipeline.transcription.whisperx_backend import (
    WhisperXTranscriber,
    _aggregate_average_log_probability,
    release_model,
    resolve_device,
)

Progress = Callable[[float, str], None]


def _seconds(sample: int) -> float:
    return sample / SAMPLE_RATE


def _sample(seconds: float) -> int:
    return max(0, round(seconds * SAMPLE_RATE))


def _segments_from_annotation(annotation: AnnotationDocument) -> list[SpeakerSegment]:
    return [
        SpeakerSegment(
            _seconds(region.start_sample),
            _seconds(region.end_sample),
            region.speaker,
        )
        for region in annotation.activities
    ]


def _speaker_for_interval(
    start: float, end: float, segments: list[SpeakerSegment]
) -> str | None:
    overlaps: dict[str, float] = {}
    for segment in segments:
        overlap = max(0.0, min(end, segment.end) - max(start, segment.start))
        if overlap:
            overlaps[segment.speaker] = overlaps.get(segment.speaker, 0.0) + overlap
    return max(overlaps.items(), key=lambda value: (value[1], value[0]))[0] if overlaps else None


def _map_diarization(
    exclusive: list[SpeakerSegment],
    report: dict[str, Any],
    mapping: dict[str, str] | None = None,
) -> tuple[list[SpeakerSegment], list[SpeakerSegment], dict[str, str]]:
    speakers = sorted({segment.speaker for segment in exclusive})
    if len(speakers) != 2:
        raise ValueError(f"Exactly two speakers are required, detected {len(speakers)}")
    mapping = mapping or {speakers[0]: "A", speakers[1]: "B"}
    if set(mapping) != set(speakers) or set(mapping.values()) != {"A", "B"}:
        raise ValueError("Speaker identity mapping must map both detected voices to A and B")
    mapped_exclusive = [
        SpeakerSegment(segment.start, segment.end, mapping[segment.speaker])
        for segment in exclusive
    ]
    mapped_overlap = [
        SpeakerSegment.from_dict(
            {
                "start": value["start"],
                "end": value["end"],
                "speaker": mapping[str(value["speaker"])],
            }
        )
        for value in report.get("segments", [])
        if str(value.get("speaker")) in mapping
    ]
    return mapped_exclusive, mapped_overlap, mapping


def _transcript_from_raw(
    raw: dict[str, Any], exclusive: list[SpeakerSegment]
) -> list[TranscriptUtterance]:
    values: list[TranscriptUtterance] = []
    for segment in raw.get("segments", []):
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if end <= start:
            continue
        speaker = _speaker_for_interval(start, end, exclusive)
        text = str(segment.get("text", "")).strip()
        review_candidates: list[TranscriptCandidate] = []
        retry = segment.get("retry_candidate")
        if (
            isinstance(retry, dict)
            and str(retry.get("text", "")).strip()
            and str(retry.get("text", "")).strip() != text
        ):
            review_candidates.append(
                TranscriptCandidate(
                    source="retry",
                    model=str(raw.get("model", "whisperx")),
                    text=str(retry["text"]).strip(),
                    average_log_probability=retry.get("avg_logprob"),
                    quality_flags=list(retry.get("quality_flags", [])),
                )
            )
        values.append(
            TranscriptUtterance(
                speaker=speaker,
                start_sample=_sample(start),
                end_sample=_sample(end),
                text=text,
                model_text=text,
                model_speaker=speaker,
                quality_flags=list(segment.get("quality_flags", [])),
                alignment_status="aligned",
                review_candidates=review_candidates,
            )
        )
    return values


def _flag_overlapping_transcript(
    transcript: list[TranscriptUtterance],
    activities: list[ActivityRegion],
) -> list[TranscriptUtterance]:
    overlaps = derived_overlaps(activities)
    values: list[TranscriptUtterance] = []
    for utterance in transcript:
        overlap_samples = sum(
            max(
                0,
                min(utterance.end_sample, end) - max(utterance.start_sample, start),
            )
            for start, end in overlaps
        )
        duration = utterance.end_sample - utterance.start_sample
        threshold = max(SAMPLE_RATE // 4, round(duration * 0.05))
        flags = set(utterance.quality_flags) - {"overlapping_speech"}
        if overlap_samples >= threshold:
            flags.add("overlapping_speech")
        values.append(
            utterance.model_copy(update={"quality_flags": sorted(flags)})
        )
    return values


def rediarize_source(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    annotation = catalog.latest_annotation(source_id)
    if {value.speaker for value in annotation.speaker_references} != {"A", "B"}:
        raise ValueError("Confirm one clean reference region for Speaker A and Speaker B")
    progress(0.05, "Detecting the two speakers")
    exclusive, diarization = WhisperXDiarizer().diarize(
        paths.canonical_audio(source_id),
        config.diarization,
        config.transcription,
    )
    progress(0.58, "Loading confirmed speaker references")
    matcher = SpeakerReferenceMatcher(
        paths.canonical_audio(source_id),
        annotation.speaker_references,
        config.separation,
        resolve_device(config.transcription.device),
    )
    progress(0.72, "Matching detected voices to confirmed identities")
    identity_mapping, identity_report = matcher.match(exclusive)
    mapped_exclusive, mapped_overlap, mapping = _map_diarization(
        exclusive,
        diarization,
        identity_mapping,
    )
    words = [Word.from_dict(value) for value in annotation.aligned_words]
    words, uncertain = assign_speakers_to_words(
        words,
        mapped_exclusive,
        config.diarization.min_assignment_overlap,
    )
    activities = [
        ActivityRegion(
            speaker=segment.speaker,
            start_sample=_sample(segment.start),
            end_sample=_sample(segment.end),
            origin="model",
        )
        for segment in mapped_overlap
    ]
    activities, repaired_gaps = close_word_supported_activity_gaps(
        activities,
        words,
        annotation.exclusions,
        config.diarization.activity_merge_gap_seconds,
    )
    transcript = []
    for utterance in annotation.transcript:
        speaker = _speaker_for_interval(
            _seconds(utterance.start_sample),
            _seconds(utterance.end_sample),
            mapped_exclusive,
        )
        transcript.append(
            utterance.model_copy(
                update={
                    "speaker": speaker,
                    "human_verified": (
                        utterance.human_verified if speaker == utterance.speaker else False
                    ),
                }
            )
        )
    transcript = _flag_overlapping_transcript(transcript, activities)
    source = catalog.get_source(source_id)
    updated = normalize_annotation_bounds(
        annotation.model_copy(
            update={
                "activities": activities,
                "activities_finalized": False,
                "transcript": transcript,
                "aligned_words": [word.to_dict() for word in words],
            }
        ),
        int(source["duration_samples"] or 0),
    )
    saved = catalog.save_annotation(source_id, annotation.version, updated)
    diarization["speaker_mapping"] = mapping
    diarization["identity_lock"] = identity_report
    diarization["word_supported_activity_gap_merges"] = repaired_gaps
    atomic_write_json(paths.artifact(source_id, "diarization.json"), diarization)
    progress(1.0, "Stable speaker identities are ready for human confirmation")
    return {
        "source_id": source_id,
        "annotation_version": saved.version,
        "mapping": mapping,
        "uncertain_word_assignments": uncertain,
    }


def initialize_source(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    mode: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    source = catalog.get_source(source_id)
    original = paths.resolve_relative(source["stored_path"])
    source_root = paths.source_root(source_id)
    source_root.mkdir(parents=True, exist_ok=True)
    canonical = paths.canonical_audio(source_id)
    progress(0.05, "Inspecting source media")
    inspection = source.get("inspection") or inspect_media(original)
    if not canonical.exists():
        progress(0.18, "Extracting canonical 24 kHz audio")
        extract_working_wav(original, canonical, SAMPLE_RATE)
    else:
        progress(0.18, "Reusing immutable canonical audio")
    info = audio_info(canonical)
    peaks_path = paths.peaks(source_id)
    if peaks_path.exists():
        peaks = load_json_file(peaks_path, {})
        progress(0.30, "Reusing immutable waveform overview")
    else:
        progress(0.30, "Building waveform overview")
        peaks = create_waveform_peaks(canonical, peaks_path)
    proxy_created = False
    if inspection.get("has_video"):
        proxy = paths.video_proxy(source_id)
        if not proxy.exists():
            progress(0.36, "Creating synchronized video proxy")
            create_video_proxy(original, proxy)
        else:
            progress(0.36, "Reusing immutable synchronized video proxy")
        proxy_created = True
    annotation = AnnotationDocument(source_id=source_id)
    if mode == "assisted":
        progress(0.43, "Transcribing Egyptian Arabic")
        raw = WhisperXTranscriber().transcribe(canonical, config.transcription)
        atomic_write_json(paths.artifact(source_id, "raw_transcript.json"), raw)
        progress(0.61, "Aligning transcript words")
        aligned, words = WhisperXAlignmentBackend().align(
            canonical, raw, config.transcription, config.alignment
        )
        atomic_write_json(paths.artifact(source_id, "aligned_transcript.json"), aligned)
        progress(0.76, "Detecting the two speakers")
        exclusive, diarization = WhisperXDiarizer().diarize(
            canonical, config.diarization, config.transcription
        )
        mapped_exclusive, mapped_overlap, mapping = _map_diarization(exclusive, diarization)
        words, _ = assign_speakers_to_words(
            words, mapped_exclusive, config.diarization.min_assignment_overlap
        )
        activities = [
            ActivityRegion(
                speaker=segment.speaker,
                start_sample=_sample(segment.start),
                end_sample=_sample(segment.end),
                origin="model",
            )
            for segment in mapped_overlap
        ]
        activities, repaired_gaps = close_word_supported_activity_gaps(
            activities,
            words,
            [],
            config.diarization.activity_merge_gap_seconds,
        )
        transcript = _flag_overlapping_transcript(
            _transcript_from_raw(raw, mapped_exclusive),
            activities,
        )
        annotation = AnnotationDocument(
            source_id=source_id,
            activities=activities,
            transcript=transcript,
            aligned_words=[word.to_dict() for word in words],
        )
        diarization["speaker_mapping"] = mapping
        diarization["word_supported_activity_gap_merges"] = repaired_gaps
        atomic_write_json(paths.artifact(source_id, "diarization.json"), diarization)
        release_model()
    annotation = normalize_annotation_bounds(annotation, int(info["samples"]))
    progress(0.91, "Saving the source annotation")
    if catalog.latest_annotation(source_id).version == 0:
        annotation = catalog.replace_initial_annotation(source_id, annotation)
    catalog.update_source(
        source_id,
        status="ready",
        init_mode=mode,
        duration_samples=int(info["samples"]),
        inspection=inspection,
        clips_stale=True,
    )
    progress(1.0, "Source is ready for annotation")
    return {
        "source_id": source_id,
        "annotation_version": annotation.version,
        "duration_samples": int(info["samples"]),
        "waveform_points": len(peaks["points"]),
        "video_proxy": proxy_created,
    }


def transcribe_source(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    canonical = paths.canonical_audio(source_id)
    source = catalog.get_source(source_id)
    annotation = catalog.latest_annotation(source_id)
    progress(0.10, "Transcribing source")
    raw = WhisperXTranscriber().transcribe(canonical, config.transcription)
    atomic_write_json(paths.artifact(source_id, "raw_transcript.json"), raw)
    progress(0.48, "Aligning words")
    aligned, words = WhisperXAlignmentBackend().align(
        canonical, raw, config.transcription, config.alignment
    )
    segments = _segments_from_annotation(annotation)
    words, _ = assign_speakers_to_words(
        words, segments, config.diarization.min_assignment_overlap
    )
    updated = annotation.model_copy(
        update={
            "transcript": _flag_overlapping_transcript(
                _transcript_from_raw(raw, segments),
                annotation.activities,
            ),
            "aligned_words": [word.to_dict() for word in words],
        }
    )
    updated = normalize_annotation_bounds(
        updated, int(source["duration_samples"] or 0)
    )
    saved = catalog.save_annotation(source_id, annotation.version, updated)
    atomic_write_json(paths.artifact(source_id, "aligned_transcript.json"), aligned)
    progress(1.0, "Transcript is ready for correction")
    return {"source_id": source_id, "annotation_version": saved.version}


def _candidate_from_result(
    result: dict[str, Any],
    source: str,
    model: str,
) -> TranscriptCandidate:
    segments = list(result.get("segments", []))
    return TranscriptCandidate(
        source=source,
        model=model,
        text=" ".join(
            str(segment.get("text", "")).strip()
            for segment in segments
            if str(segment.get("text", "")).strip()
        ).strip(),
        average_log_probability=_aggregate_average_log_probability(segments),
        quality_flags=sorted(
            {
                str(flag)
                for segment in segments
                for flag in segment.get("quality_flags", [])
            }
        ),
    )


def generate_review_candidates(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    annotation = catalog.latest_annotation(source_id)
    flagged = [
        utterance
        for utterance in annotation.transcript
        if HIGH_RISK_TRANSCRIPT_FLAGS.intersection(utterance.quality_flags)
        and not utterance.human_verified
    ]
    if not flagged:
        progress(1.0, "No unresolved high-risk utterances need a second pass")
        return {
            "source_id": source_id,
            "annotation_version": annotation.version,
            "candidates": 0,
        }
    review_model = config.transcription.review_model or config.transcription.model
    review_config = config.transcription.model_copy(
        update={
            "model": review_model,
            "batch_size": 1,
            "chunk_size": config.transcription.retry_chunk_size,
            "beam_size": config.transcription.review_beam_size,
        }
    )
    output_root = (
        paths.source_root(source_id)
        / "review_candidates"
        / f"annotation_v{annotation.version}"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    candidates: dict[str, TranscriptCandidate] = {}
    for index, utterance in enumerate(flagged):
        progress(
            0.05 + 0.88 * index / max(1, len(flagged)),
            f"Second-pass transcription {index + 1} of {len(flagged)}",
        )
        audio, sample_rate = read_audio_segment(
            paths.canonical_audio(source_id),
            _seconds(utterance.start_sample),
            _seconds(utterance.end_sample),
            SAMPLE_RATE,
        )
        audio_path = output_root / f"{utterance.id}.wav"
        if not audio_path.exists():
            write_pcm16(audio_path, audio[:, :1], sample_rate)
        result = WhisperXTranscriber().transcribe(audio_path, review_config)
        atomic_write_json(output_root / f"{utterance.id}.json", result)
        candidates[utterance.id] = _candidate_from_result(
            result,
            "secondary_asr",
            review_model,
        )
    transcript = [
        utterance.model_copy(
            update={
                "review_candidates": [
                    *[
                        value
                        for value in utterance.review_candidates
                        if value.source != "secondary_asr"
                    ],
                    candidates[utterance.id],
                ]
            }
        )
        if utterance.id in candidates
        else utterance
        for utterance in annotation.transcript
    ]
    saved = catalog.save_annotation(
        source_id,
        annotation.version,
        annotation.model_copy(update={"transcript": transcript}),
    )
    progress(1.0, "Second-pass candidates are ready for human comparison")
    return {
        "source_id": source_id,
        "annotation_version": saved.version,
        "candidates": len(candidates),
        "model": review_model,
    }


def realign_source(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    source = catalog.get_source(source_id)
    annotation = catalog.latest_annotation(source_id)
    raw_path = paths.artifact(source_id, "raw_transcript.json")
    if raw_path.exists():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        raw = {
            "language": config.transcription.language,
            "segments": [
                {
                    "start": _seconds(value.start_sample),
                    "end": _seconds(value.end_sample),
                    "text": value.text,
                }
                for value in annotation.transcript
            ],
        }
    corrected = {
        "segments": [
            {
                "start": _seconds(value.start_sample),
                "end": _seconds(value.end_sample),
                "text": value.text,
            }
            for value in annotation.transcript
            if value.text.strip()
        ]
    }
    progress(0.15, "Realigning corrected transcript")
    aligned, words = WhisperXAlignmentBackend().align(
        paths.canonical_audio(source_id),
        raw,
        config.transcription,
        config.alignment,
        corrected,
    )
    segments = _segments_from_annotation(annotation)
    words, uncertain = assign_speakers_to_words(
        words, segments, config.diarization.min_assignment_overlap
    )
    transcript: list[TranscriptUtterance] = []
    for utterance in annotation.transcript:
        utterance_words = [
            word
            for word in words
            if (
                word.start is not None
                and word.end is not None
                and word.end > _seconds(utterance.start_sample)
                and word.start < _seconds(utterance.end_sample)
            )
        ]
        has_word = bool(utterance_words)
        low_confidence = has_word and any(
            word.score is None or word.score < config.alignment.low_confidence_score
            for word in utterance_words
        )
        status = (
            "unaligned"
            if not has_word
            else "low_confidence"
            if low_confidence
            else "aligned"
        )
        quality_flags = [
            value
            for value in utterance.quality_flags
            if value not in {"low_confidence_alignment", "unaligned_words"}
        ]
        if status == "low_confidence":
            quality_flags.append("low_confidence_alignment")
        elif status == "unaligned":
            quality_flags.append("unaligned_words")
        transcript.append(
            utterance.model_copy(
                update={
                    "alignment_status": status,
                    "quality_flags": quality_flags,
                }
            )
        )
    updated = normalize_annotation_bounds(
        annotation.model_copy(
            update={
                "transcript": transcript,
                "aligned_words": [word.to_dict() for word in words],
            }
        ),
        int(source["duration_samples"] or 0),
    )
    saved = catalog.save_annotation(
        source_id,
        annotation.version,
        updated,
    )
    atomic_write_json(paths.artifact(source_id, "aligned_transcript.json"), aligned)
    progress(1.0, "Corrected transcript aligned")
    return {
        "source_id": source_id,
        "annotation_version": saved.version,
        "words": len(words),
        "uncertain_assignments": uncertain,
    }


def transcribe_overlap_stems(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    region_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    record = next(
        (
            value
            for value in catalog.overlap_recoveries(source_id)
            if value["region_id"] == region_id
        ),
        None,
    )
    if record is None or record["status"] != "recovered":
        raise ValueError("Recover this overlap before transcribing its isolated stems")
    review_model = config.transcription.review_model or config.transcription.model
    review_config = config.transcription.model_copy(
        update={
            "model": review_model,
            "batch_size": 1,
            "chunk_size": config.transcription.retry_chunk_size,
            "beam_size": config.transcription.review_beam_size,
        }
    )
    stem_transcripts: dict[str, Any] = {}
    for index, role in enumerate(("assistant", "user")):
        progress(0.08 + 0.42 * index, f"Transcribing recovered {role} voice")
        path = paths.resolve_relative(str(record[f"{role}_path"]))
        result = WhisperXTranscriber().transcribe(path, review_config)
        candidate = _candidate_from_result(
            result,
            "overlap_assistant" if role == "assistant" else "overlap_user",
            review_model,
        )
        stem_transcripts[role] = candidate.model_dump(mode="json")
    details = {
        **record["details"],
        "stem_transcripts": stem_transcripts,
    }
    catalog.update_overlap_details(source_id, region_id, details)
    progress(1.0, "Recovered-stem transcripts are ready for comparison")
    return {
        "source_id": source_id,
        "region_id": region_id,
        "model": review_model,
        "stem_transcripts": stem_transcripts,
    }


def _overlap_id(source_id: str, version: int, start: int, end: int) -> str:
    digest = hashlib.sha256(
        f"{source_id}:{version}:{start}:{end}".encode()
    ).hexdigest()[:16]
    return f"overlap_{digest}"


def recover_source_overlaps(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    annotation = catalog.latest_annotation(source_id)
    if annotation.assistant_speaker is None:
        raise ValueError("Choose the Moshi speaker before recovering overlap")
    if not annotation.activities_finalized:
        raise ValueError("Finalize the human speaker regions before recovering overlap")
    overlaps = derived_overlaps(annotation.activities)
    if not overlaps:
        catalog.replace_overlap_recoveries(source_id, annotation.version, [])
        progress(1.0, "No overlap regions need recovery")
        return {"source_id": source_id, "regions": 0, "recovered": 0}
    output_root = (
        paths.source_root(source_id)
        / "recovery"
        / new_id(f"annotation_v{annotation.version}")
    )
    output_root.mkdir(parents=True, exist_ok=True)
    segments = _segments_from_annotation(annotation)
    config.separation.enabled = True
    progress(0.05, "Loading overlap separation models")
    separator = build_overlap_separator(
        paths.canonical_audio(source_id),
        segments,
        segments,
        annotation.assistant_speaker,
        config.separation,
        resolve_device(config.transcription.device),
    )
    records: list[dict[str, Any]] = []
    source = catalog.get_source(source_id)
    duration_samples = int(source["duration_samples"])
    for index, (start, end) in enumerate(overlaps):
        region_id = _overlap_id(source_id, annotation.version, start, end)
        base: dict[str, Any] = {
            "region_id": region_id,
            "start_sample": start,
            "end_sample": end,
            "status": "failed",
            "details": {},
        }
        progress(
            0.10 + 0.82 * index / max(1, len(overlaps)),
            f"Recovering overlap {index + 1} of {len(overlaps)}",
        )
        original_audio, original_rate = read_audio_segment(
            paths.canonical_audio(source_id),
            _seconds(start),
            _seconds(end),
            SAMPLE_RATE,
        )
        original_path = output_root / f"{region_id}_original.wav"
        write_pcm16(original_path, original_audio[:, :1], original_rate)
        base["original_path"] = paths.relative(original_path)
        if end - start > round(config.separation.max_window_seconds * SAMPLE_RATE):
            base["details"] = {"reason": "overlap_exceeds_maximum_window"}
            records.append(base)
            continue
        if separator is None:
            base["details"] = {"reason": "separation_backend_unavailable"}
            records.append(base)
            continue
        context = round(config.separation.context_seconds * SAMPLE_RATE)
        clip_start_sample = max(0, start - context)
        clip_end_sample = min(duration_samples, end + context)
        audio, sample_rate = read_audio_segment(
            paths.canonical_audio(source_id),
            _seconds(clip_start_sample),
            _seconds(clip_end_sample),
            SAMPLE_RATE,
        )
        mono = audio[:, 0]
        recovery = separator.recover_clip(
            mono,
            sample_rate,
            _seconds(clip_start_sample),
            _seconds(clip_end_sample),
        )
        core_first = start - clip_start_sample
        core_last = core_first + (end - start)
        coverage = float(recovery.mask[core_first:core_last].mean())
        assistant_path = output_root / f"{region_id}_assistant.wav"
        user_path = output_root / f"{region_id}_user.wav"
        if coverage >= 0.95:
            write_pcm16(
                assistant_path,
                recovery.assistant[core_first:core_last, None],
                sample_rate,
            )
            write_pcm16(
                user_path,
                recovery.user[core_first:core_last, None],
                sample_rate,
            )
            base.update(
                {
                    "status": "recovered",
                    "assistant_path": paths.relative(assistant_path),
                    "user_path": paths.relative(user_path),
                    "details": {"coverage": coverage, "failures": recovery.failures},
                }
            )
        else:
            base.update(
                {
                    "details": {"coverage": coverage, "failures": recovery.failures},
                }
            )
        records.append(base)
    catalog.replace_overlap_recoveries(source_id, annotation.version, records)
    catalog.update_source(source_id, clips_stale=True)
    release_model()
    progress(1.0, "Overlap recovery is ready for review")
    return {
        "source_id": source_id,
        "regions": len(records),
        "recovered": sum(value["status"] == "recovered" for value in records),
    }


def _approved_recovery(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    annotation: AnnotationDocument,
    clip_start: int,
    clip_end: int,
) -> OverlapRecovery | None:
    length = clip_end - clip_start
    assistant = np.zeros(length, dtype=np.float32)
    user = np.zeros(length, dtype=np.float32)
    mask = np.zeros(length, dtype=bool)
    result = OverlapRecovery(assistant, user, mask)
    for record in catalog.overlap_recoveries(source_id):
        if (
            record["annotation_version"] != annotation.version
            or record["status"] != "recovered"
            or record["decision"] != "approve"
            or not record["auditioned"]
            or record["end_sample"] <= clip_start
            or record["start_sample"] >= clip_end
        ):
            continue
        recovered_start = int(record["start_sample"])
        recovered_end = int(record["end_sample"])
        left = max(clip_start, recovered_start)
        right = min(clip_end, recovered_end)
        assistant_audio, _ = read_audio_segment(
            paths.resolve_relative(record["assistant_path"]),
            _seconds(left - recovered_start),
            _seconds(right - recovered_start),
            SAMPLE_RATE,
        )
        user_audio, _ = read_audio_segment(
            paths.resolve_relative(record["user_path"]),
            _seconds(left - recovered_start),
            _seconds(right - recovered_start),
            SAMPLE_RATE,
        )
        target_start = left - clip_start
        target_end = target_start + len(assistant_audio)
        assistant[target_start:target_end] = assistant_audio[:, 0]
        user[target_start:target_end] = user_audio[:, 0]
        mask[target_start:target_end] = True
        result.recovered_intervals.append((_seconds(left), _seconds(right)))
    return result if result.used else None


def render_source_clips(
    catalog: StudioCatalog,
    paths: StudioPaths,
    source_id: str,
    config: PipelineConfig,
    progress: Progress,
) -> dict[str, Any]:
    source = catalog.get_source(source_id)
    annotation = catalog.latest_annotation(source_id)
    plan = catalog.get_clip_plan(source_id)
    if plan is None or plan.annotation_version != annotation.version:
        raise ValueError("Create a current clip plan before generation")
    if not plan.feasible or any(clip.status != "valid" for clip in plan.clips):
        raise ValueError("Every clip must pass the 20–100 second conversation checks")
    if annotation.assistant_speaker is None:
        raise ValueError("Choose the Moshi speaker before generating clips")
    if not annotation.activities_finalized:
        raise ValueError("Finalize the human speaker regions before generating clips")
    if not annotation.aligned_words:
        raise ValueError("Align the Moshi transcript before generating clips")
    output_root = (
        paths.source_root(source_id)
        / "clips"
        / new_id(f"annotation_v{annotation.version}")
    )
    output_root.mkdir(parents=True, exist_ok=True)
    segments = _segments_from_annotation(annotation)
    words = [Word.from_dict(value) for value in annotation.aligned_words]
    artifacts: list[dict[str, Any]] = []
    for index, clip in enumerate(plan.clips):
        progress(
            0.05 + 0.90 * index / max(1, len(plan.clips)),
            f"Rendering clip {index + 1} of {len(plan.clips)}",
        )
        audio, sample_rate = read_audio_segment(
            paths.canonical_audio(source_id),
            _seconds(clip.start_sample),
            _seconds(clip.end_sample),
            SAMPLE_RATE,
        )
        recovered = _approved_recovery(
            catalog,
            paths,
            source_id,
            annotation,
            clip.start_sample,
            clip.end_sample,
        )
        rendered = render_stereo(
            audio[:, 0],
            sample_rate,
            _seconds(clip.start_sample),
            _seconds(clip.end_sample),
            segments,
            annotation.assistant_speaker,
            config.audio.fade_ms,
            segments,
            recovered,
        )
        for exclusion in annotation.exclusions:
            left = max(clip.start_sample, exclusion.start_sample)
            right = min(clip.end_sample, exclusion.end_sample)
            if right > left:
                rendered.stereo[left - clip.start_sample : right - clip.start_sample] = 0
                rendered.assistant_mask[
                    left - clip.start_sample : right - clip.start_sample
                ] = False
                rendered.user_mask[left - clip.start_sample : right - clip.start_sample] = False
        wav_path = output_root / f"{clip.id}.wav"
        json_path = output_root / f"{clip.id}.json"
        write_pcm16(wav_path, rendered.stereo, sample_rate)
        payload, transcript_report = build_moshi_payload(
            words,
            annotation.assistant_speaker,
            _seconds(clip.start_sample),
            _seconds(clip.end_sample),
            config.normalization,
        )
        atomic_write_json(json_path, payload)
        raw_overlap_ratio = float(clip.metrics.get("overlap_ratio", 0.0))
        overlap_samples = round(raw_overlap_ratio * len(rendered.stereo))
        separation_coverage = (
            min(1.0, float(recovered.mask.sum()) / overlap_samples)
            if recovered is not None and overlap_samples > 0
            else 0.0
        )
        qc = validate_clip(
            wav_path,
            json_path,
            config,
            assistant_mask=rendered.assistant_mask,
            user_mask=rendered.user_mask,
            overlap_ratio=raw_overlap_ratio,
            separation_used=bool(recovered and recovered.used),
            separation_coverage=separation_coverage,
            expected_duration=(clip.end_sample - clip.start_sample) / SAMPLE_RATE,
            total_words=len(payload["alignments"]),
        )
        if not payload["alignments"]:
            qc.status = QCStatus.REJECT
            qc.reasons = sorted({*qc.reasons, "missing_assistant_alignment"})
        artifacts.append(
            {
                "clip": clip.model_dump(mode="json"),
                "wav_path": paths.relative(wav_path),
                "json_path": paths.relative(json_path),
                "qc": qc.to_dict(),
                "transcript": transcript_report,
                "raw_overlap_ratio": raw_overlap_ratio,
                "separation_used": bool(recovered and recovered.used),
            }
        )
    manifest = {
        "source_id": source_id,
        "annotation_version": annotation.version,
        "source_name": source["original_name"],
        "artifacts": artifacts,
    }
    manifest_path = output_root / "clip_artifacts.json"
    atomic_write_json(manifest_path, manifest)
    catalog.update_source(
        source_id,
        status="clips_ready",
        clips_stale=False,
        clip_artifacts_path=paths.relative(manifest_path),
    )
    progress(1.0, "Clips are ready for listening review")
    return {"source_id": source_id, "clips": len(artifacts)}


def clip_artifacts(
    catalog: StudioCatalog, paths: StudioPaths, source_id: str
) -> dict[str, Any]:
    source = catalog.get_source(source_id)
    annotation = catalog.latest_annotation(source_id)
    stored_path = source.get("clip_artifacts_path")
    path = paths.resolve_relative(stored_path) if stored_path else None
    if source["clips_stale"] or path is None or not path.exists():
        return {
            "source_id": source_id,
            "annotation_version": annotation.version,
            "stale": True,
            "artifacts": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    value["stale"] = False
    decisions = catalog.clip_decisions(source_id)
    for artifact in value["artifacts"]:
        artifact["decision"] = decisions.get(artifact["clip"]["id"])
    return value
