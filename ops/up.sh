#!/usr/bin/env bash
# README の手順 1〜7 をまとめて打つ（ECR → イメージ → 本体 → Web の部品と手順書 → ロググループ → ポートフォワーディング）。
# 毎日全部消す運用向け。何度打っても同じ状態に収束する（できているものは Terraform が差分なしで飛ばす）。
# Terraform の state はこの PC のリポジトリの中（terraform/<ルート>/terraform.tfstate）に置く。消すのは ops/down.sh。
#
# 使い方（リポジトリの直下で。aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/up.sh                  # 手順 1〜7。最後にポートフォワーディングを開いたまま止まる（Ctrl+C で閉じる）
#   NO_PORTFORWARD=1 ops/up.sh # 手順 5 まで（ポートフォワーディングは開かない）
#
# 環境変数で変えられるもの（全部任意）:
#   IMAGE_TAG               イメージのタグ。既定 v1。ECR にそのタグが無いときだけビルドして push する（タグは上書きできない）
#   ADMIN_ARN               terraform/main の kb_admin_principal_arn。既定は空（Terraform が今の認証情報から決める）
#   VPC_CIDR                terraform/main の vpc_cidr（社内と重なるとき）
#   CLIENT_CIDR             terraform/main の client_cidr（DX / VPN 経由のとき）
#   OPENSEARCH_CACERT_FILE  terraform/main の opensearch_cacert_file（社内の SSL 検査の CA の PEM）。既定は AWS_CA_BUNDLE と同じ
#   LOCAL_PORT              PC 側のポート。既定 8080
#
# lab / stream / graph / build はこのスクリプトでは作らない（README の各節で手で apply する）。ops/down.sh は全部消す。
set -euo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
OWNER=fukuda
IMAGE_TAG="${IMAGE_TAG:-v1}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
tf() {  # tf <ルート> <terraform のサブコマンドと引数…>
  local root="$1"; shift
  terraform -chdir="terraform/$root" "$@"
}
tf_apply() {  # tf_apply <ルート> [-var 名前=値 …]
  local root="$1"; shift
  tf "$root" init -input=false >/dev/null \
    || die "terraform/$root の init に失敗した（provider の取得。社内 PC は README「社内 PC で使うとき」）"
  tf "$root" apply -input=false -auto-approve -var "owner=$OWNER" "$@" \
    || die "terraform/$root の apply に失敗した（上のエラー。README の「うまくいかないとき」。直したらもう一度 ops/up.sh）"
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
run_on_instance() {  # run_on_instance <インスタンス ID> <コマンド…>  cloud-init（user_data）が終わるのを待ってから打つ
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

# ---- 0. 道具と認証 -------------------------------------------------------------
log "0. 道具と認証を確かめる"
command -v aws >/dev/null || die "aws CLI が無い（README「WSL2 の準備」）"
command -v terraform >/dev/null || die "terraform が無い（README「WSL2 の準備」。1.11 以上）"
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text) || die "認証が通っていない（aws-vault なら --no-session のサブシェルの中で打つ）"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
case "$CALLER_ARN" in
  arn:aws:iam::*:user/*)
    if [ -n "${AWS_SESSION_TOKEN:-}" ]; then
      die "IAM ユーザーの一時セッション（get-session-token）で入っている。IAM の API が呼べないので aws-vault exec <プロファイル> --no-session で入り直す"
    fi ;;
  arn:aws:sts::*:assumed-role/*) ;;
  *) [ -n "${ADMIN_ARN:-}" ] || die "この認証情報の形（$CALLER_ARN）では kb_admin_principal_arn を決められない。ADMIN_ARN=arn:aws:iam::<アカウント ID>:role/<ロール名> を環境変数で渡す" ;;
esac
# CloudFormation 版（2026-09-15 まで）のスタックが残っていると、同じ名前のリソースを作れずに apply が途中で落ちる
OLD_STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --query "StackSummaries[?starts_with(StackName, '$PREFIX') && StackStatus != 'DELETE_COMPLETE'].StackName" \
  --output text 2>/dev/null || true)
if [ -n "$OLD_STACKS" ] && [ "$OLD_STACKS" != None ]; then
  die "CloudFormation 版のスタックが残っている: $OLD_STACKS 。名前がぶつかるので先に消す（README「CloudFormation 版から移るとき」）"
fi
CACERT="${OPENSEARCH_CACERT_FILE:-${AWS_CA_BUNDLE:-}}"
echo "ACCOUNT_ID=$ACCOUNT_ID"
echo "CALLER_ARN=$CALLER_ARN"
echo "IMAGE_TAG=$IMAGE_TAG"

# ---- 1. ECR --------------------------------------------------------------------
log "1. ECR リポジトリ（terraform/ecr）"
tf_apply ecr
REPO=$(tf ecr output -raw agent_repository_url); echo "REPO=$REPO"

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
log "3. 本体（terraform/main。初回は 10〜20 分）"
MAIN_VARS=(-var "agent_image_tag=$IMAGE_TAG")
if [ -n "${ADMIN_ARN:-}" ];   then MAIN_VARS+=(-var "kb_admin_principal_arn=$ADMIN_ARN"); fi
if [ -n "${VPC_CIDR:-}" ];    then MAIN_VARS+=(-var "vpc_cidr=$VPC_CIDR"); fi
if [ -n "${CLIENT_CIDR:-}" ]; then MAIN_VARS+=(-var "client_cidr=$CLIENT_CIDR"); fi
if [ -n "$CACERT" ];          then MAIN_VARS+=(-var "opensearch_cacert_file=$CACERT"); fi
tf_apply main "${MAIN_VARS[@]}"
INSTANCE_ID=$(tf main output -raw web_instance_id)
KB_BUCKET=$(tf main output -raw kb_bucket_name)
KB_ID=$(tf main output -raw knowledge_base_id)
DS_ID=$(tf main output -raw data_source_id)
LOG_GROUP=$(tf main output -raw runtime_log_group_name)
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

log "4-4. EC2 を再起動して Web を立てる（初回の apply 時点では web/ が無いため）"
wait_ssm_online "$INSTANCE_ID"
run_on_instance "$INSTANCE_ID" "true"          # 初回の user_data が終わるのを待ってから再起動する
aws ec2 reboot-instances --region "$REGION" --instance-ids "$INSTANCE_ID"
sleep 30
wait_ssm_online "$INSTANCE_ID"
# 起動の失敗（環境変数や依存の不足）は数秒後に落ちる。1 分待って動かなければ status と journald を出して止まる
run_on_instance "$INSTANCE_ID" "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do systemctl is-active --quiet $PREFIX-web.service && exit 0; sleep 5; done; systemctl --no-pager status $PREFIX-web.service; journalctl --no-pager -u $PREFIX-web.service -n 50; exit 1"
echo "Web が動いている"

# ---- 5. Runtime のロググループ -------------------------------------------------------
# AgentCore が最初の呼び出しで作るもので、Terraform の管理外。保持期間とタグだけ付け、ops/down.sh が消す
log "5. ロググループ $LOG_GROUP（保持 7 日 + タグ）"
if ! aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7 2>/dev/null; then
  aws logs create-log-group --region "$REGION" --log-group-name "$LOG_GROUP"
  aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7
fi
aws logs tag-resource --region "$REGION" \
  --resource-arn "arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$LOG_GROUP" \
  --tags "Project=$PREFIX,owner=$OWNER"

# ---- 6 は人に権限を渡す作業なのでスクリプトにしない（README の手順 6） ----------------------------

# ---- 7. ポートフォワーディング -------------------------------------------------------------
log "できた。利用者に配るコマンド:"
tf main output -raw start_session_command; echo
if [ -n "${NO_PORTFORWARD:-}" ]; then exit 0; fi
log "7. ポートフォワーディング（http://localhost:$LOCAL_PORT/ 。Ctrl+C で閉じる）"
exec aws ssm start-session --region "$REGION" --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
