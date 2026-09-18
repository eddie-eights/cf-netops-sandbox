#!/usr/bin/env bash
# docs/deploy-manual.md の「片付け」をまとめて打つ。Terraform のルートを依存の逆順に destroy し、消え終わるまで待つ。
# state（terraform/<ルート>/terraform.tfstate）にリソースが載っているルートだけを消す。作っていないルートは飛ばす。
#
# 使い方（リポジトリの直下で。先に AWS CLI の認証を通しておく。IAM ユーザーなら長期キーのまま打つ）:
#   ops/down.sh              # 全部消す（workflow → analytics → graph → stream → lab → agent → main → ecr → Runtime のロググループ）。KEEP_ECR=0 と同じ
#   KEEP_ECR=1 ops/down.sh   # ECR（イメージ）だけ残す。翌日の ops/up.sh でビルドを飛ばせる（保管料は月数円）
#
# ops/up.sh と同じ deploy.env（DEPLOY_ENV_FILE=<パス> で別のファイル）を読む。環境変数はファイルより優先。
# ここで使うキー（任意）:
#   KEEP_ECR   ECR を残すか。1 = 残す、0 = 消す（既定）。それ以外の値は何も消さずに止まる
#   AWS_PROFILE / AWS_CA_BUNDLE / OPENSEARCH_CACERT_FILE  ops/up.sh と同じ
#
# PIPELINE / AGENT / WORKFLOW / SKIP_* / CREATE_S3_SINK / CREATE_KB は見ない。機能の設定に関係なく、state にリソースが載っているルートを全部消す
# （作っていないルートは飛ばし、S3 sink の有無は state から読む）。analytics は Spark のジョブを止めてから消す。
#
# 社内の SSL 検査がある PC では ops/up.sh と同じく AWS_CA_BUNDLE（または OPENSEARCH_CACERT_FILE）を入れてから打つ。
# terraform/agent の destroy は（KB を作っていたとき）ベクトルインデックスを消すために OpenSearch Serverless のエンドポイントへ HTTPS でつなぐ。
set -uo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
OWNER=fukuda
. "$(dirname "$0")/deploy-env.sh"
resolve_deploy_env_file  # DEPLOY_ENV_FILE の相対パスは、下の cd の前の場所から見る
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
# terraform に渡す認証情報。terraform/agent の opensearch provider は古い AWS SDK（Go v1）で、`aws login` で入ったプロファイル
# （login_session）を読めずに NoCredentialProviders で落ちる。鍵が環境変数に無い（= プロファイルから読む）ときは、AWS CLI から
# 資格情報を受け取る credential_process だけのプロファイルを一時ファイルに書き、terraform にはそちらを読ませる
# （AWS CLI ユーザーガイド「Sharing Login credentials as process credentials」の形。15 分ごとの更新は CLI が続ける）
TF_AWS_CONFIG=""
TF_AWS_ENV=()
tf_use_cli_credentials() {
  if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then  # 鍵が環境変数にあるときはそのまま渡す
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
  tf "$1" init -input=false >/dev/null || die "terraform/$1 の init に失敗した（provider の取得。社内 PC は docs/setup.md「社内 PC で使うとき」）"
  [ -n "$(tf "$1" state list 2>/dev/null)" ]
}
destroy_root() {  # destroy_root <ルート> [-var 名前=値 …]
  local root="$1"; shift
  if ! has_resources "$root"; then echo "terraform/$root: 無い（state が無いか空）"; return 0; fi
  echo "terraform/$root: 消す"
  tf_logged "$root" destroy -input=false -auto-approve -var "owner=$OWNER" "$@" \
    || die "terraform/$root が消えなかった（上のエラー。全文は $(tf_log_file "$root" destroy)。Runtime の ENI でサブネットや SG が消えないときは最大 8 時間待って ops/down.sh を打ち直す）"
  echo "terraform/$root: 消えた"
}
# VPC の中の Lambda は、関数を消しても ENI が available のまま 20〜40 分残り、SG とサブネットの削除を DependencyViolation で待たせる
# （2026-09-18 に graph の destroy が 20 分以上止まった）。destroy の間、その関数の available な ENI だけを裏で消し続ける。
reap_lambda_enis() {  # reap_lambda_enis <関数名>  親（このスクリプト）が終われば止まる
  local e
  while kill -0 $$ 2>/dev/null; do
    for e in $(aws ec2 describe-network-interfaces --region "$REGION" \
        --filters "Name=description,Values=AWS Lambda VPC ENI-$1-*" Name=status,Values=available \
        --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null); do
      if aws ec2 delete-network-interface --region "$REGION" --network-interface-id "$e" >/dev/null 2>&1; then
        echo "Lambda（$1）の残った ENI を 1 つ消した"
      fi
    done
    sleep 20
  done
}
destroy_lambda_root() {  # destroy_lambda_root <ルート> <VPC の中の Lambda の関数名> [-var 名前=値 …]
  local root="$1" fn="$2" reaper; shift 2
  reap_lambda_enis "$fn" &
  reaper=$!
  destroy_root "$root" "$@"
  kill "$reaper" 2>/dev/null; wait "$reaper" 2>/dev/null || true
}

log "0. 設定と道具と認証"
load_deploy_env
KEEP_ECR="${KEEP_ECR:-0}"
case "$KEEP_ECR" in
  0) echo "KEEP_ECR=0: ECR もイメージごと消す（残すなら KEEP_ECR=1）" ;;
  1) echo "KEEP_ECR=1: ECR（イメージ）は残す" ;;
  *) die "KEEP_ECR は 1（ECR を残す）か 0（消す。既定）（いまは「${KEEP_ECR}」）。まだ何も消していない" ;;
esac
command -v aws >/dev/null || die "aws CLI が無い"
command -v terraform >/dev/null || die "terraform が無い"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || die "認証が通っていない（aws configure か aws login で入り直す）"
echo "ACCOUNT_ID=$ACCOUNT_ID"
tf_use_cli_credentials
CACERT="${OPENSEARCH_CACERT_FILE:-${AWS_CA_BUNDLE:-}}"
AGENT_VARS=()
if [ -n "$CACERT" ]; then AGENT_VARS+=(-var "opensearch_cacert_file=$CACERT"); fi

log "1. analytics → graph → stream（analytics は stream の Kafka を読み、stream は lab の state を読むので、この順）"
# EMR Serverless のアプリケーションは、ジョブが動いているか STARTED のままだと destroy が落ちる。先にジョブを止め、アプリケーションを止める
if has_resources pipeline/analytics; then
  APP_ID=$(tf pipeline/analytics output -raw application_id 2>/dev/null || true)
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
# workflow の worker_image_tag は必須変数だが destroy では使われないので、何でもよい値を渡す
destroy_lambda_root workflow "$PREFIX-tools" -var "worker_image_tag=${IMAGE_TAG:-destroy}"
destroy_root pipeline/analytics
destroy_lambda_root pipeline/graph "$PREFIX-graph-status"
STREAM_VARS=()
# S3 sink 無しで作った stream（CREATE_S3_SINK=0 ops/up.sh）は、既定の create_s3_sink=true のまま destroy すると zip の有無を確かめに行って止まる
if has_resources pipeline/stream && ! tf pipeline/stream state list 2>/dev/null | grep -Eq '\.(connect|s3_sink)\['; then
  STREAM_VARS+=(-var create_s3_sink=false)
fi
destroy_root pipeline/stream ${STREAM_VARS[@]+"${STREAM_VARS[@]}"}

log "2. lab"
destroy_root pipeline/lab

log "3. agent（Runtime / ガードレール / KB。terraform/base/core のロールにポリシーを付けているので main より先）"
LOG_GROUP=""
if has_resources agent; then
  LOG_GROUP=$(tf agent output -raw runtime_log_group_name 2>/dev/null || true)
fi
# create_knowledge_base=true で作った agent は、既定の false のまま destroy すると KB のリソースを state から外そうとして止まらないよう、state から読む
if has_resources agent && tf agent state list 2>/dev/null | grep -q '^aws_opensearchserverless_collection\.kb\['; then
  AGENT_VARS+=(-var create_knowledge_base=true)
fi
destroy_root agent ${AGENT_VARS[@]+"${AGENT_VARS[@]}"}

log "3-2. 土台（terraform/base/core。VPC / Web の EC2 / バケット（中身ごと消える）/ ロール）"
# Runtime の ENI（種類 agentic_ai。AWS 側の所有で、自分では外せない）は Runtime を消したあとも最大 8 時間残り、その間はサブネットと
# Runtime の SG が DependencyViolation で消えない（terraform は 20 分待ってから落ちる）。残っているあいだは、それ以外だけを消して先へ進む。
# 残る VPC・サブネット・SG に時間課金は無く、次の ops/up.sh はそのまま使い回す
MAIN_LEFT=0
if has_resources base/core; then
  # 確認そのものが落ちたときに黙って全部消しにいくと 20 分待ちに戻るので、結果は必ず表示し、エラーも隠さない
  # VPC は terraform の output でなくタグで引く。destroy が途中で落ちた state には output が残らず（terraform は output を先に外す）、
  # `terraform output -raw` は空を返して成功するので、打ち直しのとき（= いちばん要るとき）に読めない
  VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" --filters "Name=tag:Name,Values=$PREFIX-vpc" \
    --query 'Vpcs[0].VpcId' --output text) || { echo "注意: VPC を引けなかった（上のエラー）"; VPC_ID=""; }
  if [ "$VPC_ID" = None ]; then VPC_ID=""; fi
  AGENT_ENIS=""
  if [ -n "$VPC_ID" ]; then
    AGENT_ENIS=$(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query "NetworkInterfaces[?InterfaceType=='agentic_ai'].NetworkInterfaceId" --output text) \
      || echo "注意: ENI の確認に失敗した（上のエラー）。残っていない扱いで進む"
  fi
  echo "Runtime の ENI の確認: VPC=${VPC_ID:-（読めない）} 残り=${AGENT_ENIS:-なし}"
  if [ -n "$AGENT_ENIS" ] && [ "$AGENT_ENIS" != None ]; then
    MAIN_LEFT=1
    echo "Runtime の ENI が残っている: $AGENT_ENIS"
    echo "VPC・サブネット・Runtime の SG は残し、それ以外を消す"
    MAIN_TARGETS=()
    while IFS= read -r addr; do
      case "$addr" in
        ""|data.*|aws_vpc.this|aws_subnet.*|aws_security_group.runtime) ;;
        *) MAIN_TARGETS+=("-target=$addr") ;;
      esac
    done < <(tf base/core state list 2>/dev/null)
    if [ "${#MAIN_TARGETS[@]}" -gt 0 ]; then
      tf_logged base/core destroy -input=false -auto-approve -var "owner=$OWNER" "${MAIN_TARGETS[@]}" \
        || die "terraform/base/core の ENI に関わらない部分が消えなかった（上のエラー）"
    else
      echo "terraform/base/core: 残っているのは VPC・サブネット・Runtime の SG だけ"
    fi
  else
    destroy_root base/core
  fi
else
  echo "terraform/base/core: 無い（state が無いか空）"
fi

if [ "$KEEP_ECR" = 1 ]; then
  log "4. ECR は残す（KEEP_ECR=1）"
else
  log "4. ECR（イメージごと消える）"
  destroy_root base/ecr
fi

log "5. Runtime のロググループ（AgentCore が作るもので Terraform の管理外）"
# 名前は agent の state からも読めるが、前回の down.sh が途中（main の destroy など）で落ちていると、打ち直しのときには state が空で読めない。
# Runtime はここまでに消してあるので、この接頭辞のロググループを全部消す（Runtime を作り直すたびに末尾の ID が変わり、古いものが溜まる）
LOG_PREFIX="/aws/bedrock-agentcore/runtimes/${PREFIX//-/_}_agent-"
LOG_GROUPS=$(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$LOG_PREFIX" \
  --query 'logGroups[].logGroupName' --output text) || echo "注意: ロググループを引けなかった（上のエラー）"
if [ -n "$LOG_GROUP" ]; then LOG_GROUPS="$LOG_GROUPS $LOG_GROUP"; fi
LOG_GROUPS=$(printf '%s\n' $LOG_GROUPS | grep -v '^None$' | sort -u)
if [ -z "$LOG_GROUPS" ]; then echo "$LOG_PREFIX*: 無い"; fi
for g in $LOG_GROUPS; do
  aws logs delete-log-group --region "$REGION" --log-group-name "$g" 2>/dev/null && echo "$g: 消した" || echo "$g: 無い"
done

log "6. 残っていないか（Project=$PREFIX のタグ）"
aws resourcegroupstaggingapi get-resources --region "$REGION" --tag-filters "Key=Project,Values=$PREFIX" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d' || true
echo "（何も出なければ全部消えている。ecr を残したときはリポジトリが出る。消した直後の数分は消えたものが出ることがある）"
OLD_STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --query "StackSummaries[?starts_with(StackName, '$PREFIX') && StackStatus != 'DELETE_COMPLETE'].StackName" \
  --output text 2>/dev/null || true)
if [ "$MAIN_LEFT" = 1 ]; then
  echo "terraform/base/core の VPC・サブネット・Runtime の SG は残した（Runtime の ENI 待ち。時間課金は無い）。"
  echo "すぐ使うなら ops/up.sh がそのまま使い回す。消し切るなら数時間おいて ops/down.sh を打ち直す"
fi
if [ -n "$OLD_STACKS" ] && [ "$OLD_STACKS" != None ]; then
  echo "CloudFormation 版のスタックも残っている: $OLD_STACKS （docs/deploy-manual.md「CloudFormation 版から移るとき」）"
fi
