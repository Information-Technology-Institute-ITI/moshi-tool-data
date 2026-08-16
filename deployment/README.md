# m8i and g4dn push deployment

This is the current two-machine deployment guide. The m8i host is authoritative for the website,
queue, artifacts, lifecycle state, functional-check history, and SQLite. The g4dn host accepts
immutable protocol 2.0 dispatches, executes one GPU job at a time, and returns results through the
existing protocol 1.0 callback API.

Do not mount the m8i workspace on g4dn, expose SQLite, open port 5432, or give g4dn EC2 lifecycle
permissions.

## Fixed network map

```text
browser                     -> m8i port 80
m8i dispatcher              -> 172.31.26.80:8766
g4dn callback/outbox        -> 172.31.52.46:80 /internal/v1/*
```

The web listener reads `MOSHI_WEB_PORT` from its protected environment. On this Windows test host it
binds directly to loopback port 80, so there is no second web listener or port-forward.

Port 80 is plaintext in this agreed initial configuration. Audio, results, and credentials are not
confidential in transit. HTTPS 443 or private TLS/mTLS is required hardening before treating this
as a confidential production transport.

## Current m8i deployment stop condition

The inspected host is Windows Server 2025. At the 2026-08-16 inspection:

- Uvicorn listened on `0.0.0.0:8765` with one worker and returned a healthy m8i response;
- no process listened on port 80 or 443;
- IIS web, Basic Authentication, IP Security, and request-filtering features were disabled;
- Nginx was absent;
- the Uvicorn process was not associated with an identified Windows service or scheduled task;
- a legacy Windows Firewall rule allowed g4dn `172.31.26.80` to reach port 8765.

The GPU intake itself is reachable from m8i on 8766 and reports protocol 2.0 and standalone build
`b86e2016dbc31058408dc7b3b3ac241397b8a828`.

Production cutover must stop until the actual supervised port-80 server and authentication boundary
are selected, installed by the operator, and validated. Do not bind Uvicorn directly to port 80,
blindly install Nginx/IIS/Caddy, redirect port 80 to 8765, or broaden a security group or firewall
as a workaround. The checked-in Nginx file is a Linux example, not the live Windows configuration.

### Loopback-only website test

For browser testing on the m8i host itself, set `MOSHI_WEB_PORT=80` and
`MOSHI_WEB_BIND_ADDRESS=127.0.0.1` in the protected `.env`, then run:

```powershell
./deployment/restart-web.ps1 -Action Restart
```

The restart script loads `.env` without displaying values, removes its former owned port mapping,
stops only identified Moshi web processes, creates and verifies a SQLite backup, and starts one
fresh process. Open `http://127.0.0.1` on the same machine. Inspect or stop it with:

```powershell
./deployment/restart-web.ps1 -Action Status
./deployment/restart-web.ps1 -Action Stop
```

The script creates no firewall rule and refuses a non-loopback bind unless the operator explicitly
acknowledges that exposure. Loopback mode is not the production authentication boundary and cannot
receive GPU callbacks.

Host-specific nonsecret values may be placed in the ignored `.runtime/web-runtime.env`. Install the
separate GPU dispatch credential through hidden input into the ACL-restricted ignored secret file:

```powershell
./deployment/set-web-dispatch-token.ps1
./deployment/restart-web.ps1 -Action Restart
```

Neither script displays the token. The restart loads `.env`, then the runtime overlay, then the
protected secret overlay.

For a browser running directly on the m8i console, the optional
`MOSHI_LOOPBACK_AUTHENTICATED_USER` setting supplies a test-only identity to requests whose socket
source is loopback. It never trusts a browser-provided identity header and does not authenticate
remote website users. Remove it when the reviewed reverse-proxy authentication boundary is
installed.

## AWS and host prerequisites

Keep both instances in the same VPC and region. Record the exact region, instance IDs, private
addresses, security-group IDs, storage volumes, and deployed revisions in protected operations
notes.

The g4dn security group and host firewall allow inbound TCP 8766 only from the m8i security group
and `172.31.52.46`. They must not expose 8766 publicly.

The m8i port-80 boundary accepts the existing browser traffic and the fixed GPU callback source. It
must restrict callback routes by both source IP and path. Windows Firewall supplies a host-level
source/port rule, while the HTTP server supplies path and method rules.

The m8i instance role uses the default AWS credential chain and needs:

- `ec2:DescribeInstances`;
- `ec2:StartInstances` and `ec2:StopInstances` restricted to the exact g4dn instance;
- any ECR/SSM/KMS permissions required by the chosen deployment and protected-secret workflow.

The g4dn role may pull its image or read its own protected model/token parameters, but receives no
EC2 start/stop permission. Require IMDSv2 and encrypted persistent volumes on both hosts.

## Read-only AWS preflight

The preflight verifies caller identity, the selected GPU instance type offering and quota, the AMI
parameter, and the existence and SecureString type of the worker, dispatcher, and optional Hugging
Face token parameters. It never decrypts them.

```powershell
./deployment/preflight.ps1 -Region <actual-region> -ProcessingInstanceType <actual-g4dn-type> -WorkerTokenParameter /moshi/production/worker-token -DispatchTokenParameter /moshi/production/dispatch-token -HfTokenParameter /moshi/production/huggingface-token
```

An advertised offering and quota do not guarantee current On-Demand capacity. Confirm the actual
Availability Zone and instance separately.

## Build and publish immutable deployables

The web generation and GPU build are different deployables. The build script requires separate
identifiers so an operator cannot accidentally treat them as the same value:

```powershell
./deployment/build-push.ps1 -WebGeneration <m8i-revision> -GpuBuildId b86e2016dbc31058408dc7b3b3ac241397b8a828 -Region <actual-region> -WebRepository <account>.dkr.ecr.<region>.amazonaws.com/moshi-web -ProcessingRepository <account>.dkr.ecr.<region>.amazonaws.com/moshi-processing
```

Configure `MOSHI_DEPLOYMENT_GENERATION` with the web revision. Configure
`MOSHI_GPU_REQUIRED_BUILD_ID` on m8i and `MOSHI_BUILD_ID` on g4dn with the exact GPU build.
Never use `latest` as the only deployment identifier. Retain the prior web and GPU revisions until
outstanding leases and rollback windows have ended.

The build contexts are independent. The web image remains lightweight and contains no FFmpeg,
WhisperX, PyTorch, CUDA, or local ML runtime.

## m8i nonsecret configuration

Supply nonsecret settings through the reviewed Windows supervisor or a protected configuration file
outside the repository:

```dotenv
MOSHI_WORKSPACE=<actual persistent workspace>
MOSHI_GPU_INTERNAL_URL=http://172.31.26.80:8766
MOSHI_GPU_REQUIRED_BUILD_ID=b86e2016dbc31058408dc7b3b3ac241397b8a828
MOSHI_GPU_INSTANCE_ID=<exact g4dn instance ID>
AWS_REGION=<actual region>
MOSHI_DEPLOYMENT_GENERATION=<m8i deployment revision>
MOSHI_GPU_IDLE_SECONDS=900
MOSHI_WORKER_SOURCE_IPS=172.31.26.80
MOSHI_TRUSTED_ORIGINS=http://<public-m8i-host>
MOSHI_AUTHENTICATED_USER_HEADER=X-Moshi-Authenticated-User
MOSHI_TRUST_PROXY_AUTH=1
MOSHI_LOOPBACK_AUTHENTICATED_USER=
```

Use the default AWS credential chain. Do not put AWS access keys in this file.

The m8i lifecycle controller owns the idle timer and every EC2
`DescribeInstances`/`StartInstances`/`StopInstances` call. `MOSHI_GPU_IDLE_SECONDS` defaults to 900
seconds. The GPU reports whether callbacks, checks, dispatches, and its durable outbox are safe; it
does not stop its own EC2 instance.

Run one Uvicorn worker with the local ML worker disabled and the durable dispatcher enabled. If the
dispatcher remains owned by the web lifespan, do not scale Uvicorn to multiple processes against
the same SQLite database. Keep Uvicorn private to the reviewed port-80 server; a loopback bind is
preferred once the boundary is installed.

## Separate protected credentials

Use unrelated high-entropy values:

- `MOSHI_DISPATCH_TOKEN`: m8i authenticates to g4dn protocol 2.0.
- `MOSHI_WORKER_TOKEN`: g4dn authenticates to m8i protocol 1.0 callbacks.

The GPU callback token was rotated after the GPU rollout. Both machines must receive the current
protected worker value through a secure terminal or managed secret store. Never paste either value
into chat, place it in a URL or process argument, print it during verification, or commit it.

A missing credential is a deployment stop. Create protected files atomically from a hidden-input
operator terminal, grant only the reviewed service identity access, and load them through the
supervisor. Record file paths and ACL verification, not contents.

## Required m8i port-80 behavior

The selected Windows HTTP server must preserve the existing website authentication and apply these
rules before forwarding requests to the application:

1. Browser routes retain authentication.
2. The server strips any incoming `X-Moshi-Authenticated-User`,
   `X-Forwarded-For`, and equivalent trusted headers.
3. For authenticated browser requests, it sets `X-Moshi-Authenticated-User` from the server's
   authenticated identity and overwrites forwarding data with the actual peer.
4. Only these callback shapes bypass website Basic Auth:
   - `POST /internal/v1/workers/heartbeat`
   - `POST /internal/v1/jobs/{job_id}/heartbeat`
   - `POST /internal/v1/jobs/{job_id}/uploads`
   - `POST /internal/v1/jobs/{job_id}/complete`
   - `POST /internal/v1/jobs/{job_id}/fail`
   - `HEAD|PUT /internal/v1/uploads/{upload_id}`
5. Those shapes allow source `172.31.26.80` only. Every other `/internal/*` route returns 404.
6. Authorization and `X-Lease-Token` reach FastAPI but are never written to access or error logs.
7. Uploads stream without proxy buffering and use the agreed maximum body size, bounded
   connections/rate, and long but finite body/send/read timeouts.

FastAPI still requires the worker Bearer token, lease token, input fingerprint, job kind, artifact
checksum, and active attempt. Proxy filtering is an additional boundary, not a replacement.
Application source checking through `MOSHI_WORKER_SOURCE_IPS` also remains enabled.

The Linux Nginx example in `web_service/nginx/moshi.conf` demonstrates equivalent rules and can be
validated with `nginx -t` in a disposable Linux environment. It must not be copied onto the live
Windows host without an explicit platform decision.

## g4dn push service

Run `moshi-gpu-intake.service` only after stopping and disabling the legacy pull worker. Required
nonsecret values include:

```dotenv
MOSHI_WEB_INTERNAL_URL=http://172.31.52.46
MOSHI_BUILD_ID=b86e2016dbc31058408dc7b3b3ac241397b8a828
MOSHI_WORKER_CACHE=/home/ubuntu/moshi-worker-cache
MOSHI_GPU_INTAKE_PORT=8766
MOSHI_CONFIG=<absolute processing config path>
MOSHI_SELF_TEST_METADATA=<absolute fixture metadata path>
```

Load the dispatcher, worker, and Hugging Face credentials from separate mode-0600 files outside the
repository. Persist intake state, verified inputs, functional-check history, model cache, and
callback outbox on encrypted EBS. Do not store durable state on instance NVMe.

## Backup, migration, and rollback

Before applying a migration to the production workspace:

1. Resolve the actual `catalog.sqlite3` used by the running process; do not assume a repository
   default.
2. Mark the dispatcher draining and confirm no valid lease, active upload/dispatch/check, or
   unacknowledged result exists.
3. Identify and stop or quiesce every SQLite writer through its actual supervisor.
4. Create a timestamped online SQLite backup in the protected backup location.
5. Run `PRAGMA integrity_check` against the backup and record its SHA-256 and path without
   exposing user data.
6. Take a matching volume recovery point or filesystem backup.
7. Test migration dry-run, apply, verification, and rollback/restoration on a copy.
8. Retain the prior application revision, but never run it against an incompatibly migrated
   catalog.

The migration commands are:

```powershell
python -m moshi_data_pipeline migrate-workspace --workspace <actual-workspace>
python -m moshi_data_pipeline migrate-workspace --workspace <actual-workspace> --apply
python -m moshi_data_pipeline migrate-workspace --workspace <actual-workspace> --verify
```

Do not continue if the database cannot be quiesced, backed up, and verified.

## Verification and cutover

Run Ruff, the complete Python suite, both frontend test/typecheck contexts, production frontend
builds, repository synchronization checks, and proxy validation appropriate to the chosen server.

Then verify in order:

1. The supervised Uvicorn process is healthy on the configured `MOSHI_WEB_PORT`.
2. The reviewed boundary listens on m8i port 80 and preserves website authentication.
3. From m8i, bounded `GET http://172.31.26.80:8766/health/live` succeeds.
4. Authenticated readiness matches dispatch protocol 2.0 and exact GPU build without displaying the
   token.
5. A g4dn worker heartbeat reaches m8i port 80 and blocked paths/sources do not.
6. A website-originated manual check starts a stopped GPU if necessary and its durable shared
   record appears in m8i SQLite and the status page.
7. With g4dn stopped, submit one small job through the website and observe stopped, pending,
   running, application readiness, deduplicated preflight, lease, resumable transfer, execution,
   callback, and atomic visibility.
8. Confirm no duplicate attempt/artifact, then observe queue drain and the approximately 15-minute
   idle stop.
9. Repeat a cold-start job and one forced interruption/reconciliation test.

All distributed jobs originate from the website. Do not process the ignored test media directly on
g4dn.

## Stop conditions

Stop without weakening security if any of these occurs:

- either protected token is unavailable;
- callback authentication returns 401;
- protocol or GPU build differs;
- private TCP 8766 is unreachable from m8i;
- g4dn cannot reach m8i port 80;
- the actual port-80 authentication/proxy boundary is unidentified;
- a safe verified database backup cannot be created;
- an existing user change conflicts with migration;
- the dispatcher cannot establish single leadership;
- a stale lease, attempt, or completion returns 409.

Do not convert these conditions into broad firewall rules, disabled authentication, direct database
access, or a second attempt.
