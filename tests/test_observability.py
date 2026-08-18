from __future__ import annotations

from moshi_data_pipeline.studio.catalog import StudioCatalog
from moshi_data_pipeline.studio.observability import CloudWatchMetricsPublisher


class MetricsClient:
    def __init__(self) -> None:
        self.calls = []

    def put_metric_data(self, **values) -> None:
        self.calls.append(values)


def test_cloudwatch_metrics_cover_queue_cost_disk_backup_and_controller(tmp_path) -> None:
    workspace = tmp_path / "studio_workspace"
    workspace.mkdir()
    catalog = StudioCatalog(workspace / "catalog.sqlite3")
    project = catalog.create_project(
        "Metrics", owner_user_id=catalog.ensure_local_admin()["id"]
    )
    catalog.create_job(project["id"], "transcribe", None)
    client = MetricsClient()
    publisher = CloudWatchMetricsPublisher(
        catalog,
        workspace,
        client=client,
    )
    publisher(
        {
            "instance_state": "running",
            "blocked_reason": "incompatible worker",
            "last_error": None,
        }
    )
    assert len(client.calls) == 1
    assert client.calls[0]["Namespace"] == "Moshi/Studio"
    metrics = {
        value["MetricName"]: value["Value"]
        for value in client.calls[0]["MetricData"]
    }
    assert metrics["QueuedJobs"] == 1
    assert metrics["WorkerUnavailableWithQueuedWork"] == 1
    assert metrics["LifecycleBlocked"] == 1
    assert metrics["GpuInstanceRunning"] == 1
    assert 0 <= metrics["WorkspaceUsedPercent"] <= 100
    assert metrics["SqliteBackupAgeSeconds"] == 7 * 24 * 60 * 60

