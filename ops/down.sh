#!/usr/bin/env bash
# README の「片付け」をまとめて打つ。スタックを依存の逆順に消し、消え終わるまで待つ。
#
# 使い方（aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/down.sh              # 全部消す（graph → stream → lab → 本体 → ecr → build → Runtime のロググループ）
#   KEEP_ECR=1 ops/down.sh   # ECR（イメージ）だけ残す。翌日の ops/up.sh でビルドを飛ばせる（保管料は月数円）
set -uo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
status() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
}
delete_and_wait() {  # delete_and_wait <スタック名…>  同時に消してよいものをまとめて渡す
  local s
  for s in "$@"; do
    if [ "$(status "$s")" = NONE ]; then echo "$s: 無い"; continue; fi
    echo "$s: 消す"
    aws cloudformation delete-stack --region "$REGION" --stack-name "$s"
  done
  for s in "$@"; do
    [ "$(status "$s")" = NONE ] && continue
    if ! aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$s"; then
      aws cloudformation describe-stack-events --region "$REGION" --stack-name "$s" \
        --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output text >&2
      die "$s が消えなかった（上の理由。Runtime の ENI なら最大 8 時間待って打ち直す）"
    fi
    echo "$s: 消えた"
  done
}
empty_bucket() {
  if aws s3api head-bucket --bucket "$1" 2>/dev/null; then
    echo "s3://$1 を空にする"; aws s3 rm "s3://$1" --recursive --only-show-errors
  fi
}

log "0. 認証"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || die "認証が通っていない"
echo "ACCOUNT_ID=$ACCOUNT_ID"
LOG_GROUP=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$PREFIX" \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeLogGroupName'].OutputValue" --output text 2>/dev/null || true)

log "1. フェーズ 2（graph / stream）"
delete_and_wait "$PREFIX-graph" "$PREFIX-stream"

log "2. lab"
delete_and_wait "$PREFIX-lab"

log "3. 本体"
empty_bucket "$PREFIX-kb-$ACCOUNT_ID"
delete_and_wait "$PREFIX"

if [ -n "${KEEP_ECR:-}" ]; then
  log "4. ECR は残す（KEEP_ECR）"
else
  log "4. ECR"
  delete_and_wait "$PREFIX-ecr"
fi

log "5. build（作っていれば）"
empty_bucket "$PREFIX-build-$ACCOUNT_ID"
delete_and_wait "$PREFIX-build"

log "6. Runtime のロググループ"
if [ -n "$LOG_GROUP" ] && [ "$LOG_GROUP" != None ]; then
  aws logs delete-log-group --region "$REGION" --log-group-name "$LOG_GROUP" 2>/dev/null && echo "$LOG_GROUP: 消した" || echo "$LOG_GROUP: 無い"
else
  echo "本体が無かったので名前が取れない。残っていれば: aws logs describe-log-groups --region $REGION --log-group-name-prefix /aws/bedrock-agentcore/runtimes/${PREFIX//-/_}"
fi

log "7. 残っていないか（Project=$PREFIX のタグ）"
aws resourcegroupstaggingapi get-resources --region "$REGION" --tag-filters "Key=Project,Values=$PREFIX" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d' || true
echo "（何も出なければ全部消えている。ecr を残したときはリポジトリ 4 つが出る）"
