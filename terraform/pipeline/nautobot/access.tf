# ---------------------------------------------------------------- IAM
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

# 実行ロール: イメージの pull、ログ、secrets の SecureString（AWS 管理の aws/ssm キーなので kms:Decrypt は要らない）
resource "aws_iam_role" "exec" {
  name               = "${local.name_prefix}-nautobot-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "exec_secrets" {
  name = "secrets"
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameters"
      Resource = values(local.secret_arns)
    }]
  })
}

# タスクロール: Job が呼ぶ AWS の API だけ。dialin の一覧の 2 つのパラメータの読み書き、dialin のサービスの作り直し、Neptune の読み書き
resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-nautobot-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "task" {
  name = "sync"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    # 相手のルート（stream / graph）が無いときは、その分の statement を出さない
    Statement = [for s in [
      {
        on       = true
        Sid      = "EcsExec"
        Effect   = "Allow"
        Action   = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]
        Resource = ["*"]
      },
      {
        on       = local.dialin_from_nautobot
        Sid      = "DialinTargets"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = [for name in values(local.dialin_parameters) : "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${name}"]
      },
      {
        on       = local.dialin_from_nautobot
        Sid      = "DialinRedeploy"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService"]
        Resource = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:service/${local.telegraf_cluster}/${local.telegraf_service}"]
      },
      {
        on       = local.neptune_resource_id != ""
        Sid      = "NeptuneGremlin"
        Effect   = "Allow"
        Action   = ["neptune-db:ReadDataViaQuery", "neptune-db:WriteDataViaQuery", "neptune-db:DeleteDataViaQuery", "neptune-db:GetEngineStatus", "neptune-db:GetQueryStatus", "neptune-db:CancelQuery"]
        Resource = ["arn:${local.partition}:neptune-db:${var.region}:${local.account_id}:${local.neptune_resource_id}/*"]
      },
    ] : { Sid = s.Sid, Effect = s.Effect, Action = s.Action, Resource = s.Resource } if s.on]
  })
}

# VPC エンドポイントを通らない AWS の API を拒む（terraform/base/core の perimeter.tf）。NETWORK_PERIMETER=0 なら付けない
resource "aws_iam_role_policy_attachment" "exec_perimeter" {
  count = local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.exec.name
  policy_arn = local.perimeter_policy_arn
}

resource "aws_iam_role_policy_attachment" "task_perimeter" {
  count = local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.task.name
  policy_arn = local.perimeter_policy_arn
}
