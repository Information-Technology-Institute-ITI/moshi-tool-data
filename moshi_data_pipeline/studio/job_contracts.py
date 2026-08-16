from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from moshi_data_pipeline.gpu_job_protocol import StrictModel


class InitializeResult(StrictModel):
    kind: Literal["initialize"] = "initialize"
    source_id: str
    annotation: dict[str, Any]
    inspection: dict[str, Any]
    duration_samples: int = Field(ge=0)
    source_updates: dict[str, Any]


class AnnotationResult(StrictModel):
    kind: Literal["transcribe", "review_transcript", "rediarize", "realign"]
    source_id: str
    expected_annotation_version: int = Field(ge=0)
    annotation: dict[str, Any]
    metrics: dict[str, Any] = Field(default_factory=dict)


class RecoveryResult(StrictModel):
    kind: Literal["recover_overlap"] = "recover_overlap"
    source_id: str
    expected_annotation_version: int = Field(ge=0)
    recoveries: list[dict[str, Any]]


class OverlapTranscriptResult(StrictModel):
    kind: Literal["transcribe_overlap"] = "transcribe_overlap"
    source_id: str
    region_id: str
    expected_annotation_version: int = Field(ge=0)
    stem_transcripts: dict[str, Any]


class GenerationResult(StrictModel):
    kind: Literal["generate"] = "generate"
    source_id: str
    expected_annotation_version: int = Field(ge=0)
    clip_manifest: dict[str, Any]


class ExportResult(StrictModel):
    kind: Literal["export"] = "export"
    export_id: str
    report: dict[str, Any]
    export_manifest: dict[str, Any]


JobResult = (
    InitializeResult
    | AnnotationResult
    | RecoveryResult
    | OverlapTranscriptResult
    | GenerationResult
    | ExportResult
)


RESULT_MODELS: dict[str, type[StrictModel]] = {
    "initialize": InitializeResult,
    "transcribe": AnnotationResult,
    "review_transcript": AnnotationResult,
    "rediarize": AnnotationResult,
    "realign": AnnotationResult,
    "recover_overlap": RecoveryResult,
    "transcribe_overlap": OverlapTranscriptResult,
    "generate": GenerationResult,
    "export": ExportResult,
}


def validate_job_result(kind: str, value: dict[str, Any]) -> StrictModel:
    try:
        model = RESULT_MODELS[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown job kind: {kind}") from exc
    result = model.model_validate(value)
    if result.kind != kind:
        raise ValueError("Result kind does not match the leased job")
    return result
