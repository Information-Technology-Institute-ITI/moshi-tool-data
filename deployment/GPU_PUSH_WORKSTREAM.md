# GPU push workstream

This document tracks the migration from the protocol 1.0 pull worker to the agreed protocol 2.0
push design. The m8i web host remains authoritative for jobs, artifacts, and SQLite. The g4dn host
never mounts or writes the web database.

## Network and trust boundary

The connection direction determines which host needs an inbound listener:

```text
m8i dispatcher  -- private TCP 8766 -->  g4dn intake
g4dn callbacks  -- private HTTP 80  -->  m8i internal API
browser         -- current HTTP 80  -->  m8i website
```

Add one g4dn security-group inbound rule for TCP 8766 whose source is only the m8i security group.
Add a matching host-firewall rule for only the m8i private IP. Do not add a public rule and do not
open SQLite. Reusing m8i port 80 avoids a new listener, but callback paths still require Bearer
authentication, lease fencing, body limits, checksum validation, and an exact GPU-source allowlist
at the reverse proxy. Moving the public site and internal callback path to TLS is a required
production hardening follow-up because HTTP does not encrypt audio, results, or credentials.

Use two independent secrets:

- `MOSHI_DISPATCH_TOKEN` authenticates m8i requests to g4dn TCP 8766.
- `MOSHI_WORKER_TOKEN` authenticates g4dn callbacks to m8i TCP 80.

Store secrets only in protected environment files or a managed secret service. Never put them in
unit files, command lines, source control, test fixtures, or access logs. The intake supports
`MOSHI_DISPATCH_TOKEN_NEXT` for overlap during rotation.

## Phase 1: private intake and persistent state

The first implementation phase is intentionally bounded. It provides:

- unauthenticated `GET /health/live` containing no operational secrets;
- authenticated `GET /internal/v2/health/ready` and `GET /internal/v2/status`;
- idempotent `POST /internal/v2/dispatches` with exact protocol/build checks;
- resumable `HEAD`/`PUT /internal/v2/dispatches/{id}/inputs/{artifact_id}`;
- durable `POST /internal/v2/dispatches/{id}/start` and `.../cancel` transitions.

Receipt is at-least-once and acceptance is idempotent. Reusing a dispatch ID with different
content is a conflict. Input bytes are acknowledged only after `fsync`; complete content is
SHA-256 verified and atomically moved to the content-addressed cache. SQLite uses WAL,
foreign-key enforcement, a busy timeout, and full synchronous writes. The service records host
boot ID, service boot ID, exact build, protocol, fixed callback origin, and a persistent heartbeat.

This phase can persist a `queued` dispatch, but it does not yet launch WhisperX or send a result.
Do not cut m8i over to push dispatch or advertise model-ready status until Phase 2 is complete.

## Phase 2: execution and durable callback outbox

The next g4dn phase will consume one queued attempt at a time through the existing
`ContextJobExecutor`. It must keep the general service heartbeat alive while busy, persist result
manifests before callback, upload through the m8i API, retry ambiguous network failures
idempotently, and retain output until m8i acknowledges the atomic commit. A 401 must enter
`auth_blocked` without retrying or exposing either credential. Interrupted `running` state must be
reconciled against the m8i lease before rerun.

Only after that phase should the m8i dispatcher be enabled. The legacy pull worker and the push
intake must never run simultaneously.

## Phase 3: functional check and status integration

The functional fixture will be a consented or synthetic short WAV on persistent EBS. A pass is
valid once per matching host boot, exact build, protocol, model/config fingerprint, fixture hash,
and check-definition version. It must exercise CUDA model load and inference plus the authenticated
m8i callback. It is not rerun before every job, and historical success from an earlier boot is not
shown as current readiness.

The m8i GPU page will display EC2 observation, intake heartbeat age, worker/build/protocol state,
the latest functional check, and bounded history as separate facts. Authenticated users may trigger
a deduplicated, rate-limited check. The m8i API remains the only writer of shared health-check
records in its local SQLite database.

The g4dn portion is implemented as authenticated `GET/POST /internal/v2/self-checks`. Results are
durable in `state/self-check.sqlite3`; decoded/reference text is deliberately excluded. The
current readiness payload distinguishes `functional_check` from normal job `execution` and the
callback `outbox`. The latter two remain false until the next workstream connects execution and
m8i result delivery.

## Source-host service

The checked-in unit is a template. Before installing it, create a protected environment file at
`/home/ubuntu/.config/moshi/gpu-intake.env` with directory mode 0700, file mode 0600, and owner
`ubuntu:ubuntu`. It must define the required variables described in
`processing_service/README.md`. Enter secrets interactively on the host; never paste them into an
automation chat or shell history.

Install and enable the unit only after reviewing the absolute repository/cache paths:

```bash
sudo install -o root -g root -m 0644 \
  processing_service/systemd/moshi-gpu-intake.service \
  /etc/systemd/system/moshi-gpu-intake.service
sudo systemctl daemon-reload
sudo systemctl enable moshi-gpu-intake.service
```

Do not start it while the pull worker is running. Before production cutover, verify m8i-to-g4dn
TCP 8766 and g4dn-to-m8i HTTP 80, then perform a UI-originated cold-start job. Any health-endpoint
timeout is a cutover blocker and must be diagnosed without broadening either security group or
firewall.
