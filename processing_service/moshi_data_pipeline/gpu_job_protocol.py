"""Typed immutable job context consumed by the GPU processing service."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from moshi_data_pipeline.callback_contract import CALLBACK_PROTOCOL_VERSION

JobKind = Literal["transcribe"]

JOB_KINDS: tuple[str, ...] = ("transcribe",)

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class ArtifactRef(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    filename: str = Field(min_length=1, max_length=180)
    project_id: str | None = Field(default=None, max_length=120)
    source_id: str | None = Field(default=None, max_length=120)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not _SAFE_NAME_PATTERN.fullmatch(value) or value in {".", ".."}:
            raise ValueError("filename must be a safe basename")
        return value


class JobContext(StrictModel):
    protocol_version: str = Field(default=CALLBACK_PROTOCOL_VERSION, max_length=20)
    job_id: str = Field(min_length=1, max_length=120)
    kind: JobKind
    attempt: int = Field(ge=1)
    lease_expires_at: str
    input_fingerprint: str = Field(pattern=_SHA256_PATTERN.pattern)
    payload: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    inputs: list[ArtifactRef] = Field(default_factory=list)
