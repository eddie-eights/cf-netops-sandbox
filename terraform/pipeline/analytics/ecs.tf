# ---------------------------------------------------------------- ECS（Grafana と Splunk の共有部分）
# Grafana OSS（grafana.tf）と Splunk Enterprise（splunk.tf）を Fargate で動かすクラスタと、タスクを名前で引く Cloud Map の名前空間。
# どちらかがあるときだけ作る（local.create_ecs）。タスクはどちらも web の EC2 と同じサブネットに置き、公開 IP も LB も付けない:
#   人      → SSM のポートフォワード（AWS-StartPortForwardingSessionToRemoteHost）で web の EC2 を踏み台に grafana.<名前空間>:3000 / splunk.<名前空間>:8000
#   Spark   → splunk.<名前空間>:8088（HEC）
# タスクの IP は作り直すたびに変わるので、Cloud Map の A レコード（TTL 10 秒）で引く。
# SG は terraform/base/core の grafana と splunk（Web の EC2 から 3000 / 8000、Spark から 8088 を受ける。security_groups.tf の通信の表）。
# ECR / CloudWatch Logs / SSM の API は VPC エンドポイントを通る（ops/up.sh が足す）

resource "aws_ecs_cluster" "analytics" {
  count = local.create_ecs ? 1 : 0

  name = "${local.name_prefix}-analytics"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "analytics" {
  count = local.create_ecs ? 1 : 0

  name        = local.service_namespace
  description = "Grafana / Splunk tasks of ${local.name_prefix} (terraform/pipeline/analytics)"
  vpc         = local.vpc_id
}

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
