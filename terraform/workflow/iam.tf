# ---------------------------------------------------------------- execution role (ECS agent: pull images, write logs)
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name_prefix}-workflow-exec"
  description        = "ECS task execution role of the workflow task (ECR pull, CloudWatch Logs)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------- task role (the worker)
resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-workflow-task"
  description        = "Workflow worker - anomaly queue, Neptune (anomalies and proposals), proposal audit table (S3 Tables), chat runtime, SSM Run Command on the lab EC2, ECS Exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "AnomalyQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.anomalies.arn]
  }

  # 異常の頂点を読み、修復案の頂点を読み書きする（workflow/awsio.py の gremlin）
  statement {
    sid = "Neptune"
    actions = [
      "neptune-db:ReadDataViaQuery",
      "neptune-db:WriteDataViaQuery",
      "neptune-db:DeleteDataViaQuery",
      "neptune-db:GetQueryStatus",
    ]
    resources = [local.neptune_data_arn]
  }

  # 修復案の証跡を proposal_events に append する（PyIceberg から S3 Tables の Iceberg REST エンドポイント）
  statement {
    sid = "AuditTable"
    actions = [
      "s3tables:GetTableBucket",
      "s3tables:GetNamespace",
      "s3tables:GetTable",
      "s3tables:GetTableMetadataLocation",
      "s3tables:UpdateTableMetadataLocation",
      "s3tables:GetTableData",
      "s3tables:PutTableData",
    ]
    resources = [
      local.audit_bucket_arn,
      "${local.audit_bucket_arn}/table/*",
    ]
  }

  statement {
    sid     = "InvokeRuntime"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      local.runtime_arn,
      "${local.runtime_arn}/runtime-endpoint/*",
    ]
  }

  statement {
    sid       = "Parameters"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.param_prefix}/*"]
  }

  # Apply: `sudo lab <cmd>` を lab EC2 で打つ（lab が無いときは statement ごと無い）
  dynamic "statement" {
    for_each = local.lab_instance_id != "" ? [1] : []
    content {
      sid     = "RunCommand"
      actions = ["ssm:SendCommand"]
      resources = [
        "arn:${local.partition}:ec2:${var.region}:${local.account_id}:instance/${local.lab_instance_id}",
        "arn:${local.partition}:ssm:${var.region}::document/AWS-RunShellScript",
      ]
    }
  }

  statement {
    sid       = "RunCommandResult"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }

  # ECS Exec（aws ecs execute-command）
  statement {
    sid = "EcsExec"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name_prefix}-workflow-task"
  role   = aws_iam_role.task.name
  policy = data.aws_iam_policy_document.task.json
}
