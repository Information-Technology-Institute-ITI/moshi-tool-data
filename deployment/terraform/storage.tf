resource "aws_kms_key" "ebs" {
  description             = "${var.name} EBS volume encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "ebs" {
  name          = "alias/${var.name}-ebs"
  target_key_id = aws_kms_key.ebs.key_id
}

resource "aws_ebs_volume" "workspace" {
  availability_zone = local.availability_zone
  size              = var.web_workspace_size_gib
  type              = "gp3"
  encrypted         = true
  kms_key_id        = aws_kms_key.ebs.arn

  tags = {
    Name   = "${var.name}-workspace"
    Backup = "Moshi"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ebs_volume" "processing_cache" {
  availability_zone = local.availability_zone
  size              = var.processing_cache_size_gib
  type              = "gp3"
  encrypted         = true
  kms_key_id        = aws_kms_key.ebs.arn

  tags = {
    Name   = "${var.name}-processing-cache"
    Backup = "Moshi"
  }


  lifecycle {
    prevent_destroy = true
  }
}
