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
from datetime import UTC, datetime
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
            if version > 1:
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
        if dispatch is None or str(dispatch["state"]) in TERMINAL_STATES:
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

    def status(self) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            service = connection.execute(
                "SELECT * FROM service_state WHERE singleton=1"
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
            "accepting_dispatches": active is None,
            "safe_to_stop": active is None,
            "current_dispatch": current,
            "dispatch_counts": counts,
            "persistent_state": "available",
        }
