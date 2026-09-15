# fukuda-nwc-poc - phase 2 stream root module. MSK (2 brokers, IAM auth) receives SNMP polls and traps from Telegraf on the lab EC2 (terraform/lab),
# a detector Lambda writes link_down / trap anomalies to DynamoDB, and MSK Connect keeps the raw messages in the asset bucket (S3 sink).
# The chat runtime and web read the anomaly table. Costs about 0.13 USD per hour while it exists - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケット / ロール名は terraform/main と terraform/lab の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../main/terraform.tfstate"
  }
}

data "terraform_remote_state" "lab" {
  backend = "local"

  config = {
    path = "${path.module}/../lab/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id            = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids        = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  route_table_ids   = data.terraform_remote_state.main.outputs.route_table_ids
  endpoint_sg_id    = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  bucket            = data.terraform_remote_state.main.outputs.kb_bucket_name
  reader_role_names = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])

  # lab が無いと Telegraf の送信元が無い。下の precondition で「lab を先に」と出す
  lab_sg_id     = try(data.terraform_remote_state.lab.outputs.lab_security_group_id, "")
  lab_role_name = try(data.terraform_remote_state.lab.outputs.lab_role_name, "")

  # ブローカー 2 台 = サブネット 2 つ（terraform/main の Runtime サブネット）
  broker_subnet_ids = slice(local.subnet_ids, 0, 2)

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/* と group/<name>/<uuid>/*
  topic_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":topic/")}/*"
  group_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":group/")}/*"

  bucket_arn = "arn:${local.partition}:s3:::${local.bucket}"
}

# ---------------------------------------------------------------- security groups
resource "aws_security_group" "msk" {
  name        = "${var.name_prefix}-msk"
  description = "MSK brokers - Kafka IAM (9098) from the lab EC2, from MSK Connect and from the Lambda event source mapping (same group)"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-msk" }

  lifecycle {
    precondition {
      condition     = local.lab_sg_id != "" && local.lab_role_name != ""
      error_message = "terraform/lab の state（terraform/lab/terraform.tfstate）から lab_security_group_id / lab_role_name が読めない。terraform/lab を先に apply する。"
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "msk_https" {
  security_group_id = aws_security_group.msk.id
  description       = "MSK Connect workers and the ESM ENIs - S3 gateway, logs / lambda / sts endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "msk_kafka" {
  security_group_id = aws_security_group.msk.id
  description       = "Kafka between brokers, Connect workers and ESM ENIs (all carry this group)"
  ip_protocol       = "tcp"
  from_port         = 9092
  to_port           = 9098
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "msk_self" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Brokers, MSK Connect workers and Lambda ESM ENIs talk to each other"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.msk.id
}

resource "aws_vpc_security_group_ingress_rule" "msk_from_lab" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Telegraf on the lab EC2 (Kafka IAM)"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = local.lab_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_msk" {
  security_group_id            = local.endpoint_sg_id
  description                  = "MSK Connect worker logs and Lambda ESM through the endpoints of terraform/main"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.msk.id
}

resource "aws_security_group" "stream_endpoints" {
  count = var.create_lambda_endpoints ? 1 : 0

  name        = "${var.name_prefix}-stream-endpoints"
  description = "lambda / sts interface endpoints - HTTPS from the MSK security group"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-stream-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "stream_endpoints_from_msk" {
  count = var.create_lambda_endpoints ? 1 : 0

  security_group_id            = aws_security_group.stream_endpoints[0].id
  description                  = "HTTPS from the ESM ENIs and MSK Connect workers"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.msk.id
}

# ---------------------------------------------------------------- VPC endpoints
# ESM の ENI は MSK のサブネット / SG に置かれ、Lambda と STS へ届く必要がある（NAT が無いので interface endpoint。1 AZ で足りる）
resource "aws_vpc_endpoint" "stream" {
  for_each = var.create_lambda_endpoints ? toset(["lambda", "sts"]) : toset([])

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [local.subnet_ids[0]]
  security_group_ids  = [aws_security_group.stream_endpoints[0].id]

  tags = { Name = "${var.name_prefix}-${each.key}" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = var.create_dynamodb_endpoint ? 1 : 0

  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids

  tags = { Name = "${var.name_prefix}-dynamodb" }
}

# ---------------------------------------------------------------- MSK
resource "aws_msk_configuration" "stream" {
  name           = "${var.name_prefix}-stream"
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
  name              = "/${var.name_prefix}/msk"
  retention_in_days = var.log_retention_days
}

resource "aws_msk_cluster" "stream" {
  cluster_name           = "${var.name_prefix}-stream"
  kafka_version          = var.kafka_version
  number_of_broker_nodes = 2

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = local.broker_subnet_ids
    security_groups = [aws_security_group.msk.id]

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

  tags = { Name = "${var.name_prefix}-stream" }

  depends_on = [
    aws_vpc_security_group_egress_rule.msk_kafka,
    aws_vpc_security_group_ingress_rule.msk_self,
  ]
}

# ブローカーのアドレスはクラスタ作成後にしか分からない。Telegraf（lab EC2）が起動時にここから読む
resource "aws_ssm_parameter" "bootstrap" {
  name        = "/${var.name_prefix}/msk-bootstrap"
  type        = "String"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
  description = "MSK bootstrap brokers (SASL/IAM, 9098). Read by Telegraf on the lab EC2 at start."
}

# ---------------------------------------------------------------- anomaly list
resource "aws_dynamodb_table" "anomalies" {
  name         = "${var.name_prefix}-anomalies"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "anomaly_id"

  attribute {
    name = "anomaly_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "last_seen"
    type = "N"
  }

  global_secondary_index {
    name            = "status-last_seen-index"
    projection_type = "ALL"

    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "last_seen"
      key_type       = "RANGE"
    }
  }
}

resource "aws_ssm_parameter" "anomaly_table" {
  name        = "/${var.name_prefix}/anomaly-table"
  type        = "String"
  value       = aws_dynamodb_table.anomalies.name
  description = "DynamoDB table of the anomaly list. Read by the chat runtime and the chat web (agent/anomalies.py)."
}

# ---------------------------------------------------------------- detector Lambda
resource "aws_iam_role" "detector" {
  name        = "${var.name_prefix}-detector"
  description = "fukuda-nwc-poc detector - read MSK topics through the event source mapping, write the anomaly table"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "detector_msk" {
  role       = aws_iam_role.detector.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaMSKExecutionRole"
}

resource "aws_iam_role_policy" "detector" {
  name = "detector"
  role = aws_iam_role.detector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KafkaRead"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeClusterDynamicConfiguration",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns, local.group_arns]
      },
      {
        Sid      = "Table"
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.anomalies.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "detector" {
  name              = "/aws/lambda/${var.name_prefix}-detector"
  retention_in_days = var.log_retention_days
}

# stream/detector.py をそのまま index.py として zip にする（apply のたびに中身のハッシュで差分を見る）
data "archive_file" "detector" {
  type        = "zip"
  output_path = "${path.module}/.build/detector.zip"

  source {
    content  = file("${path.module}/../../stream/detector.py")
    filename = "index.py"
  }
}

resource "aws_lambda_function" "detector" {
  function_name    = "${var.name_prefix}-detector"
  description      = "Reads the metrics / traps topics and records link_down and trap anomalies in DynamoDB (code is stream/detector.py)"
  role             = aws_iam_role.detector.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.detector.output_path
  source_code_hash = data.archive_file.detector.output_base64sha256

  environment {
    variables = {
      TABLE      = aws_dynamodb_table.anomalies.name
      DEVICE_MAP = var.device_map
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.detector,
    aws_iam_role_policy.detector,
    aws_iam_role_policy_attachment.detector_msk,
  ]
}

resource "aws_lambda_event_source_mapping" "detector" {
  for_each = {
    metrics = { batch_size = 100, window = 5 }
    traps   = { batch_size = 10, window = 1 }
  }

  function_name                      = aws_lambda_function.detector.arn
  event_source_arn                   = aws_msk_cluster.stream.arn
  topics                             = [each.key]
  starting_position                  = "LATEST"
  batch_size                         = each.value.batch_size
  maximum_batching_window_in_seconds = each.value.window

  # ESM の ENI が Lambda / STS / ブローカーへ届いてから作る（NAT が無い）
  depends_on = [
    aws_vpc_endpoint.stream,
    aws_vpc_security_group_ingress_rule.stream_endpoints_from_msk,
    aws_vpc_security_group_egress_rule.msk_https,
    aws_iam_role_policy.detector,
    aws_iam_role_policy_attachment.detector_msk,
  ]
}

# ---------------------------------------------------------------- MSK Connect S3 sink (raw messages to s3://<bucket>/stream/)
resource "aws_iam_role" "connect" {
  count = var.create_s3_sink ? 1 : 0

  name        = "${var.name_prefix}-connect"
  description = "fukuda-nwc-poc MSK Connect - read the topics, write objects under stream/ in the asset bucket"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "kafkaconnect.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:kafkaconnect:${var.region}:${local.account_id}:connector/${var.name_prefix}-s3-sink/*" }
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

  name              = "/${var.name_prefix}/connect"
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

  name         = "${var.name_prefix}-s3-sink"
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
      error_message = "s3://<kb_bucket_name of terraform/main>/<s3_sink_plugin_key> に Confluent S3 sink の zip が無い。README の s-1 で置いてから apply する（シンク無しで立てるなら -var create_s3_sink=false）。"
    }
  }
}

resource "aws_mskconnect_connector" "s3_sink" {
  count = var.create_s3_sink ? 1 : 0

  name                       = "${var.name_prefix}-s3-sink"
  description                = "metrics and traps topics to s3://<bucket>/stream/<topic>/dt=.../hour=.../ as JSON lines, rotated every minute"
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
    "topics"                         = "metrics,traps"
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

# ---------------------------------------------------------------- access for the roles of terraform/main and terraform/lab
resource "aws_iam_role_policy" "anomalies_read" {
  for_each = local.reader_role_names

  name = "${var.name_prefix}-anomalies-read"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Table"
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.anomalies.arn, "${aws_dynamodb_table.anomalies.arn}/index/*"]
      },
      {
        Sid      = "Parameters"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "stream_produce" {
  name = "${var.name_prefix}-stream-produce"
  role = local.lab_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Kafka"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:WriteData",
          "kafka-cluster:WriteDataIdempotently",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:CreateTopic",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns]
      },
      {
        Sid      = "Bootstrap"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/msk-bootstrap"
      },
    ]
  })
}
