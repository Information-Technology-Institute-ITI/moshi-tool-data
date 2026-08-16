from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from moshi_data_pipeline.config import load_config
from moshi_data_pipeline.gpu_dispatch_protocol import GPU_DISPATCH_PROTOCOL_VERSION
from moshi_data_pipeline.transcription.whisperx_backend import WhisperXTranscriber

SELF_CHECK_DEFINITION_VERSION = "1"


class SelfCheckError(RuntimeError):
    pass


class SelfCheckRateLimitError(SelfCheckError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("A manual functional check was run too recently")
        self.retry_after = retry_after


class SelfCheckFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str = Field(min_length=1, max_length=128)
    audio_file: str = Field(min_length=1, max_length=255)
    sha256: str
    reference_text: str = Field(min_length=1, max_length=4000)
    language: str = Field(min_length=2, max_length=16)
    dataset: str = Field(min_length=1, max_length=128)
    dataset_config: str = Field(min_length=1, max_length=128)
    split: str = Field(min_length=1, max_length=32)
    row_index: int = Field(ge=0)
    record_id: int = Field(ge=0)
    license: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=512)

    @field_validator("audio_file")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("audio_file must be a plain filename")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("sha256 must be a lowercase digest")
        return value


@dataclass(frozen=True)
class SelfCheckDefinition:
    metadata_path: Path
    config_path: Path
    build_id: str
    host_boot_id: str
    max_cer: float = 0.20
    validity_hours: int = 6
    manual_cooldown_seconds: int = 600


def _now() -> datetime:
    return datetime.now(UTC)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.casefold().split())


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_character in enumerate(left, 1):
        current = [row_index]
        for column_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column_index] + 1,
                    previous[column_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


class FunctionalCheckRunner:
    def __init__(self, definition: SelfCheckDefinition) -> None:
        self.definition = definition
        try:
            raw = json.loads(definition.metadata_path.read_text(encoding="utf-8"))
            self.fixture = SelfCheckFixture.model_validate(raw)
        except Exception as exc:
            raise SelfCheckError("Functional-check metadata is invalid") from exc
        self.audio_path = definition.metadata_path.parent / self.fixture.audio_file
        self.pipeline_config = load_config(definition.config_path)
        self.transcription_config = self.pipeline_config.transcription.model_copy(
            update={"device": "cuda", "language": self.fixture.language}
        )
        self.config_fingerprint = _value_sha256(
            self.transcription_config.model_dump(mode="json")
        )
        self.requirement_key = _value_sha256(
            {
                "definition_version": SELF_CHECK_DEFINITION_VERSION,
                "host_boot_id": definition.host_boot_id,
                "build_id": definition.build_id,
                "protocol_version": GPU_DISPATCH_PROTOCOL_VERSION,
                "model_config": self.transcription_config.model_dump(mode="json"),
                "fixture_id": self.fixture.fixture_id,
                "fixture_sha256": self.fixture.sha256,
            }
        )

    def descriptor(self) -> dict[str, Any]:
        return {
            "requirement_key": self.requirement_key,
            "definition_version": SELF_CHECK_DEFINITION_VERSION,
            "host_boot_id": self.definition.host_boot_id,
            "build_id": self.definition.build_id,
            "protocol_version": GPU_DISPATCH_PROTOCOL_VERSION,
            "config_fingerprint": self.config_fingerprint,
            "fixture_id": self.fixture.fixture_id,
            "fixture_sha256": self.fixture.sha256,
            "model": self.transcription_config.model,
            "requested_model_revision": self.transcription_config.model_revision,
            "compute_type": self.transcription_config.compute_type,
            "max_cer": self.definition.max_cer,
        }

    def run(self) -> dict[str, Any]:
        if not self.audio_path.is_file():
            raise SelfCheckError("Functional-check audio is missing")
        if _file_sha256(self.audio_path) != self.fixture.sha256:
            raise SelfCheckError("Functional-check audio checksum does not match")
        try:
            import torch
        except ImportError as exc:
            raise SelfCheckError("PyTorch is unavailable") from exc
        if not torch.cuda.is_available():
            raise SelfCheckError("CUDA is unavailable")

        started = monotonic()
        result = WhisperXTranscriber().transcribe(
            self.audio_path, self.transcription_config
        )
        total_ms = round((monotonic() - started) * 1000)
        hypothesis = _normalize_text(
            " ".join(str(segment.get("text", "")) for segment in result.get("segments", []))
        )
        reference = _normalize_text(self.fixture.reference_text)
        cer = _edit_distance(reference, hypothesis) / max(1, len(reference))
        requested_revision = self.transcription_config.model_revision
        actual_revision = str(result.get("model_revision") or "")
        if result.get("device") != "cuda":
            raise SelfCheckError("WhisperX did not execute on CUDA")
        if requested_revision and actual_revision != requested_revision:
            raise SelfCheckError("WhisperX resolved an unexpected model revision")
        if not hypothesis:
            raise SelfCheckError("WhisperX produced no speech text")
        if cer > self.definition.max_cer:
            raise SelfCheckError("WhisperX output exceeded the functional CER threshold")
        return {
            "model_revision": actual_revision,
            "device": "cuda",
            "gpu_name": str(torch.cuda.get_device_name(0)),
            "segment_count": len(result.get("segments", [])),
            "output_sha256": hashlib.sha256(hypothesis.encode()).hexdigest(),
            "cer": cer,
            "total_ms": total_ms,
        }


class SelfCheckRepository:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.chmod(0o700)
        self.database = self.state_root / "self-check.sqlite3"
        descriptor = os.open(self.database, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.database.chmod(0o600)
        self._lock = threading.RLock()
        self._initialize()
        self._recover_interrupted()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;

                CREATE TABLE IF NOT EXISTS self_checks (
                    id TEXT PRIMARY KEY,
                    requirement_key TEXT NOT NULL,
                    definition_version TEXT NOT NULL,
                    trigger TEXT NOT NULL CHECK (trigger IN ('manual','job_preflight')),
                    status TEXT NOT NULL CHECK (
                        status IN ('queued','running','passed','failed')
                    ),
                    host_boot_id TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    fixture_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requested_model_revision TEXT,
                    model_revision TEXT,
                    compute_type TEXT NOT NULL,
                    device TEXT,
                    gpu_name TEXT,
                    segment_count INTEGER,
                    output_sha256 TEXT,
                    cer REAL,
                    max_cer REAL NOT NULL,
                    total_ms INTEGER,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    valid_until TEXT,
                    failure_class TEXT,
                    failure_summary TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_self_check_one_active
                ON self_checks((1)) WHERE status IN ('queued','running');
                CREATE INDEX IF NOT EXISTS idx_self_check_history
                ON self_checks(requested_at DESC);
                CREATE INDEX IF NOT EXISTS idx_self_check_requirement
                ON self_checks(requirement_key, status, valid_until);
                """
            )
        self.database.chmod(0o600)

    def _recover_interrupted(self) -> None:
        timestamp = _now().isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE self_checks
                SET status='failed', finished_at=?, failure_class='service_interrupted',
                    failure_summary='GPU service stopped before the functional check completed'
                WHERE status IN ('queued','running')
                """,
                (timestamp,),
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def request(
        self,
        descriptor: dict[str, Any],
        *,
        trigger: Literal["manual", "job_preflight"],
        force: bool,
        manual_cooldown_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        with self._lock, self.connect() as connection:
            active = connection.execute(
                """
                SELECT * FROM self_checks WHERE status IN ('queued','running')
                ORDER BY requested_at DESC LIMIT 1
                """
            ).fetchone()
            if active is not None:
                return self._public(active), False
            if not force:
                passed = connection.execute(
                    """
                    SELECT * FROM self_checks
                    WHERE requirement_key=? AND status='passed' AND valid_until>?
                    ORDER BY finished_at DESC LIMIT 1
                    """,
                    (descriptor["requirement_key"], now.isoformat()),
                ).fetchone()
                if passed is not None:
                    return self._public(passed), False
            if trigger == "manual":
                latest_manual = connection.execute(
                    """
                    SELECT requested_at FROM self_checks
                    WHERE trigger='manual' ORDER BY requested_at DESC LIMIT 1
                    """
                ).fetchone()
                if latest_manual is not None:
                    elapsed = (now - datetime.fromisoformat(latest_manual["requested_at"])).total_seconds()
                    if elapsed < manual_cooldown_seconds:
                        raise SelfCheckRateLimitError(
                            max(1, round(manual_cooldown_seconds - elapsed))
                        )
            identifier = uuid4().hex
            connection.execute(
                """
                INSERT INTO self_checks(
                    id, requirement_key, definition_version, trigger, status,
                    host_boot_id, build_id, protocol_version, config_fingerprint,
                    fixture_id, fixture_sha256, model, requested_model_revision,
                    compute_type, max_cer, requested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    identifier,
                    descriptor["requirement_key"],
                    descriptor["definition_version"],
                    trigger,
                    "queued",
                    descriptor["host_boot_id"],
                    descriptor["build_id"],
                    descriptor["protocol_version"],
                    descriptor["config_fingerprint"],
                    descriptor["fixture_id"],
                    descriptor["fixture_sha256"],
                    descriptor["model"],
                    descriptor["requested_model_revision"],
                    descriptor["compute_type"],
                    descriptor["max_cer"],
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM self_checks WHERE id=?", (identifier,)
            ).fetchone()
            return self._public(row), True

    def mark_running(self, identifier: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE self_checks SET status='running', started_at=?
                WHERE id=? AND status='queued'
                """,
                (_now().isoformat(), identifier),
            )

    def pass_check(
        self, identifier: str, result: dict[str, Any], validity_hours: int
    ) -> dict[str, Any]:
        finished = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE self_checks
                SET status='passed', model_revision=?, device=?, gpu_name=?,
                    segment_count=?, output_sha256=?, cer=?, total_ms=?,
                    finished_at=?, valid_until=?, failure_class=NULL,
                    failure_summary=NULL
                WHERE id=? AND status='running'
                """,
                (
                    result["model_revision"],
                    result["device"],
                    result["gpu_name"],
                    result["segment_count"],
                    result["output_sha256"],
                    result["cer"],
                    result["total_ms"],
                    finished.isoformat(),
                    (finished + timedelta(hours=validity_hours)).isoformat(),
                    identifier,
                ),
            )
            return self.get(identifier, connection=connection)

    def fail_check(self, identifier: str, exc: Exception) -> dict[str, Any]:
        failure_class = type(exc).__name__[:80]
        if isinstance(exc, SelfCheckError):
            summary = str(exc)[:300]
        else:
            summary = "Unexpected functional-check failure"
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE self_checks
                SET status='failed', finished_at=?, failure_class=?, failure_summary=?
                WHERE id=? AND status='running'
                """,
                (_now().isoformat(), failure_class, summary, identifier),
            )
            return self.get(identifier, connection=connection)

    def get(
        self, identifier: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM self_checks WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self._public(row)
        with self._lock, self.connect() as local:
            return self.get(identifier, connection=local)

    def latest(self) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM self_checks ORDER BY requested_at DESC LIMIT 1"
            ).fetchone()
            return self._public(row) if row is not None else None

    def history(self, limit: int) -> list[dict[str, Any]]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM self_checks ORDER BY requested_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._public(row) for row in rows]


class SelfCheckCoordinator:
    def __init__(
        self,
        runner: FunctionalCheckRunner,
        repository: SelfCheckRepository,
    ) -> None:
        self.runner = runner
        self.repository = repository
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def trigger(
        self, trigger: Literal["manual", "job_preflight"], force: bool
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            record, created = self.repository.request(
                self.runner.descriptor(),
                trigger=trigger,
                force=force,
                manual_cooldown_seconds=self.runner.definition.manual_cooldown_seconds,
            )
            if created:
                self._task = asyncio.create_task(self._execute(str(record["id"])))
            return record, created

    async def _execute(self, identifier: str) -> None:
        self.repository.mark_running(identifier)
        try:
            result = await asyncio.to_thread(self.runner.run)
        except Exception as exc:
            self.repository.fail_check(identifier, exc)
        else:
            self.repository.pass_check(
                identifier, result, self.runner.definition.validity_hours
            )

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            await self._task

    def latest(self) -> dict[str, Any] | None:
        return self.repository.latest()

    def history(self, limit: int) -> list[dict[str, Any]]:
        return self.repository.history(limit)
