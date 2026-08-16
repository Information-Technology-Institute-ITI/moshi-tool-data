# Manual AWS deployment

This project uses two manually provisioned EC2 machines. It does not require an infrastructure-as-
code tool:

- an always-on `t3.large` web host with a public Elastic IP and a persistent workspace volume;
- an on-demand `g6.2xlarge` NVIDIA L4 worker with a persistent model/cache volume.

Only the web host receives public traffic. The processing host has no inbound public rules and
reaches the web service over the VPC private network. Use Systems Manager Session Manager for
administration where possible.

## 1. Create the AWS resources

Create these resources manually in one AWS region and Availability Zone:

1. Two immutable private ECR repositories, one for `moshi-web` and one for `moshi-processing`.
2. A VPC/subnet arrangement that gives both machines outbound HTTPS access. A public IPv4 address
   with no inbound rules is acceptable for the intermittent worker and avoids a permanent NAT
   Gateway charge.
3. A web security group allowing public HTTPS on port 443 and private TCP 8765 only from the
   processing security group.
4. A processing security group with no inbound rules.
5. An Ubuntu x86-64 `t3.large` web instance, an Elastic IP, and an encrypted persistent workspace
   EBS volume (500 GiB recommended).
6. An x86-64 `g6.2xlarge` instance based on the AWS Deep Learning Base OSS NVIDIA Driver GPU AMI,
   plus an encrypted persistent cache EBS volume (150 GiB recommended).
7. Two SSM `SecureString` parameters: a high-entropy shared worker token and the Hugging Face token.
8. An SNS topic with a confirmed operator subscription, CloudWatch alarms, and AWS Backup plans for
   the persistent volumes.

Attach instance profiles that permit SSM Session Manager, ECR image pulls, and decryption of only
the parameters each machine needs. The web role also needs `ec2:DescribeInstances`, plus
`ec2:StartInstances` and `ec2:StopInstances` restricted to the exact processing instance. Require
IMDSv2 on both machines. Do not give the processing role EC2 lifecycle permissions.

Record the region, account ID, ECR URLs, instance IDs, private IP addresses, security-group IDs,
volume IDs, parameter names, domain, and immutable image tag in the team's secure operations notes.

## 2. Run the preflight

Install AWS CLI v2, Docker, and PowerShell 7 on the build machine and authenticate to the target
account. The read-only preflight checks credentials, the GPU AMI, G6 offerings, quota, and SSM
parameter types:

```powershell
./deployment/preflight.ps1 -Region eu-central-1 `
  -WorkerTokenParameter /moshi/production/worker-token `
  -HfTokenParameter /moshi/production/huggingface-token
```

An advertised offering and sufficient quota do not guarantee current On-Demand capacity. Confirm
the selected Availability Zone with a short processing-machine test.

## 3. Build and publish the images

The AWS operator supplies the two ECR repository URLs directly. Use one immutable tag for both
images; a commit SHA is preferred:

```powershell
./deployment/build-push.ps1 `
  -ImageTag 0123456789abcdef0123456789abcdef01234567 `
  -Region eu-central-1 `
  -WebRepository 123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-web `
  -ProcessingRepository 123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-processing
```

The script builds Linux AMD64 images from the independent `web_service/` and
`processing_service/` contexts, logs in to ECR, and pushes both images. It never sends the root
`.env` file or local workspace to either build context. Never use `latest` as the only deployment
identifier, and retain the previous processing image until its outstanding leases have ended.

## 4. Configure the web machine

Install `awscli`, `docker.io`, `nginx`, and `sqlite3`. Format and mount the workspace EBS volume at
`/data`, persist it in `/etc/fstab` by filesystem UUID, and create:

```text
/data/studio_workspace
/data/backups
```

Use the instance role to authenticate to ECR and retrieve the worker token from SSM. Create a
root-readable-only environment file such as `/run/moshi-web.env`:

```dotenv
MOSHI_WORKER_TOKEN=<value read from SSM>
MOSHI_WORKSPACE=/data/studio_workspace
MOSHI_GPU_INSTANCE_ID=<processing instance ID>
MOSHI_DEPLOYMENT_GENERATION=<immutable image tag>
MOSHI_TRUSTED_ORIGINS=https://studio.example.com
MOSHI_CLOUDWATCH_NAMESPACE=Moshi/Studio
MOSHI_BACKUP_DIRECTORY=/data/backups
AWS_REGION=eu-central-1
```

Pull and run the pinned web image:

```bash
aws ecr get-login-password --region eu-central-1 | \
  docker login --username AWS --password-stdin 123456789012.dkr.ecr.eu-central-1.amazonaws.com
docker pull 123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-web:IMAGE_TAG
docker rm -f moshi-web 2>/dev/null || true
docker run -d \
  --name moshi-web \
  --restart unless-stopped \
  --network host \
  --env-file /run/moshi-web.env \
  --volume /data/studio_workspace:/data/studio_workspace \
  123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-web:IMAGE_TAG
```

Configure a daily systemd timer that uses SQLite's `.backup` command, verifies
`PRAGMA integrity_check`, writes a SHA-256 sidecar, and removes backups older than the agreed
retention period. Test restoration rather than assuming snapshots are usable.

## 5. Configure the processing machine

Install `awscli` and `docker.io`. Confirm that `nvidia-smi` and `nvidia-ctk` are available, then
configure Docker's NVIDIA runtime:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-smi
```

Format and mount the cache EBS volume at `/cache`, persist it in `/etc/fstab` by filesystem UUID,
and create `/cache/huggingface`, `/cache/inputs`, and `/cache/attempts`. Retrieve both SecureStrings
through the instance role and create `/run/moshi-processing.env` with mode `0600`:

```dotenv
MOSHI_WEB_INTERNAL_URL=http://<web private IP>:8765
MOSHI_WORKER_TOKEN=<same value used by the web machine>
MOSHI_BUILD_ID=<same immutable image tag>
MOSHI_WORKER_CACHE=/cache
HF_HOME=/cache/huggingface
HF_TOKEN=<value read from SSM>
```

Pull and run the pinned processing image:

```bash
aws ecr get-login-password --region eu-central-1 | \
  docker login --username AWS --password-stdin 123456789012.dkr.ecr.eu-central-1.amazonaws.com
docker pull 123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-processing:IMAGE_TAG
docker rm -f moshi-processing 2>/dev/null || true
docker run -d \
  --name moshi-processing \
  --restart unless-stopped \
  --network host \
  --gpus all \
  --env-file /run/moshi-processing.env \
  --volume /cache:/cache \
  123456789012.dkr.ecr.eu-central-1.amazonaws.com/moshi-processing:IMAGE_TAG
```

The restart policy starts the worker container when EC2 starts. The web lifecycle controller uses
the configured processing instance ID and its instance role to start and stop that EC2 machine.

## 6. DNS, HTTPS, and Basic Auth

Point the domain A record at the web Elastic IP. Install a trusted certificate and private key,
create individual Basic Auth accounts with `htpasswd`, and install
`web_service/nginx/moshi.conf` after replacing the placeholder domain. Remove the default Nginx
site, run `nginx -t`, and enable Nginx. Keep port 8765 closed to the public internet; the supplied
configuration rejects `/internal/*` on the public listener.

## 7. Cutover and acceptance

Before opening production, run the workspace migration from the new web image:

```bash
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --apply
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --verify
```

Retain the migration backup and take a matching EBS recovery point. Never run the old binary against
the migrated catalog. Submit a job while the GPU instance is stopped and verify queueing, EC2 start,
worker readiness, processing, atomic result visibility, complete drain, and automatic stop after the
full idle interval. Also test a forced worker interruption and an incompatible protocol build.
