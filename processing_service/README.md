# Moshi processing service

This is the independent CUDA worker build context. It downloads immutable job inputs
through `/internal/v1`, executes one job at a time in an isolated attempt directory,
uploads checksum-verified artifacts, and never opens or mounts the web SQLite/workspace.

Required runtime variables:

- `MOSHI_WEB_INTERNAL_URL`, for example `http://10.0.1.10:8765`.
- `MOSHI_WORKER_TOKEN`, shared through SSM Parameter Store or Secrets Manager.
- `MOSHI_BUILD_ID`, normally the deployed Git commit SHA.
- `HF_TOKEN` for gated models.

Persist `/cache` on encrypted EBS. It holds the SHA-256 input cache and Hugging Face
model cache; per-attempt directories are disposable. The host requires the NVIDIA driver,
Docker, and NVIDIA Container Toolkit.

```bash
docker build -t moshi-processing ./processing_service
docker run --rm --gpus all --network host \
  -e MOSHI_WEB_INTERNAL_URL=http://10.0.1.10:8765 \
  -e MOSHI_WORKER_TOKEN=replace-with-the-same-random-secret \
  -e MOSHI_BUILD_ID=$(git rev-parse HEAD) \
  -e HF_TOKEN \
  -v moshi-worker-cache:/cache \
  moshi-processing
```

The image retains the legacy `python -m moshi_data_pipeline` CLI. The production worker
entrypoint never exposes an inbound port.
