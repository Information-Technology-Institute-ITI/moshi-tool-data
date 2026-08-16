from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from moshi_data_pipeline.studio.catalog import WORKER_PROTOCOL_VERSION

JobKind = Literal[
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
WorkerStatus = Literal[
    "ready",
    "busy",
    "draining",
    "incompatible",
    "idle",
    "offline",
]

JOB_KINDS: tuple[str, ...] = (
    "initialize",
    "transcribe",
    "review_transcript",
    "rediarize",
    "realign",
    "recover_overlap",
    "transcribe_overlap",
    "generate",
    "export",
)

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class ArtifactRef(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    filename: str = Field(min_length=1, max_length=180)
    project_id: str | None = Field(default=None, max_length=120)
    source_id: str | None = Field(default=None, max_length=120)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not SAFE_NAME_PATTERN.fullmatch(value) or value in {".", ".."}:
            raise ValueError("filename must be a safe basename")
        return value


class WorkerHeartbeat(StrictModel):
    protocol_version: str = Field(default=WORKER_PROTOCOL_VERSION, max_length=20)
    worker_id: str = Field(min_length=1, max_length=120)
    boot_id: str = Field(min_length=1, max_length=120)
    build_id: str = Field(min_length=1, max_length=200)
    supported_kinds: list[JobKind] = Field(min_length=1)
    status: WorkerStatus
    current_job_id: str | None = Field(default=None, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)


class ClaimRequest(StrictModel):
    protocol_version: str = Field(default=WORKER_PROTOCOL_VERSION, max_length=20)
    worker_id: str = Field(min_length=1, max_length=120)
    boot_id: str = Field(min_length=1, max_length=120)
    build_id: str = Field(min_length=1, max_length=200)
    supported_kinds: list[JobKind] = Field(min_length=1)


class JobContext(StrictModel):
    protocol_version: str = Field(default=WORKER_PROTOCOL_VERSION, max_length=20)
    job_id: str = Field(min_length=1, max_length=120)
    kind: JobKind
    attempt: int = Field(ge=1)
    lease_expires_at: str
    input_fingerprint: str = Field(pattern=SHA256_PATTERN.pattern)
    payload: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    inputs: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_job_payload(self) -> JobContext:
        required: dict[str, set[str]] = {
            "initialize": {"mode"},
            "realign": {"annotation_version"},
            "transcribe_overlap": {"region_id"},
            "export": {"export_id"},
        }
        missing = required.get(self.kind, set()) - self.payload.keys()
        if missing:
            raise ValueError(
                f"{self.kind} payload is missing: {', '.join(sorted(missing))}"
            )
        return self


class ClaimResponse(StrictModel):
    job: JobContext | None = None
    lease_token: str | None = Field(default=None, min_length=32, max_length=200)

    @model_validator(mode="after")
    def token_matches_job(self) -> ClaimResponse:
        if (self.job is None) != (self.lease_token is None):
            raise ValueError("job and lease_token must both be present or absent")
        return self


class JobHeartbeat(StrictModel):
    worker_id: str = Field(min_length=1, max_length=120)
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = Field(default=None, max_length=1_000)


class ProducedArtifact(StrictModel):
    upload_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=200)


class JobCompletion(StrictModel):
    worker_id: str = Field(min_length=1, max_length=120)
    input_fingerprint: str = Field(pattern=SHA256_PATTERN.pattern)
    kind: JobKind
    result: dict[str, Any]
    artifacts: list[ProducedArtifact] = Field(default_factory=list)


class JobFailure(StrictModel):
    worker_id: str = Field(min_length=1, max_length=120)
    failure_class: str = Field(min_length=1, max_length=120)
    error: str = Field(min_length=1, max_length=4_000)
    retryable: bool = False


class UploadCreate(StrictModel):
    worker_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    filename: str = Field(min_length=1, max_length=180)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not SAFE_NAME_PATTERN.fullmatch(value) or value in {".", ".."}:
            raise ValueError("filename must be a safe basename")
        return value
