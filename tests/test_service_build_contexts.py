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
        "gpu_dispatch_protocol.py",
        "gpu_dispatch_state.py",
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
    assert '"--workers", "1"' in dockerfile


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
        "gpu_intake.py",
        "gpu_intake_main.py",
        "gpu_self_check.py",
    ):
        root = (ROOT / "moshi_data_pipeline" / relative).read_bytes()
        processing = (
            ROOT / "processing_service" / "moshi_data_pipeline" / relative
        ).read_bytes()
        assert root == processing, f"{relative} is out of sync"


def test_root_compose_does_not_mount_web_data_into_worker() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    processing = re.search(
        r"(?ms)^  processing:\s*(.*?)(?=^  [a-zA-Z]|^networks:)", compose
    )
    assert processing is not None
    block = processing.group(1)
    assert "studio_workspace" not in block
    assert "worker_cache:/cache" in block
