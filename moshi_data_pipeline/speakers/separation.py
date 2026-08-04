from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
from scipy.signal import resample_poly

from moshi_data_pipeline.audio.io import read_audio
from moshi_data_pipeline.config import SeparationConfig
from moshi_data_pipeline.models import SpeakerSegment
from moshi_data_pipeline.speakers.overlap import merge_intervals, overlap_intervals

LOGGER = logging.getLogger(__name__)


class SourceSeparator(Protocol):
    def separate(self, audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
        """Return two isolated sources; outputs always require manual review."""


@dataclass(slots=True)
class OverlapRecovery:
    assistant: np.ndarray
    user: np.ndarray
    mask: np.ndarray
    recovered_intervals: list[tuple[float, float]] = field(default_factory=list)
    failures: list[dict[str, float | str]] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.recovered_intervals)


def _cosine(first: np.ndarray, second: np.ndarray) -> float:
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return float(np.dot(first, second) / denominator) if denominator else -1.0


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    divisor = int(np.gcd(source_rate, target_rate))
    return resample_poly(audio, target_rate // divisor, source_rate // divisor).astype(np.float32)


def _subtract_intervals(
    start: float, end: float, excluded: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    remaining = [(start, end)]
    for cut_start, cut_end in excluded:
        updated: list[tuple[float, float]] = []
        for first, last in remaining:
            if cut_end <= first or cut_start >= last:
                updated.append((first, last))
                continue
            if cut_start > first:
                updated.append((first, min(last, cut_start)))
            if cut_end < last:
                updated.append((max(first, cut_end), last))
        remaining = updated
    return [(first, last) for first, last in remaining if last > first]


class SpeechBrainOverlapSeparator:
    def __init__(
        self,
        audio_path: Path,
        segments: list[SpeakerSegment],
        overlap_segments: list[SpeakerSegment],
        assistant_speaker: str,
        config: SeparationConfig,
        device: str,
    ):
        try:
            import torch
            from speechbrain.inference.classifiers import EncoderClassifier
            from speechbrain.inference.separation import SepformerSeparation
        except ImportError as exc:
            raise RuntimeError(
                'Overlap separation requires the optional dependency: pip install -e ".[separation]"'
            ) from exc

        self.config = config
        self.device = "cuda:0" if device == "cuda" else device
        self.torch = torch
        self.separator = SepformerSeparation.from_hparams(
            source=config.model,
            run_opts={"device": self.device},
        )
        self.encoder = EncoderClassifier.from_hparams(
            source=config.embedding_model,
            run_opts={"device": self.device},
        )
        self.assistant_speaker = assistant_speaker
        audio, self.sample_rate = read_audio(audio_path)
        self.full_audio = audio[:, 0]
        self.overlaps = overlap_intervals(overlap_segments)
        self.prototypes = self._build_prototypes(segments)

    def _embedding(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        resampled = _resample(audio, sample_rate, self.config.sample_rate)
        tensor = self.torch.from_numpy(resampled).to(self.device).unsqueeze(0)
        with self.torch.inference_mode():
            encoded = self.encoder.encode_batch(tensor)
        return encoded.detach().float().cpu().numpy().reshape(-1)

    def _build_prototypes(self, segments: list[SpeakerSegment]) -> dict[str, np.ndarray]:
        by_speaker: dict[str, list[np.ndarray]] = {}
        overlap_padding = [
            (max(0.0, start - 0.25), end + 0.25) for start, end in self.overlaps
        ]
        collected: dict[str, float] = {}
        for segment in segments:
            if collected.get(segment.speaker, 0.0) >= self.config.enrollment_seconds:
                continue
            for start, end in _subtract_intervals(
                segment.start, segment.end, overlap_padding
            ):
                duration = end - start
                if duration < self.config.minimum_enrollment_turn:
                    continue
                remaining = self.config.enrollment_seconds - collected.get(segment.speaker, 0.0)
                end = min(end, start + remaining)
                first = round(start * self.sample_rate)
                last = round(end * self.sample_rate)
                by_speaker.setdefault(segment.speaker, []).append(self.full_audio[first:last])
                collected[segment.speaker] = collected.get(segment.speaker, 0.0) + end - start
        prototypes = {
            speaker: self._embedding(np.concatenate(chunks), self.sample_rate)
            for speaker, chunks in by_speaker.items()
            if chunks
        }
        if len(prototypes) != 2:
            raise RuntimeError("Could not build clean enrollment audio for both speakers")
        return prototypes

    def _separate(self, audio: np.ndarray, sample_rate: int) -> list[np.ndarray]:
        resampled = _resample(audio, sample_rate, self.config.sample_rate)
        tensor = self.torch.from_numpy(resampled).to(self.device).unsqueeze(0)
        with self.torch.inference_mode():
            separated = self.separator.separate_batch(tensor)
        values = separated.detach().float().cpu().numpy()
        if values.ndim != 3:
            raise RuntimeError(f"Unexpected separator output shape {values.shape}")
        if values.shape[0] == 1:
            values = values[0]
        if values.shape[-1] == 2:
            stems = [values[:, 0], values[:, 1]]
        elif values.shape[0] == 2:
            stems = [values[0], values[1]]
        else:
            raise RuntimeError(f"Separator did not return two sources: {values.shape}")
        return [_resample(stem, self.config.sample_rate, sample_rate) for stem in stems]

    def _assign_stems(
        self, stems: list[np.ndarray], sample_rate: int
    ) -> tuple[np.ndarray, np.ndarray] | None:
        speakers = sorted(self.prototypes)
        embeddings = [self._embedding(stem, sample_rate) for stem in stems]
        similarities = np.asarray(
            [
                [_cosine(embedding, self.prototypes[speaker]) for speaker in speakers]
                for embedding in embeddings
            ]
        )
        direct = similarities[0, 0] + similarities[1, 1]
        crossed = similarities[0, 1] + similarities[1, 0]
        assignment = (0, 1) if direct >= crossed else (1, 0)
        for stem_index, speaker_index in enumerate(assignment):
            matched = similarities[stem_index, speaker_index]
            other = similarities[stem_index, 1 - speaker_index]
            if (
                matched < self.config.minimum_identity_similarity
                or matched - other < self.config.minimum_identity_margin
            ):
                return None
        stems_by_speaker = {
            speakers[speaker_index]: stems[stem_index]
            for stem_index, speaker_index in enumerate(assignment)
        }
        return (
            stems_by_speaker[self.assistant_speaker],
            next(
                stem
                for speaker, stem in stems_by_speaker.items()
                if speaker != self.assistant_speaker
            ),
        )

    def recover_clip(
        self,
        mono: np.ndarray,
        sample_rate: int,
        clip_start: float,
        clip_end: float,
    ) -> OverlapRecovery:
        length = len(mono)
        assistant = np.zeros(length, dtype=np.float32)
        user = np.zeros(length, dtype=np.float32)
        mask = np.zeros(length, dtype=bool)
        result = OverlapRecovery(assistant, user, mask)
        core_limit = max(
            0.5, self.config.max_window_seconds - 2 * self.config.context_seconds
        )
        clipped = [
            (max(clip_start, start), min(clip_end, end))
            for start, end in self.overlaps
            if end > clip_start and start < clip_end
        ]
        cores: list[tuple[float, float]] = []
        for start, end in merge_intervals(clipped):
            cursor = start
            while cursor < end:
                core_end = min(end, cursor + core_limit)
                cores.append((cursor, core_end))
                cursor = core_end
        for core_start, core_end in cores:
            window_start = max(clip_start, core_start - self.config.context_seconds)
            window_end = min(clip_end, core_end + self.config.context_seconds)
            first = round((window_start - clip_start) * sample_rate)
            last = round((window_end - clip_start) * sample_rate)
            try:
                stems = self._separate(mono[first:last], sample_rate)
                assigned = self._assign_stems(stems, sample_rate)
            except Exception as exc:
                LOGGER.warning(
                    "Overlap separation failed for %.2f-%.2fs: %s",
                    core_start,
                    core_end,
                    exc,
                )
                assigned = None
            if assigned is None:
                result.failures.append(
                    {
                        "start": core_start,
                        "end": core_end,
                        "reason": "ambiguous_or_failed_identity",
                    }
                )
                continue
            assistant_stem, user_stem = assigned
            core_first_in_window = round((core_start - window_start) * sample_rate)
            core_length = round((core_end - core_start) * sample_rate)
            output_first = round((core_start - clip_start) * sample_rate)
            output_last = min(length, output_first + core_length)
            actual_length = output_last - output_first
            assistant[output_first:output_last] = assistant_stem[
                core_first_in_window : core_first_in_window + actual_length
            ]
            user[output_first:output_last] = user_stem[
                core_first_in_window : core_first_in_window + actual_length
            ]
            mask[output_first:output_last] = True
            result.recovered_intervals.append((core_start, core_end))
        return result


def build_overlap_separator(
    audio_path: Path,
    segments: list[SpeakerSegment],
    overlap_segments: list[SpeakerSegment],
    assistant_speaker: str,
    config: SeparationConfig,
    device: str,
) -> SpeechBrainOverlapSeparator | None:
    if not config.enabled:
        return None
    try:
        return SpeechBrainOverlapSeparator(
            audio_path,
            segments,
            overlap_segments,
            assistant_speaker,
            config,
            device,
        )
    except Exception as exc:
        LOGGER.warning("Overlap separation disabled for this file: %s", exc)
        return None


EXPERIMENTAL_SEPARATION_IMPLEMENTED = True
