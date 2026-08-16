# Moshi GPU processing service

This folder is the independent CUDA build context for g4dn. Push intake is the production mode:

```text
python -m moshi_data_pipeline.gpu_intake_main
systemd service: moshi-gpu-intake.service
private intake: 172.31.26.80:8766
callback origin: http://172.31.52.46
```

The legacy `remote_worker_main` protocol 1.0 pull process is rollback-only. Never run it at the
same time as `gpu_intake_main`. The checked-in container command may remain pull-mode until an
explicit container cutover; override it with `python -m moshi_data_pipeline.gpu_intake_main` for
push testing. The source-host production service uses the checked-in systemd unit.

## Trust boundary and credentials

The GPU accepts m8i dispatch requests only on private TCP 8766. The security-group source must be
the m8i security group and the host-firewall source must be `172.31.52.46`. Never publish this
port to the internet.

Every `/internal/v2/*` request requires `MOSHI_DISPATCH_TOKEN`. The GPU uses a different
`MOSHI_WORKER_TOKEN` for callbacks to m8i `/internal/v1/*` on port 80. Protocol 2.0 dispatch and
protocol 1.0 callback are independent contracts; do not give them a common version or token.

Store both credentials in protected files or a managed secret store. Do not place them in unit
files, command lines, source control, container layers, URLs, or logs. The dispatcher token supports
a bounded rotation overlap through `MOSHI_DISPATCH_TOKEN_NEXT`.

Port 80 is plaintext in the agreed initial topology. It does not protect audio, result artifacts,
or Bearer credentials from network observation. HTTPS 443 or private TLS/mTLS is required
hardening.

## Push-mode configuration

Required nonsecret values for the deployed service are:

```dotenv
MOSHI_WEB_INTERNAL_URL=http://172.31.52.46
MOSHI_BUILD_ID=b86e2016dbc31058408dc7b3b3ac241397b8a828
MOSHI_WORKER_CACHE=/home/ubuntu/moshi-worker-cache
MOSHI_CONFIG=<absolute processing config path>
MOSHI_SELF_TEST_METADATA=<absolute functional-fixture metadata path>
```

Separate protected files supply the names `MOSHI_DISPATCH_TOKEN`, `MOSHI_WORKER_TOKEN`, and, when
required, `HF_TOKEN`; documentation and example files leave their values empty.

`MOSHI_BUILD_ID` identifies the GPU deployable. It must equal m8i's
`MOSHI_GPU_REQUIRED_BUILD_ID`; it must not be replaced with m8i's
`MOSHI_DEPLOYMENT_GENERATION`.

Optional settings include `MOSHI_GPU_INTAKE_PORT` (default 8766),
`MOSHI_GPU_MAX_INPUT_BYTES` (default 20 GiB), `MOSHI_GPU_MIN_FREE_BYTES` (default 10 GiB),
callback/job heartbeat intervals, and functional-check validity/cooldown settings.

## Intake, execution, and restart behavior

The protocol 2.0 intake provides unauthenticated liveness only at `GET /health/live`. Status,
readiness, functional checks, dispatch creation, resumable input upload, start, and cancel are
authenticated under `/internal/v2/*`.

Receipt is at-least-once and idempotent:

- an identical dispatch ID and manifest returns the durable receipt;
- conflicting content for an existing ID returns 409;
- uploads resume from `X-Accepted-Offset` and require `Content-Range`;
- input SHA-256 is verified before start;
- lost responses are reconciled with GET or HEAD, never by inventing a new attempt;
- one dispatch executes at a time.

Execution starts only after the exact protocol/build match, a current boot/build/model functional
pass, a recent callback heartbeat, and validation of the m8i lease. Restarted running work is fenced
through m8i before model execution resumes.

Results remain in the persistent callback outbox until m8i acknowledges the typed, checksum-verified
atomic commit. Callback 401 enters an authentication-blocked alarm and is not retried. Callback 409
fences the obsolete attempt. The GPU never mounts or writes m8i SQLite.

## Functional check

The fixture is fixed licensed or synthetic audio on persistent encrypted EBS. It runs real CUDA
WhisperX load and inference. A pass is reusable only for the same host boot, service boot, GPU build,
dispatch protocol, model/config fingerprint, fixture hash, and check definition, and only until its
validity deadline.

Stored and returned health data includes bounded identifiers, CER, threshold, timing, GPU device,
and sanitized failure details. It excludes transcripts, real user audio, private paths, raw stack
traces, and credentials.

## Persistent layout

```text
/home/ubuntu/moshi-worker-cache/
  state/dispatch.sqlite3
  state/self-check.sqlite3
  incoming/
  inputs/sha256/
  attempts/
  outbox/
  huggingface/
  self-test/v1/
  logs/gpu-intake.log
```

Use encrypted persistent EBS with restrictive ownership and modes. Do not use
`/opt/dlami/nvme` for state that must survive EC2 stop/start. Do not mount the m8i workspace.

## Source-host service

Review the repository and cache paths in
`processing_service/systemd/moshi-gpu-intake.service`, then install it as
`moshi-gpu-intake.service`. Load nonsecret settings and each protected credential from the
separate files documented in `REPOSITORY_STRUCTURE.md`. Do not start the unit until any legacy
pull worker is stopped and both private directions are verified.

A bounded data-plane liveness check from m8i is:

```powershell
curl.exe --fail --silent --show-error --max-time 5 http://172.31.26.80:8766/health/live
```

Run authenticated readiness only from a secure terminal without echoing or logging the token.
