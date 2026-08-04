from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class QCStatus(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass(slots=True)
class Word:
    word: str
    start: float | None
    end: float | None
    speaker: str | None = None
    score: float | None = None
    original: str | None = None
    assignment_confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Word:
        return cls(**{key: value.get(key) for key in cls.__dataclass_fields__})


@dataclass(slots=True, frozen=True)
class SpeakerSegment:
    start: float
    end: float
    speaker: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SpeakerSegment:
        return cls(
            start=float(value["start"]),
            end=float(value["end"]),
            speaker=str(value["speaker"]),
        )


@dataclass(slots=True)
class ClipPlan:
    clip_id: str
    start: float
    end: float
    status: QCStatus = QCStatus.PASS
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["duration"] = self.duration
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ClipPlan:
        return cls(
            clip_id=str(value["clip_id"]),
            start=float(value["start"]),
            end=float(value["end"]),
            status=QCStatus(value.get("status", "PASS")),
            reasons=list(value.get("reasons", [])),
            metrics=dict(value.get("metrics", {})),
        )


@dataclass(slots=True)
class QCResult:
    status: QCStatus
    reasons: list[str]
    metrics: dict[str, Any]
    clip_id: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value
