from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

SAMPLE_RATE = 24_000
Speaker = Literal["A", "B"]
RightsBasis = Literal["owned", "consent", "licensed", "public_domain", "other"]
ExclusionKind = Literal["music", "advertisement", "noise", "third_speaker", "unusable"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    language: str = Field(default="ar", min_length=2, max_length=16)


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    language: str = Field(default="ar", min_length=2, max_length=16)


class SourceRights(BaseModel):
    origin: str = Field(min_length=1, max_length=2_000)
    rights_basis: RightsBasis
    rights_notes: str = Field(default="", max_length=4_000)
    rights_confirmed: bool = False


class ActivityRegion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("activity"))
    speaker: Speaker
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    origin: Literal["manual", "model"] = "manual"
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_bounds(self) -> ActivityRegion:
        if self.end_sample <= self.start_sample:
            raise ValueError("Activity end_sample must be greater than start_sample")
        return self


class ExclusionRegion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exclude"))
    kind: ExclusionKind
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    note: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def valid_bounds(self) -> ExclusionRegion:
        if self.end_sample <= self.start_sample:
            raise ValueError("Exclusion end_sample must be greater than start_sample")
        return self


class SpeakerReferenceRegion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("speaker_reference"))
    speaker: Speaker
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_bounds(self) -> SpeakerReferenceRegion:
        if self.end_sample <= self.start_sample:
            raise ValueError("Speaker reference end_sample must be greater than start_sample")
        return self


class TranscriptCandidate(BaseModel):
    source: Literal["retry", "secondary_asr", "overlap_assistant", "overlap_user"]
    model: str
    text: str
    average_log_probability: float | None = None
    quality_flags: list[str] = Field(default_factory=list)


class TranscriptUtterance(BaseModel):
    id: str = Field(default_factory=lambda: new_id("utterance"))
    speaker: Speaker | None = None
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    text: str = ""
    model_text: str = ""
    model_speaker: Speaker | None = None
    quality_flags: list[str] = Field(default_factory=list)
    alignment_status: Literal["not_run", "aligned", "low_confidence", "unaligned"] = "not_run"
    human_verified: bool = False
    review_candidates: list[TranscriptCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_bounds(self) -> TranscriptUtterance:
        if self.end_sample <= self.start_sample:
            raise ValueError("Utterance end_sample must be greater than start_sample")
        return self


class AnnotationDocument(BaseModel):
    source_id: str
    version: int = Field(default=0, ge=0)
    assistant_speaker: Speaker | None = None
    activities_finalized: bool = False
    activities: list[ActivityRegion] = Field(default_factory=list)
    speaker_references: list[SpeakerReferenceRegion] = Field(default_factory=list)
    exclusions: list[ExclusionRegion] = Field(default_factory=list)
    transcript: list[TranscriptUtterance] = Field(default_factory=list)
    aligned_words: list[dict[str, Any]] = Field(default_factory=list)
    note: str = Field(default="", max_length=4_000)


class AnnotationSave(BaseModel):
    expected_version: int = Field(ge=0)
    annotation: AnnotationDocument


class ClipPlanRequest(BaseModel):
    mode: Literal["count", "target_duration", "manual"]
    count: int | None = Field(default=None, ge=1, le=500)
    target_duration_seconds: float | None = Field(default=None, ge=1, le=600)
    boundaries_samples: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def mode_value_present(self) -> ClipPlanRequest:
        if self.mode == "count" and self.count is None:
            raise ValueError("count is required in count mode")
        if self.mode == "target_duration" and self.target_duration_seconds is None:
            raise ValueError("target_duration_seconds is required in target_duration mode")
        if self.mode == "manual" and len(self.boundaries_samples) < 2:
            raise ValueError("manual mode requires at least two boundaries")
        return self


class ClipSpec(BaseModel):
    id: str
    start_sample: int
    end_sample: int
    status: Literal["valid", "invalid"]
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | str] = Field(default_factory=dict)


class ClipPlanDocument(BaseModel):
    source_id: str
    annotation_version: int
    mode: Literal["count", "target_duration", "manual"]
    request: dict[str, Any]
    feasible: bool
    message: str = ""
    clips: list[ClipSpec]


class DecisionPayload(BaseModel):
    decision: Literal["approve", "reject", "needs_work"]
    auditioned: bool = False


class OverlapDecisionPayload(BaseModel):
    decision: Literal["approve", "reject"]
    auditioned: bool = False

    @model_validator(mode="after")
    def approval_requires_audition(self) -> OverlapDecisionPayload:
        if not self.auditioned:
            raise ValueError("A recovered-overlap decision requires audition")
        return self


class JobCreate(BaseModel):
    kind: Literal[
        "initialize",
        "transcribe",
        "review_transcript",
        "rediarize",
        "realign",
        "recover_overlap",
        "transcribe_overlap",
        "generate",
        "export",
    ]
    project_id: str
    source_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ExportCreate(BaseModel):
    name: str = Field(default="dataset", min_length=1, max_length=120)
