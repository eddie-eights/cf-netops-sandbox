#!/usr/bin/env bash
# README の「片付け」をまとめて打つ。Terraform のルートを依存の逆順に destroy し、消え終わるまで待つ。
# state（terraform/<ルート>/terraform.tfstate）にリソースが載っているルートだけを消す。作っていないルートは飛ばす。
#
# 使い方（リポジトリの直下で。aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/down.sh              # 全部消す（analytics → graph → stream → lab → main → ecr → Runtime のロググループ）。KEEP_ECR=0 と同じ
#   KEEP_ECR=1 ops/down.sh   # ECR（イメージ）だけ残す。翌日の ops/up.sh でビルドを飛ばせる（保管料は月数円）
#
# ops/up.sh と同じ deploy.env（DEPLOY_ENV_FILE=<パス> で別のファイル）を読む。環境変数はファイルより優先。
# ここで使うキー（任意）:
#   KEEP_ECR   ECR を残すか。1 = 残す、0 = 消す（既定）。それ以外の値は何も消さずに止まる
#   AWS_PROFILE / AWS_CA_BUNDLE / OPENSEARCH_CACERT_FILE  ops/up.sh と同じ
#
# PHASE / SKIP_* / CREATE_S3_SINK は見ない。PHASE に関係なく、state にリソースが載っているルートを全部消す
# （作っていないルートは飛ばし、S3 sink の有無は state から読む）。analytics は Spark のジョブを止めてから消す。
#
# 社内の SSL 検査がある PC では ops/up.sh と同じく AWS_CA_BUNDLE（または OPENSEARCH_CACERT_FILE）を入れてから打つ。
# terraform/main の destroy はベクトルインデックスを消すために OpenSearch Serverless のエンドポイントへ HTTPS でつなぐ。
set -uo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
OWNER=fukuda
. "$(dirname "$0")/deploy-env.sh"
resolve_deploy_env_file  # DEPLOY_ENV_FILE の相対パスは、下の cd の前の場所から見る
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
# terraform に渡す認証情報。terraform/main の opensearch provider は古い AWS SDK（Go v1）で、`aws login` で入ったプロファイル
# （login_session）を読めずに NoCredentialProviders で落ちる。鍵が環境変数に無い（= プロファイルから読む）ときは、AWS CLI から
# 資格情報を受け取る credential_process だけのプロファイルを一時ファイルに書き、terraform にはそちらを読ませる
# （AWS CLI ユーザーガイド「Sharing Login credentials as process credentials」の形。15 分ごとの更新は CLI が続ける）
TF_AWS_CONFIG=""
TF_AWS_ENV=()
tf_use_cli_credentials() {
  if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then  # aws-vault など、鍵が環境変数にあるときはそのまま渡す
    echo "terraform の認証情報: 環境変数の鍵"
    return 0
  fi
  local profile_opt=""
  if [ -n "${AWS_PROFILE:-}" ]; then profile_opt="--profile $(printf '%q' "$AWS_PROFILE")"; fi
  TF_AWS_CONFIG=$(mktemp "${TMPDIR:-/tmp}/$PREFIX-aws-config.XXXXXX") || die "一時ファイルを作れない（TMPDIR）"
  trap 'rm -f "$TF_AWS_CONFIG"' EXIT
  printf '[profile %s-terraform]\ncredential_process = env -u AWS_PROFILE AWS_CONFIG_FILE=%q aws configure export-credentials %s --format process\n' \
    "$PREFIX" "${AWS_CONFIG_FILE:-$HOME/.aws/config}" "$profile_opt" >"$TF_AWS_CONFIG"
  TF_AWS_ENV=(AWS_CONFIG_FILE="$TF_AWS_CONFIG" AWS_PROFILE="$PREFIX-terraform")
  echo "terraform の認証情報: プロファイル ${AWS_PROFILE:-（既定）} を AWS CLI 経由（credential_process）で渡す"
}
tf() {  # tf <ルート> <terraform のサブコマンドと引数…>
  local root="$1"; shift
  env ${TF_AWS_ENV[@]+"${TF_AWS_ENV[@]}"} terraform -chdir="terraform/$root" "$@"
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

log "0. 設定と道具と認証"
load_deploy_env
KEEP_ECR="${KEEP_ECR:-0}"
case "$KEEP_ECR" in
  0) echo "KEEP_ECR=0: ECR もイメージごと消す（残すなら KEEP_ECR=1）" ;;
  1) echo "KEEP_ECR=1: ECR（イメージ）は残す" ;;
  *) die "KEEP_ECR は 1（ECR を残す）か 0（消す。既定）（いまは「$KEEP_ECR」）。まだ何も消していない" ;;
esac
command -v aws >/dev/null || die "aws CLI が無い"
command -v terraform >/dev/null || die "terraform が無い"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || die "認証が通っていない（aws-vault なら --no-session のサブシェルの中で打つ。aws login なら打ち直す）"
echo "ACCOUNT_ID=$ACCOUNT_ID"
tf_use_cli_credentials
CACERT="${OPENSEARCH_CACERT_FILE:-${AWS_CA_BUNDLE:-}}"
MAIN_VARS=()
if [ -n "$CACERT" ]; then MAIN_VARS+=(-var "opensearch_cacert_file=$CACERT"); fi

log "1. analytics → graph → stream（analytics は stream の Kafka を読み、stream は lab の state を読むので、この順）"
# EMR Serverless のアプリケーションは、ジョブが動いているか STARTED のままだと destroy が落ちる。先にジョブを止め、アプリケーションを止める
if has_resources analytics; then
  APP_ID=$(tf analytics output -raw application_id 2>/dev/null || true)
  if [ -n "$APP_ID" ]; then
    RUNNING=$(aws emr-serverless list-job-runs --region "$REGION" --application-id "$APP_ID" \
      --states SUBMITTED PENDING SCHEDULED RUNNING --query 'jobRuns[].id' --output text 2>/dev/null || true)
    if [ -n "$RUNNING" ] && [ "$RUNNING" != None ]; then
      for id in $RUNNING; do
        echo "Spark のジョブ $id を止める"
        aws emr-serverless cancel-job-run --region "$REGION" --application-id "$APP_ID" --job-run-id "$id" >/dev/null || true
      done
      for i in $(seq 1 24); do  # 止まるまで最大 2 分
        LEFT=$(aws emr-serverless list-job-runs --region "$REGION" --application-id "$APP_ID" \
          --states SUBMITTED PENDING SCHEDULED RUNNING CANCELLING --query 'jobRuns[].id' --output text 2>/dev/null || true)
        if [ -z "$LEFT" ] || [ "$LEFT" = None ]; then break; fi
        sleep 5
      done
    fi
    aws emr-serverless stop-application --region "$REGION" --application-id "$APP_ID" >/dev/null 2>&1 || true
    for i in $(seq 1 24); do  # STOPPED になるまで最大 2 分
      STATE=$(aws emr-serverless get-application --region "$REGION" --application-id "$APP_ID" --query application.state --output text 2>/dev/null || echo "")
      case "$STATE" in STOPPED|CREATED|"") break ;; esac
      sleep 5
    done
  fi
fi
destroy_root analytics
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

if [ "$KEEP_ECR" = 1 ]; then
  log "4. ECR は残す（KEEP_ECR=1）"
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
