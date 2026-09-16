#!/usr/bin/env bash
# fukuda-nwc-poc - AWS に触らずに打てる検査をまとめて打つ。README の「手元で確かめる」と同じ内容。
#   1. terraform fmt -check -recursive
#   2. 7 つのルートで init -backend=false + validate（provider を取るだけで state には触らない）
#   3. ops スクリプトの構文（bash -n と、EC2 の上で打つ ops/seed_graph.py、EMR Serverless で打つ spark/snmp_sinks.py、
#      ECS で打つ workflow/worker.py、Lambda の tools/handler.py、agent/ の mcp_client.py と proposals.py）
#   4. 模擬テスト 5 本（AWS に触れない）
# 最後の行が「すべて通過」なら健全。途中で落ちたらそこで止まる。
set -euo pipefail

cd "$(dirname "$0")/.."

ROOTS=(ecr main lab stream analytics graph workflow)

log() { printf '\n== %s\n' "$*"; }
die() {
  printf '\n!! %s\n' "$*" >&2
  exit 1
}

command -v terraform >/dev/null || die "terraform が無い（README の「Terraform を打つ PC 側」）"

log "1. terraform fmt -check -recursive terraform"
terraform fmt -check -recursive terraform || die "整形されていないファイルがある。terraform fmt -recursive terraform で直す"
echo "差分なし"

log "2. 7 つのルートの validate"
for r in "${ROOTS[@]}"; do
  terraform -chdir="terraform/$r" init -backend=false -input=false >/dev/null || die "terraform/$r の init が失敗した"
  terraform -chdir="terraform/$r" validate >/dev/null || die "terraform/$r の validate が失敗した（terraform -chdir=terraform/$r validate で中身を見る）"
  echo "terraform/$r  OK"
done

log "3. ops スクリプトの構文"
bash -n ops/up.sh ops/down.sh ops/deploy-env.sh ops/check.sh ops/vscode-setup.sh
if command -v python3 >/dev/null; then PY=(python3); else PY=(uv run --python 3.13 python); fi
for p in ops/seed_graph.py spark/snmp_sinks.py workflow/worker.py tools/handler.py agent/mcp_client.py agent/proposals.py; do
  "${PY[@]}" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])' "$p"
done
echo "構文エラーなし"

log "4. 模擬テスト"
if command -v uv >/dev/null; then
  for t in tests/test_app.py tests/test_graph.py tests/test_stream.py tests/test_analytics.py tests/test_workflow.py; do
    uv run --group dev python "$t" || die "$t が失敗した"
  done
else
  echo "uv が無いので飛ばす（README の「手元で確かめる」の通り uv sync --group dev を入れてから打つ）"
fi

printf '\nすべて通過\n'
