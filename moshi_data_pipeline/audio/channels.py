from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from moshi_data_pipeline.audio.io import read_audio_segment
from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.speakers.overlap import overlap_intervals
from moshi_data_pipeline.speakers.separation import OverlapRecovery


@dataclass(slots=True)
class RenderResult:
    stereo: np.ndarray
    assistant_mask: np.ndarray
    user_mask: np.ndarray
    overlap_mask: np.ndarray
    sample_rate: int
    routing_method: str = "diarization_masked_mono"


def _interval_mask(
    length: int,
    sample_rate: int,
    clip_start: float,
    clip_end: float,
    segments: list[SpeakerSegment],
    predicate,
) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    for segment in segments:
        if not predicate(segment):
            continue
        start = max(segment.start, clip_start)
        end = min(segment.end, clip_end)
        if end <= start:
            continue
        first = max(0, round((start - clip_start) * sample_rate))
        last = min(length, round((end - clip_start) * sample_rate))
        mask[first:last] = True
    return mask


def _apply_boundary_fades(signal: np.ndarray, mask: np.ndarray, fade_samples: int) -> None:
    if fade_samples <= 0 or not mask.any():
        return
    padded = np.pad(mask.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    for start, end in zip(starts, ends, strict=True):
        run_length = end - start
        width = min(fade_samples, run_length // 2)
        if width <= 0:
            continue
        signal[start : start + width] *= np.linspace(0.0, 1.0, width, endpoint=True)
        signal[end - width : end] *= np.linspace(1.0, 0.0, width, endpoint=True)


def _insert_recovered_with_seams(
    target: np.ndarray,
    replacement: np.ndarray,
    recovered_mask: np.ndarray,
    active_mask: np.ndarray,
    reference: np.ndarray,
    seam_samples: int,
) -> None:
    padded = np.pad(recovered_mask.astype(np.int8), (1, 1))
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1)
    for start, end in zip(starts, ends, strict=True):
        values = replacement[start:end].copy()
        width = min(max(0, seam_samples), len(values) // 2)
        if width:
            ramp = np.linspace(0.0, 1.0, width, endpoint=True, dtype=np.float32)
            start_base = (
                reference[start : start + width]
                if start > 0 and active_mask[start - 1]
                else np.zeros(width, dtype=np.float32)
            )
            end_base = (
                reference[end - width : end]
                if end < len(active_mask) and active_mask[end]
                else np.zeros(width, dtype=np.float32)
            )
            values[:width] = start_base * (1.0 - ramp) + values[:width] * ramp
            values[-width:] = values[-width:] * (1.0 - ramp) + end_base * ramp
        target[start:end] = values


def render_stereo(
    mono: np.ndarray,
    sample_rate: int,
    clip_start: float,
    clip_end: float,
    segments: list[SpeakerSegment],
    assistant_speaker: str,
    fade_ms: float = 10.0,
    overlap_segments: list[SpeakerSegment] | None = None,
    recovered_overlap: OverlapRecovery | None = None,
    recovery_seam_ms: float = 20.0,
) -> RenderResult:
    if mono.ndim == 2:
        if mono.shape[1] != 1:
            raise ValueError("render_stereo expects mono input")
        mono = mono[:, 0]
    if mono.ndim != 1:
        raise ValueError("render_stereo expects a one-dimensional waveform")
    assistant_mask = _interval_mask(
        len(mono),
        sample_rate,
        clip_start,
        clip_end,
        segments,
        lambda segment: segment.speaker == assistant_speaker,
    )
    user_mask = _interval_mask(
        len(mono),
        sample_rate,
        clip_start,
        clip_end,
        segments,
        lambda segment: segment.speaker != assistant_speaker,
    )
    if overlap_segments is None:
        overlap_mask = assistant_mask & user_mask
    else:
        intervals = [
            SpeakerSegment(start, end, "__OVERLAP__")
            for start, end in overlap_intervals(overlap_segments)
        ]
        overlap_mask = _interval_mask(
            len(mono),
            sample_rate,
            clip_start,
            clip_end,
            intervals,
            lambda _: True,
        )
    # Mixed overlap cannot be separated by diarization masks. Keep it out of both channels.
    assistant_mask &= ~overlap_mask
    user_mask &= ~overlap_mask
    left = np.where(assistant_mask, mono, 0.0).astype(np.float32, copy=False)
    right = np.where(user_mask, mono, 0.0).astype(np.float32, copy=False)
    if recovered_overlap is not None and recovered_overlap.used:
        if len(recovered_overlap.mask) != len(mono):
            raise ValueError("Recovered overlap length does not match the rendered clip")
        recovered = recovered_overlap.mask
        seam_samples = round(sample_rate * recovery_seam_ms / 1000.0)
        _insert_recovered_with_seams(
            left,
            recovered_overlap.assistant,
            recovered,
            assistant_mask,
            mono,
            seam_samples,
        )
        _insert_recovered_with_seams(
            right,
            recovered_overlap.user,
            recovered,
            user_mask,
            mono,
            seam_samples,
        )
        assistant_mask |= recovered
        user_mask |= recovered
        overlap_mask &= ~recovered
    fade_samples = round(sample_rate * fade_ms / 1000.0)
    _apply_boundary_fades(left, assistant_mask, fade_samples)
    _apply_boundary_fades(right, user_mask, fade_samples)
    return RenderResult(
        stereo=np.column_stack((left, right)),
        assistant_mask=assistant_mask,
        user_mask=user_mask,
        overlap_mask=overlap_mask,
        sample_rate=sample_rate,
        routing_method="diarization_masked_mono",
    )


def render_independent_stereo(
    source_channels: np.ndarray,
    sample_rate: int,
    clip_start: float,
    clip_end: float,
    segments: list[SpeakerSegment],
    assistant_speaker: str,
    speaker_channel_map: dict[str, int],
    fade_ms: float = 10.0,
    overlap_segments: list[SpeakerSegment] | None = None,
    recovered_overlap: OverlapRecovery | None = None,
    recovery_seam_ms: float = 20.0,
) -> RenderResult:
    if source_channels.ndim != 2 or source_channels.shape[1] != 2:
        raise ValueError("Independent channel routing requires stereo input")
    speakers = {assistant_speaker, *[segment.speaker for segment in segments]}
    if set(speaker_channel_map) != speakers or set(speaker_channel_map.values()) != {0, 1}:
        raise ValueError("Independent routing requires a one-to-one speaker/channel map")
    user_speaker = next(speaker for speaker in speakers if speaker != assistant_speaker)
    assistant_source = source_channels[:, speaker_channel_map[assistant_speaker]]
    user_source = source_channels[:, speaker_channel_map[user_speaker]]
    assistant_mask = _interval_mask(
        len(source_channels),
        sample_rate,
        clip_start,
        clip_end,
        segments,
        lambda segment: segment.speaker == assistant_speaker,
    )
    user_mask = _interval_mask(
        len(source_channels),
        sample_rate,
        clip_start,
        clip_end,
        segments,
        lambda segment: segment.speaker != assistant_speaker,
    )
    if overlap_segments is None:
        overlap_mask = assistant_mask & user_mask
    else:
        overlap_values = [
            SpeakerSegment(start, end, "__OVERLAP__")
            for start, end in overlap_intervals(overlap_segments)
        ]
        overlap_mask = _interval_mask(
            len(source_channels),
            sample_rate,
            clip_start,
            clip_end,
            overlap_values,
            lambda _: True,
        )
    assistant_mask &= ~overlap_mask
    user_mask &= ~overlap_mask
    left = np.where(assistant_mask, assistant_source, 0.0).astype(np.float32, copy=False)
    right = np.where(user_mask, user_source, 0.0).astype(np.float32, copy=False)
    if recovered_overlap is not None and recovered_overlap.used:
        if len(recovered_overlap.mask) != len(source_channels):
            raise ValueError("Recovered overlap length does not match the routed clip")
        recovered = recovered_overlap.mask
        seam_samples = round(sample_rate * recovery_seam_ms / 1000.0)
        _insert_recovered_with_seams(
            left,
            recovered_overlap.assistant,
            recovered,
            assistant_mask,
            assistant_source,
            seam_samples,
        )
        _insert_recovered_with_seams(
            right,
            recovered_overlap.user,
            recovered,
            user_mask,
            user_source,
            seam_samples,
        )
        assistant_mask |= recovered
        user_mask |= recovered
        overlap_mask &= ~recovered
    fade_samples = round(sample_rate * fade_ms / 1000.0)
    _apply_boundary_fades(left, assistant_mask, fade_samples)
    _apply_boundary_fades(right, user_mask, fade_samples)
    return RenderResult(
        stereo=np.column_stack((left, right)),
        assistant_mask=assistant_mask,
        user_mask=user_mask,
        overlap_mask=overlap_mask,
        sample_rate=sample_rate,
        routing_method="verified_independent_stereo",
    )


def render_stereo_from_file(
    source: Path,
    clip_start: float,
    clip_end: float,
    segments: list[SpeakerSegment],
    assistant_speaker: str,
    fade_ms: float = 10.0,
    sample_rate: int = 24_000,
    overlap_segments: list[SpeakerSegment] | None = None,
    recovered_overlap: OverlapRecovery | None = None,
    recovery_seam_ms: float = 20.0,
) -> RenderResult:
    mono, actual_rate = read_audio_segment(source, clip_start, clip_end, sample_rate)
    return render_stereo(
        mono,
        actual_rate,
        clip_start,
        clip_end,
        segments,
        assistant_speaker,
        fade_ms,
        overlap_segments,
        recovered_overlap,
        recovery_seam_ms,
    )
