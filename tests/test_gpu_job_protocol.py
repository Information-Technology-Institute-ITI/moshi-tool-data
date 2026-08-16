from __future__ import annotations

import pytest
from pydantic import ValidationError

from moshi_data_pipeline.gpu_job_protocol import JOB_KINDS, JobContext


def _context(kind: str) -> dict:
    return {
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


def test_gpu_service_accepts_only_transcription_jobs() -> None:
    assert JOB_KINDS == ("transcribe",)
    assert JobContext.model_validate(_context("transcribe")).kind == "transcribe"
    with pytest.raises(ValidationError):
        JobContext.model_validate(_context("initialize"))
