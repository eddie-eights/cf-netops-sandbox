# ---------------------------------------------------------------- Grafana OSS（ECS on Fargate）
# Prometheus のワークスペース（メトリクス）と OpenSearch Serverless のコレクション（ログ）はどちらも AWS のマネージドだが GUI が無い
# （OpenSearch Serverless の Dashboards は VPC エンドポイントだけのコレクションには届かない）ので、Grafana OSS を 1 タスク立てて見る。
# Amazon Managed Grafana はサインインに IAM Identity Center か SAML の IdP が要り、このアカウントはどちらも無いので使わない。
# イメージは grafana/Dockerfile（公式の grafana にデータソースの plugin と provisioning を焼き込んだもの。AWS の外へ出る経路が無いので起動時に plugin を落とせない）。
# データソースは SigV4（タスクロール）で、Prometheus の API は aps-workspaces、OpenSearch は土台の aoss の VPC エンドポイントを通る。
# アラート（grafana/provisioning/alerting。Prometheus のメトリクスを見るルール）は SNS のコンタクトポイントから土台のトピック
# （terraform/base/core の alerts.tf）へ publish する。これもタスクロールの SigV4 で、sns のエンドポイントを通る。
# 開き方は output grafana_port_forward_command（web の EC2 を踏み台にした SSM のポートフォワード。PoC 用）。admin のパスワードは
# ops/up.sh が作る SSM の SecureString（output grafana_password_command）。ダッシュボードは provisioning だけで、UI で変えたものはタスクと一緒に消える

locals {
  grafana_image     = "${try(data.terraform_remote_state.ecr.outputs.grafana_repository_url, "")}:${var.grafana_image_tag}"
  grafana_log_group = "/ecs/${local.name_prefix}-grafana"
  # prometheus_endpoint は / で終わる。Grafana のデータソースの URL は末尾の / なし
  grafana_prometheus_url = local.sink_prometheus ? trimsuffix(aws_prometheus_workspace.metrics[0].prometheus_endpoint, "/") : ""
  grafana_password_arn   = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.grafana_password_parameter}"
}

resource "aws_cloudwatch_log_group" "grafana" {
  count = local.create_grafana ? 1 : 0

  name              = local.grafana_log_group
  retention_in_days = var.log_retention_days
}

resource "aws_service_discovery_service" "grafana" {
  count = local.create_grafana ? 1 : 0

  name = "grafana"
  # タスクの登録が残っていても destroy できるようにする
  force_destroy = true

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.analytics[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

resource "aws_ecs_task_definition" "grafana" {
  count = local.create_grafana ? 1 : 0

  family                   = "${local.name_prefix}-grafana"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.grafana_task_cpu
  memory                   = var.grafana_task_memory
  execution_role_arn       = aws_iam_role.grafana_execution[0].arn
  task_role_arn            = aws_iam_role.grafana_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  # readonlyRootFilesystem は付けない（Grafana は /var/lib/grafana に SQLite を、grafana/start.sh は /tmp に provisioning を書く。
  # Fargate の空のボリュームは root の持ち物で、非 root の Grafana（uid 472）が書けない）
  container_definitions = jsonencode([
    {
      name         = "grafana"
      image        = local.grafana_image
      essential    = true
      portMappings = [{ containerPort = 3000, protocol = "tcp" }]
      # grafana/start.sh が、値のあるデータソースとダッシュボードだけを provisioning に入れる
      environment = [
        { name = "AWS_REGION", value = var.region },
        { name = "PROMETHEUS_URL", value = local.grafana_prometheus_url },
        { name = "OPENSEARCH_URL", value = local.opensearch_endpoint },
        { name = "OPENSEARCH_INDEX", value = local.opensearch_index },
        # アラートの送り先（grafana/start.sh は PROMETHEUS_URL とこれがあるときだけ alerting の provisioning を入れる）
        { name = "ALERTS_TOPIC_ARN", value = local.alerts_topic_arn },
      ]
      secrets = [
        { name = "GF_SECURITY_ADMIN_PASSWORD", valueFrom = local.grafana_password_arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.grafana[0].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "grafana"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = try(data.terraform_remote_state.ecr.outputs.grafana_repository_url, "") != ""
      error_message = "terraform/base/ecr の state から grafana_repository_url が読めない。terraform/base/ecr を先に apply する（ops/up.sh の手順 1）。"
    }
    precondition {
      condition     = local.alerts_topic_arn != ""
      error_message = "terraform/base/core の state から alerts_topic_arn が読めない（2026-10-02 より前の土台）。terraform/base/core を先に apply する。"
    }
  }
}

resource "aws_ecs_service" "grafana" {
  count = local.create_grafana ? 1 : 0

  name            = "${local.name_prefix}-grafana"
  cluster         = aws_ecs_cluster.analytics[0].id
  task_definition = aws_ecs_task_definition.grafana[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # 状態を持たないので、作り直すときに古いタスクを待たない
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = [local.instance_subnet_id]
    security_groups  = [local.grafana_sg_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.grafana[0].arn
  }

  depends_on = [
    aws_iam_role_policy.grafana_task,
    aws_iam_role_policy.grafana_execution,
    aws_iam_role_policy_attachment.grafana_execution,
    aws_opensearchserverless_access_policy.grafana,
  ]
}

# ---------------------------------------------------------------- IAM
resource "aws_iam_role" "grafana_execution" {
  count = local.create_grafana ? 1 : 0

  name               = "${local.name_prefix}-grafana-exec"
  description        = "ECS task execution role of the Grafana task (ECR pull, CloudWatch Logs, admin password from SSM)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "grafana_execution" {
  count = local.create_grafana ? 1 : 0

  role       = aws_iam_role.grafana_execution[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# secrets の SecureString（AWS 管理の aws/ssm キーなので kms:Decrypt は要らない）
resource "aws_iam_role_policy" "grafana_execution" {
  count = local.create_grafana ? 1 : 0

  name = "${local.name_prefix}-grafana-exec"
  role = aws_iam_role.grafana_execution[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AdminPassword"
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = local.grafana_password_arn
    }]
  })
}

resource "aws_iam_role" "grafana_task" {
  count = local.create_grafana ? 1 : 0

  name               = "${local.name_prefix}-grafana-task"
  description        = "Grafana task - query the Prometheus workspace, read the OpenSearch logs collection and publish alerts to the SNS topic (SigV4)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "grafana_task" {
  count = local.create_grafana ? 1 : 0

  name = "${local.name_prefix}-grafana-task"
  role = aws_iam_role.grafana_task[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [for s in [{
        Sid      = "PrometheusQuery"
        Effect   = "Allow"
        Action   = ["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]
        Resource = try(aws_prometheus_workspace.metrics[0].arn, "")
      }] : s if local.sink_prometheus],
      # 読める範囲はデータアクセスポリシー（下の aws_opensearchserverless_access_policy.grafana）で絞る
      [for s in [{
        Sid      = "OpenSearchApi"
        Effect   = "Allow"
        Action   = ["aoss:APIAccessAll"]
        Resource = try(aws_opensearchserverless_collection.logs[0].arn, "")
      }] : s if local.sink_opensearch],
      # アラートのコンタクトポイント（SNS）。トピックの鍵は AWS 管理の aws/sns なので kms の許可は要らない
      [for s in [{
        Sid      = "PublishAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = local.alerts_topic_arn
      }] : s if local.alerts_topic_arn != ""],
    )
  })
}

resource "aws_opensearchserverless_access_policy" "grafana" {
  count = local.create_grafana && local.sink_opensearch ? 1 : 0

  name        = "${local.name_prefix}-grafana"
  type        = "data"
  description = "Grafana task reads the snmp-logs index"

  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.logs_collection}"]
        Permission   = ["aoss:DescribeCollectionItems"]
      },
      {
        ResourceType = "index"
        Resource     = ["index/${local.logs_collection}/${local.opensearch_index}*"]
        Permission   = ["aoss:DescribeIndex", "aoss:ReadDocument"]
      },
    ]
    Principal = [aws_iam_role.grafana_task[0].arn]
  }])
}

# terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない呼び出しを拒む）。実行ロールとタスクロールの両方に付ける
resource "aws_iam_role_policy_attachment" "grafana_execution_perimeter" {
  count = local.create_grafana && local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.grafana_execution[0].name
  policy_arn = local.perimeter_policy_arn
}

resource "aws_iam_role_policy_attachment" "grafana_task_perimeter" {
  count = local.create_grafana && local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.grafana_task[0].name
  policy_arn = local.perimeter_policy_arn
}
