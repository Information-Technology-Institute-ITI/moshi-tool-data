from __future__ import annotations

import pytest
from pydantic import ValidationError

from moshi_data_pipeline.gpu_job_protocol import JOB_KINDS, JobContext


def _context(kind: str, *, mode: str = "manual") -> dict:
    value = {
        "job_id": "job-1",
        "kind": kind,
        "attempt": 1,
        "lease_expires_at": "2026-08-16T22:00:00+00:00",
        "input_fingerprint": "a" * 64,
        "payload": {},
        "preconditions": {},
        "config": {},
        "inputs": [],
    }
    if kind == "initialize":
        value.update(
            payload={"mode": mode},
            preconditions={
                "project": {"id": "project-1"},
                "source": {"id": "source-1", "project_id": "project-1"},
                "annotation": {"source_id": "source-1", "version": 0},
            },
            inputs=[
                {
                    "artifact_id": "original-1",
                    "role": "source.original",
                    "sha256": "b" * 64,
                    "size_bytes": 123,
                    "media_type": "video/mp4",
                    "filename": "episode.mp4",
                    "project_id": "project-1",
                    "source_id": "source-1",
                }
            ],
        )
    return value


def test_gpu_service_accepts_initialize_and_transcription_jobs() -> None:
    assert JOB_KINDS == ("initialize", "transcribe")
    assert JobContext.model_validate(_context("transcribe")).kind == "transcribe"
    assert JobContext.model_validate(_context("initialize")).kind == "initialize"


@pytest.mark.parametrize("mode", ["manual", "assisted"])
def test_initialize_accepts_typed_modes(mode: str) -> None:
    assert JobContext.model_validate(_context("initialize", mode=mode)).payload == {
        "mode": mode
    }


@pytest.mark.parametrize("mode", ["", "automatic", "MANUAL"])
def test_initialize_rejects_unknown_modes(mode: str) -> None:
    with pytest.raises(ValidationError):
        JobContext.model_validate(_context("initialize", mode=mode))


def test_initialize_requires_matching_original_input() -> None:
    value = _context("initialize")
    value["inputs"][0]["role"] = "source.canonical"
    with pytest.raises(ValidationError, match="source.original"):
        JobContext.model_validate(value)

    value = _context("initialize")
    value["inputs"][0]["source_id"] = "source-2"
    with pytest.raises(ValidationError, match="source precondition"):
        JobContext.model_validate(value)


def test_gpu_service_rejects_removed_job_kinds() -> None:
    with pytest.raises(ValidationError):
        JobContext.model_validate(_context("rediarize"))
