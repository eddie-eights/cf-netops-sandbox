# ---------------------------------------------------------------- anomaly list
resource "aws_dynamodb_table" "anomalies" {
  name         = "${local.name_prefix}-anomalies"
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
  name        = "/${local.name_prefix}/anomaly-table"
  type        = "String"
  value       = aws_dynamodb_table.anomalies.name
  description = "DynamoDB table of the anomaly list. Read by the chat runtime and the chat web (agent/anomalies.py)."
}
