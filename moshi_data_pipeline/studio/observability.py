from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moshi_data_pipeline.studio.catalog import StudioCatalog


class CloudWatchMetricsPublisher:
    """Publish low-cardinality operational metrics without job or transcript content."""

    def __init__(
        self,
        catalog: StudioCatalog,
        workspace: Path,
        *,
        namespace: str = "Moshi/Studio",
        backup_directory: Path | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("cloudwatch")
        self.client = client
        self.catalog = catalog
        self.workspace = workspace.resolve()
        self.namespace = namespace
        self.backup_directory = (
            backup_directory.resolve()
            if backup_directory
            else self.workspace.parent / "backups"
        )

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    def __call__(self, lifecycle: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        queue = self.catalog.queue_summary()
        worker = self.catalog.latest_worker_state()
        heartbeat = self._parse(worker["last_heartbeat"]) if worker else None
        heartbeat_age = max(0.0, (now - heartbeat).total_seconds()) if heartbeat else 0.0
        with self.catalog.connect() as connection:
            oldest = connection.execute(
                "SELECT MIN(created_at) AS value FROM jobs WHERE status='queued'"
            ).fetchone()["value"]
        oldest_time = self._parse(oldest)
        queue_age = max(0.0, (now - oldest_time).total_seconds()) if oldest_time else 0.0
        disk = shutil.disk_usage(self.workspace)
        disk_percent = 100.0 * (disk.total - disk.free) / disk.total
        backup_files = list(self.backup_directory.glob("catalog-*.sqlite3"))
        backup_age = (
            max(0.0, now.timestamp() - max(path.stat().st_mtime for path in backup_files))
            if backup_files
            else 7 * 24 * 60 * 60.0
        )
        queued_work = queue["queued"] > 0
        worker_unavailable = bool(
            queued_work
            and (
                heartbeat is None
                or heartbeat_age > 120
                or not worker
                or not worker["compatible"]
            )
        )
        metrics = [
            ("QueuedJobs", float(queue["queued"]), "Count"),
            ("RunningLeases", float(queue["running"]), "Count"),
            ("QueueOldestAgeSeconds", queue_age, "Seconds"),
            ("WorkerUnavailableWithQueuedWork", float(worker_unavailable), "Count"),
            ("LifecycleBlocked", float(bool(lifecycle.get("blocked_reason"))), "Count"),
            ("ControllerError", float(bool(lifecycle.get("last_error"))), "Count"),
            ("GpuInstanceRunning", float(lifecycle.get("instance_state") == "running"), "Count"),
            ("WorkspaceUsedPercent", disk_percent, "Percent"),
            ("SqliteBackupAgeSeconds", backup_age, "Seconds"),
        ]
        self.client.put_metric_data(
            Namespace=self.namespace,
            MetricData=[
                {"MetricName": name, "Value": value, "Unit": unit}
                for name, value, unit in metrics
            ],
        )

