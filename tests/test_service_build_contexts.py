from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_root_is_the_only_web_build_context() -> None:
    assert (ROOT / "Dockerfile").is_file()
    assert (ROOT / "frontend/package.json").is_file()
    assert (ROOT / "moshi_data_pipeline/studio/asgi.py").is_file()
    assert not (ROOT / "web/package.json").exists()
    assert not (ROOT / "web_service/Dockerfile").exists()
    assert not (ROOT / "processing_service/Dockerfile").exists()


def test_web_runtime_omits_gpu_execution_runtime() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").casefold()
    for value in ("torch", "whisper", "pyannote", '"numpy', '"scipy', '"soundfile'):
        assert value not in project
    assert "ffmpeg" not in dockerfile
    assert "moshi_data_pipeline.studio.web_main" in dockerfile
    for relative in (
        "gpu_dispatch_state.py",
        "gpu_execution.py",
        "gpu_intake.py",
        "gpu_intake_main.py",
        "gpu_self_check.py",
    ):
        assert not (ROOT / "moshi_data_pipeline" / relative).exists()


def test_dockerfile_uses_only_the_root_context() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip().upper().startswith(("COPY ", "ADD "))
    ]
    assert copy_lines
    assert all("../" not in line and "..\\" not in line for line in copy_lines)


def test_environment_example_contains_names_not_secret_values() -> None:
    values = {
        key: value
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }
    assert values["MOSHI_WORKER_TOKEN"] == ""
    assert values["MOSHI_DISPATCH_TOKEN"] == ""
    assert values["MOSHI_SMTP_PASSWORD"] == ""
    assert values["MOSHI_WEB_PORT"] == "80"
    assert values["MOSHI_REQUIRE_SIGN_IN"] == "1"


def test_root_compose_runs_only_the_web_service() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "context: ." in compose
    assert "\n  processing:" not in compose
    assert "gpu_intake_main" not in compose
    assert "MOSHI_GPU_INTERNAL_URL" in compose
    assert "MOSHI_GPU_REQUIRED_BUILD_ID" in compose


def test_linux_proxy_example_has_narrow_callback_boundaries() -> None:
    nginx = (ROOT / "nginx/moshi.conf").read_text(encoding="utf-8")
    assert "listen 80 default_server" in nginx
    assert "allow 172.31.26.80" in nginx
    assert "location = /internal/v1/workers/heartbeat" in nginx
    assert "(heartbeat|uploads|complete|fail)" in nginx
    assert "location ~ ^/internal/v1/uploads/" in nginx
    assert "limit_except HEAD PUT" in nginx
    assert "location ^~ /internal/" in nginx
    assert "proxy_request_buffering off" in nginx


def test_frontend_build_targets_the_packaged_static_directory() -> None:
    vite = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    assert "../moshi_data_pipeline/studio/static" in vite
    assert (ROOT / "moshi_data_pipeline/studio/static/index.html").is_file()


def test_worker_protocol_schema_has_one_authoritative_copy() -> None:
    assert (ROOT / "protocol/worker_protocol.schema.json").is_file()
