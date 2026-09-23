# ---------------------------------------------------------------- S3 Tables (Iceberg)
# テーブルバケット名はハイフン可、namespace とテーブル名はアンダースコアだけ（S3 Tables の命名規則、2026-09-17 確認）。
# テーブルは Terraform で作る（terraform destroy でバケットまで消せるように。テーブルが残るとバケットは消えない）。
# 列は Telegraf の JSON（{"fields":{…},"name":"…","tags":{…},"timestamp":秒}）をそのまま持つ。
# tags と fields は JSON 文字列のまま入れる（機器やメトリクスが増えても列を変えないため。列の型は Iceberg のプリミティブだけ）
# テーブルバケットと namespace はいつも作る（証跡の anomaly_events / proposal_events が入る。2026-09-24）。
# 生データの snmp_metrics だけは var.sinks に iceberg があるときだけ作る（deploy.env の SINK_S3。格納先は 1 つずつ 1 / 0 で選べる）
resource "aws_s3tables_table_bucket" "tables" {
  name = local.table_bucket
}

resource "aws_s3tables_namespace" "netops" {
  namespace        = var.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables.arn
}

# 2026-09-24 まではバケットと namespace も iceberg のときだけ（count）だった。state の [0] をそのまま引き継ぐ
moved {
  from = aws_s3tables_table_bucket.tables[0]
  to   = aws_s3tables_table_bucket.tables
}

moved {
  from = aws_s3tables_namespace.netops[0]
  to   = aws_s3tables_namespace.netops
}

resource "aws_s3tables_table" "snmp_metrics" {
  count = local.sink_iceberg ? 1 : 0

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

# ---------------------------------------------------------------- 証跡（2026-09-24）
# 異常が開いた・閉じたの履歴。書くのは Spark の detect（spark/snmp_sinks.py の ANOMALY_EVENT_COLUMNS と同じ列・同じ順）。
# 追記だけで、1 回の発生（occurrence_id = <anomaly_id>#<first_seen>）に opened と resolved の 2 行。
# 書き込みは少なくとも 1 回（落ちて読み直すと同じ行がもう 1 度入る）なので、数えるときは event_id で重複を落とす。
# 列はどれも required = false（Spark の DataFrame の列は nullable で、required の列には書けない）。
# 時刻は timestamptz（Spark の TimestampType と PyArrow の tz 付き timestamp がそのまま入る型）
resource "aws_s3tables_table" "anomaly_events" {
  name             = "anomaly_events"
  namespace        = aws_s3tables_namespace.netops.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables.arn
  format           = "ICEBERG"

  metadata {
    iceberg {
      schema {
        field {
          name     = "event_id"
          type     = "string"
          required = false
        }
        field {
          name     = "anomaly_id"
          type     = "string"
          required = false
        }
        field {
          name     = "occurrence_id"
          type     = "string"
          required = false
        }
        field {
          name     = "event"
          type     = "string"
          required = false
        }
        field {
          name     = "device_id"
          type     = "string"
          required = false
        }
        field {
          name     = "kind"
          type     = "string"
          required = false
        }
        field {
          name     = "target"
          type     = "string"
          required = false
        }
        field {
          name     = "source"
          type     = "string"
          required = false
        }
        field {
          name     = "detail"
          type     = "string"
          required = false
        }
        field {
          name     = "first_seen"
          type     = "timestamptz"
          required = false
        }
        field {
          name     = "resolved_at"
          type     = "timestamptz"
          required = false
        }
        field {
          name     = "event_time"
          type     = "timestamptz"
          required = false
        }
      }
    }
  }
}

# 修復案の作成・承認・却下・適用・確認の履歴。書くのは terraform/workflow の worker（workflow/awsio.py の append_proposal_events、PyIceberg）。
# event は created / approved / rejected / expired / obsolete / applied / failed / verified。event_id = <proposal_id>#<event>。
# 列は workflow/rules.py の PROPOSAL_EVENT_COLUMNS と同じ順・同じ型。action / cause はエージェントの答え、decided_by は承認・却下した人。
# 修復案の「いま」は Neptune の proposal の頂点（Web の承認タブが読み書きする）で、ここは証跡
resource "aws_s3tables_table" "proposal_events" {
  name             = "proposal_events"
  namespace        = aws_s3tables_namespace.netops.namespace
  table_bucket_arn = aws_s3tables_table_bucket.tables.arn
  format           = "ICEBERG"

  metadata {
    iceberg {
      schema {
        field {
          name     = "event_id"
          type     = "string"
          required = false
        }
        field {
          name     = "proposal_id"
          type     = "string"
          required = false
        }
        field {
          name     = "anomaly_id"
          type     = "string"
          required = false
        }
        field {
          name     = "event"
          type     = "string"
          required = false
        }
        field {
          name     = "status"
          type     = "string"
          required = false
        }
        field {
          name     = "device_id"
          type     = "string"
          required = false
        }
        field {
          name     = "action"
          type     = "string"
          required = false
        }
        field {
          name     = "cause"
          type     = "string"
          required = false
        }
        field {
          name     = "command"
          type     = "string"
          required = false
        }
        field {
          name     = "decided_by"
          type     = "string"
          required = false
        }
        field {
          name     = "detail"
          type     = "string"
          required = false
        }
        field {
          name     = "event_time"
          type     = "timestamptz"
          required = false
        }
      }
    }
  }
}
