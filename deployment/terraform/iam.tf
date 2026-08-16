data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  worker_parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.worker_token_parameter_name, "/")}"
  hf_parameter_arns = var.hf_token_parameter_name == "" ? [] : [
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.hf_token_parameter_name, "/")}"
  ]
  ec2_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "web" {
  name               = "${var.name}-web"
  assume_role_policy = local.ec2_assume_role
}

resource "aws_iam_role" "processing" {
  name               = "${var.name}-processing"
  assume_role_policy = local.ec2_assume_role
}

resource "aws_iam_role_policy_attachment" "web_ssm" {
  role       = aws_iam_role.web.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "processing_ssm" {
  role       = aws_iam_role.processing.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "web_ecr" {
  role       = aws_iam_role.web.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "processing_ecr" {
  role       = aws_iam_role.processing.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy" "web_runtime" {
  name = "${var.name}-web-runtime"
  role = aws_iam_role.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWorkerToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.worker_parameter_arn
      },
      {
        Sid      = "DescribeProcessingInstance"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "PublishOperationalMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "Moshi/Studio"
          }
        }
      },
      {
        Sid      = "ControlOnlyProcessingInstance"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances", "ec2:StopInstances"]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${aws_instance.processing.id}"
      }
    ]
  })
}

resource "aws_iam_role_policy" "processing_runtime" {
  name = "${var.name}-processing-runtime"
  role = aws_iam_role.processing.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadRuntimeTokens"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = concat([local.worker_parameter_arn], local.hf_parameter_arns)
    }]
  })
}

resource "aws_iam_instance_profile" "web" {
  name = "${var.name}-web"
  role = aws_iam_role.web.name
}

resource "aws_iam_instance_profile" "processing" {
  name = "${var.name}-processing"
  role = aws_iam_role.processing.name
}

resource "aws_iam_role" "backup" {
  name = "${var.name}-backup"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "backup.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "backup_restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

resource "aws_iam_role_policy" "backup_kms" {
  name = "${var.name}-backup-kms"
  role = aws_iam_role.backup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "kms:CreateGrant",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:ReEncryptFrom",
        "kms:ReEncryptTo"
      ]
      Resource = aws_kms_key.ebs.arn
    }]
  })
}
