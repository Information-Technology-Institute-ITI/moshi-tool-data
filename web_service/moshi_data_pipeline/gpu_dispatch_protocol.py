from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from moshi_data_pipeline.gpu_job_protocol import JobContext

GPU_DISPATCH_PROTOCOL_VERSION = "2.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$")


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("must be a safe identifier")
    return value


class DispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    role: str
    sha256: str
    size_bytes: int = Field(ge=1)
    media_type: str
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("artifact_id", "role")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _safe_identifier(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        if not _MEDIA_TYPE.fullmatch(value):
            raise ValueError("must be a valid media type")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("must be a plain filename")
        return value


class DispatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_id: str
    job_id: str
    attempt: int = Field(ge=1)
    protocol_version: str = Field(min_length=1, max_length=32)
    required_build_id: str = Field(min_length=1, max_length=128)
    input_fingerprint: str
    inputs: list[DispatchInput] = Field(min_length=1, max_length=16)

    @field_validator("dispatch_id", "job_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _safe_identifier(value)

    @field_validator("protocol_version", "required_build_id")
    @classmethod
    def validate_version_identifier(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("must not contain whitespace")
        return value

    @field_validator("input_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> DispatchCreate:
        identifiers = [item.artifact_id for item in self.inputs]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("input artifact IDs must be unique")
        return self


class DispatchStart(BaseModel):
    """Execution context sent only after all registered input bytes are verified."""

    model_config = ConfigDict(extra="forbid")

    context: JobContext


class SelfCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: Literal["manual", "job_preflight"] = "manual"
    force: bool = False
