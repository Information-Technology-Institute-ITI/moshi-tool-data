from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from moshi_data_pipeline.gpu_dispatch_protocol import DispatchCreate, DispatchStart

ACTIVE_STATES = (
    "receiving",
    "verified",
    "queued",
    "running",
    "outbox_pending",
    "callback_uploading",
    "auth_blocked",
)
TERMINAL_STATES = ("acknowledged", "cancelled", "rejected", "failed", "orphaned")


class DispatchStateError(RuntimeError):
    pass


class DispatchNotFoundError(DispatchStateError):
    pass


class DispatchConflictError(DispatchStateError):
    pass


class DispatchCapacityError(DispatchStateError):
    pass


class DispatchStorageError(DispatchStateError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class GpuDispatchStore:
    """Durable, single-capacity receipt ledger for pushed GPU work.

    The web service remains authoritative. This local database records byte receipt,
    execution fencing data, and callback/outbox state so a GPU service restart cannot
    silently duplicate or lose an accepted attempt.
    """

    def __init__(
        self,
        cache_root: Path,
        *,
        max_input_bytes: int = 20 * 1024**3,
        min_free_bytes: int = 10 * 1024**3,
    ) -> None:
        self.cache_root = cache_root.resolve()
        self.state_root = self.cache_root / "state"
        self.incoming_root = self.cache_root / "incoming"
        self.input_root = self.cache_root / "inputs" / "sha256"
        self.outbox_root = self.cache_root / "outbox"
        self.database = self.state_root / "dispatch.sqlite3"
        self.max_input_bytes = max_input_bytes
        self.min_free_bytes = min_free_bytes
        self._lock = threading.RLock()
        self._append_lock = asyncio.Lock()
        self._prepare_directories()
        self._initialize()
        self.reconcile_files()
        self.recover_execution_state()

    def _prepare_directories(self) -> None:
        for path in (
            self.cache_root,
            self.state_root,
            self.incoming_root,
            self.input_root,
            self.outbox_root,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        descriptor = os.open(self.database, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.database.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > 2:
                raise RuntimeError(f"Unsupported GPU dispatch database version {version}")
            if version == 0:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA synchronous = FULL;

                    CREATE TABLE dispatches (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL CHECK (attempt > 0),
                        manifest_sha256 TEXT NOT NULL,
                        protocol_version TEXT NOT NULL,
                        required_build_id TEXT NOT NULL,
                        input_fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN (
                            'receiving','verified','queued','running',
                            'outbox_pending','callback_uploading','auth_blocked',
                            'acknowledged','cancelled','rejected','failed','orphaned'
                        )),
                        start_sha256 TEXT,
                        start_payload_json TEXT,
                        lease_token TEXT,
                        lease_token_sha256 TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        queued_at TEXT,
                        finished_at TEXT,
                        UNIQUE(job_id, attempt)
                    );

                    CREATE TABLE dispatch_inputs (
                        dispatch_id TEXT NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                        artifact_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        expected_sha256 TEXT NOT NULL,
                        expected_size INTEGER NOT NULL CHECK (expected_size > 0),
                        media_type TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        accepted_offset INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL CHECK (state IN ('open','verified')),
                        cache_path TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(dispatch_id, artifact_id)
                    );

                    CREATE INDEX idx_gpu_dispatch_state
                    ON dispatches(state, created_at);

                    CREATE TABLE service_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        host_boot_id TEXT NOT NULL,
                        service_boot_id TEXT NOT NULL,
                        protocol_version TEXT NOT NULL,
                        build_id TEXT NOT NULL,
                        callback_origin TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL
                    );

                    PRAGMA user_version = 1;
                    """
                )
            if version <= 1:
                connection.executescript(
                    """
                    ALTER TABLE dispatches ADD COLUMN progress REAL NOT NULL DEFAULT 0;
                    ALTER TABLE dispatches ADD COLUMN progress_message TEXT;
                    ALTER TABLE dispatches ADD COLUMN execution_retry_count INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE dispatches ADD COLUMN next_execution_at TEXT;
                    ALTER TABLE dispatches ADD COLUMN outbox_kind TEXT;
                    ALTER TABLE dispatches ADD COLUMN outbox_payload_json TEXT;
                    ALTER TABLE dispatches ADD COLUMN callback_retry_count INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE dispatches ADD COLUMN next_callback_at TEXT;
                    ALTER TABLE dispatches ADD COLUMN last_callback_error TEXT;
                    ALTER TABLE dispatches ADD COLUMN callback_http_status INTEGER;

                    CREATE TABLE dispatch_outputs (
                        dispatch_id TEXT NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                        role TEXT NOT NULL,
                        path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        media_type TEXT NOT NULL,
                        upload_id TEXT,
                        state TEXT NOT NULL CHECK (state IN ('pending','uploaded')),
                        PRIMARY KEY(dispatch_id, ordinal)
                    );

                    CREATE TABLE callback_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        auth_blocked INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_success_at TEXT,
                        last_http_status INTEGER,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error_class TEXT
                    );
                    INSERT OR IGNORE INTO callback_state(singleton) VALUES (1);

                    PRAGMA user_version = 2;
                    """
                )
        self.database.chmod(0o600)

    def _incoming_path(self, dispatch_id: str, artifact_id: str) -> Path:
        return self.incoming_root / dispatch_id / f"{artifact_id}.part"

    def _cache_path(self, digest: str) -> Path:
        return self.input_root / digest[:2] / digest

    @staticmethod
    def _valid_cached(path: Path, digest: str, size: int) -> bool:
        return path.is_file() and path.stat().st_size == size and _sha256(path) == digest

    def reconcile_files(self) -> None:
        """Reconcile database offsets with fsynced files after an unclean stop."""
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT inputs.*, dispatches.state AS dispatch_state
                FROM dispatch_inputs AS inputs
                JOIN dispatches ON dispatches.id = inputs.dispatch_id
                ORDER BY inputs.dispatch_id, inputs.artifact_id
                """
            ).fetchall()
            affected: set[str] = set()
            for row in rows:
                dispatch_id = str(row["dispatch_id"])
                artifact_id = str(row["artifact_id"])
                digest = str(row["expected_sha256"])
                expected_size = int(row["expected_size"])
                if str(row["dispatch_state"]) in TERMINAL_STATES:
                    self._incoming_path(dispatch_id, artifact_id).unlink(missing_ok=True)
                    continue
                cached = self._cache_path(digest)
                if self._valid_cached(cached, digest, expected_size):
                    connection.execute(
                        """
                        UPDATE dispatch_inputs
                        SET state='verified', accepted_offset=?, cache_path=?, updated_at=?
                        WHERE dispatch_id=? AND artifact_id=?
                        """,
                        (expected_size, str(cached), _now(), dispatch_id, artifact_id),
                    )
                    affected.add(dispatch_id)
                    continue
                partial = self._incoming_path(dispatch_id, artifact_id)
                partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                partial.parent.chmod(0o700)
                if not partial.exists():
                    descriptor = os.open(partial, os.O_CREAT | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                partial.chmod(0o600)
                actual = min(partial.stat().st_size, expected_size)
                if partial.stat().st_size != actual:
                    with partial.open("r+b") as stream:
                        stream.truncate(actual)
                        stream.flush()
                        os.fsync(stream.fileno())
                connection.execute(
                    """
                    UPDATE dispatch_inputs
                    SET state='open', accepted_offset=?, cache_path=NULL, updated_at=?
                    WHERE dispatch_id=? AND artifact_id=?
                    """,
                    (actual, _now(), dispatch_id, artifact_id),
                )
                affected.add(dispatch_id)
            for dispatch_id in affected:
                self._refresh_dispatch_state(connection, dispatch_id)

    def mark_service_started(
        self,
        *,
        host_boot_id: str,
        service_boot_id: str,
        protocol_version: str,
        build_id: str,
        callback_origin: str,
    ) -> None:
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO service_state(
                    singleton, host_boot_id, service_boot_id, protocol_version,
                    build_id, callback_origin, started_at, heartbeat_at
                ) VALUES (1,?,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    host_boot_id=excluded.host_boot_id,
                    service_boot_id=excluded.service_boot_id,
                    protocol_version=excluded.protocol_version,
                    build_id=excluded.build_id,
                    callback_origin=excluded.callback_origin,
                    started_at=excluded.started_at,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (
                    host_boot_id,
                    service_boot_id,
                    protocol_version,
                    build_id,
                    callback_origin,
                    timestamp,
                    timestamp,
                ),
            )

    def heartbeat(self) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                "UPDATE service_state SET heartbeat_at=? WHERE singleton=1", (_now(),)
            )

    def create_dispatch(self, payload: DispatchCreate) -> tuple[dict[str, Any], bool]:
        serialized = payload.model_dump(mode="json")
        manifest_sha256 = _canonical_hash(serialized)
        total_bytes = sum(item.size_bytes for item in payload.inputs)
        if total_bytes > self.max_input_bytes:
            raise DispatchStorageError("Dispatch inputs exceed the configured size limit")

        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT manifest_sha256 FROM dispatches WHERE id=?", (payload.dispatch_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_sha256"]) != manifest_sha256:
                    raise DispatchConflictError(
                        "Dispatch ID is already registered with a different manifest"
                    )
                return self._get_dispatch(connection, payload.dispatch_id), False

            active = connection.execute(
                f"SELECT id FROM dispatches WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)}) LIMIT 1",
                ACTIVE_STATES,
            ).fetchone()
            if active is not None:
                raise DispatchCapacityError("GPU intake already has an active dispatch")

            reusable: dict[str, Path] = {}
            required_bytes = 0
            for item in payload.inputs:
                cached = self._cache_path(item.sha256)
                if self._valid_cached(cached, item.sha256, item.size_bytes):
                    reusable[item.artifact_id] = cached
                else:
                    required_bytes += item.size_bytes
            if shutil.disk_usage(self.cache_root).free - required_bytes < self.min_free_bytes:
                raise DispatchStorageError("Insufficient persistent cache space")

            timestamp = _now()
            state = "verified" if len(reusable) == len(payload.inputs) else "receiving"
            try:
                connection.execute(
                    """
                    INSERT INTO dispatches(
                        id, job_id, attempt, manifest_sha256, protocol_version,
                        required_build_id, input_fingerprint, state, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        payload.dispatch_id,
                        payload.job_id,
                        payload.attempt,
                        manifest_sha256,
                        payload.protocol_version,
                        payload.required_build_id,
                        payload.input_fingerprint,
                        state,
                        timestamp,
                        timestamp,
                    ),
                )
                for item in payload.inputs:
                    cached = reusable.get(item.artifact_id)
                    connection.execute(
                        """
                        INSERT INTO dispatch_inputs(
                            dispatch_id, artifact_id, role, expected_sha256,
                            expected_size, media_type, filename, accepted_offset,
                            state, cache_path, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            payload.dispatch_id,
                            item.artifact_id,
                            item.role,
                            item.sha256,
                            item.size_bytes,
                            item.media_type,
                            item.filename,
                            item.size_bytes if cached else 0,
                            "verified" if cached else "open",
                            str(cached) if cached else None,
                            timestamp,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise DispatchConflictError(
                    "This web job attempt is already registered"
                ) from exc

        for item in payload.inputs:
            if item.artifact_id in reusable:
                continue
            partial = self._incoming_path(payload.dispatch_id, item.artifact_id)
            partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            partial.parent.chmod(0o700)
            descriptor = os.open(partial, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            partial.chmod(0o600)
        return self.get_dispatch(payload.dispatch_id), True

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            return self._get_dispatch(connection, dispatch_id)

    def _get_dispatch(
        self, connection: sqlite3.Connection, dispatch_id: str
    ) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise DispatchNotFoundError("Dispatch does not exist")
        inputs = connection.execute(
            """
            SELECT artifact_id, role, expected_sha256 AS sha256,
                   expected_size AS size_bytes, media_type, filename,
                   accepted_offset, state
            FROM dispatch_inputs WHERE dispatch_id=? ORDER BY artifact_id
            """,
            (dispatch_id,),
        ).fetchall()
        return {
            "dispatch_id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "attempt": int(row["attempt"]),
            "protocol_version": str(row["protocol_version"]),
            "required_build_id": str(row["required_build_id"]),
            "input_fingerprint": str(row["input_fingerprint"]),
            "state": str(row["state"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "queued_at": row["queued_at"],
            "finished_at": row["finished_at"],
            "last_error": row["last_error"],
            "progress": float(row["progress"]),
            "progress_message": row["progress_message"],
            "execution_retry_count": int(row["execution_retry_count"]),
            "callback_retry_count": int(row["callback_retry_count"]),
            "next_execution_at": row["next_execution_at"],
            "next_callback_at": row["next_callback_at"],
            "last_callback_error": row["last_callback_error"],
            "callback_http_status": row["callback_http_status"],
            "inputs": [dict(item) for item in inputs],
        }

    def get_input(self, dispatch_id: str, artifact_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT artifact_id, expected_sha256 AS sha256,
                       expected_size AS size_bytes, accepted_offset, state
                FROM dispatch_inputs WHERE dispatch_id=? AND artifact_id=?
                """,
                (dispatch_id, artifact_id),
            ).fetchone()
            if row is None:
                raise DispatchNotFoundError("Dispatch input does not exist")
            return dict(row)

    @staticmethod
    def parse_content_range(value: str, expected_total: int) -> tuple[int, int]:
        if not value.startswith("bytes ") or "/" not in value or "-" not in value:
            raise ValueError("Content-Range must be 'bytes start-end/total'")
        byte_range, total_text = value[6:].split("/", 1)
        start_text, end_text = byte_range.split("-", 1)
        try:
            start, end, total = int(start_text), int(end_text), int(total_text)
        except ValueError as exc:
            raise ValueError("Content-Range must contain integers") from exc
        if total != expected_total:
            raise DispatchConflictError("Content-Range total does not match input size")
        if start < 0 or start > end or end >= total:
            raise ValueError("Content-Range is outside the registered input")
        return start, end

    async def append_input(
        self,
        dispatch_id: str,
        artifact_id: str,
        content_range: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        async with self._append_lock:
            item = self.get_input(dispatch_id, artifact_id)
            if item["state"] == "verified":
                raise DispatchConflictError("Input is already verified")
            expected_size = int(item["size_bytes"])
            start, end = self.parse_content_range(content_range, expected_size)
            accepted = int(item["accepted_offset"])
            if start > accepted:
                raise DispatchConflictError(f"Upload must resume at byte {accepted}")
            duplicate = end < accepted
            if start < accepted and not duplicate:
                raise DispatchConflictError(
                    "A chunk cannot partially overlap previously accepted bytes"
                )
            expected_chunk_size = end - start + 1
            partial = self._incoming_path(dispatch_id, artifact_id)
            partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not partial.exists():
                descriptor = os.open(partial, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(descriptor)
            received = 0
            try:
                with partial.open("rb" if duplicate else "r+b") as stream:
                    stream.seek(start)
                    async for chunk in chunks:
                        if not chunk:
                            continue
                        if received + len(chunk) > expected_chunk_size:
                            raise DispatchConflictError("Request body exceeds Content-Range")
                        if duplicate:
                            if stream.read(len(chunk)) != chunk:
                                raise DispatchConflictError(
                                    "Repeated chunk conflicts with accepted bytes"
                                )
                        else:
                            stream.write(chunk)
                        received += len(chunk)
                    if received != expected_chunk_size:
                        raise DispatchConflictError("Request body is shorter than Content-Range")
                    if not duplicate:
                        stream.flush()
                        os.fsync(stream.fileno())
            except Exception:
                if not duplicate:
                    with partial.open("r+b") as stream:
                        stream.truncate(accepted)
                        stream.flush()
                        os.fsync(stream.fileno())
                raise
            if duplicate:
                return self.get_input(dispatch_id, artifact_id)

            accepted = end + 1
            if accepted == expected_size:
                if _sha256(partial) != str(item["sha256"]):
                    partial.unlink(missing_ok=True)
                    descriptor = os.open(partial, os.O_CREAT | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                    with self._lock, self.connect() as connection:
                        connection.execute(
                            """
                            UPDATE dispatch_inputs SET accepted_offset=0, updated_at=?
                            WHERE dispatch_id=? AND artifact_id=?
                            """,
                            (_now(), dispatch_id, artifact_id),
                        )
                    raise DispatchConflictError("Completed input checksum does not match")
                cached = self._cache_path(str(item["sha256"]))
                cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                cached.parent.chmod(0o700)
                if self._valid_cached(cached, str(item["sha256"]), expected_size):
                    partial.unlink(missing_ok=True)
                else:
                    os.replace(partial, cached)
                    cached.chmod(0o600)
                    _fsync_directory(cached.parent)
                with self._lock, self.connect() as connection:
                    connection.execute(
                        """
                        UPDATE dispatch_inputs
                        SET accepted_offset=?, state='verified', cache_path=?, updated_at=?
                        WHERE dispatch_id=? AND artifact_id=?
                        """,
                        (expected_size, str(cached), _now(), dispatch_id, artifact_id),
                    )
                    self._refresh_dispatch_state(connection, dispatch_id)
            else:
                with self._lock, self.connect() as connection:
                    connection.execute(
                        """
                        UPDATE dispatch_inputs SET accepted_offset=?, updated_at=?
                        WHERE dispatch_id=? AND artifact_id=?
                        """,
                        (accepted, _now(), dispatch_id, artifact_id),
                    )
            return self.get_input(dispatch_id, artifact_id)

    def _refresh_dispatch_state(
        self, connection: sqlite3.Connection, dispatch_id: str
    ) -> None:
        dispatch = connection.execute(
            "SELECT state, start_payload_json FROM dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        if dispatch is None or str(dispatch["state"]) not in {
            "receiving",
            "verified",
            "queued",
        }:
            return
        incomplete = connection.execute(
            """
            SELECT COUNT(*) FROM dispatch_inputs
            WHERE dispatch_id=? AND state != 'verified'
            """,
            (dispatch_id,),
        ).fetchone()[0]
        if incomplete:
            state = "receiving"
        elif dispatch["start_payload_json"]:
            state = "queued"
        else:
            state = "verified"
        connection.execute(
            "UPDATE dispatches SET state=?, updated_at=? WHERE id=?",
            (state, _now(), dispatch_id),
        )

    def start_dispatch(
        self, dispatch_id: str, payload: DispatchStart, lease_token: str
    ) -> tuple[dict[str, Any], bool]:
        if len(lease_token) < 32:
            raise ValueError("X-Lease-Token is missing or invalid")
        serialized = payload.model_dump(mode="json")
        start_sha256 = _canonical_hash(serialized)
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            if row is None:
                raise DispatchNotFoundError("Dispatch does not exist")
            if row["start_sha256"] is not None:
                lease_token_sha256 = hashlib.sha256(lease_token.encode()).hexdigest()
                if (
                    str(row["start_sha256"]) != start_sha256
                    or str(row["lease_token_sha256"]) != lease_token_sha256
                ):
                    raise DispatchConflictError(
                        "Dispatch was already queued with different fencing data"
                    )
                return self._get_dispatch(connection, dispatch_id), False
            if str(row["state"]) != "verified":
                raise DispatchConflictError("Every dispatch input must be verified first")
            context = payload.context
            if (
                context.job_id != str(row["job_id"])
                or context.attempt != int(row["attempt"])
                or context.input_fingerprint != str(row["input_fingerprint"])
            ):
                raise DispatchConflictError("Execution context does not match the dispatch")
            registered = {
                str(item["artifact_id"]): (
                    str(item["sha256"]),
                    int(item["size_bytes"]),
                )
                for item in self._get_dispatch(connection, dispatch_id)["inputs"]
            }
            supplied = {
                item.artifact_id: (item.sha256, item.size_bytes) for item in context.inputs
            }
            if registered != supplied:
                raise DispatchConflictError("Execution inputs do not match verified inputs")
            timestamp = _now()
            lease_token_sha256 = hashlib.sha256(lease_token.encode()).hexdigest()
            connection.execute(
                """
                UPDATE dispatches
                SET state='queued', start_sha256=?, start_payload_json=?,
                    lease_token=?, lease_token_sha256=?, queued_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    start_sha256,
                    json.dumps(serialized, sort_keys=True, separators=(",", ":")),
                    lease_token,
                    lease_token_sha256,
                    timestamp,
                    timestamp,
                    dispatch_id,
                ),
            )
            return self._get_dispatch(connection, dispatch_id), True

    def cancel_dispatch(self, dispatch_id: str) -> tuple[dict[str, Any], bool]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            if row is None:
                raise DispatchNotFoundError("Dispatch does not exist")
            state = str(row["state"])
            if state == "cancelled":
                return self._get_dispatch(connection, dispatch_id), False
            if state not in {"receiving", "verified", "queued"}:
                raise DispatchConflictError(f"Dispatch cannot be cancelled from {state}")
            timestamp = _now()
            connection.execute(
                """
                UPDATE dispatches
                SET state='cancelled', lease_token=NULL, lease_token_sha256=NULL,
                    finished_at=?, updated_at=?
                WHERE id=?
                """,
                (timestamp, timestamp, dispatch_id),
            )
            result = self._get_dispatch(connection, dispatch_id)
        incoming = self.incoming_root / dispatch_id
        if incoming.is_dir():
            for path in incoming.glob("*.part"):
                path.unlink(missing_ok=True)
            with suppress(OSError):
                incoming.rmdir()
        return result, True

    def recover_execution_state(self) -> None:
        """Make interrupted local work safely eligible after an operator restart."""
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state='queued', next_execution_at=?, updated_at=?,
                    last_error='GPU service restarted during execution'
                WHERE state='running'
                """,
                (timestamp, timestamp),
            )
            connection.execute(
                """
                UPDATE dispatches SET state='outbox_pending', next_callback_at=?, updated_at=?
                WHERE state='callback_uploading'
                """,
                (timestamp, timestamp),
            )
            connection.execute(
                """
                UPDATE dispatches
                SET state=CASE WHEN outbox_kind IS NULL THEN 'queued' ELSE 'outbox_pending' END,
                    next_execution_at=CASE WHEN outbox_kind IS NULL THEN ? ELSE next_execution_at END,
                    next_callback_at=CASE WHEN outbox_kind IS NULL THEN next_callback_at ELSE ? END,
                    updated_at=?
                WHERE state='auth_blocked'
                """,
                (timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                UPDATE callback_state
                SET auth_blocked=0, next_retry_at=?, last_error_class=NULL
                WHERE singleton=1
                """,
                (timestamp,),
            )

    def _execution_record(
        self, connection: sqlite3.Connection, dispatch_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise DispatchNotFoundError("Dispatch does not exist")
        inputs = [
            dict(value)
            for value in connection.execute(
                """
                SELECT artifact_id, role, expected_sha256 AS sha256,
                       expected_size AS size_bytes, media_type, filename,
                       state, cache_path
                FROM dispatch_inputs WHERE dispatch_id=? ORDER BY artifact_id
                """,
                (dispatch_id,),
            ).fetchall()
        ]
        outputs = [
            dict(value)
            for value in connection.execute(
                """
                SELECT ordinal, role, path, filename, sha256, size_bytes,
                       media_type, upload_id, state
                FROM dispatch_outputs WHERE dispatch_id=? ORDER BY ordinal
                """,
                (dispatch_id,),
            ).fetchall()
        ]
        return {
            "dispatch_id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "attempt": int(row["attempt"]),
            "state": str(row["state"]),
            "input_fingerprint": str(row["input_fingerprint"]),
            "context": (
                json.loads(str(row["start_payload_json"]))
                if row["start_payload_json"]
                else None
            ),
            "lease_token": row["lease_token"],
            "outbox_kind": row["outbox_kind"],
            "outbox_payload": (
                json.loads(str(row["outbox_payload_json"]))
                if row["outbox_payload_json"]
                else None
            ),
            "inputs": inputs,
            "outputs": outputs,
            "execution_retry_count": int(row["execution_retry_count"]),
            "callback_retry_count": int(row["callback_retry_count"]),
        }

    def claim_queued_dispatch(self) -> dict[str, Any] | None:
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM dispatches
                WHERE state='queued'
                  AND (next_execution_at IS NULL OR next_execution_at<=?)
                ORDER BY queued_at, created_at LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            dispatch_id = str(row["id"])
            connection.execute(
                """
                UPDATE dispatches
                SET state='running', progress=0, progress_message='Starting',
                    next_execution_at=NULL, updated_at=?
                WHERE id=? AND state='queued'
                """,
                (timestamp, dispatch_id),
            )
            return self._execution_record(connection, dispatch_id)

    def defer_execution(
        self,
        dispatch_id: str,
        *,
        delay_seconds: int,
        error_summary: str,
        http_status: int | None = None,
    ) -> None:
        next_attempt = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state='queued', execution_retry_count=execution_retry_count+1,
                    next_execution_at=?, last_error=?, callback_http_status=?, updated_at=?
                WHERE id=? AND state='running'
                """,
                (next_attempt, error_summary[:300], http_status, _now(), dispatch_id),
            )

    def abandon_running(
        self,
        dispatch_id: str,
        *,
        state: str,
        error_summary: str,
        http_status: int | None = None,
    ) -> None:
        if state not in {"orphaned", "rejected"}:
            raise ValueError("Invalid abandoned dispatch state")
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state=?, lease_token=NULL, lease_token_sha256=NULL,
                    last_error=?, callback_http_status=?, finished_at=?, updated_at=?
                WHERE id=? AND state='running'
                """,
                (
                    state,
                    error_summary[:300],
                    http_status,
                    timestamp,
                    timestamp,
                    dispatch_id,
                ),
            )

    def update_progress(self, dispatch_id: str, value: float, message: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches SET progress=?, progress_message=?, updated_at=?
                WHERE id=? AND state='running'
                """,
                (max(0.0, min(1.0, value)), message[:1000], _now(), dispatch_id),
            )

    def record_execution_success(
        self,
        dispatch_id: str,
        completion_payload: dict[str, Any],
        outputs: list[dict[str, Any]],
    ) -> None:
        timestamp = _now()
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            if row is None or str(row["state"]) != "running":
                raise DispatchConflictError("Dispatch is no longer running")
            connection.execute(
                "DELETE FROM dispatch_outputs WHERE dispatch_id=?", (dispatch_id,)
            )
            for ordinal, output in enumerate(outputs):
                connection.execute(
                    """
                    INSERT INTO dispatch_outputs(
                        dispatch_id, ordinal, role, path, filename, sha256,
                        size_bytes, media_type, state
                    ) VALUES (?,?,?,?,?,?,?,?, 'pending')
                    """,
                    (
                        dispatch_id,
                        ordinal,
                        output["role"],
                        output["path"],
                        output["filename"],
                        output["sha256"],
                        output["size_bytes"],
                        output["media_type"],
                    ),
                )
            connection.execute(
                """
                UPDATE dispatches
                SET state='outbox_pending', outbox_kind='complete',
                    outbox_payload_json=?, progress=1, progress_message='Result pending callback',
                    next_callback_at=?, callback_retry_count=0,
                    last_callback_error=NULL, callback_http_status=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(completion_payload, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    dispatch_id,
                ),
            )

    def record_execution_failure(
        self, dispatch_id: str, failure_payload: dict[str, Any]
    ) -> None:
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM dispatch_outputs WHERE dispatch_id=?", (dispatch_id,)
            )
            connection.execute(
                """
                UPDATE dispatches
                SET state='outbox_pending', outbox_kind='fail', outbox_payload_json=?,
                    progress_message='Failure pending callback', next_callback_at=?,
                    callback_retry_count=0, last_callback_error=NULL,
                    callback_http_status=NULL, updated_at=?
                WHERE id=? AND state='running'
                """,
                (
                    json.dumps(failure_payload, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                    dispatch_id,
                ),
            )

    def claim_pending_callback(self) -> dict[str, Any] | None:
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM dispatches
                WHERE state='outbox_pending'
                  AND (next_callback_at IS NULL OR next_callback_at<=?)
                ORDER BY updated_at LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            dispatch_id = str(row["id"])
            connection.execute(
                "UPDATE dispatches SET state='callback_uploading', updated_at=? WHERE id=?",
                (timestamp, dispatch_id),
            )
            return self._execution_record(connection, dispatch_id)

    def set_output_upload_id(
        self, dispatch_id: str, ordinal: int, upload_id: str
    ) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatch_outputs SET upload_id=?
                WHERE dispatch_id=? AND ordinal=? AND upload_id IS NULL
                """,
                (upload_id, dispatch_id, ordinal),
            )

    def mark_output_uploaded(self, dispatch_id: str, ordinal: int) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatch_outputs SET state='uploaded'
                WHERE dispatch_id=? AND ordinal=?
                """,
                (dispatch_id, ordinal),
            )

    def retry_callback(
        self,
        dispatch_id: str,
        *,
        delay_seconds: int,
        error_class: str,
        http_status: int | None,
    ) -> None:
        next_attempt = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state='outbox_pending', callback_retry_count=callback_retry_count+1,
                    next_callback_at=?, last_callback_error=?, callback_http_status=?,
                    updated_at=?
                WHERE id=? AND state='callback_uploading'
                """,
                (next_attempt, error_class[:120], http_status, _now(), dispatch_id),
            )

    def block_callback_auth(self, dispatch_id: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state='auth_blocked', last_callback_error='authentication_failed',
                    callback_http_status=401, updated_at=?
                WHERE id=?
                """,
                (_now(), dispatch_id),
            )
            connection.execute(
                """
                UPDATE callback_state SET auth_blocked=1, last_http_status=401,
                    last_attempt_at=?, consecutive_failures=consecutive_failures+1,
                    next_retry_at=NULL, last_error_class='authentication_failed'
                WHERE singleton=1
                """,
                (_now(),),
            )

    def finish_callback(self, dispatch_id: str, state: str) -> None:
        if state not in {"acknowledged", "orphaned", "rejected"}:
            raise ValueError("Invalid terminal callback state")
        timestamp = _now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE dispatches
                SET state=?, lease_token=NULL, lease_token_sha256=NULL,
                    outbox_payload_json=NULL, next_callback_at=NULL,
                    progress_message=?, finished_at=?, updated_at=?
                WHERE id=? AND state='callback_uploading'
                """,
                (state, state.replace("_", " ").title(), timestamp, timestamp, dispatch_id),
            )

    def record_callback_health(
        self,
        *,
        success: bool,
        http_status: int | None,
        error_class: str | None,
        next_retry_seconds: int | None = None,
    ) -> None:
        timestamp = _now()
        next_retry = (
            (datetime.now(UTC) + timedelta(seconds=next_retry_seconds)).isoformat()
            if next_retry_seconds is not None
            else None
        )
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE callback_state
                SET last_attempt_at=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                    last_http_status=?, consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures+1 END,
                    next_retry_at=?, last_error_class=?, auth_blocked=CASE WHEN ?=401 THEN 1 ELSE auth_blocked END
                WHERE singleton=1
                """,
                (
                    timestamp,
                    int(success),
                    timestamp,
                    http_status,
                    int(success),
                    next_retry,
                    error_class,
                    http_status,
                ),
            )

    def callback_auth_blocked(self) -> bool:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT auth_blocked FROM callback_state WHERE singleton=1"
            ).fetchone()
            return bool(row["auth_blocked"])

    def status(self) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            service = connection.execute(
                "SELECT * FROM service_state WHERE singleton=1"
            ).fetchone()
            callback = connection.execute(
                "SELECT * FROM callback_state WHERE singleton=1"
            ).fetchone()
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM dispatches GROUP BY state"
                ).fetchall()
            }
            active = connection.execute(
                f"""
                SELECT id FROM dispatches
                WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)})
                ORDER BY created_at LIMIT 1
                """,
                ACTIVE_STATES,
            ).fetchone()
            current = self._get_dispatch(connection, str(active["id"])) if active else None
        service_data = dict(service) if service is not None else None
        return {
            "service": service_data,
            "callback": dict(callback) if callback is not None else None,
            "accepting_dispatches": active is None,
            "safe_to_stop": active is None,
            "current_dispatch": current,
            "dispatch_counts": counts,
            "persistent_state": "available",
        }
