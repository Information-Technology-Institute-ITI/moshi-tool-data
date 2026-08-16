# Two-Service Moshi Deployment Plan

Status: implementation draft v2, revised 2026-08-13

## Outcome

Restructure the repository into two independently buildable and deployable services:

- `web_service/`: the always-on React and FastAPI application, SQLite catalog, persistent
  workspace, artifact registry, durable job queue, worker API, authentication proxy, and EC2
  lifecycle controller.
- `processing_service/`: the on-demand worker, ML/audio toolchain, result producer, model cache,
  and legacy non-Studio CLI.

The web machine is the only authority for catalog state and permanent workspace files. The
processing machine never opens the web SQLite database and never mounts the web workspace. It
receives immutable, typed job inputs over an authenticated private API and returns staged,
checksum-verified artifacts plus a typed result manifest.

The GPU instance starts when runnable work exists, drains one job at a time, and stops after 15
minutes of confirmed idle time. A failed startup or incompatible worker enters a visible blocked
state instead of creating an endless start/stop billing loop.

## Scope and non-goals

This version keeps:

- SQLite and all authoritative media on the web machine's encrypted EBS volume.
- One web application process, one processing worker, and one active processing job.
- The current public API, React behavior, media URLs, and SSE job updates unless an additive field
  is documented below.
- All nine Studio job kinds: `initialize`, `transcribe`, `review_transcript`, `rediarize`,
  `realign`, `recover_overlap`, `transcribe_overlap`, `generate`, and `export`.
- CPU-only generation and export on the processing machine, as requested.

This version does not introduce S3, SQS, RDS, EFS, ECS, or Kubernetes. It does not add application
roles, per-edit user attribution, multiple workers, job priorities, or horizontal web replicas.
Nginx Basic Auth protects the product, but the application continues to treat authenticated users
as one logical operator.

## Current baseline

As of 2026-08-13:

- The repository has 62 passing tests (`.venv/Scripts/python.exe -m pytest -q`).
- `StudioWorker` runs in a thread inside the web process and claims jobs directly from SQLite.
- The nine processing paths directly read and mutate `StudioCatalog` and `StudioPaths`. Removing
  that coupling is the first architectural milestone; the folder split must not be the first step.
- The existing workspace is about 528 MB. Its 5.2 MB catalog contains 6 projects, 6 sources, 85
  annotation revisions, 37 jobs, 1 clip plan, 26 overlap recovery rows, and no exports. Migration
  preflight must recalculate and record these counts because the workspace may change.
- `raw/podcast_test_short.mp4` exists for the production-model smoke test.
- The local machine has a 6 GB RTX 3060 Laptop GPU. CPU contract tests and both independent image
  builds are mandatory here; the real `large-v3` smoke may record only an explicit local VRAM
  limitation and must be repeated on `g6.2xlarge`.

## Decisions captured by this draft

The following are defaults for implementation and can be changed before the named phase begins:

1. Use JSON Schema draft 2020-12 for worker protocol version `1.0`.
2. Use SHA-256 fingerprints of canonical job context, not one ambiguous integer
   `input_revision`, to detect stale work.
3. Keep Basic Auth for v1; do not imply that edits are attributed to the individual account.
4. Retain processing CLI commands `process`, `batch`, `inspect`, `extract`, `rebuild-manifest`,
   `approve-review`, `review`, `benchmark`, `stages`, and `evaluate-separation`. The `web` command
   moves to `web_service` and is not exposed by the processing image.
5. Use Terraform for reproducible AWS infrastructure unless manual provisioning is explicitly
   selected before Phase 7.
6. Use On-Demand EC2 for the first rollout. Spot is a later optimization after lease recovery has
   been proven under forced interruption.

Decisions still requiring confirmation are collected at the end of this document.

## Repository target

```text
/
├── PLAN.md
├── README.md
├── compose.yaml
├── web_service/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── app/
│   ├── frontend/
│   ├── migrations/
│   ├── protocol/worker_protocol.schema.json
│   ├── nginx/
│   ├── tests/
│   └── README.md
├── processing_service/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── app/
│   ├── protocol/worker_protocol.schema.json
│   ├── tests/
│   └── README.md
├── contract_tests/
│   └── test_worker_protocol_sync.py
└── deployment/
    └── terraform/
```

`deployment/` is infrastructure only, not a third runtime package. Each Docker build uses its
service folder as the complete build context. Neither service may import the other service or rely
on a root Python package. The root contract test verifies that both protocol schema files are
byte-identical and have the expected SHA-256 hash.

## Service boundaries

### Web service

The web service owns:

- React source and compiled static files.
- FastAPI public routes, SSE, upload handling, media streaming, and HTTP Range responses.
- SQLite migrations and all catalog mutations.
- Workspace path validation and the permanent artifact registry.
- Worker authentication, leases, input manifests, upload staging, and atomic result commits.
- The lifecycle provider interface, the local no-op provider, and the AWS EC2 provider.
- Nginx configuration, public TLS, individual Basic Auth accounts, request-size limits, and SSE
  proxy settings.

It has no Torch, Whisper, WhisperX, pyannote, SpeechBrain, or NVIDIA dependencies. NumPy,
SoundFile, and FFmpeg are allowed only if a remaining web-side validation or media operation
demonstrably needs them.

Run exactly one Uvicorn worker. This prevents duplicate lifecycle-controller loops; it does not
mean one persistent SQLite connection. Request handlers use short WAL-mode connections, a 30
second busy timeout, explicit transactions, and retry only safe transient lock failures. The EBS
volume is locally attached block storage, never a network SQLite filesystem.

### Processing service

The processing service owns:

- FFmpeg, audio inspection, transcription, alignment, diarization, identity matching, separation,
  clip rendering, QC, and export construction.
- CUDA 12.6/cuDNN 9 runtime dependencies and the pinned Python ML stack.
- The HTTP worker client, checksum cache, per-job temporary directories, heartbeat thread, and
  one-job-at-a-time executor.
- The retained legacy CLI commands listed above.

Processing functions accept a materialized `JobContext` and return a `JobResult`. They do not
receive a web catalog object, a web workspace object, SQL, or unrestricted destination paths.
Every output path is local to the current job directory and becomes visible to the web service
only after upload and commit.

## Execution seam before the folder split

The current processing code has many direct calls to `StudioCatalog` and `StudioPaths`. Refactor
each job behind these service-neutral types while the application can still run locally:

- `ArtifactRef`: artifact ID, role, SHA-256, byte size, media type, and suggested safe filename.
- `JobContext`: protocol version, job ID/kind/attempt, resolved non-secret config, typed job payload,
  preconditions, input fingerprint, and input artifact references.
- `ProducedArtifact`: logical output role, local relative path, SHA-256, size, and media type.
- `JobResult`: a job-kind-specific result payload plus produced artifacts.
- `JobExecutor`: dispatches one of the nine typed contexts and reports progress without knowing how
  that progress is persisted.

Add a local adapter that creates a `JobContext` from the current catalog, materializes inputs,
executes locally, and commits through the same result validator planned for the HTTP API. Existing
UI behavior must stay working through this adapter. Only after all nine jobs pass through this seam
should code move into the two final directories.

## Worker protocol v1

### Compatibility and authentication

- Every request sends `Authorization: Bearer <worker-token>`.
- Lease-scoped operations also send the opaque lease token returned at claim time. SQLite stores
  only a SHA-256 hash of that lease token.
- The worker reports protocol version, build ID, supported job kinds, boot ID, and readiness before
  claiming.
- Major protocol versions must match exactly. Unsupported workers cannot claim jobs; jobs remain
  queued and `/api/system/worker` reports the incompatibility.
- Worker tokens are injected at runtime and never written to images, Git, logs, job contexts, or
  result manifests. Rotation supports current and next tokens during a bounded deployment window.
- The external Nginx listener returns 404 for `/internal/*`. The internal FastAPI port is reachable
  only from the processing security group. Whether this private hop also uses TLS is an open
  deployment decision below.

### Internal endpoints

- `POST /internal/v1/workers/heartbeat`: report ready, busy, draining, incompatible, or idle state.
- `POST /internal/v1/jobs/claim`: atomically claim the oldest compatible runnable job.
- `POST /internal/v1/jobs/{job_id}/heartbeat`: renew the lease and update bounded progress/message
  fields. The response can request cancellation when the input has become stale.
- `GET /internal/v1/artifacts/{artifact_id}/content`: download a registered input with Range,
  `Content-Length`, checksum ETag, and `If-Range` support.
- `POST /internal/v1/jobs/{job_id}/uploads`: register an expected output and receive an upload ID.
- `HEAD /internal/v1/uploads/{upload_id}`: return verified next offset and total size.
- `PUT /internal/v1/uploads/{upload_id}`: append a sequential `Content-Range` chunk. Repeating an
  already accepted identical chunk is idempotent; gaps and conflicting bytes are rejected.
- `POST /internal/v1/jobs/{job_id}/complete`: validate and atomically commit a typed result.
- `POST /internal/v1/jobs/{job_id}/fail`: record a sanitized failure classification.

No endpoint accepts SQL, absolute paths, `..` components, arbitrary catalog field names, or archive
extraction. Request and response sizes are bounded. Upload chunks are streamed to disk, not buffered
in memory.

### Lease and retry rules

- A claim transaction increments `attempt`, creates a random lease token, and sets a two-minute
  expiry using web-server UTC time.
- A separate worker thread heartbeats every 15 seconds even when an ML call is blocking.
- Only the current lease token can heartbeat, register/upload outputs, complete, or fail the job.
- Expired leases are requeued transactionally. After three claimed attempts, another expiry marks
  the job failed.
- Retryable failures include interrupted HTTP transfers, transient worker loss, and explicitly
  classified service/runtime failures. Schema errors, invalid inputs, unsupported configuration,
  and reproducible application errors fail immediately.
- GPU out-of-memory is non-retryable for the same worker build and configuration. The UI can create
  an explicit new retry only after the operator changes the model/configuration or capacity.
- Manual retry clears staged uploads, preserves attempt history, and issues a new lease. Failure
  text is sanitized and bounded before it enters SQLite or SSE.

### Stale-input protection

At enqueue, the web service creates a canonical, job-specific context and stores:

- `preconditions_json`: the exact annotation, clip-plan, recovery, decision, export, source, and
  artifact revisions relevant to that job kind.
- `input_fingerprint`: SHA-256 of the canonical context and ordered input artifact checksums.

The web service recomputes the fingerprint before committing a result. A mismatch changes the job
to `superseded`, discards its staging area, and never replaces active artifacts. Queued jobs can be
superseded proactively when an authoritative edit invalidates them. Running jobs receive a cancel
request on the next heartbeat, but correctness does not depend on cooperative cancellation.

### Job contracts

| Job kind | Required input snapshot | Typed result |
|---|---|---|
| `initialize` | Original media artifact, source row revision, mode, resolved config | Canonical audio, optional preserved channels, proxy, peaks, inspection, initial annotation, and analysis artifacts |
| `transcribe` | Canonical audio, source/annotation revision, transcription config | Raw/aligned transcript artifacts and proposed next annotation |
| `review_transcript` | Canonical audio and exact annotation revision | Review candidates and proposed next annotation |
| `rediarize` | Canonical audio/channels, exact annotation and speaker references | Diarization artifact, inspection updates, and proposed next annotation |
| `realign` | Canonical audio, exact edited annotation, raw transcript if required | Alignment artifact and proposed next annotation |
| `recover_overlap` | Canonical audio/channels and exact finalized annotation | Per-region original/stem artifacts and complete replacement recovery manifest |
| `transcribe_overlap` | Exact recovery record plus both registered stem artifacts | Bounded stem-transcript details for that recovery record |
| `generate` | Exact annotation, clip plan, approved recoveries, canonical/channels, and generation config | Clip WAV/JSON artifacts, QC data, and clip-artifact manifest |
| `export` | Immutable project snapshot including rights, annotations, approved clips, decisions, and config | Complete export file manifest and validation report |

Each proposed catalog mutation has a dedicated Pydantic model and explicit commit handler. There is
no generic `set_fields` result operation.

## SQLite and artifact model

Introduce numbered, transactional migrations and a `schema_migrations` table. Remove ad hoc schema
changes from normal application initialization.

Extend `jobs` with:

- `attempt`, `max_attempts`, `lease_owner`, `lease_token_hash`, `lease_expires_at`.
- `retryable`, `protocol_version`, `worker_build_id`.
- `preconditions_json`, `input_fingerprint`.
- `started_at`, `finished_at`, and bounded failure classification.

Keep job states `queued`, `running`, `complete`, `failed`, and `superseded`. Add database checks and
indexes for valid state/lease combinations and claim ordering.

Add:

- `worker_state`: worker/boot ID, protocol/build, capabilities, readiness, current job, heartbeat,
  and `idle_since`.
- `artifacts`: ID, role, relative permanent path, SHA-256, size, media type, project/source owner,
  producing job, state, and timestamps.
- `artifact_uploads`: upload ID, job/attempt owner, staging path, expected checksum/size, accepted
  offset, state, and expiry.
- `job_attempts`: immutable claim, worker, timing, terminal classification, and diagnostic summary
  for each attempt.
- `lifecycle_state`: provider state, desired instance state, last transition, startup deadline,
  recovery count, blocked reason, and controller generation.

Artifact relative paths are generated by the web service from typed role and owner, never supplied
as trusted worker destinations.

## Atomic artifact commit

1. The worker downloads missing inputs into an EBS cache keyed by SHA-256 and verifies every hit.
2. It hard-links or copies verified inputs into a fresh job/attempt directory.
3. It writes outputs locally, closes them, calculates SHA-256 and size, then registers and uploads
   them.
4. The web service stores chunks under a per-job staging directory on the same EBS filesystem as
   the final workspace.
5. After the full size arrives, the web service fsyncs and verifies the checksum.
6. Completion opens an immediate SQLite transaction, verifies lease and input fingerprint, and
   validates the typed result.
7. Files move from staging to versioned final paths with atomic same-filesystem renames. Existing
   active paths are not overwritten in place.
8. SQLite rows switch the new artifact versions active and the transaction commits.
9. If a crash occurs between file rename and database commit, startup reconciliation registers or
   removes only files identified by the commit journal. It never guesses from arbitrary files.

Expired, failed, and superseded staging areas are garbage-collected after a retention window. Active
downloads and uploads are protected from cleanup by database state.

## Public web behavior

- Preserve existing public routes and response fields.
- Extend job responses with attempt, retryability, lease/start timestamps, and worker/startup status.
- Add `GET /api/system/worker` with queue counts, EC2 state, worker heartbeat/readiness/build,
  protocol compatibility, idle time, lifecycle desired state, and actionable blocked reason.
- Add an authenticated operator action to retry a blocked GPU startup. This action does not bypass
  protocol compatibility or lease safety.
- SSE terminates on `complete`, `failed`, or `superseded` and sends keepalive comments through
  Nginx with proxy buffering disabled.
- Public and worker media responses support tested single-range requests, ETag, `If-Range`, 206,
  and 416 behavior. Multi-range responses are out of scope for v1.
- Nginx sets an explicit upload limit suitable for the source library and streams request bodies to
  FastAPI. State-changing public API requests reject untrusted cross-origin requests.

## Lifecycle controller

Implement a provider interface with `LocalLifecycleProvider` and `Ec2LifecycleProvider`. The local
provider records desired actions but never invokes Docker or AWS. The controller runs every 30
seconds and immediately after a job transaction commits.

Rules, in priority order:

1. A valid running lease always prevents stop.
2. Runnable queued work requests `running`. If EC2 is stopped, start it; if pending/running, do
   nothing; if stopping, remember `running` and start only after it reaches stopped.
3. Stop only when there are no queued jobs, no valid running leases, a fresh compatible worker
   reports no current job, and `idle_since` is at least 15 minutes old.
4. A missing worker receives a startup grace period, proposed as 10 minutes. One automatic recovery
   stop/start is allowed after an unexpected worker loss.
5. If the worker is still absent, unhealthy, or incompatible, set `lifecycle_state.blocked_reason`,
   stop the idle instance safely, and suppress automatic restarts until an operator retries or a new
   deployment generation is recorded. Jobs remain durable and queued.
6. EC2 API errors use bounded exponential backoff and are exposed in `/api/system/worker`.

Use the instance ID and an immutable deployment tag as defense in depth. `StartInstances` and
`StopInstances` are restricted to the exact GPU instance ARN. `DescribeInstances` necessarily uses
`Resource: "*"`, because AWS does not support resource-level permission for that action; results
are filtered and validated by instance ID. No terminate permission is granted.

## Local Compose

The root `compose.yaml` builds each folder with its own context and defines:

- `web`: workspace bind mount at `local_data/studio_workspace`, health check, and local lifecycle
  provider.
- `processing`: a `gpu` profile, worker cache at `local_data/worker_cache`, NVIDIA reservation,
  private network only, and one-job concurrency.
- A generated local worker token supplied through an ignored environment/secrets file.

Default `docker compose up web` leaves the worker offline. Start it explicitly with the `gpu`
profile to test durable offline queueing. Stop it during download, upload, and execution tests to
exercise resume and lease expiry.

The GPU smoke test uses `large-v3`, float16, and `raw/podcast_test_short.mp4`. It must never silently
downgrade the model. A local out-of-memory result is recorded as a hardware limitation; protocol,
queue, and atomicity tests must still pass with a deterministic fake executor.

## Phased implementation

### Phase 0 — Lock invariants and characterization tests

- Record the nine current job paths, their catalog reads/writes, files, and invalidation behavior.
- Add characterization fixtures for current job results and public API responses.
- Record baseline test, workspace-count, toolchain, Docker, GPU, and media preflight commands.
- Write short architecture decisions for authentication scope, protocol versioning, Terraform,
  network/TLS, and retry classifications.

Gate: all existing tests still pass and every current job mutation is represented in a contract
table or characterization test.

### Phase 1 — Protocol, migrations, and lease queue

- Add the duplicated protocol schema and root equality/hash test.
- Add numbered SQLite migrations, the new tables/columns, and injectable-clock lease tests.
- Implement atomic claim, heartbeat, expiry/requeue, attempt history, manual retry, and supersede.
- Keep the current in-process worker temporarily, adapted to lease tokens.

Gate: queue tests cover concurrent claim, expired lease, stale token, duplicate completion, three
attempts, deterministic failure, protocol mismatch, and no real-time sleeps.

### Phase 2 — Decouple all processing from web state

- Introduce `JobContext`, `JobResult`, artifacts, and typed per-kind result models.
- Refactor one job at a time to remove `StudioCatalog` and web `StudioPaths` parameters.
- Route the in-process local adapter through the same context builder and commit validator.
- Move export construction behind the same boundary.

Gate: all nine jobs execute through the new seam; a static import test rejects processing modules
that import web catalog/server/service modules.

### Phase 3 — Internal API and deterministic remote worker

- Implement worker auth/readiness, claim, heartbeat, download, resumable upload, complete, and fail.
- Implement artifact staging, checksum verification, commit journal, and reconciliation.
- Build a deterministic CPU fake worker that uses only HTTP and has no shared filesystem.
- Preserve SSE/public API compatibility and add worker status.

Gate: the fake worker completes every typed job contract over HTTP, including interrupted transfers,
stale inputs, traversal attempts, checksum mismatch, and duplicate requests.

### Phase 4 — Real processing worker and folder split

- Implement cache, materialization, heartbeat thread, executor dispatch, cleanup, and sanitized
  failure reporting.
- Prove one real end-to-end `initialize` vertical slice, then migrate the remaining eight job kinds.
- Move code into `web_service/` and `processing_service/`, resolving ownership rather than importing
  across the boundary.
- Create independent locks and Docker build contexts.

Gate: each service installs, tests, and builds from only its own directory; the root import/contract
tests pass; each real job kind has an integration fixture.

### Phase 5 — Containers and local failure matrix

- Add multi-stage web build, CUDA worker build, root Compose profiles, health checks, and READMEs.
- Run offline queue, restart, resume, Range, SSE, and atomic visibility tests.
- Run the production-model short-video smoke test when Docker/GPU preflight passes.

Gate: CPU boundary suite passes everywhere; GPU smoke passes or records only the explicitly allowed
6 GB hardware limitation with logs and no model downgrade.

### Phase 6 — Lifecycle controller and cost guardrails

- Implement/test local provider, AWS provider, state machine, startup deadline, blocked state,
  operator retry, and deployment generation.
- Add alarms/logging for prolonged GPU runtime, worker absence, blocked queue, disk pressure, and
  controller API errors. Alarms notify; they do not forcibly stop a valid lease.

Gate: exhaustive state-machine tests prove no active lease is stopped and no incompatible/crashed
worker causes an unbounded restart loop.

### Phase 7 — Migration tooling and AWS infrastructure

- Implement `migrate-workspace --dry-run`, `--apply`, `--verify`, and resumable artifact scanning.
- Provision network, security groups, IAM, EBS, EC2, ECR, SSM, DNS/TLS, backups, and monitoring.
- Resolve the GPU AMI per region rather than hard-coding an AMI ID.
- Deploy images and perform a migration rehearsal against a copy of the workspace.

Gate: rehearsal counts match, every registered artifact checksum verifies, rollback succeeds, and
the complete AWS acceptance run passes.

### Phase 8 — Production cutover

- Enter maintenance mode and stop the old app/worker.
- Take an EBS snapshot and a SQLite backup, run migration, and preserve both rollback artifacts.
- Start web only, validate browsing/editing/media with GPU stopped, then submit one smoke job.
- Confirm automatic start, processing, result visibility, queue drain, 15-minute idle stop, backup,
  and alert delivery.

Gate: operator signs off the acceptance checklist before the old deployment and rollback artifacts
are retired.

## Workspace migration

Migration is idempotent and runs only on the web machine:

1. Refuse to apply while the old worker or web writer is active unless maintenance mode is proven.
2. Capture table counts, schema version, workspace size, and a manifest before changing anything.
3. Create a consistent backup through SQLite's backup API and retain the pre-migration EBS snapshot.
4. Apply numbered additive migrations transactionally.
5. Convert legacy `running` jobs to `queued` with an explicit migration reason and no active lease.
6. Scan originals, source artifacts, recoveries, clips, and exports. Validate every path remains
   inside the workspace; record size and SHA-256 without rewriting content.
7. Resume safely after interruption using scan checkpoints; repeated runs do not duplicate rows.
8. Verify project, source, annotation, job, plan, decision, recovery, clip, and export counts plus all
   registered checksums.

Rollback means stopping the new app and restoring both the pre-migration database and its matching
EBS snapshot/copy. Do not run the old binary against the migrated database.

## AWS deployment

### Proposed machines

- Web: `t3.large`, Ubuntu 22.04, 80 GB encrypted gp3 root, 500 GB encrypted gp3 workspace volume.
- Processing: `g6.2xlarge`, region-appropriate Ubuntu 22.04 GPU/driver AMI, 80 GB encrypted gp3 root,
  and at least 150 GB encrypted gp3 model/input cache volume.

`g6.2xlarge` is provisional until the target region, Availability Zone, service quota, On-Demand
capacity, CUDA image, and real smoke test are verified. G6 provides an NVIDIA L4 with 24 GB GPU
memory, which is materially safer for this workload than the 6 GB local card.

### Network and access

- Public traffic reaches only Nginx HTTPS 443 on the web machine.
- The processing security group has no inbound rules. Administration uses Systems Manager Session
  Manager; SSH is not required.
- Worker-to-web traffic uses the web private address and a dedicated internal port allowed only from
  the processing security group.
- The processing host needs controlled outbound HTTPS for ECR, SSM, package/model access, and time
  synchronization. The public-IP versus private-subnet/NAT choice is still open.
- Require IMDSv2. Encrypt EBS with a customer-managed or account-managed KMS key and set data-volume
  deletion-on-termination to false.

### Runtime and secrets

- Publish separate immutable images to ECR, tagged with commit SHA; never deploy `latest` alone.
- Mount EBS volumes by UUID before containers start. A systemd unit authenticates to ECR and starts
  the pinned Compose service after the mount and network are ready.
- The processing container starts at EC2 boot, verifies CUDA/model-cache readiness, reports ready,
  then drains the queue.
- Store worker tokens and deployment secrets in SSM Parameter Store or Secrets Manager. Store Nginx
  Basic Auth hashes outside the image. Never place Hugging Face tokens in Terraform state or user
  data.
- Grant both machines `AmazonSSMManagedInstanceCore` or an equivalent least-privilege custom role.
  The web role gets only describe plus start/stop permissions described above; the GPU role has no
  EC2 lifecycle permission.

### Backup and observability

- Run daily SQLite-consistent backups and daily workspace EBS snapshots with documented retention.
- Monitor filesystem usage, SQLite backup age, queue age, worker heartbeat, lifecycle blocked state,
  GPU runtime duration, and container health.
- Test restoration to a separate volume before declaring backups production-ready.
- Log job IDs, attempts, worker build, timings, and artifact IDs, but never bearer/lease/Hugging Face
  tokens or source transcript contents by default.

AWS stop/start preserves EBS volumes but erases instance-store data. Therefore all required model
and input cache data must live on the attached EBS cache volume; instance store may be used only for
disposable per-attempt scratch space.

## Test and acceptance matrix

### Fast tests

- Existing 62-test behavior baseline.
- Schema equality/hash and golden protocol messages.
- Independent import/build boundaries.
- Migration ordering/idempotency and legacy fixture migration.
- Lease state machine with concurrent claim and an injectable clock.
- Job-specific input fingerprints and typed result validation.
- Lifecycle state machine across stopped, pending, running, stopping, stale, incompatible, and API
  failure states.

### HTTP and security tests

- Missing/wrong/rotated bearer token and stale lease token.
- Path traversal, unsafe filenames, oversized bodies/messages, and unknown artifact IDs.
- Range/ETag/If-Range behavior, interrupted download, upload resume, conflicting chunk, checksum
  mismatch, and expired upload.
- Duplicate claim/heartbeat/complete/fail and completion after lease expiry.
- Public Nginx rejection of `/internal/*`, Basic Auth enforcement, trusted-origin checks, and no
  secret leakage in logs/errors.

### Functional tests

- Browse, edit, upload, stream, and queue while worker is offline.
- Run all nine job kinds remotely and preserve SSE behavior.
- Edit annotations/plans/decisions during execution and confirm `superseded` without artifact
  visibility.
- Kill the worker during download, processing, upload, and after upload/before completion.
- Restart web during staging and verify deterministic reconciliation.
- Validate migrated workspace counts and open every existing source/project.
- Run `large-v3` float16 short-video workflow locally and on `g6.2xlarge`.

### AWS end-to-end test

With the GPU stopped, submit a job through the website and verify durable queueing, EC2 start,
worker readiness, processing, progress, atomic result visibility, complete queue drain, and stop
after 15 confirmed idle minutes. Repeat once with a forced worker interruption and once with an
intentionally incompatible protocol build to verify the visible blocked/cost-guard state.

## Implementation status and adopted deployment defaults

Phases 0-7 are implemented in this repository: protocol/migrations/leases, typed execution seams,
authenticated internal transfer APIs, remote worker, independent build contexts, lifecycle cost
guards, workspace migration, and Terraform deployment assets. Phase 8 is deliberately an operator
cutover because it changes live AWS resources, DNS, certificates, credentials, and production data.

The implementation adopts these v1 defaults:

1. **AWS region and domain:** supplied as deployment variables. The read-only preflight rejects a
   region without the required G/VT quota, a G6 offering, the DLAMI parameter, or SecureStrings.
2. **Infrastructure:** Terraform 1.7+ with the AWS provider and independent immutable ECR images.
3. **GPU egress:** public IPv4 with no inbound security-group rules, avoiding standing NAT cost.
4. **Private API:** security-group-restricted VPC HTTP plus a rotated bearer token and per-lease
   capability token. Public Nginx rejects the entire `/internal/*` namespace.
5. **User authentication:** Nginx Basic Auth with individual account hashes outside images and
   Terraform; v1 does not claim per-user edit attribution. Browser mutations enforce same-origin.
6. **Backup retention:** 14 daily SQLite backups and AWS Backup EBS recovery points by default,
   configurable in Terraform. Restoration to a separate volume remains a production acceptance
   gate.

Before Phase 8, the operator still selects the concrete region/AZ/domain, verifies live G6 capacity
and price, installs trusted TLS and Basic Auth material, rehearses migration on a workspace copy,
and runs the AWS end-to-end acceptance matrix.
