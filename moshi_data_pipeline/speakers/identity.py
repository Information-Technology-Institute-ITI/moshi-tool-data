from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import resample_poly

from moshi_data_pipeline.audio.io import read_audio
from moshi_data_pipeline.config import SeparationConfig
from moshi_data_pipeline.models import SpeakerSegment

SAMPLE_RATE = 24_000


def _cosine(first: np.ndarray, second: np.ndarray) -> float:
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return float(np.dot(first, second) / denominator) if denominator else -1.0


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    divisor = int(np.gcd(source_rate, target_rate))
    return resample_poly(
        audio,
        target_rate // divisor,
        source_rate // divisor,
    ).astype(np.float32)


def choose_identity_mapping(
    similarities: dict[str, dict[str, float]],
    minimum_similarity: float,
    minimum_margin: float,
) -> dict[str, str]:
    detected = sorted(similarities)
    if len(detected) != 2 or any(set(similarities[label]) != {"A", "B"} for label in detected):
        raise ValueError("Identity matching requires exactly two detected and two reference speakers")
    direct = similarities[detected[0]]["A"] + similarities[detected[1]]["B"]
    crossed = similarities[detected[0]]["B"] + similarities[detected[1]]["A"]
    mapping = (
        {detected[0]: "A", detected[1]: "B"}
        if direct >= crossed
        else {detected[0]: "B", detected[1]: "A"}
    )
    for label, speaker in mapping.items():
        other = "B" if speaker == "A" else "A"
        matched = similarities[label][speaker]
        margin = matched - similarities[label][other]
        if matched < minimum_similarity or margin < minimum_margin:
            raise ValueError(
                f"Speaker identity is ambiguous for {label}: "
                f"similarity={matched:.3f}, margin={margin:.3f}"
            )
    return mapping


class SpeakerReferenceMatcher:
    def __init__(
        self,
        audio_path: Path,
        references: list[Any],
        config: SeparationConfig,
        device: str,
    ):
        by_speaker = {reference.speaker: reference for reference in references}
        if set(by_speaker) != {"A", "B"}:
            raise ValueError("Confirm one clean reference region for Speaker A and Speaker B")
        try:
            import torch
            from speechbrain.inference.classifiers import EncoderClassifier
        except ImportError as exc:
            raise RuntimeError(
                'Stable speaker identity requires: pip install -e ".[separation]"'
            ) from exc
        self.config = config
        self.torch = torch
        self.device = "cuda:0" if device == "cuda" else device
        self.encoder = EncoderClassifier.from_hparams(
            source=config.embedding_model,
            run_opts={"device": self.device},
        )
        audio, self.sample_rate = read_audio(audio_path)
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError(f"Expected {SAMPLE_RATE} Hz canonical audio")
        self.audio = audio[:, 0]
        self.reference_embeddings = {
            speaker: self._embedding(
                self.audio[reference.start_sample : reference.end_sample]
            )
            for speaker, reference in by_speaker.items()
        }

    def _embedding(self, audio: np.ndarray) -> np.ndarray:
        if len(audio) < round(self.config.minimum_enrollment_turn * self.sample_rate):
            raise ValueError("Speaker reference is too short for stable identity matching")
        resampled = _resample(audio, self.sample_rate, self.config.sample_rate)
        tensor = self.torch.from_numpy(resampled).to(self.device).unsqueeze(0)
        with self.torch.inference_mode():
            encoded = self.encoder.encode_batch(tensor)
        return encoded.detach().float().cpu().numpy().reshape(-1)

    def match(
        self, detected_segments: list[SpeakerSegment]
    ) -> tuple[dict[str, str], dict[str, Any]]:
        chunks: dict[str, list[np.ndarray]] = {}
        collected: dict[str, float] = {}
        for segment in detected_segments:
            if collected.get(segment.speaker, 0.0) >= self.config.enrollment_seconds:
                continue
            duration = segment.end - segment.start
            if duration < self.config.minimum_enrollment_turn:
                continue
            remaining = self.config.enrollment_seconds - collected.get(segment.speaker, 0.0)
            end = min(segment.end, segment.start + remaining)
            first = max(0, round(segment.start * self.sample_rate))
            last = min(len(self.audio), round(end * self.sample_rate))
            if last <= first:
                continue
            chunks.setdefault(segment.speaker, []).append(self.audio[first:last])
            collected[segment.speaker] = collected.get(segment.speaker, 0.0) + (
                last - first
            ) / self.sample_rate
        if len(chunks) != 2:
            raise ValueError("Could not collect clean detected speech for both speakers")
        similarities = {
            label: {
                speaker: _cosine(
                    self._embedding(np.concatenate(values)),
                    reference,
                )
                for speaker, reference in self.reference_embeddings.items()
            }
            for label, values in chunks.items()
        }
        mapping = choose_identity_mapping(
            similarities,
            self.config.minimum_identity_similarity,
            self.config.minimum_identity_margin,
        )
        return mapping, {
            "mapping": mapping,
            "similarities": similarities,
            "embedding_model": self.config.embedding_model,
            "enrollment_seconds": collected,
        }
