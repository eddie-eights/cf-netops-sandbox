# ---------------------------------------------------------------- EMR Serverless (Spark)
# アプリケーションは器だけで、ジョブが動いていなければ課金されない（pre-initialized capacity は持たない）。
# ストリーミングのジョブは ops/up.sh が start-job-run で起こす（Terraform にジョブのリソースは無い。手打ちは output の job_driver_json / configuration_overrides_json）。
# arm64 なのは lab / web の EC2 と同じ理由（単価が x86 より約 20% 低い）
resource "aws_emrserverless_application" "spark" {
  name          = "${local.name_prefix}-spark"
  release_label = var.emr_release_label
  type          = "spark"
  architecture  = "ARM64"

  auto_start_configuration {
    enabled = true
  }

  auto_stop_configuration {
    enabled              = true
    idle_timeout_minutes = var.idle_timeout_minutes
  }

  maximum_capacity {
    cpu    = var.max_cpu
    memory = var.max_memory
  }

  # SG は terraform/base/core の internal（network.tf）
  network_configuration {
    subnet_ids         = slice(local.subnet_ids, 0, 2)
    security_group_ids = [local.internal_sg_id]
  }

  # scheduler_configuration（max_concurrent_runs / queue_timeout_minutes）は書かない。API が既定値（15 / 360）を返すので、
  # 書かないと provider（aws 6.64）が毎回 plan に差分を出し、書くと queue_timeout_minutes がアプリ STARTED 中は更新できず apply が 400 で落ちる
  # （2026-09-17 に Mac で両方実測。ジョブが動いていると stop-application もできない）。ignore_changes で差分そのものを見ないようにする
  lifecycle {
    ignore_changes = [scheduler_configuration]

    precondition {
      condition     = local.msk_cluster_arn != "" && local.bootstrap != ""
      error_message = "terraform/pipeline/stream の state（terraform/pipeline/stream/terraform.tfstate）から msk_cluster_arn / bootstrap_brokers が読めない。terraform/pipeline/stream を先に apply する。"
    }
    precondition {
      condition     = local.neptune_host != "" && local.neptune_resource_id != ""
      error_message = "terraform/pipeline/graph の state（terraform/pipeline/graph/terraform.tfstate）から cluster_endpoint / cluster_resource_id が読めない。検知は異常の「いま」を Neptune に書くので、terraform/pipeline/graph を先に apply する（2026-09-24 から）。"
    }
    precondition {
      condition     = !local.sink_splunk || var.splunk_hec_url != ""
      error_message = "sinks に splunk があるのに splunk_hec_url が空。HEC の URL（https://<host>:8088）を渡す（ops/up.sh なら deploy.env の SPLUNK_HEC_URL）。"
    }
  }

  tags = { Name = "${local.name_prefix}-spark" }
}

# ジョブのドライバーのログ。start-job-run の monitoringConfiguration で指す（output の configuration_overrides_json）
resource "aws_cloudwatch_log_group" "emr" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}
