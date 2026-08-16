from __future__ import annotations

from collections.abc import Callable

from moshi_data_pipeline.config import SegmentationConfig
from moshi_data_pipeline.models import ClipPlan, SpeakerSegment
from moshi_data_pipeline.segmentation.policies import evaluate_window


def segment_conversation(
    segments: list[SpeakerSegment],
    assistant_speaker: str,
    source_duration: float,
    config: SegmentationConfig,
    clip_id_factory: Callable[[], str],
    overlap_segments: list[SpeakerSegment] | None = None,
) -> list[ClipPlan]:
    if not segments:
        return []
    first_speech = max(0.0, min(segment.start for segment in segments) - config.context_before)
    last_speech = min(
        source_duration, max(segment.end for segment in segments) + config.context_after
    )
    boundaries = sorted(
        {
            min(source_duration, max(first_speech, segment.end + config.context_after))
            for segment in segments
        }
    )
    plans: list[ClipPlan] = []
    cursor = first_speech
    while cursor < last_speech - 1e-6:
        candidates = [
            boundary
            for boundary in boundaries
            if config.min_duration <= boundary - cursor <= config.max_duration
        ]
        if candidates:
            evaluated = [
                (
                    boundary,
                    *evaluate_window(
                        cursor,
                        boundary,
                        segments,
                        assistant_speaker,
                        config,
                        overlap_segments,
                    ),
                )
                for boundary in candidates
            ]

            def candidate_score(value, current_cursor=cursor):
                boundary, status, _, metrics = value
                status_rank = {
                    "PASS": 0,
                    "REVIEW": 1,
                    "REJECT": 2,
                }[status.value]
                minimum_share = min(
                    float(metrics["assistant_speech_share"]),
                    float(metrics["user_speech_share"]),
                )
                return (
                    status_rank,
                    float(metrics["overlap_ratio"]),
                    abs(boundary - current_cursor - config.target_duration),
                    -minimum_share,
                    boundary,
                )

            end = min(evaluated, key=candidate_score)[0]
        else:
            end = min(last_speech, cursor + config.max_duration)
        if last_speech - end < config.min_duration and last_speech - cursor <= config.max_duration:
            end = last_speech
        if end <= cursor:
            break
        status, reasons, metrics = evaluate_window(
            cursor, end, segments, assistant_speaker, config, overlap_segments
        )
        plans.append(
            ClipPlan(
                clip_id=clip_id_factory(),
                start=cursor,
                end=end,
                status=status,
                reasons=reasons,
                metrics=metrics,
            )
        )
        cursor = end
    return plans
