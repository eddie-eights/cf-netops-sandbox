# ---------------------------------------------------------------- S3 Tables (Iceberg)
# テーブルバケット名はハイフン可、namespace とテーブル名はアンダースコアだけ（S3 Tables の命名規則、2026-09-17 確認）。
# テーブルは Terraform で作る（terraform destroy でバケットまで消せるように。テーブルが残るとバケットは消えない）。
# 列は Telegraf の JSON（{"fields":{…},"name":"…","tags":{…},"timestamp":秒}）をそのまま持つ。
# tags と fields は JSON 文字列のまま入れる（機器やメトリクスが増えても列を変えないため。列の型は Iceberg のプリミティブだけ）
# var.sinks に iceberg があるときだけ作る（deploy.env の SINK_S3。格納先は 1 つずつ 1 / 0 で選べる）
resource "aws_s3tables_table_bucket" "tables" {
  count = local.sink_iceberg ? 1 : 0

  name = local.table_bucket
}

resource "aws_s3tables_namespace" "netops" {
  count = local.sink_iceberg ? 1 : 0

  namespace        = var.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables[0].arn
}

resource "aws_s3tables_table" "snmp_metrics" {
  count = local.sink_iceberg ? 1 : 0

  name             = var.table_name
  namespace        = aws_s3tables_namespace.netops[0].namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables[0].arn
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
