# AWS deployment

This package provisions the two-machine production design in one Availability Zone:

- an always-on `t3.large` web host with an Elastic IP and a preserved 500 GiB workspace EBS volume;
- an on-demand `g6.2xlarge` NVIDIA L4 worker with no inbound rules and a preserved 150 GiB cache volume;
- immutable ECR repositories, least-privilege instance roles, IMDSv2, KMS encryption, daily AWS
  Backup recovery points, daily SQLite backups, and EC2 status alarms.

The default worker design uses a public IPv4 address with zero inbound security-group rules. This
avoids permanent NAT Gateway cost for an intermittent machine. Only the web host receives public
traffic, and only on 443; worker traffic reaches private port 8765 from the processing security
group. Administration is through Systems Manager Session Manager.

## Prerequisites

Install AWS CLI v2, Terraform 1.7 or newer, Docker, and PowerShell 7. Authenticate the AWS CLI to
the target account. In the SSM Parameter Store console, create a high-entropy worker token and the
Hugging Face token as `SecureString` parameters before Terraform. Their names, but never their
values, go in `terraform.tfvars`. Do not place the values in Terraform state, shell history, user
data, or build arguments.

Copy `terraform/terraform.tfvars.example` to the ignored `terraform/terraform.tfvars`, choose a
region and AZ, and supply an existing SNS topic with a confirmed on-call subscription. Then run the
read-only preflight. It verifies credentials, the current DLAMI public
parameter, G6 offerings, the G/VT On-Demand vCPU quota, and both parameter types:

```powershell
./deployment/preflight.ps1 -Region eu-central-1 `
  -WorkerTokenParameter /moshi/production/worker-token `
  -HfTokenParameter /moshi/production/huggingface-token
```

An advertised offering and sufficient quota do not guarantee live On-Demand capacity. Confirm the
selected AZ with a short smoke deployment before production cutover.

## Provision and publish

ECR must exist before images can be pushed. Initialize Terraform, create only the two repositories,
publish both build contexts under the same immutable tag, and then apply the complete graph:

```powershell
terraform -chdir=deployment/terraform init
terraform -chdir=deployment/terraform fmt -check
terraform -chdir=deployment/terraform validate
terraform -chdir=deployment/terraform apply `
  -target=aws_ecr_repository.web -target=aws_ecr_repository.processing
./deployment/build-push.ps1 -ImageTag 0123456789abcdef0123456789abcdef01234567 `
  -Region eu-central-1
terraform -chdir=deployment/terraform apply
```

Both data volumes have Terraform `prevent_destroy` guards and survive EC2 replacement. Removing the
stack therefore intentionally requires an explicit data-retention decision and a separate edit.
If `image_tag` changes, Terraform replaces the instances because their bootstrap data is immutable;
the two separately attached guarded volumes are reattached to the replacements. During a rollout,
leave the old processing image in ECR until all outstanding leases from that build have ended.

The bootstrap scripts mount data volumes by EBS volume ID and then persist their filesystem UUIDs
in `fstab`. The web container runs with one Uvicorn worker. The processing container uses the NVIDIA
runtime, reports protocol/build compatibility, processes one leased job at a time, and is stopped by
the web lifecycle controller only after the queue is empty and no valid leases exist for 15 minutes.

## DNS, HTTPS, and Basic Auth

Point the domain A record at Terraform's `web_elastic_ip` output. Through an SSM session, install a
trusted certificate and private key under `/etc/letsencrypt/live/<domain>/`, create individual
accounts with `htpasswd /etc/nginx/moshi.htpasswd <username>`, and copy
`web_service/nginx/moshi.conf` to `/etc/nginx/sites-enabled/moshi` after replacing the placeholder
domain. Remove the default site, run `nginx -t`, and enable Nginx. The shipped configuration rejects
all `/internal/*` requests at the public listener and disables proxy buffering for job event streams.
TLS and password hashes are deliberately outside Terraform and container images.

## Cutover and recovery

Before the first production start, stop the old application and run the migration against the
mounted workspace from the new web image:

```bash
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --apply
python -m moshi_data_pipeline migrate-workspace --workspace /data/studio_workspace --verify
```

Retain the migration's SQLite backup and take a matching EBS recovery point. Never run the old binary
against the migrated catalog. Test restoration by restoring the workspace recovery point to a new
volume and verifying the latest `/data/backups/catalog-*.sqlite3` checksum and SQLite integrity.

The deployment is not accepted until a queued job starts a stopped GPU, completes through the remote
protocol, drains the queue, and stops the GPU after the full idle window. Also force one worker
interruption and one incompatible protocol build to verify recovery and the visible cost guard.
