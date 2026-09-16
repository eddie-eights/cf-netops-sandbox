# ---------------------------------------------------------------- S3 Tables (Iceberg)
# テーブルバケット名はハイフン可、namespace とテーブル名はアンダースコアだけ（S3 Tables の命名規則、2026-09-17 確認）。
# テーブルは Terraform で作る（terraform destroy でバケットまで消せるように。テーブルが残るとバケットは消えない）。
# 列は Telegraf の JSON（{"fields":{…},"name":"…","tags":{…},"timestamp":秒}）をそのまま持つ。
# tags と fields は JSON 文字列のまま入れる（機器やメトリクスが増えても列を変えないため。列の型は Iceberg のプリミティブだけ）
resource "aws_s3tables_table_bucket" "tables" {
  name = local.table_bucket
}

resource "aws_s3tables_namespace" "netops" {
  namespace        = var.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables.arn
}

resource "aws_s3tables_table" "snmp_metrics" {
  name             = var.table_name
  namespace        = aws_s3tables_namespace.netops.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables.arn
  format           = "ICEBERG"

  metadata {
    iceberg {
      schema {
        field {
          name     = "ts"
          type     = "timestamp"
          required = true
        }
        field {
          name     = "topic"
          type     = "string"
          required = true
        }
        field {
          name     = "measurement"
          type     = "string"
          required = false
        }
        field {
          name     = "agent_host"
          type     = "string"
          required = false
        }
        field {
          name     = "host"
          type     = "string"
          required = false
        }
        field {
          name     = "tags_json"
          type     = "string"
          required = false
        }
        field {
          name     = "fields_json"
          type     = "string"
          required = false
        }
        field {
          name     = "ingested_at"
          type     = "timestamp"
          required = true
        }
      }
    }
  }
}
