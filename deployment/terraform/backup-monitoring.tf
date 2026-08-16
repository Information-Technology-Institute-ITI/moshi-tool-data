resource "aws_backup_vault" "main" {
  name        = replace("${var.name}-vault", "-", "_")
  kms_key_arn = aws_kms_key.ebs.arn
}

resource "aws_backup_plan" "daily" {
  name = "${var.name}-daily"

  rule {
    rule_name         = "daily-ebs"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 3 * * ? *)"
    start_window      = 60
    completion_window = 180

    lifecycle {
      delete_after = var.backup_retention_days
    }

    recovery_point_tags = {
      Application = var.name
      Schedule    = "daily"
    }
  }
}

resource "aws_backup_selection" "data_volumes" {
  name         = "${var.name}-data-volumes"
  plan_id      = aws_backup_plan.daily.id
  iam_role_arn = aws_iam_role.backup.arn
  resources = [
    aws_ebs_volume.workspace.arn,
    aws_ebs_volume.processing_cache.arn,
  ]
}

locals {
  alarm_actions = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "web_status" {
  alarm_name          = "${var.name}-web-status-check"
  alarm_description   = "Web EC2 status check failed"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions

  dimensions = { InstanceId = aws_instance.web.id }
}

resource "aws_cloudwatch_metric_alarm" "processing_status" {
  alarm_name          = "${var.name}-processing-status-check"
  alarm_description   = "Processing EC2 status check failed while running"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = { InstanceId = aws_instance.processing.id }
}

locals {
  custom_alarms = {
    lifecycle-blocked = {
      metric             = "LifecycleBlocked"
      threshold          = 0
      period             = 60
      evaluation_periods = 1
    }
    controller-error = {
      metric             = "ControllerError"
      threshold          = 0
      period             = 60
      evaluation_periods = 2
    }
    worker-unavailable = {
      metric             = "WorkerUnavailableWithQueuedWork"
      threshold          = 0
      period             = 60
      evaluation_periods = 3
    }
    queue-age = {
      metric             = "QueueOldestAgeSeconds"
      threshold          = 1800
      period             = 60
      evaluation_periods = 3
    }
    workspace-disk = {
      metric             = "WorkspaceUsedPercent"
      threshold          = 85
      period             = 300
      evaluation_periods = 3
    }
    sqlite-backup-age = {
      metric             = "SqliteBackupAgeSeconds"
      threshold          = 129600
      period             = 300
      evaluation_periods = 2
    }
    gpu-running-four-hours = {
      metric             = "GpuInstanceRunning"
      threshold          = 0
      period             = 3600
      evaluation_periods = 4
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "operational" {
  for_each = local.custom_alarms

  alarm_name          = "${var.name}-${each.key}"
  namespace           = "Moshi/Studio"
  metric_name         = each.value.metric
  statistic           = "Maximum"
  period              = each.value.period
  evaluation_periods  = each.value.evaluation_periods
  datapoints_to_alarm = each.value.evaluation_periods
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
