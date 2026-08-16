from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
CANONICAL_ROOT = ROOT / "moshi_data_pipeline"
DEPLOYED_ROOT = ROOT / "processing_service/moshi_data_pipeline"


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
    for package_root in (CANONICAL_ROOT, DEPLOYED_ROOT):
        for path in package_root.rglob("*.py"):
            assert not (_imports(path) & forbidden), (
                f"{path.relative_to(package_root)} imports web state"
            )


def test_gpu_build_context_omits_web_backend_and_pull_entrypoints() -> None:
    for name in (
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
        assert not (DEPLOYED_ROOT / name).exists()


def test_gpu_sources_match_canonical_implementations() -> None:
    canonical_files = {
        path.relative_to(CANONICAL_ROOT) for path in CANONICAL_ROOT.rglob("*.py")
    }
    deployed_files = {
        path.relative_to(DEPLOYED_ROOT) for path in DEPLOYED_ROOT.rglob("*.py")
    }
    assert canonical_files == deployed_files
    for relative in canonical_files:
        assert (CANONICAL_ROOT / relative).read_bytes() == (
            DEPLOYED_ROOT / relative
        ).read_bytes(), f"{relative} is out of sync"


def test_gpu_container_starts_push_intake_only() -> None:
    dockerfile = (ROOT / "processing_service/Dockerfile").read_text(encoding="utf-8")
    assert "moshi_data_pipeline.gpu_intake_main" in dockerfile
    assert "remote_worker" not in dockerfile
    assert "../" not in dockerfile


def test_gpu_compose_has_private_cache_and_separate_tokens() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "web_service" not in compose
    assert "depends_on" not in compose
    assert "MOSHI_DISPATCH_TOKEN" in compose
    assert "MOSHI_WORKER_TOKEN" in compose
    assert "MOSHI_WEB_INTERNAL_URL" in compose
    assert "MOSHI_GPU_CACHE: /cache" in compose
    assert "gpu_cache:/cache" in compose
    assert "network_mode: host" in compose


def test_gpu_systemd_unit_starts_intake_with_protected_callback_token() -> None:
    unit = (ROOT / "processing_service/systemd/moshi-gpu-intake.service").read_text(
        encoding="utf-8"
    )
    token_file = "/home/ubuntu/.config/moshi/callback-token.env"
    assert f"EnvironmentFile={token_file}" not in unit
    assert f". {token_file}" in unit
    assert "moshi_data_pipeline.gpu_intake_main" in unit
    assert "set -a" in unit
    assert "set +a" in unit
