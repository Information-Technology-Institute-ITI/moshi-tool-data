# Moshi WhisperX GPU Service

This repository is the g4dn/WhisperX half of Moshi. The m8i server owns the website, user actions, job authority, permanent artifacts, and its SQLite catalog. This service never runs the website and never opens or migrates the m8i database.

## Responsibility

The GPU service:

- exposes a private authenticated intake on TCP 8766;
- accepts immutable protocol 2.0 transcription dispatches from m8i;
- verifies input sizes and SHA-256 checksums;
- runs one WhisperX transcription at a time;
- retains progress and callback state across restarts;
- uploads artifacts and sends completion or failure callbacks to m8i.

Only the `transcribe` job kind is accepted.

```text
browser -> m8i website
m8i -> g4dn /internal/v2/*
g4dn -> m8i /internal/v1/* callbacks
```

There is no pull loop, local website executor, or separate worker service.

## Storage

`MOSHI_GPU_CACHE` is private GPU state and model storage. It may contain `dispatch.sqlite3` and `self-check.sqlite3` for restart recovery. These files are not the m8i catalog and have no catalog migrations.

```text
<gpu-cache>/
  state/
  inputs/
  attempts/
  outbox/
  logs/
  huggingface/
```

## Configuration

Required variables:

- `MOSHI_DISPATCH_TOKEN`: authenticates m8i requests to the GPU intake.
- `MOSHI_WORKER_TOKEN`: authenticates GPU callbacks to m8i. This name is retained only for callback protocol 1.0 compatibility.
- `MOSHI_BUILD_ID`: immutable deployed build identifier.
- `MOSHI_WEB_INTERNAL_URL`: private m8i callback origin.
- `MOSHI_GPU_CACHE`: persistent GPU cache and dispatch-state directory.
- `HF_TOKEN`: Hugging Face credential when required by the configured models.

Optional intake settings include `MOSHI_GPU_INTAKE_HOST`, `MOSHI_GPU_INTAKE_PORT`, and `MOSHI_DISPATCH_TOKEN_NEXT`.

## Run

The source-host service entrypoint is:

```bash
python -m moshi_data_pipeline.gpu_intake_main
```

For an Ubuntu source deployment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e .
sudo install -o root -g root -m 0644 \
  systemd/moshi-gpu-intake.service \
  /etc/systemd/system/moshi-gpu-intake.service
sudo systemctl daemon-reload
sudo systemctl enable --now moshi-gpu-intake.service
```

The service expects protected environment files in `/home/ubuntu/.config/moshi/`
and persistent state in `/home/ubuntu/moshi-gpu-cache/`. AWS security groups and
the host firewall must restrict TCP 8766 to the m8i private address.

## Layout

- `moshi_data_pipeline/`: source-host GPU intake and WhisperX implementation.
- `systemd/`: hardened source-host service unit.
- `tests/`: GPU intake, callback, execution, protocol, and runtime-layout tests.
- `requirements.txt`, `pyproject.toml`, `uv.lock`: locked Python runtime inputs.

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check moshi_data_pipeline tests
systemd-analyze verify systemd/moshi-gpu-intake.service
```

The private `/internal/v2/*` routes require Bearer authentication. Only `GET /health/live` is unauthenticated, and port 8766 must never be exposed publicly.
