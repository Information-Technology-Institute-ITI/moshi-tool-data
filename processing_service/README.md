# Moshi processing service

This is the independent CUDA build context. Two entrypoints coexist during the push-dispatch
migration:

- `python -m moshi_data_pipeline.remote_worker_main` is the existing protocol 1.0 pull worker.
- `python -m moshi_data_pipeline.gpu_intake_main` is the protocol 2.0 private push intake.

Run exactly one mode. The pull worker remains available for rollback until the m8i dispatcher has
passed cold-start acceptance. The push service durably receives inputs, runs the existing job
executor after validating the m8i lease, and retains a resumable callback outbox until m8i
acknowledges completion.

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

The image retains the legacy `python -m moshi_data_pipeline` CLI. The default container command
remains the pull worker during this migration.

## Private push intake (protocol 2.0)

The intake listens on TCP 8766. Restrict the AWS security-group rule to the m8i security group and
the host-firewall rule to the m8i private address. Never publish this port to the internet. Every
`/internal/v2/*` request additionally requires a high-entropy `MOSHI_DISPATCH_TOKEN`; this is a
different credential from the callback `MOSHI_WORKER_TOKEN`.

Required variables:

- `MOSHI_DISPATCH_TOKEN`, loaded from a mode-0600 environment file.
- `MOSHI_WORKER_TOKEN`, the separate Bearer credential for callbacks to m8i.
- `MOSHI_WEB_INTERNAL_URL=http://<m8i-private-ip>`; callbacks use m8i port 80 initially.
- `MOSHI_BUILD_ID`, the exact immutable commit deployed on both machines.
- `MOSHI_WORKER_CACHE=/home/ubuntu/moshi-worker-cache` for this source deployment.

Optional variables include `MOSHI_DISPATCH_TOKEN_NEXT` for rotation,
`MOSHI_GPU_INTAKE_PORT` (default 8766), `MOSHI_GPU_MAX_INPUT_BYTES` (default 20 GiB), and
`MOSHI_GPU_MIN_FREE_BYTES` (default 10 GiB).

Configure the functional check with `MOSHI_CONFIG` and `MOSHI_SELF_TEST_METADATA`. Both must be
present or both absent. The metadata names a fixed local audio file, expected SHA-256, reference
text, language, source, and license. `POST /internal/v2/self-checks` runs the exact configured
WhisperX model on CUDA. Its pass is reused for the current host boot/build/model/fixture for six
hours by default. The API stores CER, timings, model revision, GPU name, and output hash but never
stores or returns the decoded or reference transcript. Manual forced checks have a ten-minute
GPU-side cooldown; the m8i API will add per-user rate limits.

The intake accepts a manifest only when protocol and build match exactly. It then accepts
sequential `Content-Range` uploads, permits byte-identical retries, rejects gaps and conflicting
replays, verifies the full SHA-256, and atomically promotes content into the persistent cache.
One active dispatch is allowed. The fixed callback origin comes from the host environment and is
never accepted from a request.

After `start`, the service verifies a current functional pass and a recent authenticated callback
heartbeat. It then validates the lease with m8i before touching the model. Interrupted execution is
requeued on service restart. Successful outputs are moved atomically into the EBS outbox; failed
jobs also create a durable callback record. Artifact upload IDs and offsets survive restart, and
ambiguous completion responses are retried idempotently. HTTP 401 enters `auth_blocked` and stops
retrying until an operator corrects the token and restarts the service. HTTP 409 fences out the old
attempt. The GPU never writes or mounts m8i SQLite.

Persistent files are rooted under `MOSHI_WORKER_CACHE`:

```text
state/dispatch.sqlite3       durable dispatch, input, and service state
incoming/<dispatch>/*.part  resumable input transfers
inputs/sha256/<prefix>/<sha> verified content-addressed inputs
attempts/                    isolated, disposable active execution directories
outbox/                      results retained until m8i acknowledges them
logs/gpu-intake.log          intake process log (never contains credentials)
```

The cache and state directories are mode 0700; the database, partial inputs, verified inputs, and
logs are mode 0600. Use persistent encrypted EBS, not `/opt/dlami/nvme`. See
`deployment/GPU_PUSH_WORKSTREAM.md` and the provided systemd unit for the source-host deployment.
