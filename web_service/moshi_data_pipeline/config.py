from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from moshi_data_pipeline.exceptions import ConfigurationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioConfig(StrictModel):
    sample_rate: int = 24_000
    sample_width: int = 16
    fade_ms: float = 10.0
    preserve_source_channels: bool = True


class ChannelRoutingConfig(StrictModel):
    mode: Literal["review", "mono"] = "review"
    analysis_seconds: float = 300.0
    activity_threshold_db: float = -48.0
    dominance_db: float = 9.0
    minimum_dominant_fraction: float = 0.05
    maximum_absolute_correlation: float = 0.80
    mapping_minimum_margin_db: float = 6.0

    @model_validator(mode="after")
    def routing_values_are_valid(self) -> ChannelRoutingConfig:
        if self.analysis_seconds <= 0:
            raise ValueError("channel routing analysis_seconds must be positive")
        if self.dominance_db <= 0 or self.mapping_minimum_margin_db <= 0:
            raise ValueError("channel routing margins must be positive")
        if not 0 <= self.minimum_dominant_fraction <= 1:
            raise ValueError("minimum_dominant_fraction must be between 0 and 1")
        if not 0 <= self.maximum_absolute_correlation <= 1:
            raise ValueError("maximum_absolute_correlation must be between 0 and 1")
        return self


class TranscriptionConfig(StrictModel):
    quality_profile: str = "balanced"
    model: str = "large-v3"
    model_repository: str | None = None
    model_revision: str | None = None
    review_model: str | None = None
    review_model_repository: str | None = None
    review_model_revision: str | None = None
    language: str = "ar"
    device: str = "auto"
    compute_type: str = "float16"
    batch_size: int = 1
    chunk_size: int = 20
    retry_chunk_size: int = 10
    beam_size: int = 5
    review_beam_size: int = 8
    repetition_penalty: float = 1.1
    no_repeat_ngram_size: int = 3
    hallucination_silence_threshold: float = 1.0
    suspect_avg_logprob: float = -0.50
    min_words_per_second: float = 0.35
    max_words_per_second: float = 6.0
    max_repeated_ngram_occurrences: int = 3
    decode_disagreement_ratio: float = 0.25

    @model_validator(mode="after")
    def transcription_values_are_valid(self) -> TranscriptionConfig:
        if self.quality_profile != "balanced":
            raise ValueError("quality_profile must be 'balanced'")
        if self.chunk_size < 1 or self.retry_chunk_size < 1:
            raise ValueError("transcription chunk sizes must be positive")
        if self.beam_size < 1 or self.review_beam_size < 1 or self.no_repeat_ngram_size < 0:
            raise ValueError(
                "beam sizes must be positive and no_repeat_ngram_size non-negative"
            )
        if self.repetition_penalty < 1.0:
            raise ValueError("repetition_penalty must be at least 1.0")
        return self


class AlignmentConfig(StrictModel):
    backend: str = "whisperx"
    model: str | None = "jonatasgrosman/wav2vec2-large-xlsr-53-arabic"
    model_revision: str | None = None
    low_confidence_score: float = 0.40


class DiarizationConfig(StrictModel):
    model: str = "pyannote/speaker-diarization-community-1"
    model_revision: str | None = None
    min_speakers: int = 2
    max_speakers: int = 2
    min_assignment_overlap: float = 0.35
    short_turn_seconds: float = 0.35
    activity_merge_gap_seconds: float = 0.50

    @model_validator(mode="after")
    def speakers_are_ordered(self) -> DiarizationConfig:
        if self.min_speakers < 1 or self.max_speakers < self.min_speakers:
            raise ValueError("speaker limits must satisfy 1 <= min_speakers <= max_speakers")
        if self.activity_merge_gap_seconds < 0:
            raise ValueError("activity_merge_gap_seconds must be non-negative")
        return self


class SeparationConfig(StrictModel):
    enabled: bool = False
    backend: Literal["speechbrain"] = "speechbrain"
    model: str = "speechbrain/sepformer-whamr16k"
    model_revision: str | None = None
    embedding_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    embedding_model_revision: str | None = None
    sample_rate: int = 16_000
    context_seconds: float = 1.0
    max_window_seconds: float = 12.0
    chunk_crossfade_seconds: float = 0.20
    recovery_seam_ms: float = 20.0
    minimum_gain: float = 0.25
    maximum_gain: float = 4.0
    mixture_consistency: bool = True
    enrollment_seconds: float = 30.0
    minimum_enrollment_turn: float = 1.5
    minimum_identity_similarity: float = 0.65
    minimum_identity_margin: float = 0.10

    @model_validator(mode="after")
    def separation_values_are_valid(self) -> SeparationConfig:
        if (
            self.context_seconds < 0
            or self.max_window_seconds <= 0
            or 2 * self.context_seconds >= self.max_window_seconds
        ):
            raise ValueError("separation context/window values are invalid")
        if not 0 <= self.chunk_crossfade_seconds < self.max_window_seconds:
            raise ValueError("chunk_crossfade_seconds must be inside the model window")
        if self.recovery_seam_ms < 0:
            raise ValueError("recovery_seam_ms must be non-negative")
        if not 0 < self.minimum_gain <= self.maximum_gain:
            raise ValueError("separation gain limits must satisfy 0 < minimum <= maximum")
        return self


class SegmentationConfig(StrictModel):
    min_duration: float = 20.0
    target_duration: float = 40.0
    max_duration: float = 50.0
    max_overlap_ratio: float = 0.20
    overlap_warning_ratio: float = 0.05
    max_silence_ratio: float = 0.80
    min_speaker_duration: float = 1.0
    min_speaker_share: float = 0.02
    context_before: float = 0.25
    context_after: float = 0.25

    @model_validator(mode="after")
    def durations_are_ordered(self) -> SegmentationConfig:
        if not 0 < self.min_duration <= self.target_duration <= self.max_duration:
            raise ValueError("clip durations must satisfy 0 < min <= target <= max")
        for name in (
            "max_overlap_ratio",
            "overlap_warning_ratio",
            "max_silence_ratio",
            "min_speaker_share",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        return self


class NormalizationConfig(StrictModel):
    remove_diacritics: bool = True
    normalize_alef: bool = True
    normalize_alef_maqsura: bool = False
    keep_punctuation: bool = True


class QCConfig(StrictModel):
    silence_threshold_db: float = -50.0
    max_channel_silence_ratio: float = 0.995
    clipping_review_ratio: float = 0.001
    clipping_reject_ratio: float = 0.01
    leakage_review_ratio: float = 0.25
    reconstruction_review_error_db: float = -15.0
    reconstruction_reject_error_db: float = -5.0
    low_confidence_review_ratio: float = 0.10
    low_confidence_reject_ratio: float = 0.35
    uncertain_assignment_review_ratio: float = 0.05
    uncertain_assignment_reject_ratio: float = 0.15

    @model_validator(mode="after")
    def reconstruction_thresholds_are_ordered(self) -> QCConfig:
        if self.reconstruction_review_error_db >= self.reconstruction_reject_error_db:
            raise ValueError(
                "reconstruction review error must be below the reject error"
            )
        return self


class ReviewConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = 8765
    require_overlap_review: bool = True


class PipelineConfig(StrictModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    channel_routing: ChannelRoutingConfig = Field(default_factory=ChannelRoutingConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    alignment: AlignmentConfig = Field(default_factory=AlignmentConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    separation: SeparationConfig = Field(default_factory=SeparationConfig)
    segmentation: SegmentationConfig = Field(default_factory=SegmentationConfig)
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    qc: QCConfig = Field(default_factory=QCConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    def fingerprint(self, section: str | None = None) -> str:
        import hashlib

        value: Any = self.model_dump(mode="json")
        if section is not None:
            value = value[section]
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict):
            child = merged.get(key) if isinstance(merged.get(key), dict) else {}
            child_merged = _deep_merge(child, value)
            if child_merged:
                merged[key] = child_merged
        elif value is not None:
            merged[key] = value
    return merged


def load_config(
    config_path: Path | None = None, overrides: dict[str, Any] | None = None
) -> PipelineConfig:
    data: dict[str, Any] = {}
    if config_path is not None:
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"Cannot read configuration {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError("The YAML root must be a mapping")
        data = loaded
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        return PipelineConfig.model_validate(data)
    except Exception as exc:
        raise ConfigurationError(str(exc)) from exc
