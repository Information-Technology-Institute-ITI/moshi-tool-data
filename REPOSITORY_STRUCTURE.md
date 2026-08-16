# Repository structure and required runtime files

This repository contains one canonical Python source tree plus independent web and GPU deployment
build contexts. The deployed machines must never share a database or workspace mount. The m8i web
host owns jobs, artifacts, and SQLite; the g4dn host owns only a persistent processing cache and
temporary execution state.

## Top-level files

| Path | Purpose |
| --- | --- |
| `.env.example` | Example local environment names. It must contain no real credentials. |
| `.env` | Ignored local secrets/configuration. It is not part of Git. Production services use protected files outside the repository with mode 0600 on Linux or a restrictive Windows ACL. |
| `.gitignore` | Excludes secrets, caches, local media, virtual environments, and generated files. |
| `README.md` | Product overview, installation, CLI, and pipeline workflow. |
| `PLAN.md` | Historical architecture and implementation plan. Check current code/deployment docs before treating it as operational truth. |
| `REPOSITORY_STRUCTURE.md` | This directory and runtime-layout reference. |
| `compose.yaml` | Local push-mode container composition and cache-volume wiring. Its web port is required through `MOSHI_WEB_PORT`; it is not the EC2 supervisor. |
| `config.example.yaml` | Complete processing configuration with immutable model revisions. Do not silently change model settings between machines. |
| `pyproject.toml` | Canonical Python package metadata, exact dependencies, tests, and lint configuration. |
| `uv.lock` | Canonical reproducible Python dependency lock. |

## Tracked directories

### `moshi_data_pipeline/`

Canonical Python package used by local development and tests. GPU-specific changes are mirrored
into `processing_service/moshi_data_pipeline/`; m8i-specific changes are mirrored into
`web_service/moshi_data_pipeline/`. Both Docker builds use isolated contexts.

| Subdirectory/file group | Purpose |
| --- | --- |
| `audio/` | FFmpeg integration, PCM I/O, channel routing, audio metrics, and validation. |
| `output/` | Moshi alignment JSON, manifests, and reports. |
| `review/` | Legacy lightweight review server and static assets. It is not started on g4dn. |
| `schemas/` | JSON Schemas for manifests, alignment output, and QC reports. |
| `segmentation/` | Conversation-aware clip boundaries and segmentation policies. |
| `speakers/` | Diarization, speaker assignment/identity, overlap detection, and optional separation. |
| `studio/` | Dataset Studio domain, SQLite catalog, migrations, API, lifecycle, job contracts, isolated execution runtime, artifact commits, and web bundle. It is authoritative on m8i. GPU modules import only processing contracts/runtime, never web catalog/service state. |
| `studio/static/` | Built browser files served by the Python web application. `assets/` contains bundled JS/CSS. |
| `transcription/` | WhisperX transcription, alignment, normalization, and quality checks. |
| `gpu_dispatch_protocol.py` | Strict protocol 2.0 manifest, start, and functional-check request models. |
| `gpu_dispatch_client.py` | Authenticated m8i protocol 2.0 client, resumable upload, response classification, and reconciliation primitives. |
| `gpu_dispatch_state.py` | Persistent g4dn receipt, execution, outbox, callback, and service-state ledger. |
| `gpu_execution.py` | One-at-a-time execution, lease heartbeat, restart recovery, resumable artifact callback, and worker heartbeat loops. |
| `gpu_intake.py` | Private authenticated FastAPI intake and readiness/status endpoints on TCP 8766. |
| `gpu_intake_main.py` | Uvicorn/systemd entrypoint for the push-mode GPU service. |
| `gpu_self_check.py` | Boot/build/model/fixture-scoped CUDA WhisperX functional check and persistent history. |
| `remote_worker.py` | Shared v1 m8i callback client and legacy pull-worker implementation. Push mode reuses its transport primitives. |
| `remote_worker_main.py` | Legacy pull-mode entrypoint retained only for rollback. Never run it with `gpu_intake_main`. |
| Other top-level Python files | CLI, configuration, model pinning, logging, caches, benchmark, pipeline orchestration, and synthetic-data support. |

The important `studio/` subareas are:

| File group | Purpose |
| --- | --- |
| `catalog.py`, `migrations.py`, `media.py` | m8i SQLite schema and workspace paths. |
| `server.py`, `service.py`, `asgi.py` | HTTP endpoints and authoritative application service. |
| `lifecycle.py` | EC2 start/stop controller. It runs only on m8i. |
| `gpu_dispatcher.py`, `gpu_status.py` | Single-leader durable push coordination plus aggregated machine/service/check/history state for the website. |
| `protocol.py`, `job_contracts.py`, `job_contexts.py` | Versioned worker payloads, typed results, and immutable job snapshots. |
| `execution_runtime.py`, `execution_contracts.py`, `processing.py` | Database-free processing adapter and algorithms reused on g4dn. |
| `artifacts.py` | Resumable upload, checksum verification, and atomic artifact commit. |
| `domain.py`, `activity.py`, `planning.py`, `quality_metrics.py` | Annotation, activity, clip planning, and quality domain logic. |
| `exporter.py`, `clip_registry.py`, `cleanup.py` | Export construction, clip artifact tracking, and controlled cleanup. |
| `observability.py`, `reproducibility.py` | Metrics and pinned configuration/model evidence. |
| `workspace_migration.py`, `normalization.py` | Existing-workspace migration and persisted-data normalization. |
| `worker.py` | Legacy local worker implementation; production web mode keeps it disabled. |

### `processing_service/`

Independent GPU container/build context. It has its own package metadata and package copy so Docker
never reads files outside this folder.

| Subdirectory/file | Purpose |
| --- | --- |
| `moshi_data_pipeline/` | GPU-capable package copy. GPU runtime files are byte-checked against the canonical package by tests. Its audio/output/segmentation/speakers/transcription folders have the same responsibilities described above. |
| `protocol/` | Versioned worker JSON Schema shared with the web build context. |
| `systemd/` | Host-service templates. `moshi-gpu-intake.service` is used on this source-based g4dn deployment. |
| `Dockerfile` | CUDA/FFmpeg/ML image definition. Its default remains pull mode until container cutover is explicitly selected. |
| `.dockerignore` | Keeps secrets, caches, and unrelated files out of the GPU image context. |
| `README.md` | GPU variables, network boundary, persistent layout, and push/pull operation. |
| `pyproject.toml`, `uv.lock`, `config.example.yaml` | GPU build dependencies, lock, and configuration copy. |

### `web/`

Canonical React/TypeScript frontend source.

| Subdirectory/file | Purpose |
| --- | --- |
| `src/` | Application page, typed API client, GPU/status types, styles, tests, and browser bootstrap. |
| `src/components/` | Job progress, waveform editing, stereo playback, and the shared `GpuStatusPage` with adaptive polling and functional-check history. |
| `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml` | Exact frontend dependencies and workspace configuration. |
| `vite.config.ts`, `tsconfig*.json`, `index.html` | Vite build and TypeScript/browser entry configuration. |

### `web_service/`

Independent m8i web container/build context. Its dependencies deliberately omit CUDA, WhisperX,
FFmpeg, and processing runtimes.

| Subdirectory/file | Purpose |
| --- | --- |
| `frontend/` | Deployable mirror of canonical `web/`. Keep source/status-page changes synchronized. |
| `frontend/src/components/` | Deployable frontend components. |
| `moshi_data_pipeline/` | Web package copy containing Dataset Studio, SQLite, lifecycle, artifact, and API code. It must not execute ML jobs locally. |
| `nginx/` | Linux port-80/Basic-Auth example with exact callback path/source restrictions. It is not the live Windows server configuration. |
| `protocol/` | Worker schema; tests require it to match the GPU context. |
| `Dockerfile`, `.dockerignore` | Web image definition and context exclusions. |
| `README.md`, `pyproject.toml`, `uv.lock`, `config.example.yaml` | Web runtime documentation, minimal dependencies, lock, and configuration. |

### `deployment/`

| File | Purpose |
| --- | --- |
| `README.md` | Current Windows-m8i/g4dn push deployment, port boundary, backup, migration, rollback, and acceptance guide. |
| `GPU_PUSH_WORKSTREAM.md` | Push architecture, security/lifecycle invariants, durable reconciliation, and deployment gate. |
| `preflight.ps1` | Read-only AWS prerequisites, quota, AMI, and separate SecureString presence checks. |
| `build-push.ps1` | Builds and publishes web and GPU images under separate immutable generation/build identifiers. |
| `restart-web.ps1` | Loads the ignored protected `.env` without displaying values, removes the owned test mapping, stops only identified Moshi web listeners, and starts one web process on `MOSHI_WEB_PORT`. |
| `set-web-dispatch-token.ps1` | Prompts through hidden input and atomically installs the separate GPU dispatch token in an ignored Windows ACL-restricted file. |
| `backup-sqlite.py` | Runs SQLite's online backup API and verifies `PRAGMA integrity_check` before a web restart can continue. |

### `tests/`

Pytest suite covering configuration, media processing, contracts, SQLite migrations, lifecycle,
internal APIs, remote execution, dispatcher leadership/reconciliation, resumable upload, GPU
status/rate limits, service build synchronization, GPU intake, durable execution/outbox, and
functional checks. Frontend Vitest covers status states, history, manual trigger, errors, and
adaptive polling. Unit tests remain CPU-safe; the real T4 check is operational data outside Git.

## Ignored/generated local directories

| Path | Purpose and rule |
| --- | --- |
| `.git/` | Git metadata. |
| `.venv/` | Local Python 3.11 environment. Do not copy it between operating systems or AMIs. |
| `.pytest_cache/`, `.ruff_cache/`, `**/__pycache__/` | Disposable test/lint/bytecode caches. |
| `moshi_data_pipeline.egg-info/` | Editable-install metadata regenerated by packaging tools. |
| `raw/` | Ignored operator media. Distributed jobs must originate from m8i; do not process `raw/podcast_test_short.mp4` directly on g4dn. |

## Required g4dn runtime directories and files

These paths are outside Git. Ownership is `ubuntu:ubuntu` unless noted.

```text
/home/ubuntu/.config/moshi/                  0700  protected environment directory
  gpu-intake.env                             0600  nonsecret service configuration
  gpu-intake-token.env                       0600  MOSHI_DISPATCH_TOKEN only
  worker-token.env                           0600  MOSHI_WORKER_TOKEN callback credential
  huggingface.env                            0600  HF_TOKEN only

/home/ubuntu/moshi-worker-cache/             0700  encrypted persistent EBS cache
  state/                                     0700
    dispatch.sqlite3                         0600  input/execution/outbox/callback ledger
    self-check.sqlite3                       0600  functional-check history
  incoming/                                  0700  resumable `.part` uploads
  inputs/sha256/                             0700  verified content-addressed inputs
  attempts/                                  0700  disposable active job directories
  outbox/                                    0700  results retained until m8i acknowledgement
  huggingface/                               0700  pinned model snapshots
  self-test/v1/                              0700
    transcription-check.wav                 0444  fixed licensed test fixture
    fixture.json                             0444  ID, checksum, reference, and attribution
  logs/                                      0700
    gpu-intake.log                           0600  service log; no credentials/transcripts

/etc/systemd/system/
  moshi-gpu-intake.service                   0644 root:root, installed unit
```

`/opt/dlami/nvme` is instance storage and must not hold anything that must survive an EC2 stop. Do
not create an inbound database port or mount the m8i workspace.

## Required m8i runtime paths and files

Exact mount points may differ, but m8i needs these logical resources:

```text
<persistent workspace>/catalog.sqlite3       authoritative SQLite database
<persistent workspace>/originals/            uploaded source media
<persistent workspace>/worker_artifacts/     committed GPU results
<persistent workspace>/worker_staging/       resumable callback uploads
<persistent workspace>/exports/              generated exports
<persistent workspace>/backups/              verified SQLite/workspace backups
<protected config>/web.env                    web/lifecycle settings and build generation
<protected config>/worker-token.env           callback Bearer credential
<protected config>/gpu-dispatch-token.env     m8i-to-g4dn Bearer credential
```

The m8i service contains the push dispatcher/client, durable GPU observations/check history,
rate-limited authenticated status API, and shared frontend page. SQLite stays local to m8i and is
modified only after lease, fingerprint, job-kind, artifact-checksum, and idempotent-completion
validation.

Production traffic still requires a reviewed host boundary: browser and g4dn callback traffic use
m8i port 80, and m8i dispatches to g4dn private port 8766. The Windows test launcher can bind one
Uvicorn process to loopback port 80, but that is not a remotely reachable authentication boundary.
See `deployment/README.md`.
