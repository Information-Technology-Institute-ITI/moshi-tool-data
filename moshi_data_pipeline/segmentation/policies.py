from __future__ import annotations

from moshi_data_pipeline.config import SegmentationConfig
from moshi_data_pipeline.models import QCStatus, SpeakerSegment
from moshi_data_pipeline.speakers.overlap import (
    clipped_duration,
    interval_duration,
    overlap_intervals,
)


def evaluate_window(
    start: float,
    end: float,
    segments: list[SpeakerSegment],
    assistant_speaker: str,
    config: SegmentationConfig,
    overlap_segments: list[SpeakerSegment] | None = None,
) -> tuple[QCStatus, list[str], dict[str, float | int | str]]:
    duration = end - start
    relevant = [segment for segment in segments if segment.end > start and segment.start < end]
    speakers = sorted({segment.speaker for segment in relevant})
    speech_intervals = [(max(start, segment.start), min(end, segment.end)) for segment in relevant]
    speech_duration = interval_duration(speech_intervals)
    overlap_source = overlap_segments if overlap_segments is not None else segments
    overlap_duration = clipped_duration(overlap_intervals(overlap_source), start, end)
    assistant_duration = interval_duration(
        [
            (max(start, segment.start), min(end, segment.end))
            for segment in relevant
            if segment.speaker == assistant_speaker
        ]
    )
    user_duration = interval_duration(
        [
            (max(start, segment.start), min(end, segment.end))
            for segment in relevant
            if segment.speaker != assistant_speaker
        ]
    )
    voiced_speaker_total = assistant_duration + user_duration
    assistant_share = assistant_duration / voiced_speaker_total if voiced_speaker_total else 0.0
    user_share = user_duration / voiced_speaker_total if voiced_speaker_total else 0.0
    silence_ratio = max(0.0, 1.0 - speech_duration / duration) if duration else 1.0
    overlap_ratio = overlap_duration / duration if duration else 1.0
    reasons: list[str] = []
    status = QCStatus.PASS
    if duration < config.min_duration:
        reasons.append("clip_below_min_duration")
    if duration > config.max_duration + 1e-6:
        reasons.append("clip_above_max_duration")
    if len(speakers) > 2:
        reasons.append("more_than_two_active_speakers")
    if assistant_speaker not in speakers or len(speakers) < 2:
        reasons.append("clip_missing_required_speaker")
    if assistant_duration < config.min_speaker_duration:
        reasons.append("insufficient_assistant_speech")
    if user_duration < config.min_speaker_duration:
        reasons.append("insufficient_user_speech")
    if min(assistant_share, user_share) < config.min_speaker_share:
        reasons.append("insufficient_speaker_share")
    if silence_ratio > config.max_silence_ratio:
        reasons.append("excessive_silence")
    if overlap_ratio > config.max_overlap_ratio:
        reasons.append("excessive_overlap")
    elif overlap_ratio > config.overlap_warning_ratio:
        reasons.append("overlap_requires_review")
        status = QCStatus.REVIEW
    reject_reasons = {
        "clip_below_min_duration",
        "clip_above_max_duration",
        "more_than_two_active_speakers",
        "clip_missing_required_speaker",
        "insufficient_assistant_speech",
        "insufficient_user_speech",
        "insufficient_speaker_share",
        "excessive_silence",
        "excessive_overlap",
    }
    if reject_reasons.intersection(reasons):
        status = QCStatus.REJECT
    metrics: dict[str, float | int | str] = {
        "duration": duration,
        "speaker_count": len(speakers),
        "assistant_speech_duration": assistant_duration,
        "user_speech_duration": user_duration,
        "assistant_speech_share": assistant_share,
        "user_speech_share": user_share,
        "speech_duration": speech_duration,
        "silence_ratio": silence_ratio,
        "overlap_ratio": overlap_ratio,
        "music_detection": "not_run_no_reliable_detector_configured",
    }
    return status, reasons, metrics
