# ---------------------------------------------------------------- MSK
# KRaft モード（var.kafka_version の末尾の .kraft）。ZooKeeper のノードは無く、メタデータは MSK が持つコントローラーに載る（追加料金なし）
resource "aws_msk_configuration" "stream" {
  name           = "${local.name_prefix}-stream"
  kafka_versions = [var.kafka_version]

  server_properties = <<-EOT
    auto.create.topics.enable=true
    default.replication.factor=2
    min.insync.replicas=1
    num.partitions=2
    log.retention.hours=24
  EOT
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/${local.name_prefix}/msk"
  retention_in_days = var.log_retention_days
}

resource "aws_msk_cluster" "stream" {
  cluster_name           = "${local.name_prefix}-stream"
  kafka_version          = var.kafka_version
  number_of_broker_nodes = 2

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = local.broker_subnet_ids
    security_groups = [local.msk_sg_id]

    storage_info {
      ebs_storage_info {
        volume_size = 10
      }
    }
  }

  client_authentication {
    unauthenticated = false

    sasl {
      iam = true
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.stream.arn
    revision = aws_msk_configuration.stream.latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }

  tags = { Name = "${local.name_prefix}-stream" }
}

# ブローカーのアドレスはクラスタ作成後にしか分からない。Telegraf のタスク（telegraf.tf）は環境変数で直接受け取るので、ここは手で調べるときと
# 手動構築のために置く（2026-09-28 までは Telegraf の EC2 が起動時にここから読んでいた）
resource "aws_ssm_parameter" "bootstrap" {
  name        = "/${local.name_prefix}/msk-bootstrap"
  type        = "String"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
  description = "MSK bootstrap brokers (SASL/IAM, 9098). The Telegraf task gets them as an environment variable; kept for manual checks."
}
