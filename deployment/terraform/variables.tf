variable "aws_region" {
  description = "AWS region verified by deployment/preflight.ps1 for G6 quota and capacity."
  type        = string
}

variable "availability_zone" {
  description = "Optional verified AZ. The first available AZ is used when omitted."
  type        = string
  default     = null
}

variable "name" {
  description = "Resource name prefix."
  type        = string
  default     = "moshi-studio"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "public_subnet_cidr" {
  type    = string
  default = "10.42.10.0/24"
}

variable "public_https_cidrs" {
  description = "IPv4 networks allowed to reach Nginx HTTPS."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "web_instance_type" {
  type    = string
  default = "t3.large"
}

variable "processing_instance_type" {
  type    = string
  default = "g6.2xlarge"
}

variable "web_ami_ssm_parameter" {
  type    = string
  default = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

variable "processing_ami_ssm_parameter" {
  description = "AWS Deep Learning Base OSS NVIDIA Driver GPU AMI public parameter."
  type        = string
  default     = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
}

variable "image_tag" {
  description = "Immutable image tag, normally the Git commit SHA."
  type        = string

  validation {
    condition     = length(var.image_tag) >= 7 && var.image_tag != "latest"
    error_message = "image_tag must be an immutable build identifier and cannot be latest."
  }
}

variable "worker_token_parameter_name" {
  description = "Existing SecureString parameter containing the shared worker bearer token."
  type        = string
}

variable "hf_token_parameter_name" {
  description = "Optional existing SecureString parameter containing a Hugging Face token."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "DNS name used by the separately installed Nginx TLS configuration."
  type        = string
}

variable "web_workspace_size_gib" {
  type    = number
  default = 500
}

variable "processing_cache_size_gib" {
  type    = number
  default = 150
}

variable "backup_retention_days" {
  type    = number
  default = 14
}

variable "alarm_topic_arn" {
  description = "Optional SNS topic ARN for EC2 status alarms."
  type        = string
  default     = ""

  validation {
    condition     = var.alarm_topic_arn != ""
    error_message = "alarm_topic_arn is required so production alarms have a delivery target."
  }
}
