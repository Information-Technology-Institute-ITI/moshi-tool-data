from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = ROOT / "moshi_data_pipeline"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    values.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return values


def test_gpu_runtime_does_not_import_web_or_catalog_state() -> None:
    forbidden = {
        "moshi_data_pipeline.studio.artifacts",
        "moshi_data_pipeline.studio.asgi",
        "moshi_data_pipeline.studio.catalog",
        "moshi_data_pipeline.studio.job_contexts",
        "moshi_data_pipeline.studio.lifecycle",
        "moshi_data_pipeline.studio.migrations",
        "moshi_data_pipeline.studio.observability",
        "moshi_data_pipeline.studio.protocol",
        "moshi_data_pipeline.studio.server",
        "moshi_data_pipeline.studio.service",
    }
    for path in PACKAGE_ROOT.rglob("*.py"):
        assert not (_imports(path) & forbidden), (
            f"{path.relative_to(PACKAGE_ROOT)} imports web state"
        )


def test_repository_omits_removed_deployment_modes() -> None:
    for relative in (
        "compose.yaml",
        "deployment",
        "processing_service",
        "web",
        "web_service",
    ):
        assert not (ROOT / relative).exists()


def test_repository_omits_web_backend_and_pull_entrypoints() -> None:
    for relative in (
        "remote_worker.py",
        "remote_worker_main.py",
        "studio/artifacts.py",
        "studio/asgi.py",
        "studio/catalog.py",
        "studio/cleanup.py",
        "studio/job_contexts.py",
        "studio/lifecycle.py",
        "studio/migrations.py",
        "studio/observability.py",
        "studio/protocol.py",
        "studio/server.py",
        "studio/service.py",
        "studio/worker.py",
        "studio/workspace_migration.py",
        "studio/static",
    ):
        assert not (PACKAGE_ROOT / relative).exists()


def test_gpu_systemd_unit_starts_intake_with_protected_callback_token() -> None:
    unit = (ROOT / "systemd/moshi-gpu-intake.service").read_text(encoding="utf-8")
    token_file = "/home/ubuntu/.config/moshi/callback-token.env"
    assert f"EnvironmentFile={token_file}" not in unit
    assert f". {token_file}" in unit
    assert "moshi_data_pipeline.gpu_intake_main" in unit
    assert "ConditionPathIsDirectory=/home/ubuntu/moshi-gpu-cache" in unit
    assert "ReadWritePaths=/home/ubuntu/moshi-gpu-cache" in unit
    assert "moshi-worker-cache" not in unit
    assert "worker-token.env" not in unit
    assert "set -a" in unit
    assert "set +a" in unit


def test_environment_example_uses_gpu_cache_name() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MOSHI_GPU_CACHE=/home/ubuntu/moshi-gpu-cache" in example
    assert "MOSHI_WORKER_CACHE" not in example
