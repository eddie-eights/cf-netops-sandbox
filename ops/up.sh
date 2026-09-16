#!/usr/bin/env bash
# 全部を 1 本で起こす。README の手順 1〜5・7 に、lab、フェーズ 2（stream / graph と投入）を足したもの。
# 毎日全部消す運用向け。何度打っても同じ状態に収束する（できているものは Terraform が差分なしで飛ばし、ECR にあるタグはビルドしない）。
# Terraform の state はこの PC のリポジトリの中（terraform/<ルート>/terraform.tfstate）に置く。消すのは ops/down.sh。
#
# 使い方（リポジトリの直下で。aws-vault なら `aws-vault exec <プロファイル> --no-session` のサブシェルの中で）:
#   ops/up.sh                   # 全部。最後にポートフォワーディングを開いたまま止まる（Ctrl+C で閉じる）。初回は 40〜60 分
#   PHASE1_ONLY=1 ops/up.sh     # フェーズ 1 だけ（ECR・イメージ・本体・Web）。lab / stream / graph を作らない
#   NO_PORTFORWARD=1 ops/up.sh  # ポートフォワーディングを開かずに終わる
#
# stream（MSK / MSK Connect）と graph（Neptune）は時間課金。使い終わったら当日中に ops/down.sh を打つ（README「1 時間起動したときの試算」）。
#
# 環境変数で変えられるもの（全部任意）:
#   IMAGE_TAG               エージェントのイメージのタグ。既定 v1。ECR にそのタグが無いときだけ PC の docker buildx でビルドして push する（タグは上書きできない）
#   SKIP_LAB=1              lab を作らない（stream は lab の state を読むので、stream も作らない）
#   SKIP_STREAM=1           stream（MSK → detector → DynamoDB）を作らない
#   SKIP_GRAPH=1            graph（Neptune）を作らない
#   PHASE1_ONLY=1           上の 3 つを全部付けたのと同じ
#   CREATE_S3_SINK=0        MSK Connect の S3 sink を作らない（Confluent の zip が取れないとき。ops/down.sh は state を見て合わせる）
#   ADMIN_ARN               terraform/main の kb_admin_principal_arn。既定は空（Terraform が今の認証情報から決める）
#   VPC_CIDR                terraform/main の vpc_cidr（社内と重なるとき）
#   CLIENT_CIDR             terraform/main の client_cidr（DX / VPN 経由のとき）
#   OPENSEARCH_CACERT_FILE  terraform/main の opensearch_cacert_file（社内の SSL 検査の CA の PEM）。既定は AWS_CA_BUNDLE と同じ
#   LOCAL_PORT              PC 側のポート。既定 8080
#   NO_PORTFORWARD=1        ポートフォワーディングを開かずに終わる
#
# 手順 6（利用者への権限）は人に渡す作業なので入れていない。
set -euo pipefail

REGION=ap-northeast-1
PREFIX=fukuda-nwc-poc
OWNER=fukuda
IMAGE_TAG="${IMAGE_TAG:-v1}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
# 下の 5 つは Terraform の変数の既定値に合わせてある（terraform/lab の *_image_tag / containerlab_version / telegraf_version、
# terraform/stream の s3_sink_plugin_key）。変えるときは両方を変える
FRR_TAG=10.2.1
MULTITOOL_TAG=v0.10.0
SNMPD_TAG=v1
CONTAINERLAB_VERSION=0.79.0
TELEGRAF_VERSION=1.40.0
CONTAINERLAB_RPM="containerlab_${CONTAINERLAB_VERSION}_linux_arm64.rpm"
TELEGRAF_RPM="telegraf-${TELEGRAF_VERSION}-1.aarch64.rpm"
S3_SINK_ZIP=confluentinc-kafka-connect-s3-12.1.11.zip
S3_SINK_URL="https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-s3/versions/12.1.11/$S3_SINK_ZIP"

if [ -n "${PHASE1_ONLY:-}" ]; then SKIP_LAB=1; SKIP_STREAM=1; SKIP_GRAPH=1; fi
SKIP_LAB="${SKIP_LAB:-}"; SKIP_STREAM="${SKIP_STREAM:-}"; SKIP_GRAPH="${SKIP_GRAPH:-}"
if [ -n "$SKIP_LAB" ] && [ -z "$SKIP_STREAM" ]; then
  echo "SKIP_LAB なので stream も作らない（stream は lab の state から SG とロールを読む）"
  SKIP_STREAM=1
fi
CREATE_S3_SINK="${CREATE_S3_SINK:-1}"
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
tf() {  # tf <ルート> <terraform のサブコマンドと引数…>
  local root="$1"; shift
  terraform -chdir="terraform/$root" "$@"
}
tf_init() {  # tf_init <ルート>
  tf "$1" init -input=false >/dev/null \
    || die "terraform/$1 の init に失敗した（provider の取得。社内 PC は README「社内 PC で使うとき」）"
}
tf_apply_only() {  # tf_apply_only <ルート> [-var 名前=値 …]  init 済みのルートを apply する
  local root="$1"; shift
  tf "$root" apply -input=false -auto-approve -var "owner=$OWNER" "$@" \
    || die "terraform/$root の apply に失敗した（上のエラー。README の「うまくいかないとき」。直したらもう一度 ops/up.sh）"
}
tf_apply() {  # tf_apply <ルート> [-var 名前=値 …]
  tf_init "$1"
  tf_apply_only "$@"
}
has_resources() {  # has_resources <ルート>  state があり、リソースが 1 つ以上載っている（init 済みが前提）
  [ -f "terraform/$1/terraform.tfstate" ] && [ -n "$(tf "$1" state list 2>/dev/null)" ]
}
ecr_has() {  # ecr_has <リポジトリ名> <タグ>
  aws ecr describe-images --region "$REGION" --repository-name "$1" --image-ids imageTag="$2" >/dev/null 2>&1
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
ssm_run() {  # ssm_run <インスタンス ID> <コマンド…>  cloud-init（user_data）が終わるのを待ってから打ち、標準出力を出す。失敗なら 1
  # コマンドは JSON の文字列に埋めるので、ダブルクォートとバックスラッシュを含めない
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
      Success)
        aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$id" \
          --query StandardOutputContent --output text | sed '/^$/d'
        return 0 ;;
      Pending|InProgress|Delayed) sleep 10 ;;
      *) aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$id" \
           --query '[StandardOutputContent,StandardErrorContent]' --output text >&2
         echo "インスタンス上のコマンドが $status" >&2
         return 1 ;;
    esac
  done
}
run_on_instance() {  # run_on_instance <インスタンス ID> <コマンド…>  ssm_run の失敗で止まる版
  ssm_run "$@" || die "インスタンス $1 の上のコマンドが失敗した（上の出力）"
}
fetch() {  # fetch <URL> <ファイル名>  リポジトリの直下（gitignore 済み）に無いときだけ取る。翌日からは取り直さない
  if [ -s "$2" ]; then echo "$2: 手元にあるので取らない"; return 0; fi
  curl -fL --retry 3 -o "$2.part" "$1" || { rm -f "$2.part"; return 1; }
  mv "$2.part" "$2"
}
is_zip() {  # is_zip <ファイル>
  "${PY[@]}" -c 'import sys, zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$1"
}
GRAPH_PID=""
GRAPH_LOG=ops/logs/graph-apply.log
on_exit() {  # 途中で止まっても、バックグラウンドの graph の apply は終わるまで待つ（打ち直したときに state のロックでぶつからないように）
  if [ -n "$GRAPH_PID" ] && kill -0 "$GRAPH_PID" 2>/dev/null; then
    printf '\n%s\n' "terraform/graph の apply がまだ動いているので、終わるまで待つ（ログ: $GRAPH_LOG）。このターミナルは閉じない" >&2
    wait "$GRAPH_PID" || true
  fi
}
trap on_exit EXIT

# ---- 0. 道具と認証 -------------------------------------------------------------
log "0. 道具と認証を確かめる"
command -v aws >/dev/null || die "aws CLI が無い（README「WSL2 の準備」）"
command -v terraform >/dev/null || die "terraform が無い（README「WSL2 の準備」。1.11 以上）"
if command -v python3 >/dev/null; then PY=(python3)
elif command -v uv >/dev/null; then PY=(uv run --python 3.13 python)
else die "python3 も uv も無い（README「WSL2 の準備」）"; fi
if [ -z "$SKIP_LAB" ]; then command -v curl >/dev/null || die "curl が無い（lab の rpm を取るのに使う。sudo apt install curl）"; fi
command -v docker >/dev/null || die "docker が無い（イメージのビルドに使う。README「WSL2 の準備」）"
docker buildx version >/dev/null 2>&1 || die "docker buildx が無い（Ubuntu の docker.io には入っていない。README「WSL2 の準備」）"
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
ROOTS="ecr main"
if [ -z "$SKIP_LAB" ]; then ROOTS="$ROOTS lab"; fi
if [ -z "$SKIP_STREAM" ]; then ROOTS="$ROOTS stream"; fi
if [ -z "$SKIP_GRAPH" ]; then ROOTS="$ROOTS graph"; fi
echo "ACCOUNT_ID=$ACCOUNT_ID"
echo "CALLER_ARN=$CALLER_ARN"
echo "IMAGE_TAG=$IMAGE_TAG"
echo "作るルート: $ROOTS"
if [ -z "$SKIP_STREAM" ] || [ -z "$SKIP_GRAPH" ]; then
  printf '\033[1;33m%s\033[0m\n' "stream / graph は時間課金。使い終わったら当日中に ops/down.sh を打つ"
fi

# ---- 1. ECR --------------------------------------------------------------------
log "1. ECR リポジトリ（terraform/ecr）"
tf_apply ecr
REPO=$(tf ecr output -raw agent_repository_url); echo "REPO=$REPO"
REG="${REPO%%/*}"

# ---- 2. イメージ ----------------------------------------------------------------
log "2. イメージ（ECR に無いタグだけ作る）"
NEED_AGENT=""; NEED_FRR=""; NEED_MULTITOOL=""; NEED_SNMPD=""
if ecr_has "$PREFIX-agent" "$IMAGE_TAG"; then echo "agent:$IMAGE_TAG はある（作り直すなら IMAGE_TAG を変える）"; else NEED_AGENT=1; fi
if [ -z "$SKIP_LAB" ]; then
  if ecr_has "$PREFIX-lab-frr" "$FRR_TAG"; then echo "lab-frr:$FRR_TAG はある"; else NEED_FRR=1; fi
  if ecr_has "$PREFIX-lab-multitool" "$MULTITOOL_TAG"; then echo "lab-multitool:$MULTITOOL_TAG はある"; else NEED_MULTITOOL=1; fi
  if ecr_has "$PREFIX-lab-snmpd" "$SNMPD_TAG"; then echo "lab-snmpd:$SNMPD_TAG はある"; else NEED_SNMPD=1; fi
fi
NEED_LAB="$NEED_FRR$NEED_MULTITOOL$NEED_SNMPD"
if [ -n "$NEED_AGENT$NEED_LAB" ]; then
  docker info >/dev/null 2>&1 || die "dockerd に接続できない（WSL なら sudo service docker start。README「WSL2 の準備」）"
  # agent と snmpd は RUN があるので、x86_64 の PC では QEMU（binfmt）が要る
  if [ -n "$NEED_AGENT$NEED_SNMPD" ] && ! docker buildx ls | grep -q 'linux/arm64'; then
    die "docker buildx ls の Platforms に linux/arm64 が無い（README「WSL2 の準備」の docker の行）"
  fi
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
  if [ -n "$NEED_AGENT" ]; then
    docker buildx build --platform linux/arm64 -t "$REPO:$IMAGE_TAG" --push agent/
  fi
  if [ -n "$NEED_FRR" ]; then
    docker pull --platform linux/arm64 "quay.io/frrouting/frr:$FRR_TAG"
    docker tag "quay.io/frrouting/frr:$FRR_TAG" "$REG/$PREFIX-lab-frr:$FRR_TAG"
    docker push "$REG/$PREFIX-lab-frr:$FRR_TAG"
  fi
  if [ -n "$NEED_MULTITOOL" ]; then
    docker pull --platform linux/arm64 "wbitt/network-multitool:$MULTITOOL_TAG"
    docker tag "wbitt/network-multitool:$MULTITOOL_TAG" "$REG/$PREFIX-lab-multitool:$MULTITOOL_TAG"
    docker push "$REG/$PREFIX-lab-multitool:$MULTITOOL_TAG"
  fi
  if [ -n "$NEED_SNMPD" ]; then
    docker buildx build --platform linux/arm64 -t "$REG/$PREFIX-lab-snmpd:$SNMPD_TAG" --push lab/snmpd/
  fi
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

# graph は main の state しか読まないので、ここで裏で始めて待ち時間を重ねる（Neptune は 10〜15 分）
if [ -z "$SKIP_GRAPH" ]; then
  log "3-2. graph（Neptune）の apply を裏で始める（10〜15 分。待たずに次へ進む）"
  mkdir -p ops/logs
  tf_init graph   # init は前で済ませる（provider のキャッシュを 2 つの init で同時に触らない）
  ( tf_apply_only graph ) >"$GRAPH_LOG" 2>&1 &
  GRAPH_PID=$!
  echo "進み具合: tail -f $GRAPH_LOG"
fi

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
WEB_ACTIVE="for i in 1 2 3 4 5 6 7 8 9 10 11 12; do systemctl is-active --quiet $PREFIX-web.service && exit 0; sleep 5; done; systemctl --no-pager status $PREFIX-web.service; journalctl --no-pager -u $PREFIX-web.service -n 50; exit 1"
run_on_instance "$INSTANCE_ID" "$WEB_ACTIVE"
echo "Web が動いている"

# ---- 5. lab とフェーズ 2 の材料 --------------------------------------------------------
# lab の EC2 は起動のたびに s3://<バケット>/lab/ を読む。apply より前に置けば、Telegraf まで最初の起動で入る（再起動が要らない）
if [ -z "$SKIP_LAB" ]; then
  log "5-1. lab の材料（containerlab と Telegraf の rpm、トポロジ）を s3://$KB_BUCKET/lab/ に置く（README の lab-2 と s-1）"
  fetch "https://github.com/srl-labs/containerlab/releases/download/v$CONTAINERLAB_VERSION/$CONTAINERLAB_RPM" "$CONTAINERLAB_RPM" \
    || die "containerlab の rpm が取れない（README の lab-2。社内 PC なら「社内 PC で使うとき」の証明書）"
  aws s3 sync lab/ "s3://$KB_BUCKET/lab/" --exclude "wvs2.clab.yml" --exclude "snmpd/certs/*"
  aws s3 cp "$CONTAINERLAB_RPM" "s3://$KB_BUCKET/lab/"
  if [ -z "$SKIP_STREAM" ]; then
    fetch "https://dl.influxdata.com/telegraf/releases/$TELEGRAF_RPM" "$TELEGRAF_RPM" || die "Telegraf の rpm が取れない（README の s-1）"
    aws s3 cp "$TELEGRAF_RPM" "s3://$KB_BUCKET/lab/"
  fi
fi
STREAM_VARS=()
if [ -z "$SKIP_STREAM" ]; then
  if [ "$CREATE_S3_SINK" = 0 ]; then
    echo "CREATE_S3_SINK=0 なので S3 sink は作らない"
    STREAM_VARS+=(-var create_s3_sink=false)
  else
    log "5-2. S3 sink のプラグイン（Confluent の zip）を s3://$KB_BUCKET/stream/ に置く（README の s-1）"
    if ! fetch "$S3_SINK_URL" "$S3_SINK_ZIP" || ! is_zip "$S3_SINK_ZIP"; then
      rm -f "$S3_SINK_ZIP"
      die "Confluent の zip が取れない（利用条件への同意が要るとページが返る）。ブラウザで $S3_SINK_URL を開いて取り、リポジトリの直下に $S3_SINK_ZIP の名前で置いて打ち直す。S3 sink が要らなければ CREATE_S3_SINK=0 ops/up.sh"
    fi
    aws s3 cp "$S3_SINK_ZIP" "s3://$KB_BUCKET/stream/$S3_SINK_ZIP"
  fi
fi

# ---- 6. lab ---------------------------------------------------------------------
LAB_EXISTED=""; LAB_INSTANCE_ID=""
if [ -z "$SKIP_LAB" ]; then
  log "6. lab（terraform/lab。EC2 の中でトポロジが上がるまで 5 分ほど）"
  tf_init lab
  if has_resources lab; then LAB_EXISTED=1; fi
  tf_apply_only lab
  LAB_INSTANCE_ID=$(tf lab output -raw lab_instance_id); echo "LAB_INSTANCE_ID=$LAB_INSTANCE_ID"
fi

# ---- 7. stream ------------------------------------------------------------------
if [ -z "$SKIP_STREAM" ]; then
  log "7. stream（terraform/stream。MSK の作成に 20〜30 分）"
  tf_apply stream ${STREAM_VARS[@]+"${STREAM_VARS[@]}"}
  # lab が前からあった場合だけ、Telegraf が入っているかを見る（rpm を置く前に起動していたら入っていない。起動のたびに入れるので再起動で足りる）
  if [ -n "$LAB_EXISTED" ]; then
    log "7-2. lab の Telegraf を確かめる"
    wait_ssm_online "$LAB_INSTANCE_ID"
    # 無いときに失敗扱いのエラー出力が並ばないよう、終了コードではなく出力で見る
    if [ "$(ssm_run "$LAB_INSTANCE_ID" "systemctl cat $PREFIX-telegraf.service >/dev/null 2>&1 && echo yes || echo no" | tail -n 1)" = "yes" ]; then
      echo "Telegraf は入っている（MSK のブローカーは 60 秒ごとに読み直すので、そのままつながる）"
    else
      aws ec2 reboot-instances --region "$REGION" --instance-ids "$LAB_INSTANCE_ID"
      echo "Telegraf が無かったので lab の EC2 を再起動した（起動時に rpm を入れる。数分）"
    fi
  fi
fi

# ---- 8. graph と投入 --------------------------------------------------------------
if [ -n "$GRAPH_PID" ]; then
  log "8. graph の apply が終わるのを待つ"
  rc=0; wait "$GRAPH_PID" || rc=$?; GRAPH_PID=""
  if [ "$rc" -ne 0 ]; then
    tail -n 40 "$GRAPH_LOG" >&2
    die "terraform/graph の apply に失敗した（全文: $GRAPH_LOG）。直したらもう一度 ops/up.sh"
  fi
  tail -n 3 "$GRAPH_LOG"
  log "8-2. Neptune が空なら静的トポロジを入れる（GUI の「静的データを投入」と同じ。入っていれば何もしない）"
  # ops/seed_graph.py を Web の EC2 の上で、Web と同じ環境変数と依存で動かす。コマンドに記号を入れないよう base64 で渡す
  run_on_instance "$INSTANCE_ID" "echo $(base64 < ops/seed_graph.py | tr -d '\n') | base64 -d | /usr/bin/python3.13 -"
fi
if [ -z "$SKIP_STREAM" ] || [ -z "$SKIP_GRAPH" ]; then
  log "8-3. Web を再起動する（起動時に SSM の異常テーブルと Neptune を読むため）"
  run_on_instance "$INSTANCE_ID" "systemctl restart $PREFIX-web.service; $WEB_ACTIVE"
  echo "Web が動いている"
fi

# ---- 9. Runtime のロググループ -------------------------------------------------------
# AgentCore が最初の呼び出しで作るもので、Terraform の管理外。保持期間とタグだけ付け、ops/down.sh が消す
log "9. ロググループ $LOG_GROUP（保持 7 日 + タグ）"
if ! aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7 2>/dev/null; then
  aws logs create-log-group --region "$REGION" --log-group-name "$LOG_GROUP"
  aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7
fi
aws logs tag-resource --region "$REGION" \
  --resource-arn "arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$LOG_GROUP" \
  --tags "Project=$PREFIX,owner=$OWNER"

# ---- 10. ポートフォワーディング -------------------------------------------------------------
log "できた（$ROOTS）。利用者に配るコマンド:"
tf main output -raw start_session_command; echo
if [ -n "$LAB_INSTANCE_ID" ]; then
  echo "lab に入るコマンド:"
  tf lab output -raw start_session_command; echo
fi
if [ -z "$SKIP_STREAM" ] || [ -z "$SKIP_GRAPH" ]; then
  printf '\033[1;33m%s\033[0m\n' "stream / graph は時間課金。使い終わったら当日中に ops/down.sh"
fi
if [ -n "${NO_PORTFORWARD:-}" ]; then exit 0; fi
log "10. ポートフォワーディング（http://localhost:$LOCAL_PORT/ 。Ctrl+C で閉じる）"
trap - EXIT
exec aws ssm start-session --region "$REGION" --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
