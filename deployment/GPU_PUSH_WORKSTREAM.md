# GPU push workstream

This document records the protocol 2.0 push architecture and its deployment invariants. m8i remains
authoritative for jobs, leases, artifacts, lifecycle state, health history, and SQLite. g4dn owns
only its persistent receipt/input cache, functional-check ledger, temporary attempts, and callback
outbox.

## Fixed topology

```text
browser        -- HTTP 80 --> m8i reviewed web/auth boundary
m8i dispatcher -- TCP 8766 -> 172.31.26.80 g4dn intake
g4dn outbox    -- HTTP 80 --> 172.31.52.46 m8i /internal/v1/*
```

Production web/callback port 80 and private GPU intake port 8766 are fixed. The web process obtains
its listener from `MOSHI_WEB_PORT`; the Windows test launcher uses loopback port 80 without a
second listener. Do not expose SQLite or open port 5432.

Port 80 is plaintext in the agreed initial configuration. It is not a confidential production
transport. HTTPS or private TLS/mTLS remains required hardening.

## Trust boundary

The g4dn security group permits TCP 8766 only from the m8i security group, and its host firewall
permits only `172.31.52.46`. Every `/internal/v2/*` request additionally requires a Bearer
`MOSHI_DISPATCH_TOKEN`.

The m8i port-80 server permits only the fixed protocol 1.0 callback path shapes from
`172.31.26.80`. FastAPI additionally requires Bearer `MOSHI_WORKER_TOKEN` and, for leased
operations, `X-Lease-Token`. The boundary overwrites forwarding data, strips client-authenticated
identity headers, and sets the website identity only from its own authentication result.

These credentials are unrelated:

- `MOSHI_DISPATCH_TOKEN`: m8i to g4dn, dispatch protocol 2.0.
- `MOSHI_WORKER_TOKEN`: g4dn to m8i, callback worker protocol 1.0.

Never place either token in URLs, command lines, source, unit files, container layers, access logs,
or chat.

## GPU receipt and execution

The g4dn implementation provides:

- unauthenticated `GET /health/live` with bounded liveness data;
- authenticated readiness and status;
- durable functional-check request and history;
- idempotent dispatch creation with exact protocol/build validation;
- resumable `HEAD` and `PUT` input upload;
- durable start and cancel transitions;
- one-at-a-time execution;
- lease and worker heartbeats;
- durable, resumable callback outbox.

Receipt is at-least-once. An identical dispatch ID and manifest returns the existing receipt;
different content for the same ID returns 409. Upload bytes are acknowledged only after durable
write, the final SHA-256 is verified, and complete content is atomically promoted into the
content-addressed cache. A lost response is ambiguous and is reconciled through GET or HEAD rather
than a new attempt.

Execution begins only after:

- dispatch protocol is exactly 2.0;
- GPU build is exactly `b86e2016dbc31058408dc7b3b3ac241397b8a828`;
- callback readiness is recent;
- the current boot/build/model/fixture functional check is valid;
- the m8i lease remains current.

A callback 401 enters an authentication-blocked state and is non-retryable. A callback 409 fences
the obsolete attempt. Outputs remain on persistent EBS until m8i acknowledges atomic completion.

## m8i dispatcher and reconciliation

The m8i dispatcher is durable and runs in a single web process against the authoritative SQLite
catalog. SQLite transactions and attempt fencing serialize leadership, job claims, and dispatch
state; an in-memory mutex alone is insufficient.

For one runnable job it:

1. records lifecycle demand and starts g4dn if required;
2. waits for EC2 and authenticated application readiness;
3. validates protocol, exact GPU build, callback readiness, current functional check, and the
   expected push worker heartbeat;
4. triggers one deduplicated job-preflight check when necessary and persists the observation;
5. claims one job through existing lease machinery and creates its immutable job context;
6. creates or reconciles a stable dispatch ID and strict protocol model;
7. resumes checksum-addressed input uploads in bounded chunks;
8. renews the m8i lease about every 15 seconds through receipt and start acceptance;
9. posts start with the exact context and lease token;
10. lets the GPU callback heartbeat loop maintain the accepted lease;
11. reconciles ambiguous responses and restarts from durable state;
12. sends cancel when work is cancelled or superseded.

The dispatcher never accepts arbitrary filesystem paths. Inputs resolve through catalog/workspace
controls. Transport failures use bounded exponential backoff with jitter. A 401 is an operator
alarm, build/protocol mismatch is a blocked deployment, and obsolete 409 work is fenced rather than
retried as a fresh completion.

`MOSHI_DEPLOYMENT_GENERATION` identifies the m8i code. `MOSHI_GPU_REQUIRED_BUILD_ID` identifies
the required g4dn code. They must remain separate even when both happen to be Git revisions.

## Functional-check scope

A pass is valid once for a matching host boot, service boot, GPU build, dispatch protocol,
model/config fingerprint, fixture hash, and definition version until its expiry. It is not rerun
per user request. A previous-boot pass remains history and never makes a stopped instance READY.

m8i stores bounded shared records including identifiers, protocols/builds, timestamps, GPU device,
segment count, CER/threshold, timing, and sanitized failure class/summary. It does not store fixture
or decoded transcripts, real user audio, private paths, raw stack traces, Authorization headers, or
lease tokens.

Authenticated website users may request a manual check. The API enforces same-origin requests,
trusted proxy identity, global deduplication, per-user cooldown/hourly limits, and global cold-start
cooldown. Automatic job preflight bypasses user cooldown but remains globally deduplicated.

## Lifecycle demand and stopping

Only m8i may call EC2 Describe, Start, or Stop. Demand includes:

- runnable queued work;
- a valid lease;
- active dispatch, upload, or start reconciliation;
- an active functional check;
- an acknowledged result still awaiting reconciliation.

A manual check may wake a stopped GPU with no user job. Before stopping, m8i marks draining,
confirms no active job/check/dispatch or pending result, and rechecks transactionally immediately
before `StopInstances`. It never stops during a valid lease. The idle grace remains approximately
15 minutes. Failed readiness gates are bounded so they cannot hold the instance forever.

AWS observation, worker heartbeat, intake observation, and functional-check timestamps remain
separate.

## Status API and page

The website reads m8i SQLite/aggregated state through:

- `GET /api/system/gpu`;
- `GET /api/system/gpu/checks?limit=10`;
- `POST /api/system/gpu/checks`.

A browser never calls AWS or g4dn. The shared page shows Machine, GPU service, Functional check, and
bounded History. It polls every five seconds while transitional or active and every thirty seconds
while stable. OFF overrides a historical pass when the instance is stopped.

## Crash and race invariants

Tests and operations must preserve:

- duplicate request/tick idempotency;
- lost create/start/completion response reconciliation;
- interrupted/resumed uploads;
- m8i or GPU restart during receipt/execution;
- lease expiry and a fenced successor attempt;
- rejection of late completion from an old attempt;
- source/config supersession and cancellation;
- callback 401 alarm and 409 fencing;
- build/protocol mismatch blocking;
- GPU busy backpressure;
- functional-check failure separated from user-audio failure;
- stop request racing new demand.

Exactly-once network delivery is not assumed. Durable at-least-once delivery, idempotent acceptance,
and fencing provide the safety boundary.

## Deployment status and stop condition

The code supports the m8i dispatcher/status workstream, but production cutover is not authorized
until the live port-80 boundary exists and is reviewed. The inspected Windows Server 2025 host had
healthy Uvicorn on 8765 but no port-80/443 listener, disabled IIS web features, no Nginx, and no
identified supervisor for Uvicorn. A legacy firewall rule exposed 8765 to the GPU.

Do not work around this by binding Uvicorn to port 80 or opening 8765 more broadly. Select the
Windows HTTP server/authentication boundary, implement exact callback path/source/method controls,
install a matching Windows Firewall rule, supervise one web/dispatcher process, and validate the
boundary before migration or token-enabled cutover.

See `deployment/README.md` for backup, migration, verification, rollback, and end-to-end acceptance.
