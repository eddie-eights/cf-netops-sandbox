#!/usr/bin/env bash
# lab の定義（lab/wvs2.clab.yml.in + lab/frr/*.conf）から作ったトポロジを Neptune に入れる。ops/up.sh の 8-2 と同じ処理を単独で打つ版。
# 設計の「静的なトポロジ構成の同期（初期 & 定期ロード）」の、定期ロードのほう。lab を変えたら打つ（cron で回してもよい）。
#
# 使い方（リポジトリの直下で。ops/up.sh と同じ deploy.env と AWS の認証情報）:
#   ops/sync-graph.sh            # Neptune が空のときだけ入れる（初期ロード。入っていれば何もしない）
#   ops/sync-graph.sh --replace  # 入っていても入れ直す（Web で編集した内容と、Spark の検知で付いた status は消えて lab の定義に戻る）
#   ops/sync-graph.sh --dry-run  # Neptune には触らず、lab から作ったトポロジ（JSON）を出すだけ
#
# terraform/main（Web の EC2）と terraform/graph（Neptune）が出来ていることが前提。Web の EC2 の上で ops/seed_graph.py を SSM Run Command で動かす。
set -uo pipefail

cd "$(dirname "$0")/.."
REPLACE=""; DRY=""
for a in "$@"; do
  case "$a" in
    --replace) REPLACE=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "使い方: ops/sync-graph.sh [--replace] [--dry-run]" >&2; exit 2 ;;
  esac
done

die() { echo "NG: $*" >&2; exit 1; }
# shellcheck source=ops/deploy-env.sh
. ops/deploy-env.sh
load_deploy_env
REGION=ap-northeast-1
if command -v python3 >/dev/null; then PY=(python3)
elif command -v uv >/dev/null; then PY=(uv run --python 3.13 python)
else echo "python3 か uv が要る（lab/lab_topology.py を動かす）" >&2; exit 1; fi

TOPO_JSON=$("${PY[@]}" lab/lab_topology.py lab) || die "lab/lab_topology.py が lab の定義を読めなかった"
if [ -n "$DRY" ]; then printf '%s\n' "$TOPO_JSON"; exit 0; fi

if [ -n "${AWS_PROFILE:-}" ]; then export AWS_PROFILE; fi
INSTANCE_ID=$(terraform -chdir=terraform/main output -raw web_instance_id 2>/dev/null) || die "terraform/main の出力 web_instance_id が読めない（apply 済みか、認証情報があるか）"
[ -n "$INSTANCE_ID" ] || die "terraform/main の出力 web_instance_id が空"

# ops/up.sh の ssm_run と同じ。コマンドは JSON の文字列に埋めるので、ダブルクォートとバックスラッシュを含めない
CMD="echo $(base64 < ops/seed_graph.py | tr -d '\n') | base64 -d | LAB_TOPOLOGY_B64=$(printf '%s' "$TOPO_JSON" | base64 | tr -d '\n') GRAPH_REPLACE=${REPLACE:-0} /usr/bin/python3.13 -"
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --timeout-seconds 900 \
  --parameters "{\"commands\":[\"$CMD\"]}" --query Command.CommandId --output text) || die "SSM Run Command を送れなかった"
while :; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in
    Success)
      aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
        --query StandardOutputContent --output text | sed '/^$/d'
      echo "Web の画面は「再読み込み」で新しいトポロジになる（チャットは 60 秒以内に読み直す）"
      exit 0 ;;
    Pending|InProgress|Delayed) sleep 10 ;;
    *) aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
         --query '[StandardOutputContent,StandardErrorContent]' --output text >&2
       die "Web の EC2 の上のコマンドが $STATUS" ;;
  esac
done
