# Repository Structure

This checkout contains only the g4dn WhisperX processing service. The m8i website, frontend, catalog, workspace, and deployment automation are maintained separately.

## Root

| Path | Purpose |
| --- | --- |
| `moshi_data_pipeline/` | Canonical GPU intake, dispatch, callback, and WhisperX execution source. |
| `processing_service/` | Self-contained CUDA Docker build context for g4dn. |
| `tests/` | GPU-only contract and execution tests. |
| `compose.yaml` | Standalone GPU service composition using host networking. |
| `.env.example` | Required intake, callback, build, and model environment variables. |
| `pyproject.toml`, `uv.lock` | Source-host Python environment and frozen dependencies. |

## GPU Runtime

The active entrypoint is `moshi_data_pipeline.gpu_intake_main`. Protocol 2.0 intake modules accept only transcription dispatches. Callback protocol 1.0 names such as `MOSHI_WORKER_TOKEN`, `worker_id`, and `/internal/v1/workers/heartbeat` are compatibility fields for communication with m8i; they do not represent another runtime.

`processing_service/moshi_data_pipeline/` contains the deployed package copy. `tests/test_service_build_contexts.py` verifies the active GPU modules match the canonical source and that web/catalog modules are absent from the image context.

## Persistent Data

The GPU cache contains model files, checksum-addressed inputs, disposable attempt directories, callback outbox files, and private restart-recovery state. It never contains or mounts the m8i workspace or catalog.

## Tests

- `test_gpu_intake.py`: authentication, dispatch receipt, uploads, readiness, and functional checks.
- `test_gpu_execution.py`: execution, restart recovery, and durable callbacks.
- `test_gpu_callback.py`: callback protocol transport.
- `test_gpu_job_protocol.py`: transcription-only immutable job contexts.
- `test_service_build_contexts.py`: image isolation, source synchronization, Compose, and systemd checks.
