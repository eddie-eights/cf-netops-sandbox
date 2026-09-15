#!/usr/bin/env bash
# README の手順 1〜7 をまとめて打つ（ECR → イメージ → 本体 → Web の部品と手順書 → ロググループ → ポートフォワーディング）。
# 毎日スタックを消す運用向け。何度打っても同じ状態に収束する（できているものは飛ばす）。
#
# 使い方（リポジトリの直下で。aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/up.sh                  # 手順 1〜7。最後にポートフォワーディングを開いたまま止まる（Ctrl+C で閉じる）
#   NO_PORTFORWARD=1 ops/up.sh # 手順 6 まで（ポートフォワーディングは開かない）
#
# 環境変数で変えられるもの（全部任意）:
#   IMAGE_TAG     イメージのタグ。既定 v1。ECR にそのタグが無いときだけビルドして push する（タグは上書きできない）
#   ADMIN_ARN     main.yaml の KbAdminPrincipalArn。既定は今の認証情報から取る（IAM ユーザーはその ARN、ロールは get-role の ARN）
#   VPC_CIDR      main.yaml の VpcCidr（社内と重なるとき。例 10.123.0.0/16）
#   CLIENT_CIDR   main.yaml の ClientCidr（DX / VPN 経由のとき。例 192.0.2.0/24）
#   LOCAL_PORT    PC 側のポート。既定 8080
set -euo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
TAGS="Project=$PREFIX owner=fukuda"
IMAGE_TAG="${IMAGE_TAG:-v1}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
output() {  # output <スタック名> <OutputKey>
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}
stack_status() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
}
clear_rollback() {  # clear_rollback <スタック名>  初回作成に失敗した ROLLBACK_COMPLETE は deploy し直せないので消してから
  if [ "$(stack_status "$1")" = ROLLBACK_COMPLETE ]; then
    echo "$1 が ROLLBACK_COMPLETE（前回の作成に失敗）。消してから作り直す"
    aws cloudformation delete-stack --region "$REGION" --stack-name "$1"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$1"
  fi
}
wait_ssm_online() {  # wait_ssm_online <インスタンス ID>
  local i
  for i in $(seq 1 60); do
    if [ "$(aws ssm describe-instance-information --region "$REGION" \
          --filters "Key=InstanceIds,Values=$1" \
          --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)" = Online ]; then
      return 0
    fi
    sleep 10
  done
  die "$1 が 10 分たっても Session Manager に Online にならない（README の「うまくいかないとき」）"
}
run_on_instance() {  # run_on_instance <インスタンス ID> <コマンド…>  cloud-init（UserData）が終わるのを待ってから打つ
  local id="$1"; shift
  local cmd_id status
  cmd_id=$(aws ssm send-command --region "$REGION" --instance-ids "$id" \
    --document-name AWS-RunShellScript --timeout-seconds 900 \
    --parameters "{\"commands\":[\"cloud-init status --wait >/dev/null || true\",\"$*\"]}" \
    --query Command.CommandId --output text)
  while :; do
    status=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$id" \
      --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
      Success) return 0 ;;
      Pending|InProgress|Delayed) sleep 10 ;;
      *) aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$id" \
           --query '[StandardOutputContent,StandardErrorContent]' --output text >&2
         die "インスタンス上のコマンドが $status" ;;
    esac
  done
}

# ---- 0. 認証と環境の値 -------------------------------------------------------
log "0. 認証を確かめる"
command -v aws >/dev/null || die "aws CLI が無い"
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text) || die "認証が通っていない（aws-vault なら --no-session のサブシェルの中で打つ）"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ACCOUNT_ID
case "$CALLER_ARN" in
  arn:aws:iam::*:user/*)
    if [ -n "${AWS_SESSION_TOKEN:-}" ]; then
      die "IAM ユーザーの一時セッション（get-session-token）で入っている。IAM の API が呼べないので aws-vault exec <プロファイル> --no-session で入り直す"
    fi
    ADMIN_ARN="${ADMIN_ARN:-$CALLER_ARN}" ;;
  arn:aws:sts::*:assumed-role/*)
    if [ -z "${ADMIN_ARN:-}" ]; then
      ROLE_NAME=${CALLER_ARN#*:assumed-role/}; ROLE_NAME=${ROLE_NAME%%/*}
      ADMIN_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text) \
        || die "ロール $ROLE_NAME の ARN が取れない。ADMIN_ARN=arn:aws:iam::…:role/… を環境変数で渡す"
    fi ;;
  *) [ -n "${ADMIN_ARN:-}" ] || die "この認証情報の形（$CALLER_ARN）では KbAdminPrincipalArn を決められない。ADMIN_ARN を環境変数で渡す" ;;
esac
export ADMIN_ARN
echo "ACCOUNT_ID=$ACCOUNT_ID"
echo "ADMIN_ARN=$ADMIN_ARN"
echo "IMAGE_TAG=$IMAGE_TAG"

# ---- 1. ECR --------------------------------------------------------------------
log "1. ECR リポジトリ（$PREFIX-ecr）"
clear_rollback "$PREFIX-ecr"
aws cloudformation deploy --region "$REGION" --stack-name "$PREFIX-ecr" --template-file ecr.yaml \
  --tags $TAGS --no-fail-on-empty-changeset
REPO=$(output "$PREFIX-ecr" RepositoryUri); echo "REPO=$REPO"

# ---- 2. イメージ ----------------------------------------------------------------
log "2. イメージ $REPO:$IMAGE_TAG"
if aws ecr describe-images --region "$REGION" --repository-name "${REPO#*/}" \
     --image-ids imageTag="$IMAGE_TAG" --query 'imageDetails[0].imagePushedAt' --output text 2>/dev/null | grep -q .; then
  echo "ECR にタグ $IMAGE_TAG がある。ビルドを飛ばす（作り直すなら IMAGE_TAG を変える）"
else
  command -v docker >/dev/null || die "docker が無い。README の 2-b（CodeBuild）で push してから打ち直す"
  docker buildx version >/dev/null 2>&1 || die "docker buildx が無い（arm64 のクロスビルドに要る）。README の 2-b で push してから打ち直す"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
  docker buildx build --platform linux/arm64 -t "$REPO:$IMAGE_TAG" --push agent/
fi

# ---- 3. 本体 --------------------------------------------------------------------
log "3. 本体（$PREFIX。初回は 10〜20 分）"
clear_rollback "$PREFIX"
PARAMS="Owner=fukuda KbAdminPrincipalArn=$ADMIN_ARN AgentImageTag=$IMAGE_TAG"
[ -n "${VPC_CIDR:-}" ]    && PARAMS="$PARAMS VpcCidr=$VPC_CIDR"
[ -n "${CLIENT_CIDR:-}" ] && PARAMS="$PARAMS ClientCidr=$CLIENT_CIDR"
aws cloudformation deploy --region "$REGION" --stack-name "$PREFIX" --template-file main.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags $TAGS --no-fail-on-empty-changeset \
  --parameter-overrides $PARAMS
INSTANCE_ID=$(output "$PREFIX" WebInstanceId)
KB_BUCKET=$(output "$PREFIX" KbBucketName)
KB_ID=$(output "$PREFIX" KnowledgeBaseId)
DS_ID=$(output "$PREFIX" DataSourceId)
LOG_GROUP=$(output "$PREFIX" RuntimeLogGroupName)
echo "INSTANCE_ID=$INSTANCE_ID KB_BUCKET=$KB_BUCKET KB_ID=$KB_ID DS_ID=$DS_ID"

# ---- 4. Web の部品と手順書 ------------------------------------------------------------
log "4-1. wheel（arm64 / cp313）"
if [ -z "$(ls wheels/*.whl 2>/dev/null)" ]; then
  if command -v uv >/dev/null; then PIP="uv run --python 3.13 --with pip python -m pip"; else PIP="python3 -m pip"; fi
  $PIP download --only-binary=:all: \
    --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux_2_28_aarch64 \
    --python-version 3.13 --implementation cp --abi cp313 --abi none \
    -d wheels -r web/requirements.txt
else
  echo "wheels/ に $(ls wheels/*.whl | wc -l | tr -d ' ') 個ある。取り直すなら wheels/ を消す"
fi

log "4-2. Web の部品を s3://$KB_BUCKET/web/ に置く"
aws s3 cp web/app.py "s3://$KB_BUCKET/web/app.py"
aws s3 cp web/requirements.txt "s3://$KB_BUCKET/web/requirements.txt"
for f in topology anomalies graph; do aws s3 cp "agent/$f.py" "s3://$KB_BUCKET/web/$f.py"; done
aws s3 cp agent/data/ "s3://$KB_BUCKET/web/data/" --recursive
aws s3 sync wheels/ "s3://$KB_BUCKET/web/wheels/"

log "4-3. 手順書を置いて取り込む"
aws s3 cp kb-docs/ "s3://$KB_BUCKET/docs/" --recursive --exclude "*" --include "*.md"
JOB_ID=$(aws bedrock-agent start-ingestion-job --region "$REGION" \
  --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
  --query ingestionJob.ingestionJobId --output text)
while :; do
  ST=$(aws bedrock-agent get-ingestion-job --region "$REGION" \
    --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" --ingestion-job-id "$JOB_ID" \
    --query 'ingestionJob.[status,statistics.numberOfDocumentsFailed]' --output text)
  case "$ST" in
    COMPLETE*) echo "取り込み $ST（status failed）"; [ "${ST#COMPLETE}" = $'\t0' ] || die "取り込みに失敗した md がある。get-ingestion-job の failureReasons を見る"; break ;;
    FAILED*|STOPPED*) die "取り込みジョブが $ST" ;;
    *) sleep 10 ;;
  esac
done

log "4-4. EC2 を再起動して Web を立てる（初回のデプロイ時点では web/ が無いため）"
wait_ssm_online "$INSTANCE_ID"
run_on_instance "$INSTANCE_ID" "true"          # 初回の UserData が終わるのを待ってから再起動する
aws ec2 reboot-instances --region "$REGION" --instance-ids "$INSTANCE_ID"
sleep 30
wait_ssm_online "$INSTANCE_ID"
run_on_instance "$INSTANCE_ID" "systemctl is-active $PREFIX-web.service"
echo "Web が動いている"

# ---- 5. Runtime のロググループ -------------------------------------------------------
log "5. ロググループ $LOG_GROUP（保持 7 日 + タグ）"
if ! aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7 2>/dev/null; then
  # AgentCore が最初の呼び出しで作る。先に同じ名前で作っておく（Lambda と同じ扱い。2026-09-15 時点で AgentCore 側の挙動は未確認、README 参照）
  aws logs create-log-group --region "$REGION" --log-group-name "$LOG_GROUP"
  aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7
fi
aws logs tag-resource --region "$REGION" \
  --resource-arn "arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$LOG_GROUP" \
  --tags "Project=$PREFIX,owner=fukuda"

# ---- 6 は人に権限を渡す作業なのでスクリプトにしない（README の手順 6） ----------------------------

# ---- 7. ポートフォワーディング -------------------------------------------------------------
log "できた。利用者に配るコマンド:"
output "$PREFIX" StartSessionCommand
if [ -n "${NO_PORTFORWARD:-}" ]; then exit 0; fi
log "7. ポートフォワーディング（http://localhost:$LOCAL_PORT/ 。Ctrl+C で閉じる）"
exec aws ssm start-session --region "$REGION" --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
