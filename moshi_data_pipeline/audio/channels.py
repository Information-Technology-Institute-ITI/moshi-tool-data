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
        left[recovered] = recovered_overlap.assistant[recovered]
        right[recovered] = recovered_overlap.user[recovered]
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
    )
