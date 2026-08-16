# Moshi GPU processing service

This is the independent g4dn build context. It runs one private push-intake service and performs GPU processing. It does not run Dataset Studio, access SQLite, mount the web workspace, or poll m8i for jobs.

## Runtime

The container starts:

```bash
python -m moshi_data_pipeline.gpu_intake_main
```

m8i pushes immutable dispatch protocol 2.0 manifests and checksum-addressed inputs to the private intake. The GPU service processes one accepted dispatch at a time, persists its dispatch/outbox state under `MOSHI_GPU_CACHE`, and sends progress, artifacts, completion, or failure to m8i through callback protocol 1.0.

Required settings:

- `MOSHI_DISPATCH_TOKEN`: m8i authenticates to the GPU intake.
- `MOSHI_WORKER_TOKEN`: GPU callbacks authenticate to m8i. The variable name is frozen by callback protocol 1.0; it does not represent another worker process.
- `MOSHI_BUILD_ID`: exact deployed GPU build.
- `MOSHI_WEB_INTERNAL_URL`: m8i callback origin.
- `MOSHI_GPU_CACHE`: durable GPU dispatch, input, output, callback, and model cache.
- `HF_HOME` and `HF_TOKEN`: Hugging Face cache and credential.

Private intake endpoints are under `/internal/v2/*` on port 8766. The only unauthenticated endpoint is `GET /health/live`. Never publish port 8766 to the internet.

Callback protocol 1.0 retains `/internal/v1/workers/heartbeat` and JSON `worker_id` fields for wire compatibility. In this codebase those values identify the GPU service itself.

## Persistent layout

```text
<gpu-cache>/
  state/
  inputs/
  attempts/
  outbox/
  logs/
  huggingface/
```

The service must be able to resume dispatch receipt and callback delivery after an EC2 stop/start. Tokens must be injected from protected files or a secret manager and must never be written into images, command arguments, or logs.
