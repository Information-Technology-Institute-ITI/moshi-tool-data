data "aws_ssm_parameter" "web_ami" {
  name = var.web_ami_ssm_parameter
}

data "aws_ssm_parameter" "processing_ami" {
  name = var.processing_ami_ssm_parameter
}

locals {
  web_image        = "${aws_ecr_repository.web.repository_url}:${var.image_tag}"
  processing_image = "${aws_ecr_repository.processing.repository_url}:${var.image_tag}"
  registry_host    = split("/", aws_ecr_repository.web.repository_url)[0]
}

resource "aws_instance" "processing" {
  depends_on = [
    aws_iam_role_policy.processing_runtime,
    aws_iam_role_policy_attachment.processing_ecr,
    aws_iam_role_policy_attachment.processing_ssm,
  ]

  ami                         = data.aws_ssm_parameter.processing_ami.value
  instance_type               = var.processing_instance_type
  availability_zone           = local.availability_zone
  subnet_id                   = aws_subnet.public.id
  private_ip                  = local.processing_private_ip
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.processing.id]
  iam_instance_profile        = aws_iam_instance_profile.processing.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/processing-bootstrap.sh.tftpl", {
    aws_region              = var.aws_region
    cache_volume_id         = aws_ebs_volume.processing_cache.id
    hf_token_parameter_name = var.hf_token_parameter_name
    image                   = local.processing_image
    image_tag               = var.image_tag
    registry_host           = local.registry_host
    web_internal_url        = "http://${local.web_private_ip}:8765"
    worker_token_parameter  = var.worker_token_parameter_name
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 80
    encrypted             = true
    kms_key_id            = aws_kms_key.ebs.arn
    delete_on_termination = true
  }

  tags = {
    Name = "${var.name}-processing"
    Role = "processing"
  }
}

resource "aws_instance" "web" {
  depends_on = [
    aws_iam_role_policy.web_runtime,
    aws_iam_role_policy_attachment.web_ecr,
    aws_iam_role_policy_attachment.web_ssm,
  ]

  ami                         = data.aws_ssm_parameter.web_ami.value
  instance_type               = var.web_instance_type
  availability_zone           = local.availability_zone
  subnet_id                   = aws_subnet.public.id
  private_ip                  = local.web_private_ip
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.web.id]
  iam_instance_profile        = aws_iam_instance_profile.web.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/templates/web-bootstrap.sh.tftpl", {
    aws_region             = var.aws_region
    domain_name            = var.domain_name
    image                  = local.web_image
    image_tag              = var.image_tag
    processing_instance_id = aws_instance.processing.id
    registry_host          = local.registry_host
    backup_retention_days  = var.backup_retention_days
    worker_token_parameter = var.worker_token_parameter_name
    workspace_volume_id    = aws_ebs_volume.workspace.id
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 80
    encrypted             = true
    kms_key_id            = aws_kms_key.ebs.arn
    delete_on_termination = true
  }

  tags = {
    Name = "${var.name}-web"
    Role = "web"
  }
}

resource "aws_volume_attachment" "workspace" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.workspace.id
  instance_id = aws_instance.web.id
}

resource "aws_volume_attachment" "processing_cache" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.processing_cache.id
  instance_id = aws_instance.processing.id
}

resource "aws_eip_association" "web" {
  instance_id   = aws_instance.web.id
  allocation_id = aws_eip.web.id
}
