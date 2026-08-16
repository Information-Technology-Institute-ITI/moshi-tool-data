from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import correlate, correlation_lags

from moshi_data_pipeline.config import ChannelRoutingConfig
from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.speakers.overlap import overlap_intervals

EPSILON = 1e-12


def _db_amplitude(value: float) -> float:
    return 20.0 * float(np.log10(max(EPSILON, value)))


def _db_power_ratio(first: float, second: float) -> float:
    return 10.0 * float(np.log10((first + EPSILON) / (second + EPSILON)))


def _analysis_sample(path: Path, maximum_seconds: float) -> tuple[np.ndarray, int]:
    with sf.SoundFile(path) as stream:
        sample_rate = int(stream.samplerate)
        frames = int(stream.frames)
        channels = int(stream.channels)
        maximum_frames = max(1, round(maximum_seconds * sample_rate))
        if frames <= maximum_frames:
            return stream.read(dtype="float32", always_2d=True), sample_rate
        chunk_count = 10
        chunk_frames = max(1, maximum_frames // chunk_count)
        starts = np.linspace(0, max(0, frames - chunk_frames), chunk_count, dtype=np.int64)
        chunks: list[np.ndarray] = []
        for start in starts:
            stream.seek(int(start))
            chunks.append(
                stream.read(chunk_frames, dtype="float32", always_2d=True)
            )
        if not chunks:
            return np.empty((0, channels), dtype=np.float32), sample_rate
        return np.concatenate(chunks), sample_rate


def analyze_channel_audio(
    audio: np.ndarray,
    sample_rate: int,
    config: ChannelRoutingConfig,
) -> dict[str, Any]:
    if audio.ndim != 2:
        raise ValueError("Channel analysis expects [samples, channels] audio")
    channels = int(audio.shape[1])
    report: dict[str, Any] = {
        "channel_count": channels,
        "routing_candidate": False,
        "recommended_mode": "mono",
        "requires_human_confirmation": True,
    }
    if channels != 2 or not len(audio):
        report["reason"] = (
            "channel_first_routing_currently_requires_stereo"
            if channels != 2
            else "empty_audio"
        )
        return report

    values = np.asarray(audio, dtype=np.float64)
    channel_rms = np.sqrt(np.mean(values * values, axis=0))
    difference_rms = float(np.sqrt(np.mean(np.square(values[:, 0] - values[:, 1]))))
    program_rms = float(np.sqrt(np.mean(np.square(values))))
    relative_difference_db = _db_amplitude(difference_rms / max(EPSILON, program_rms))
    centered = values - np.mean(values, axis=0, keepdims=True)
    denominator = float(np.linalg.norm(centered[:, 0]) * np.linalg.norm(centered[:, 1]))
    correlation = (
        float(np.dot(centered[:, 0], centered[:, 1]) / denominator)
        if denominator
        else 1.0
    )

    window = max(1, round(0.25 * sample_rate))
    complete = len(values) // window * window
    if complete:
        framed = values[:complete].reshape(-1, window, 2)
        framed_rms = np.sqrt(np.mean(framed * framed, axis=1))
        active_threshold = 10.0 ** (config.activity_threshold_db / 20.0)
        active = np.max(framed_rms, axis=1) >= active_threshold
        dominance = 20.0 * np.log10(
            (framed_rms[:, 0] + EPSILON) / (framed_rms[:, 1] + EPSILON)
        )
        active_count = max(1, int(active.sum()))
        left_fraction = float(np.logical_and(active, dominance >= config.dominance_db).sum()) / active_count
        right_fraction = float(np.logical_and(active, dominance <= -config.dominance_db).sum()) / active_count
        active_fraction = float(active.mean())
    else:
        left_fraction = right_fraction = active_fraction = 0.0

    target_rate = min(sample_rate, 1_000)
    stride = max(1, sample_rate // target_rate)
    lag_values = centered[::stride]
    lag_limit = round(0.05 * target_rate)
    if len(lag_values) > 1 and lag_limit > 0:
        cross = correlate(lag_values[:, 0], lag_values[:, 1], mode="full", method="fft")
        lags = correlation_lags(len(lag_values), len(lag_values), mode="full")
        allowed = np.abs(lags) <= lag_limit
        selected = int(np.argmax(np.abs(cross[allowed])))
        allowed_lags = lags[allowed]
        allowed_cross = cross[allowed]
        lag = int(allowed_lags[selected])
        lag_denominator = float(
            np.linalg.norm(lag_values[:, 0]) * np.linalg.norm(lag_values[:, 1])
        )
        lag_correlation = (
            float(allowed_cross[selected] / lag_denominator)
            if lag_denominator
            else 0.0
        )
        lag_samples = round(lag * sample_rate / target_rate)
    else:
        lag_samples = 0
        lag_correlation = 0.0

    dual_mono = relative_difference_db <= -30.0
    candidate = (
        not dual_mono
        and abs(correlation) <= config.maximum_absolute_correlation
        and left_fraction >= config.minimum_dominant_fraction
        and right_fraction >= config.minimum_dominant_fraction
    )
    if config.mode == "mono":
        candidate = False
    report.update(
        {
            "channel_rms_db": [_db_amplitude(float(value)) for value in channel_rms],
            "channel_difference_relative_db": relative_difference_db,
            "absolute_correlation": abs(correlation),
            "correlation": correlation,
            "estimated_lag_samples": lag_samples,
            "estimated_lag_ms": 1000.0 * lag_samples / sample_rate,
            "lag_correlation": lag_correlation,
            "active_window_fraction": active_fraction,
            "left_dominant_fraction": left_fraction,
            "right_dominant_fraction": right_fraction,
            "dual_mono": dual_mono,
            "routing_candidate": candidate,
            "recommended_mode": "independent_stereo" if candidate else "mono",
            "reason": (
                "channel_first_routing_disabled_by_configuration"
                if config.mode == "mono"
                else "both_channels_show_independent_speech_dominance"
                if candidate
                else "channel_evidence_is_not_strong_enough"
            ),
        }
    )
    return report


def analyze_channel_file(path: Path, config: ChannelRoutingConfig) -> dict[str, Any]:
    audio, sample_rate = _analysis_sample(path, config.analysis_seconds)
    report = analyze_channel_audio(audio, sample_rate, config)
    report["sample_rate"] = sample_rate
    report["analysis_seconds"] = len(audio) / sample_rate if sample_rate else 0.0
    return report


def _subtract_intervals(
    start: float,
    end: float,
    excluded: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    remaining = [(start, end)]
    for cut_start, cut_end in excluded:
        updated: list[tuple[float, float]] = []
        for first, last in remaining:
            if cut_end <= first or cut_start >= last:
                updated.append((first, last))
            else:
                if cut_start > first:
                    updated.append((first, min(last, cut_start)))
                if cut_end < last:
                    updated.append((max(first, cut_end), last))
        remaining = updated
    return [(first, last) for first, last in remaining if last > first]


def infer_speaker_channel_mapping(
    audio: np.ndarray,
    sample_rate: int,
    segments: list[SpeakerSegment],
    config: ChannelRoutingConfig,
) -> tuple[dict[str, int], dict[str, Any]]:
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("Speaker/channel mapping requires stereo audio")
    speakers = sorted({segment.speaker for segment in segments})
    if len(speakers) != 2:
        raise ValueError("Speaker/channel mapping requires exactly two speakers")
    overlaps = overlap_intervals(segments)
    energy = {speaker: np.zeros(2, dtype=np.float64) for speaker in speakers}
    samples = dict.fromkeys(speakers, 0)
    for segment in segments:
        for start, end in _subtract_intervals(segment.start, segment.end, overlaps):
            first = max(0, round(start * sample_rate))
            last = min(len(audio), round(end * sample_rate))
            if last <= first:
                continue
            values = np.asarray(audio[first:last], dtype=np.float64)
            energy[segment.speaker] += np.sum(values * values, axis=0)
            samples[segment.speaker] += last - first
    if any(samples[speaker] == 0 for speaker in speakers):
        raise ValueError("Not enough non-overlapping speech to map speakers to channels")

    return _mapping_from_energy(speakers, energy, samples, config)


def _mapping_from_energy(
    speakers: list[str],
    energy: dict[str, np.ndarray],
    samples: dict[str, int],
    config: ChannelRoutingConfig,
) -> tuple[dict[str, int], dict[str, Any]]:

    direct = energy[speakers[0]][0] + energy[speakers[1]][1]
    crossed = energy[speakers[0]][1] + energy[speakers[1]][0]
    mapping = (
        {speakers[0]: 0, speakers[1]: 1}
        if direct >= crossed
        else {speakers[0]: 1, speakers[1]: 0}
    )
    margins = {
        speaker: _db_power_ratio(
            float(energy[speaker][channel]),
            float(energy[speaker][1 - channel]),
        )
        for speaker, channel in mapping.items()
    }
    if any(value < config.mapping_minimum_margin_db for value in margins.values()):
        raise ValueError(
            "Channel identity is ambiguous; confirm the A/B channel mapping manually"
        )
    return mapping, {
        "mapping": mapping,
        "speaker_channel_margin_db": margins,
        "nonoverlap_samples": samples,
        "energy": {
            speaker: [float(value) for value in values]
            for speaker, values in energy.items()
        },
    }


def infer_speaker_channel_mapping_file(
    path: Path,
    segments: list[SpeakerSegment],
    config: ChannelRoutingConfig,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Infer an A/B channel suggestion without loading a whole podcast into RAM."""
    speakers = sorted({segment.speaker for segment in segments})
    if len(speakers) != 2:
        raise ValueError("Speaker/channel mapping requires exactly two speakers")
    overlaps = overlap_intervals(segments)
    energy = {speaker: np.zeros(2, dtype=np.float64) for speaker in speakers}
    samples = dict.fromkeys(speakers, 0)
    with sf.SoundFile(path) as stream:
        if int(stream.channels) != 2:
            raise ValueError("Speaker/channel mapping requires stereo audio")
        sample_rate = int(stream.samplerate)
        maximum_per_speaker = round(config.analysis_seconds * sample_rate / 2.0)
        for segment in segments:
            remaining = maximum_per_speaker - samples[segment.speaker]
            if remaining <= 0:
                continue
            for start, end in _subtract_intervals(segment.start, segment.end, overlaps):
                first = max(0, round(start * sample_rate))
                last = min(int(stream.frames), round(end * sample_rate), first + remaining)
                if last <= first:
                    continue
                stream.seek(first)
                values = stream.read(last - first, dtype="float32", always_2d=True)
                values64 = np.asarray(values, dtype=np.float64)
                energy[segment.speaker] += np.sum(values64 * values64, axis=0)
                samples[segment.speaker] += len(values)
                remaining -= len(values)
                if remaining <= 0:
                    break
    if any(samples[speaker] == 0 for speaker in speakers):
        raise ValueError("Not enough non-overlapping speech to map speakers to channels")
    return _mapping_from_energy(speakers, energy, samples, config)
