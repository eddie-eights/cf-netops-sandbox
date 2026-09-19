# ---------------------------------------------------------------- MSK Connect S3 sink (raw messages to s3://<bucket>/stream/)
resource "aws_iam_role" "connect" {
  count = var.create_s3_sink ? 1 : 0

  name        = "${local.name_prefix}-connect"
  description = "${local.name_prefix} MSK Connect - read the topics, write objects under stream/ in the asset bucket"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "kafkaconnect.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:kafkaconnect:${var.region}:${local.account_id}:connector/${local.name_prefix}-s3-sink/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "connect" {
  count = var.create_s3_sink ? 1 : 0

  name = "connect"
  role = aws_iam_role.connect[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Kafka"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:WriteData",
          "kafka-cluster:CreateTopic",
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeGroup",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns, local.group_arns]
      },
      {
        Sid      = "Bucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
        Resource = local.bucket_arn
      },
      {
        Sid      = "Objects"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.bucket_arn}/stream/*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "connect" {
  count = var.create_s3_sink ? 1 : 0

  name              = "/${local.name_prefix}/connect"
  retention_in_days = var.log_retention_days
}

# plan の時点で zip が置いてあるかを見る（無いまま進むと MSK を作り始めてからプラグインで失敗する）
data "aws_s3_objects" "plugin" {
  count = var.create_s3_sink ? 1 : 0

  bucket   = local.bucket
  prefix   = var.s3_sink_plugin_key
  max_keys = 10
}

resource "aws_mskconnect_custom_plugin" "s3_sink" {
  count = var.create_s3_sink ? 1 : 0

  name         = "${local.name_prefix}-s3-sink"
  description  = "Confluent S3 sink connector (zip from Confluent Hub, uploaded to the asset bucket)"
  content_type = "ZIP"

  location {
    s3 {
      bucket_arn = local.bucket_arn
      file_key   = var.s3_sink_plugin_key
    }
  }

  lifecycle {
    precondition {
      condition     = contains(data.aws_s3_objects.plugin[0].keys, var.s3_sink_plugin_key)
      error_message = "s3://<kb_bucket_name of terraform/base/core>/<s3_sink_plugin_key> に Confluent S3 sink の zip が無い。ops/up.sh の手順 5 で置いてから apply する（シンク無しで立てるなら -var create_s3_sink=false）。"
    }
  }
}

resource "aws_mskconnect_connector" "s3_sink" {
  count = var.create_s3_sink ? 1 : 0

  name                       = "${local.name_prefix}-s3-sink"
  description                = "metrics, traps and logs topics to s3://<bucket>/stream/<topic>/dt=.../hour=.../ as JSON lines, rotated every minute"
  kafkaconnect_version       = var.kafka_connect_version
  service_execution_role_arn = aws_iam_role.connect[0].arn

  capacity {
    provisioned_capacity {
      mcu_count    = 1
      worker_count = 1
    }
  }

  kafka_cluster {
    apache_kafka_cluster {
      bootstrap_servers = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam

      vpc {
        security_groups = [aws_security_group.msk.id]
        subnets         = local.broker_subnet_ids
      }
    }
  }

  kafka_cluster_client_authentication {
    authentication_type = "IAM"
  }

  kafka_cluster_encryption_in_transit {
    encryption_type = "TLS"
  }

  plugin {
    custom_plugin {
      arn      = aws_mskconnect_custom_plugin.s3_sink[0].arn
      revision = aws_mskconnect_custom_plugin.s3_sink[0].latest_revision
    }
  }

  log_delivery {
    worker_log_delivery {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.connect[0].name
      }
    }
  }

  connector_configuration = {
    "connector.class"                = "io.confluent.connect.s3.S3SinkConnector"
    "tasks.max"                      = "1"
    "topics"                         = "metrics,traps,logs"
    "s3.region"                      = var.region
    "s3.bucket.name"                 = local.bucket
    "topics.dir"                     = "stream"
    "flush.size"                     = "1000"
    "rotate.schedule.interval.ms"    = "60000"
    "storage.class"                  = "io.confluent.connect.s3.storage.S3Storage"
    "format.class"                   = "io.confluent.connect.s3.format.json.JsonFormat"
    "partitioner.class"              = "io.confluent.connect.storage.partitioner.TimeBasedPartitioner"
    "path.format"                    = "'dt'=YYYY-MM-dd/'hour'=HH"
    "partition.duration.ms"          = "3600000"
    "locale"                         = "ja_JP"
    "timezone"                       = "Asia/Tokyo"
    "timestamp.extractor"            = "Record"
    "key.converter"                  = "org.apache.kafka.connect.storage.StringConverter"
    "value.converter"                = "org.apache.kafka.connect.json.JsonConverter"
    "value.converter.schemas.enable" = "false"
    "schema.compatibility"           = "NONE"
  }

  # MSK Connect の作成は 10〜20 分かかることがある（provider の既定は 20 分）
  timeouts {
    create = "40m"
    update = "40m"
    delete = "40m"
  }

  depends_on = [
    aws_iam_role_policy.connect,
    aws_vpc_security_group_egress_rule.msk_https,
    aws_vpc_security_group_ingress_rule.endpoints_from_msk,
  ]
}
