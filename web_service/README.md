# Moshi web service

This folder is the independent build context for the authoritative m8i service. It owns the
Dataset Studio website, jobs, leases, artifact commits, lifecycle state, functional-check history,
and the only SQLite workspace. The GPU never mounts or writes that workspace.

## Runtime and ports

The application process requires `MOSHI_WEB_PORT` and listens on that port. It does not contain a
hardcoded web port.

The agreed initial production map is:

```text
browser -> m8i port 80
m8i dispatcher -> 172.31.26.80:8766
g4dn callbacks -> 172.31.52.46 port 80 /internal/v1/*
```

Run exactly one Uvicorn worker. The dispatcher is part of the authoritative web process and its
leadership is transactionally leased and fenced in SQLite. Keep one web worker because lifecycle
control and the rest of the process-owned supervision are intentionally single-process. The local
ML worker remains disabled.

Port 80 is plaintext in this initial arrangement. It does not provide confidentiality for media,
results, or credentials. HTTPS 443 or private TLS/mTLS is required production hardening.

## Live Windows deployment gate

The inspected m8i host is Windows Server 2025. At the 2026-08-16 inspection, Uvicorn listened on
`0.0.0.0:8765`, but nothing listened on port 80. IIS features were disabled and Nginx was absent.
The Uvicorn process was not owned by an identified Windows service or scheduled task.

That is a deployment stop condition. Do not bind Uvicorn directly to port 80, install a parallel
proxy, or broaden a firewall rule merely to make callbacks connect. First choose and document the
actual supervised port-80 server and public-authentication boundary. It must:

- retain website authentication;
- overwrite, rather than trust, any client `X-Moshi-Authenticated-User` or forwarding header;
- set `X-Moshi-Authenticated-User` only from the authenticated proxy identity;
- allow only the fixed callback routes under `/internal/v1/` from `172.31.26.80`;
- bypass website Basic Auth only for those callback routes while retaining Bearer and lease
  validation in FastAPI;
- stream resumable uploads with bounded body size, request rate, connections, and timeouts;
- avoid logging Authorization and lease-token headers;
- return 404 for every other internal route.

Windows Firewall can restrict an address and port but cannot enforce URL paths. Both the reviewed
HTTP server and a firewall rule are required. The checked-in `nginx/moshi.conf` is a Linux example,
not permission to install Nginx on this host.

## Configuration

Nonsecret settings for m8i include:

```dotenv
MOSHI_WORKSPACE=<persistent workspace>
MOSHI_WEB_PORT=80
MOSHI_WEB_BIND_ADDRESS=127.0.0.1
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

`MOSHI_DEPLOYMENT_GENERATION` identifies the m8i deployable. It is not the GPU build ID. The
required GPU build must match the g4dn `MOSHI_BUILD_ID` exactly.

The m8i lifecycle controller owns the 900-second default idle grace and all EC2 start/stop calls.
The GPU only reports whether its callback, check, dispatch, and outbox state is safe to stop.
`MOSHI_LOOPBACK_AUTHENTICATED_USER` is a console-only integration setting, not a replacement for
the production proxy authentication boundary.

Use two unrelated protected credentials:

- `MOSHI_DISPATCH_TOKEN`: m8i authenticates to g4dn protocol 2.0.
- `MOSHI_WORKER_TOKEN`: g4dn authenticates callbacks to m8i protocol 1.0.

Load them from protected files or a managed secret store outside the repository. Never put either
token in a URL, source file, service command line, access log, or image. Do not paste them into
automation chat. A missing token stops deployment; create it from a hidden-input secure terminal
and restrict its ACL to the service identity.

## Local image

This is development/container wiring, not the production port map:

```bash
docker build -t moshi-web ./web_service
docker run --rm -p 127.0.0.1:${MOSHI_WEB_PORT}:${MOSHI_WEB_PORT} \
  --env-file /protected/path/moshi-web.env \
  -v moshi-workspace:/data/studio_workspace \
  moshi-web
```

The image deliberately omits FFmpeg, WhisperX, PyTorch, CUDA, and other processing runtimes.

## Migration and rollback

Before running a new image or source revision against a production workspace:

1. Identify the actual workspace and supervising process.
2. Drain or stop the dispatcher and quiesce every SQLite writer.
3. Create a timestamped SQLite backup with SQLite's online `.backup` operation.
4. verify `PRAGMA integrity_check` on the backup and record its SHA-256;
5. retain the prior application revision and a matching filesystem/EBS recovery point;
6. run dry-run, apply, and verify migration commands;
7. never start the old binary against a catalog after an irreversible migration.

```bash
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --apply
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --verify
```

On Windows, substitute the reviewed persistent workspace path. Do not proceed if the database
cannot be backed up and verified safely.
