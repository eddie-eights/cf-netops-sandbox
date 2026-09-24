#!/usr/bin/env bash
# netops-poc - AWS に触らずに打てる検査をまとめて打つ。docs/development.md の「手元で確かめる」と同じ内容。
#   1. terraform fmt -check -recursive
#   2. 8 つのルートで init -backend=false + validate（provider を取るだけで state には触らない）
#   3. スクリプトの構文（ops/*.sh は bash -n、リポジトリの .py は全部 ast.parse）
#   4. 模擬テスト 6 本（AWS に触れない）
# 最後の行が「すべて通過」なら健全。途中で落ちたらそこで止まる。
set -euo pipefail

cd "$(dirname "$0")/.."

ROOTS=(base/ecr base/core agent pipeline/lab pipeline/stream pipeline/analytics pipeline/graph workflow)

log() { printf '\n== %s\n' "$*"; }
die() {
  printf '\n!! %s\n' "$*" >&2
  exit 1
}

command -v terraform >/dev/null || die "terraform が無い（docs/setup.md の「Terraform を打つ PC 側」）"

log "1. terraform fmt -check -recursive terraform"
terraform fmt -check -recursive terraform || die "整形されていないファイルがある。terraform fmt -recursive terraform で直す"
echo "差分なし"

log "2. 8 つのルートの validate"
for r in "${ROOTS[@]}"; do
  terraform -chdir="terraform/$r" init -backend=false -input=false >/dev/null || die "terraform/$r の init が失敗した"
  terraform -chdir="terraform/$r" validate >/dev/null || die "terraform/$r の validate が失敗した（terraform -chdir=terraform/$r validate で中身を見る）"
  echo "terraform/$r  OK"
done

log "3. ops スクリプトの構文"
bash -n ops/up.sh ops/down.sh ops/deploy-env.sh ops/check.sh
if command -v python3 >/dev/null; then PY=(python3); else PY=(uv run --python 3.13 python); fi
# .py は名指しにせず全部見る（名指しにすると、ファイルを足したときに検査から漏れる）
find agent ops spark tests tools web workflow -name '*.py' -not -path '*/__pycache__/*' -print0 |
  xargs -0 "${PY[@]}" -c 'import ast, sys
for f in sys.argv[1:]:
    ast.parse(open(f, encoding="utf-8").read(), f)'
echo "構文エラーなし"

log "4. 模擬テスト"
if command -v uv >/dev/null; then
  for t in tests/test_app.py tests/test_graph.py tests/test_stream.py tests/test_sync.py tests/test_analytics.py tests/test_workflow.py; do
    uv run --group dev python "$t" || die "$t が失敗した"
  done
else
  echo "uv が無いので飛ばす（docs/development.md の「手元で確かめる」の通り uv sync --group dev を入れてから打つ）"
fi

printf '\nすべて通過\n'
