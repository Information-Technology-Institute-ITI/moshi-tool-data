"""Versioned contracts for the phased Studio product work.

These models intentionally have no routes or persistence side effects.  They freeze the
cross-layer vocabulary before the implementation phases add endpoints and catalog tables.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REVIEW_CONTRACT_VERSION = "studio.review/v1"
EVALUATION_CONTRACT_VERSION = "studio.evaluation/v1"
TRAINING_CONTRACT_VERSION = "studio.training/v1"
ERROR_CONTRACT_VERSION = "studio.error/v1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SampleRange(ContractModel):
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> SampleRange:
        if self.end_sample <= self.start_sample:
            raise ValueError("end_sample must be greater than start_sample")
        return self


class PlaybackRange(SampleRange):
    behavior: Literal["once", "loop"]


class PlaybackState(ContractModel):
    status: Literal["loading", "paused", "playing", "ended", "error"]
    current_sample: int = Field(ge=0)
    active_range: PlaybackRange | None = None
    rate: float = Field(default=1, gt=0, le=4)
    audition_mode: Literal["mixed", "left", "right", "speaker_a", "speaker_b"] = "mixed"


class ApiErrorDetail(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    field: str | None = None
    retryable: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


class ApiErrorEnvelope(ContractModel):
    contract_version: Literal["studio.error/v1"] = ERROR_CONTRACT_VERSION
    error: ApiErrorDetail


class ReviewChapterConfig(ContractModel):
    contract_version: Literal["studio.review/v1"] = REVIEW_CONTRACT_VERSION
    mode: Literal["max_duration", "count", "manual"] = "max_duration"
    max_duration_seconds: int = Field(default=30 * 60, ge=60, le=2 * 60 * 60)
    count: int | None = Field(default=None, ge=1, le=500)
    manual_boundaries_samples: list[int] = Field(default_factory=list)
    boundary_search_seconds: int = Field(default=30, ge=0, le=5 * 60)

    @model_validator(mode="after")
    def mode_has_value(self) -> ReviewChapterConfig:
        if self.mode == "count" and self.count is None:
            raise ValueError("count is required in count mode")
        if self.mode == "manual":
            boundaries = self.manual_boundaries_samples
            if len(boundaries) < 2 or boundaries != sorted(set(boundaries)):
                raise ValueError("manual boundaries must contain two or more unique sorted values")
        return self


class ReviewChapter(SampleRange):
    id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    boundary_reason: Literal["source_edge", "silence", "segment", "manual"]


class ReviewChapterSet(ContractModel):
    contract_version: Literal["studio.review/v1"] = REVIEW_CONTRACT_VERSION
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    annotation_version: int = Field(ge=0)
    active: bool = True
    config: ReviewChapterConfig
    chapters: list[ReviewChapter] = Field(min_length=1)

    @model_validator(mode="after")
    def chapters_are_contiguous(self) -> ReviewChapterSet:
        for expected, chapter in enumerate(self.chapters, start=1):
            if chapter.ordinal != expected:
                raise ValueError("chapter ordinals must be contiguous and one-based")
        for left, right in zip(self.chapters, self.chapters[1:], strict=False):
            if left.end_sample != right.start_sample:
                raise ValueError("chapter sample ranges must be contiguous")
        return self


class WaveformPeakWindow(SampleRange):
    contract_version: Literal["studio.review/v1"] = REVIEW_CONTRACT_VERSION
    source_id: str = Field(min_length=1)
    sample_rate: int = Field(gt=0)
    duration_samples: int = Field(gt=0)
    samples_per_point: int = Field(gt=0)
    points: list[tuple[float, float]]

    @model_validator(mode="after")
    def valid_points(self) -> WaveformPeakWindow:
        if self.end_sample > self.duration_samples:
            raise ValueError("peak window must stay inside the source")
        if any(low > high or low < -1 or high > 1 for low, high in self.points):
            raise ValueError("peak points must be ordered normalized amplitude pairs")
        return self


class ModelRunSummary(ContractModel):
    contract_version: Literal["studio.evaluation/v1"] = EVALUATION_CONTRACT_VERSION
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    initialization_job_id: str | None = None
    model_name: str = Field(min_length=1)
    model_revision: str | None = None
    language: str | None = None
    config_fingerprint: str | None = None
    raw_transcript_artifact_id: str | None = None
    aligned_transcript_artifact_id: str | None = None
    diarization_artifact_id: str | None = None
    comparison_available: bool
    created_at: str


class OverlapReviewRecord(SampleRange):
    contract_version: Literal["studio.review/v1"] = REVIEW_CONTRACT_VERSION
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    annotation_version: int = Field(ge=0)
    speaker_a_activity_id: str = Field(min_length=1)
    speaker_b_activity_id: str = Field(min_length=1)
    classification: Literal[
        "confirmed", "false_positive", "third_speaker", "noise", "unintelligible"
    ]
    training_decision: Literal["raw", "separate", "exclude", "needs_work"]
    state: Literal["current", "stale"]
    note: str = Field(default="", max_length=1_000)
    reviewer_user_id: str | None = None
    recovery_artifact_ids: list[str] = Field(default_factory=list)


class EvaluationScope(ContractModel):
    contract_version: Literal["studio.evaluation/v1"] = EVALUATION_CONTRACT_VERSION
    project_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    model_run_ids: list[str] = Field(default_factory=list)
    annotation_versions: dict[str, int] = Field(default_factory=dict)
    chapter_ids: list[str] = Field(default_factory=list)
    speakers: list[Literal["A", "B"]] = Field(default_factory=list)
    verified_only: bool = True
    normalization_policy: str = "strict-v1"
    overlap_policy: Literal["deduplicate", "include", "exclude"] = "deduplicate"


class ErrorMetric(ContractModel):
    distance: int = Field(ge=0)
    substitutions: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    reference_units: int = Field(ge=0)
    hypothesis_units: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0)


class EvaluationPreview(ContractModel):
    contract_version: Literal["studio.evaluation/v1"] = EVALUATION_CONTRACT_VERSION
    scope: EvaluationScope
    word_error: ErrorMetric
    character_error: ErrorMetric
    normalized_word_error: ErrorMetric
    normalized_character_error: ErrorMetric
    evaluated_duration_samples: int = Field(ge=0)
    eligible_duration_samples: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    breakdowns: dict[str, Any] = Field(default_factory=dict)


class TrainingProfile(ContractModel):
    contract_version: Literal["studio.training/v1"] = TRAINING_CONTRACT_VERSION
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    target: Literal["whisper", "moshi"]
    min_duration_seconds: float = Field(default=1, gt=0)
    max_duration_seconds: float = Field(default=30, gt=0)
    padding_seconds: float = Field(default=0, ge=0, le=10)
    verified_only: bool = True
    normalization_policy: str = "strict-v1"
    overlap_policy: Literal["raw", "separate", "exclude"] = "exclude"
    speaker_scope: Literal["both", "assistant", "user"] = "both"
    channel_mode: Literal["mono", "stereo"] = "mono"
    manifest_format: Literal["jsonl", "csv", "huggingface"] = "jsonl"
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def duration_ordered(self) -> TrainingProfile:
        if self.max_duration_seconds < self.min_duration_seconds:
            raise ValueError("max_duration_seconds must not be below min_duration_seconds")
        if self.target == "moshi" and self.manifest_format == "huggingface":
            raise ValueError("huggingface manifest is only available for the Whisper target")
        return self


class ValidationMessage(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_id: str | None = None
    segment_id: str | None = None


class TrainingValidation(ContractModel):
    contract_version: Literal["studio.training/v1"] = TRAINING_CONTRACT_VERSION
    valid: bool
    profile: TrainingProfile
    annotation_versions: dict[str, int]
    blockers: list[ValidationMessage] = Field(default_factory=list)
    warnings: list[ValidationMessage] = Field(default_factory=list)
    proposed_train_sources: list[str] = Field(default_factory=list)
    proposed_eval_sources: list[str] = Field(default_factory=list)
    sample_count: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)


class TrainingPackageSummary(ContractModel):
    contract_version: Literal["studio.training/v1"] = TRAINING_CONTRACT_VERSION
    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    target: Literal["whisper", "moshi"]
    status: Literal["queued", "running", "complete", "failed", "stale"]
    profile: TrainingProfile
    annotation_versions: dict[str, int]
    artifact_id: str | None = None
    created_by_user_id: str = Field(min_length=1)
    created_at: str
