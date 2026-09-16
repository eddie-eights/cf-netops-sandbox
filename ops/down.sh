#!/usr/bin/env bash
# README の「片付け」をまとめて打つ。Terraform のルートを依存の逆順に destroy し、消え終わるまで待つ。
# state（terraform/<ルート>/terraform.tfstate）にリソースが載っているルートだけを消す。作っていないルートは飛ばす。
#
# 使い方（リポジトリの直下で。aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/down.sh              # 全部消す（graph → stream → lab → main → ecr → Runtime のロググループ）
#   KEEP_ECR=1 ops/down.sh   # ECR（イメージ）だけ残す。翌日の ops/up.sh でビルドを飛ばせる（保管料は月数円）
#
# ops/up.sh の SKIP_* / CREATE_S3_SINK は渡さなくてよい（作っていないルートは飛ばし、S3 sink の有無は state から読む）。
#
# 社内の SSL 検査がある PC では ops/up.sh と同じく AWS_CA_BUNDLE（または OPENSEARCH_CACERT_FILE）を入れてから打つ。
# terraform/main の destroy はベクトルインデックスを消すために OpenSearch Serverless のエンドポイントへ HTTPS でつなぐ。
set -uo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
OWNER=fukuda
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
tf() {  # tf <ルート> <terraform のサブコマンドと引数…>
  local root="$1"; shift
  terraform -chdir="terraform/$root" "$@"
}
has_resources() {  # has_resources <ルート>  state があり、リソースが 1 つ以上載っている（init もここで済ませる）
  [ -f "terraform/$1/terraform.tfstate" ] || return 1
  tf "$1" init -input=false >/dev/null || die "terraform/$1 の init に失敗した（provider の取得。社内 PC は README「社内 PC で使うとき」）"
  [ -n "$(tf "$1" state list 2>/dev/null)" ]
}
destroy_root() {  # destroy_root <ルート> [-var 名前=値 …]
  local root="$1"; shift
  if ! has_resources "$root"; then echo "terraform/$root: 無い（state が無いか空）"; return 0; fi
  echo "terraform/$root: 消す"
  tf "$root" destroy -input=false -auto-approve -var "owner=$OWNER" "$@" \
    || die "terraform/$root が消えなかった（上のエラー。Runtime の ENI でサブネットや SG が消えないときは最大 8 時間待って ops/down.sh を打ち直す）"
  echo "terraform/$root: 消えた"
}

log "0. 道具と認証"
command -v terraform >/dev/null || die "terraform が無い"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || die "認証が通っていない（aws-vault なら --no-session のサブシェルの中で打つ）"
echo "ACCOUNT_ID=$ACCOUNT_ID"
CACERT="${OPENSEARCH_CACERT_FILE:-${AWS_CA_BUNDLE:-}}"
MAIN_VARS=()
if [ -n "$CACERT" ]; then MAIN_VARS+=(-var "opensearch_cacert_file=$CACERT"); fi

log "1. フェーズ 2（graph → stream。stream は lab の state を読むので lab より先）"
destroy_root graph
STREAM_VARS=()
# S3 sink 無しで作った stream（CREATE_S3_SINK=0 ops/up.sh）は、既定の create_s3_sink=true のまま destroy すると zip の有無を確かめに行って止まる
if has_resources stream && ! tf stream state list 2>/dev/null | grep -Eq '\.(connect|s3_sink)\['; then
  STREAM_VARS+=(-var create_s3_sink=false)
fi
destroy_root stream ${STREAM_VARS[@]+"${STREAM_VARS[@]}"}

log "2. lab"
destroy_root lab

log "3. 本体（terraform/main。バケットは中身ごと消える）"
LOG_GROUP=""
if has_resources main; then
  LOG_GROUP=$(tf main output -raw runtime_log_group_name 2>/dev/null || true)
fi
destroy_root main ${MAIN_VARS[@]+"${MAIN_VARS[@]}"}

if [ -n "${KEEP_ECR:-}" ]; then
  log "4. ECR は残す（KEEP_ECR）"
else
  log "4. ECR（イメージごと消える）"
  destroy_root ecr
fi

log "5. Runtime のロググループ（AgentCore が作るもので Terraform の管理外）"
if [ -n "$LOG_GROUP" ]; then
  aws logs delete-log-group --region "$REGION" --log-group-name "$LOG_GROUP" 2>/dev/null && echo "$LOG_GROUP: 消した" || echo "$LOG_GROUP: 無い"
else
  echo "本体の state が無かったので名前が取れない。残っていれば: aws logs describe-log-groups --region $REGION --log-group-name-prefix /aws/bedrock-agentcore/runtimes/${PREFIX//-/_}"
fi

log "6. 残っていないか（Project=$PREFIX のタグ）"
aws resourcegroupstaggingapi get-resources --region "$REGION" --tag-filters "Key=Project,Values=$PREFIX" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d' || true
echo "（何も出なければ全部消えている。ecr を残したときはリポジトリが出る。消した直後の数分は消えたものが出ることがある）"
OLD_STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --query "StackSummaries[?starts_with(StackName, '$PREFIX') && StackStatus != 'DELETE_COMPLETE'].StackName" \
  --output text 2>/dev/null || true)
if [ -n "$OLD_STACKS" ] && [ "$OLD_STACKS" != None ]; then
  echo "CloudFormation 版のスタックも残っている: $OLD_STACKS （README「CloudFormation 版から移るとき」）"
fi
