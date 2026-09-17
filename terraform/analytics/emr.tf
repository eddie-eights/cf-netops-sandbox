# ---------------------------------------------------------------- EMR Serverless (Spark)
# アプリケーションは器だけで、ジョブが動いていなければ課金されない（pre-initialized capacity は持たない）。
# ストリーミングのジョブは ops/up.sh が start-job-run で起こす（Terraform にジョブのリソースは無い。手打ちは output の job_driver_json / configuration_overrides_json。README の a-3）。
# arm64 なのは lab / web の EC2 と同じ理由（単価が x86 より約 20% 低い。README「1 時間起動したときの試算」）
resource "aws_emrserverless_application" "spark" {
  name          = "${var.name_prefix}-spark"
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

  network_configuration {
    subnet_ids         = slice(local.subnet_ids, 0, 2)
    security_group_ids = [aws_security_group.emr.id]
  }

  # scheduler_configuration（max_concurrent_runs / queue_timeout_minutes）は書かない。API が既定値（15 / 360）を返すので、
  # 書かないと provider（aws 6.64）が毎回 plan に差分を出し、書くと queue_timeout_minutes がアプリ STARTED 中は更新できず apply が 400 で落ちる
  # （2026-09-17 に Mac で両方実測。ジョブが動いていると stop-application もできない）。ignore_changes で差分そのものを見ないようにする
  lifecycle {
    ignore_changes = [scheduler_configuration]
  }

  tags = { Name = "${var.name_prefix}-spark" }
}

# ジョブのドライバーのログ。start-job-run の monitoringConfiguration で指す（output の configuration_overrides_json）
resource "aws_cloudwatch_log_group" "emr" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}
