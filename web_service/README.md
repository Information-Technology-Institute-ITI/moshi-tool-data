# Moshi web service

This folder is an independent build context for the always-on service. It owns SQLite,
the permanent Studio workspace, public UI/API, private worker protocol, artifact commits,
and EC2 lifecycle control. It must be run with one Uvicorn worker. The image deliberately
omits FFmpeg and all ML runtimes; media processing belongs to the GPU service.

## Local image

```bash
docker build -t moshi-web ./web_service
docker run --rm -p 8765:8765 \
  -e MOSHI_WORKER_TOKEN=replace-with-a-random-secret \
  -v moshi-workspace:/data/studio_workspace \
  moshi-web
```

The public AWS listener is the host Nginx configuration in `nginx/moshi.conf`. Replace
the placeholder domain and certificate paths, create `/etc/nginx/moshi.htpasswd` with
individual accounts, and ensure the public security group exposes only 443. The private
FastAPI port is allowed only from the processing security group.

Run migration in the source checkout before cutover:

```bash
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --apply
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --verify
```

`MOSHI_GPU_INSTANCE_ID` enables the AWS provider. Without it, lifecycle actions are
recorded locally but do not invoke Docker or AWS.
