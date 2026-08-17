# Repository Structure

This checkout contains only the g4dn WhisperX processing service. The m8i website, frontend, catalog, workspace, and deployment automation are maintained separately.

## Root

| Path | Purpose |
| --- | --- |
| `moshi_data_pipeline/` | GPU intake, dispatch, callback, and WhisperX execution source. |
| `tests/` | GPU-only contract and execution tests. |
| `systemd/` | Hardened source-host service unit. |
| `.env.example` | Required intake, callback, build, and model environment variables. |
| `requirements.txt`, `pyproject.toml`, `uv.lock` | Source-host Python environment and frozen dependencies. |

## GPU Runtime

The active entrypoint is `moshi_data_pipeline.gpu_intake_main`. Protocol 2.0 intake modules accept typed `initialize` and `transcribe` dispatches. Callback protocol 1.0 names such as `MOSHI_WORKER_TOKEN`, `worker_id`, and `/internal/v1/workers/heartbeat` are compatibility fields for communication with m8i; they do not represent another runtime.

There is one package tree and one deployment mode. `tests/test_runtime_layout.py`
verifies that removed web, container, and pull-worker artifacts do not return and
that the systemd unit uses the protected source-host paths.

## Persistent Data

The GPU cache contains model files, checksum-addressed inputs, disposable attempt directories, callback outbox files, and private restart-recovery state. It never contains or mounts the m8i workspace or catalog.

## Tests

- `test_gpu_intake.py`: authentication, dispatch receipt, uploads, readiness, and functional checks.
- `test_gpu_execution.py`: execution, restart recovery, and durable callbacks.
- `test_gpu_callback.py`: callback protocol transport.
- `test_gpu_job_protocol.py`: transcription-only immutable job contexts.
- `test_runtime_layout.py`: standalone repository and systemd layout checks.
