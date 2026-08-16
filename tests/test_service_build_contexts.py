from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    values.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return values


def test_services_never_import_the_other_build_context() -> None:
    for folder, forbidden in (
        (ROOT / "web_service", "processing_service"),
        (ROOT / "processing_service", "web_service"),
    ):
        for path in (folder / "moshi_data_pipeline").rglob("*.py"):
            assert all(
                not value.startswith(forbidden) for value in _imports(path)
            ), f"{path} imports {forbidden}"


def test_processing_runtime_contracts_do_not_import_web_state() -> None:
    forbidden = {
        "moshi_data_pipeline.studio.catalog",
        "moshi_data_pipeline.studio.server",
        "moshi_data_pipeline.studio.service",
    }
    root = ROOT / "processing_service/moshi_data_pipeline"
    for relative in (
        "callback_contract.py",
        "gpu_dispatch_protocol.py",
        "gpu_job_protocol.py",
        "gpu_dispatch_state.py",
        "gpu_execution.py",
        "gpu_intake.py",
        "gpu_intake_main.py",
        "gpu_self_check.py",
        "remote_worker.py",
        "remote_worker_main.py",
        "studio/execution_runtime.py",
        "studio/execution_contracts.py",
        "studio/job_contracts.py",
        "studio/processing.py",
    ):
        path = root / relative
        assert not (_imports(path) & forbidden), f"{relative} imports web state"


def test_dockerfiles_use_only_their_own_build_context() -> None:
    for folder in (ROOT / "web_service", ROOT / "processing_service"):
        dockerfile = (folder / "Dockerfile").read_text(encoding="utf-8")
        copy_lines = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip().upper().startswith(("COPY ", "ADD "))
        ]
        assert copy_lines
        assert all("../" not in line and "..\\" not in line for line in copy_lines)


def test_web_runtime_omits_processing_dependencies() -> None:
    project = (ROOT / "web_service/pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "web_service/Dockerfile").read_text(encoding="utf-8")
    for value in ("torch", "whisper", "pyannote", '"numpy', '"scipy', '"soundfile'):
        assert value not in project.casefold()
    assert "ffmpeg" not in dockerfile.casefold()
    assert "moshi_data_pipeline.studio.web_main" in dockerfile


def test_service_protocol_files_are_byte_identical() -> None:
    web = (ROOT / "web_service/protocol/worker_protocol.schema.json").read_bytes()
    processing = (
        ROOT / "processing_service/protocol/worker_protocol.schema.json"
    ).read_bytes()
    assert web == processing


def test_gpu_intake_sources_match_processing_build_context() -> None:
    for relative in (
        "gpu_dispatch_protocol.py",
        "gpu_dispatch_state.py",
        "gpu_execution.py",
        "gpu_intake.py",
        "gpu_intake_main.py",
        "gpu_self_check.py",
    ):
        root = (ROOT / "moshi_data_pipeline" / relative).read_bytes()
        processing = (
            ROOT / "processing_service" / "moshi_data_pipeline" / relative
        ).read_bytes()
        assert root == processing, f"{relative} is out of sync"


def test_frontend_build_contexts_are_byte_identical() -> None:
    canonical_root = ROOT / "web"
    deployment_root = ROOT / "web_service/frontend"

    def files(root: Path) -> dict[Path, bytes]:
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
            and not ({"node_modules", "dist"} & set(path.relative_to(root).parts))
        }

    canonical = files(canonical_root)
    deployment = files(deployment_root)
    assert canonical.keys() == deployment.keys()
    for relative, content in canonical.items():
        assert content == deployment[relative], f"frontend/{relative} is out of sync"


def test_m8i_python_sources_match_web_build_context() -> None:
    for relative in (
        "callback_contract.py",
        "gpu_dispatch_client.py",
        "gpu_dispatch_protocol.py",
        "gpu_job_protocol.py",
        "studio/__init__.py",
        "studio/artifacts.py",
        "studio/asgi.py",
        "studio/catalog.py",
        "studio/gpu_dispatcher.py",
        "studio/gpu_status.py",
        "studio/job_contexts.py",
        "studio/lifecycle.py",
        "studio/migrations.py",
        "studio/server.py",
        "studio/service.py",
        "studio/web_main.py",
    ):
        canonical = (ROOT / "moshi_data_pipeline" / relative).read_bytes()
        deployment = (
            ROOT / "web_service/moshi_data_pipeline" / relative
        ).read_bytes()
        assert canonical == deployment, f"{relative} is out of sync"


def test_environment_example_contains_names_not_secret_values() -> None:
    values = {
        key: value
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }
    assert values["MOSHI_WORKER_TOKEN"] == ""
    assert values["MOSHI_DISPATCH_TOKEN"] == ""
    assert values["gpu_port"] == "8766"
    assert values["MOSHI_WEB_PORT"] == "80"


def test_push_port_wiring_uses_gpu_port_8766_not_web_app_port() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "http://processing:${gpu_port:-8766}" in compose
    assert "MOSHI_GPU_INTAKE_PORT: ${gpu_port:-8766}" in compose
    assert '"${gpu_port:-8766}"' in compose
    assert "MOSHI_GPU_INTAKE_PORT: ${gpu_port:-8765}" not in compose
    # The private bridge callback reaches Uvicorn; production callbacks use m8i port 80.
    assert "MOSHI_WEB_INTERNAL_URL: http://web:${MOSHI_WEB_PORT}" in compose
    assert "MOSHI_GPU_REQUIRED_BUILD_ID: ${MOSHI_GPU_REQUIRED_BUILD_ID:?" in compose


def test_linux_proxy_example_has_narrow_callback_and_identity_boundaries() -> None:
    nginx = (ROOT / "web_service/nginx/moshi.conf").read_text(encoding="utf-8")
    assert "listen 80 default_server" in nginx
    assert "allow 172.31.26.80" in nginx
    assert "location = /internal/v1/workers/heartbeat" in nginx
    assert "(heartbeat|uploads|complete|fail)" in nginx
    assert "location ~ ^/internal/v1/uploads/" in nginx
    assert "limit_except HEAD PUT" in nginx
    assert "location ^~ /internal/" in nginx
    assert "proxy_set_header X-Moshi-Authenticated-User \"\"" in nginx
    assert "proxy_set_header X-Moshi-Authenticated-User $remote_user" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx
    assert "proxy_request_buffering off" in nginx


def test_root_compose_does_not_mount_web_data_into_worker() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    processing = re.search(
        r"(?ms)^  processing:\s*(.*?)(?=^  [a-zA-Z]|^networks:)", compose
    )
    assert processing is not None
    block = processing.group(1)
    assert "studio_workspace" not in block
    assert "worker_cache:/cache" in block


def test_gpu_systemd_unit_sources_exported_callback_token() -> None:
    unit = (
        ROOT / "processing_service/systemd/moshi-gpu-intake.service"
    ).read_text(encoding="utf-8")
    token_file = "/home/ubuntu/.config/moshi/worker-token.env"

    assert f"EnvironmentFile={token_file}" not in unit
    assert f". {token_file}" in unit
    assert "set -a" in unit
    assert "set +a" in unit
