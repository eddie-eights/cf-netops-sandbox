#!/usr/bin/env bash
# deploy.env の PIPELINE / AGENT / WORKFLOW で選んだ機能を 1 本で起こす。機能は互いに独立で、要るものだけ作る（費用を抑えるため）。
#   土台（必ず作る）  base/ecr + base/core（VPC / Web の EC2 / バケット / ロール）。約 $0.05/h。
#                     AGENT か lab か analytics を作るなら、共用のエンドポイント（ecr.api / ecr.dkr / logs）も土台に作る（+ 約 $0.08/h）
#   AGENT（既定 1）   agent での分析。terraform/agent（AgentCore Runtime + ガードレール + bedrock のエンドポイント。CREATE_KB=1 なら Knowledge Base も）。
#                     Web の「チャット」タブが使える
#   PIPELINE          データパイプライン。lab（containerlab。stream を作るなら Telegraf の EC2 も）→ stream（MSK）→ analytics（Spark on EMR Serverless → S3 Tables / OpenSearch / Prometheus、
#                     異常検知 → DynamoDB + EventBridge）と graph（Neptune のトポロジと投入）。Web の「トポロジ」「異常一覧」タブが動く
#   WORKFLOW          Temporal での実行。workflow（Temporal on ECS Fargate のワーカー + AgentCore Gateway（MCP）+ EventBridge → SQS）。
#                     Spark の検知が EventBridge → SQS で届き、エージェントが Neptune / OpenSearch / Prometheus を見て原因を調べて修復案を出し、
#                     Web の「承認」タブで人が承認すると Temporal が lab で直す。AGENT と PIPELINE（lab / stream / analytics）が要る
# 毎日全部消す運用向け。何度打っても同じ状態に収束する（できているものは Terraform が差分なしで飛ばし、ECR にあるタグはビルドしない）。
# あとから別の機能を 1 にして打ち直せば、その機能だけ足される（土台と他の機能は作り直さない）。
# Terraform の state はこの PC の展開したフォルダの中（terraform/<ルート>/terraform.tfstate）に置く。消すのは ops/down.sh。
#
# 使い方（展開したフォルダの直下で。先に AWS CLI の認証を通しておく。IAM ユーザーなら長期キーのまま打つ）:
#   cp deploy.env.example deploy.env  # 初回だけ。デプロイする人の名前 OWNER（必須）とどの機能を作るかを deploy.env に書く（機能を書かなければ AGENT=1 だけ）
#   ops/up.sh                         # deploy.env のとおりに作る。最後にポートフォワーディングを開いたまま止まる（Ctrl+C で閉じる）
#   PIPELINE=1 ops/up.sh              # その回だけ変える（環境変数は deploy.env より優先）。初回は PIPELINE で 40〜60 分（MSK の作成が長い）
#   DEPLOY_ENV_FILE=<パス> ops/up.sh  # 別の設定ファイルを読む
#
# どの機能も時間課金（目安は README の「作るもの」）。使い終わったら当日中に ops/down.sh を打つ。
# 機能を 0 にして打っても、前に作ったルートは消さない（消すのは ops/down.sh）。
#
# 設定できるキー（deploy.env か環境変数。**OWNER だけ必須**で、ほかは任意。意味は deploy.env.example、読み方は ops/deploy-env.sh）:
#   OWNER                   **必須。**デプロイする人の名前。英小文字で始まる 14 文字までの英小文字・数字・ハイフン（連続と末尾は不可）。
#                           リソース名の接頭辞と Project タグの値は <owner>-nwc-poc になり、owner タグには OWNER がそのまま入る。
#                           1 つの AWS アカウントを何人かで使うときに、自分の名前で自分のリソースを探せるようにするための値
#                           （AgentCore Runtime の名前はハイフンが使えないので、- を _ にした <接頭辞>_agent になる）。
#                           **作ったあとで変えると、Terraform は名前の違うリソースを作り直す**（先に ops/down.sh で消す）
#   AGENT=1                 agent での分析（既定 1）。terraform/agent を作る
#   PIPELINE=1              データパイプライン（既定 0）。lab / stream / analytics / graph を作る（SKIP_* で減らせる）
#   WORKFLOW=1              Temporal での実行（既定 0）。workflow を作る。AGENT と PIPELINE が要り、SKIP_LAB / SKIP_STREAM / SKIP_ANALYTICS は書けない
#   CREATE_KB=1             AGENT=1 で Knowledge Base（OpenSearch Serverless。+$0.36/h）も作る（既定 0）
#   SKIP_LAB=1              PIPELINE=1 で lab を作らない（stream は lab が要るので SKIP_STREAM=1 も要る）
#   SKIP_STREAM=1           PIPELINE=1 で stream と analytics（stream の Kafka を読む）を作らない
#   SKIP_ANALYTICS=1        PIPELINE=1 で analytics（Spark → S3 Tables / OpenSearch / Prometheus と異常検知）を作らない。「異常一覧」は使えない
#   SINK_S3=0 / SINK_OPENSEARCH=0 / SINK_PROMETHEUS=0
#                           analytics の Spark の格納先を 1 つずつ外す（既定は 3 つとも 1。0 にするとリソースごと作らない。1 つ以上は要る）。
#                           SINK_S3 = 全トピック → S3 Tables（Iceberg。MSK Connect の S3 sink の CREATE_S3_SINK とは別物）、SINK_OPENSEARCH = traps と logs（FRR のログ）→ OpenSearch Serverless、
#                           SINK_PROMETHEUS = metrics → Amazon Managed Service for Prometheus。terraform/pipeline/analytics の var.sinks（iceberg / opensearch / prometheus）に組んで渡す。
#   SKIP_GRAPH=1            PIPELINE=1 で graph（Neptune）を作らない
#   CREATE_S3_SINK=0        MSK Connect の S3 sink を作らない（Confluent の zip が取れないとき。ops/down.sh は state を見て合わせる）
#   IMAGE_TAG               エージェント（WORKFLOW=1 ではワーカーも）のイメージのタグ。既定 v1。ECR にそのタグが無いときだけ PC の docker buildx でビルドして push する（タグは上書きできない）
#   ADMIN_ARN               terraform/agent の kb_admin_principal_arn（CREATE_KB=1 のとき）。既定は空（Terraform が今の認証情報から決める）
#   VPC_CIDR                terraform/base/core の vpc_cidr（社内と重なるとき）
#   CLIENT_CIDR             terraform/base/core の client_cidr（DX / VPN 経由のとき）
#   OPENSEARCH_CACERT_FILE  terraform/agent の opensearch_cacert_file（社内の SSL 検査の CA の PEM）。既定は AWS_CA_BUNDLE と同じ
#   LOCAL_PORT              PC 側のポート。既定 8080
#   NO_PORTFORWARD=1        ポートフォワーディングを開かずに終わる
#   TF_VERBOSE=1            terraform の出力を全部画面に出す（既定は進みと結果だけ。全文は ops/logs/tf-<ルート>-apply.log）
#   AWS_PROFILE / AWS_CA_BUNDLE  AWS CLI と terraform がそのまま読む
# AGENT / PIPELINE / WORKFLOW / CREATE_KB / SKIP_* / SINK_* / NO_PORTFORWARD は 1 / 0 のほか true / false、yes / no でも書ける（CREATE_S3_SINK と ops/down.sh の KEEP_ECR は 1 か 0 だけ）。
#
# 手順 6（利用者への権限）は人に渡す作業なので入れていない。
set -euo pipefail

REGION=ap-northeast-1
# デプロイする人の名前 OWNER は deploy.env に書くので、OWNER と接頭辞 PREFIX=<owner>-nwc-poc が確定するのは
# load_deploy_env のあと（手順 0 の resolve_name_prefix。必須なので、無ければそこで止まる。形の検査も ops/deploy-env.sh）
# 下の 5 つは Terraform の変数の既定値に合わせてある（terraform/pipeline/lab の *_image_tag / containerlab_version / telegraf_version、
# terraform/pipeline/stream の s3_sink_plugin_key）。変えるときは両方を変える
FRR_TAG=10.2.1
MULTITOOL_TAG=v0.10.0
SNMPD_TAG=v1
CONTAINERLAB_VERSION=0.79.0
TELEGRAF_VERSION=1.40.0
CONTAINERLAB_RPM="containerlab_${CONTAINERLAB_VERSION}_linux_arm64.rpm"
TELEGRAF_RPM="telegraf-${TELEGRAF_VERSION}-1.aarch64.rpm"
S3_SINK_ZIP=confluentinc-kafka-connect-s3-12.1.11.zip
S3_SINK_URL="https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-s3/versions/12.1.11/$S3_SINK_ZIP"
# analytics の Spark ジョブに足す jar（Maven Central。2026-09-17 に 6 本とも取れることを確認）。EMR Serverless 7.13.0 の Spark 3.5.6 に合わせてある。
# terraform/pipeline/analytics の emr_release_label を変えるときは spark-sql-kafka とその依存（kafka-clients / commons-pool2 は spark-sql-kafka の pom の版）も変える
JARS_DIR=jars
MAVEN=https://repo1.maven.org/maven2
SPARK_VERSION=3.5.6
JAR_URLS=(
  "$MAVEN/org/apache/spark/spark-sql-kafka-0-10_2.12/$SPARK_VERSION/spark-sql-kafka-0-10_2.12-$SPARK_VERSION.jar"
  "$MAVEN/org/apache/spark/spark-token-provider-kafka-0-10_2.12/$SPARK_VERSION/spark-token-provider-kafka-0-10_2.12-$SPARK_VERSION.jar"
  "$MAVEN/org/apache/kafka/kafka-clients/3.4.1/kafka-clients-3.4.1.jar"
  "$MAVEN/org/apache/commons/commons-pool2/2.11.1/commons-pool2-2.11.1.jar"
  "$MAVEN/software/amazon/msk/aws-msk-iam-auth/2.3.2/aws-msk-iam-auth-2.3.2-all.jar"
  "$MAVEN/software/amazon/s3tables/s3-tables-catalog-for-iceberg-runtime/0.1.8/s3-tables-catalog-for-iceberg-runtime-0.1.8.jar"
)
SPARK_SCRIPT=spark/snmp_sinks.py

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
  printf '[profile %s-terraform]\ncredential_process = env -u AWS_PROFILE AWS_CONFIG_FILE=%q aws configure export-credentials %s --format process\n' \
    "$PREFIX" "${AWS_CONFIG_FILE:-$HOME/.aws/config}" "$profile_opt" >"$TF_AWS_CONFIG"
  TF_AWS_ENV=(AWS_CONFIG_FILE="$TF_AWS_CONFIG" AWS_PROFILE="$PREFIX-terraform")
  echo "terraform の認証情報: プロファイル ${AWS_PROFILE:-（既定）} を AWS CLI 経由（credential_process）で渡す"
}
tf() {  # tf <ルート> <terraform のサブコマンドと引数…>
  local root="$1"; shift
  env ${TF_AWS_ENV[@]+"${TF_AWS_ENV[@]}"} terraform -chdir="terraform/$root" "$@"
}
tf_init() {  # tf_init <ルート>
  tf "$1" init -input=false >/dev/null \
    || die "terraform/$1 の init に失敗した（provider の取得。社内 PC は docs/setup.md「社内 PC の CA」）"
}
tf_apply_only() {  # tf_apply_only <ルート> [-var 名前=値 …]  init 済みのルートを apply する
  local root="$1"; shift
  tf_logged "$root" apply -input=false -auto-approve -var "owner=$OWNER" "$@" \
    || die "terraform/$root の apply に失敗した（上のエラー。全文は $(tf_log_file "$root" apply)。docs/troubleshooting.md の「うまくいかないとき」。直したらもう一度 ops/up.sh）"
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
  die "$1 が 10 分たっても Session Manager に Online にならない（docs/troubleshooting.md の「画面に入れない」）"
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
fetch() {  # fetch <URL> <ファイル名>  展開したフォルダの直下に無いときだけ取る。翌日からは取り直さない
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
    printf '\n%s\n' "terraform/pipeline/graph の apply がまだ動いているので、終わるまで待つ（ログ: ${GRAPH_LOG}）。このターミナルは閉じない" >&2
    wait "$GRAPH_PID" || true
  fi
  if [ -n "$TF_AWS_CONFIG" ]; then rm -f "$TF_AWS_CONFIG"; fi
}
trap on_exit EXIT

# ---- 0. 道具と認証 -------------------------------------------------------------
log "0. 設定と道具と認証を確かめる"
load_deploy_env
resolve_name_prefix  # OWNER（必須。terraform の -var owner にそのまま渡す。下の tf_apply_only）と接頭辞 PREFIX=<owner>-nwc-poc
log "   デプロイする人の名前: ${OWNER}（リソース名の接頭辞と Project タグは ${PREFIX}）"
IMAGE_TAG="${IMAGE_TAG:-v1}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
CREATE_S3_SINK="${CREATE_S3_SINK:-1}"
case "$CREATE_S3_SINK" in
  0|1) ;;
  *) die "CREATE_S3_SINK は 1（作る。既定）か 0（作らない）（いまは「${CREATE_S3_SINK}」）。まだ何も作っていない" ;;
esac
# analytics の Spark の格納先。SINK_S3 / SINK_OPENSEARCH / SINK_PROMETHEUS を 1 / 0 で書く（既定は 3 つとも 1）。
# 0 にした格納先は Spark が書かないだけでなく、リソースも作らない。
# terraform/pipeline/analytics の var.sinks（list）に渡すので、["iceberg","opensearch"] の形に組む（SINKS_TF）
SINK_S3="${SINK_S3:-1}"; SINK_OPENSEARCH="${SINK_OPENSEARCH:-1}"; SINK_PROMETHEUS="${SINK_PROMETHEUS:-1}"
flag_value SINK_S3; flag_value SINK_OPENSEARCH; flag_value SINK_PROMETHEUS
SINKS=""
if [ -n "$SINK_S3" ]; then SINKS="iceberg"; fi
if [ -n "$SINK_OPENSEARCH" ]; then SINKS="$SINKS${SINKS:+,}opensearch"; fi
if [ -n "$SINK_PROMETHEUS" ]; then SINKS="$SINKS${SINKS:+,}prometheus"; fi
[ -n "$SINKS" ] || die "SINK_S3 / SINK_OPENSEARCH / SINK_PROMETHEUS が全部 0。Spark のジョブは格納先が 1 つ以上要る。analytics ごと要らないなら SKIP_ANALYTICS=1。まだ何も作っていない"
SINKS_TF="\"$(printf '%s' "$SINKS" | sed 's/,/","/g')\""
flag_value SKIP_LAB; flag_value SKIP_STREAM; flag_value SKIP_ANALYTICS; flag_value SKIP_GRAPH; flag_value NO_PORTFORWARD
# どの機能を作るか（既定は土台 + AGENT）
AGENT="${AGENT:-1}"
flag_value AGENT; flag_value PIPELINE; flag_value WORKFLOW; flag_value CREATE_KB
if [ -n "$WORKFLOW" ]; then
  if [ -z "$AGENT" ]; then
    die "WORKFLOW は AGENT が要る（ワーカーがエージェントの Runtime を呼ぶ。terraform/workflow は terraform/agent の state から ARN を読む）。AGENT=1 にする。まだ何も作っていない"
  fi
  if [ -z "$PIPELINE" ]; then
    die "WORKFLOW は PIPELINE が要る（analytics の Spark が異常を検知して EventBridge に出し、ワーカーが stream の異常テーブルを読み、lab の EC2 で直す）。PIPELINE=1 にする。まだ何も作っていない"
  fi
  if [ -n "$SKIP_LAB" ] || [ -n "$SKIP_STREAM" ] || [ -n "$SKIP_ANALYTICS" ]; then
    die "WORKFLOW は lab と stream と analytics が要る。SKIP_LAB / SKIP_STREAM / SKIP_ANALYTICS を外す。まだ何も作っていない"
  fi
fi
if [ -n "$PIPELINE" ]; then
  if [ -n "$SKIP_LAB" ] && [ -z "$SKIP_STREAM" ]; then
    die "stream は lab が要る（Telegraf の EC2 は terraform/pipeline/lab が作り、機器へは lab の EC2 を通って届く。terraform/pipeline/stream は lab の state から Telegraf の SG とロールを読む）。SKIP_LAB を外すか SKIP_STREAM=1 も書く。まだ何も作っていない"
  fi
  if [ -n "$SKIP_STREAM" ] && [ -z "$SKIP_ANALYTICS" ]; then
    echo "SKIP_STREAM=1 なので analytics も作らない（読む Kafka が無い）"
    SKIP_ANALYTICS=1
  fi
  if [ -n "$SKIP_LAB" ] && [ -n "$SKIP_GRAPH" ]; then
    echo "SKIP_LAB と SKIP_STREAM と SKIP_GRAPH があるので、PIPELINE=1 でも土台だけになる"
  fi
else
  SKIP_LAB=1; SKIP_STREAM=1; SKIP_ANALYTICS=1; SKIP_GRAPH=1
fi
if [ -z "$AGENT" ] && [ -n "$CREATE_KB" ]; then
  echo "AGENT=0 なので CREATE_KB は効かない（Knowledge Base は agent の一部）"
  CREATE_KB=""
fi
if [ -z "$AGENT$PIPELINE$WORKFLOW" ]; then
  echo "機能が全部 0 なので土台（base/ecr + base/core）だけ作る（Web は「チャット」で「配備されていない」と返す）"
fi
command -v aws >/dev/null || die "aws CLI が無い（docs/setup.md「Terraform を打つ PC 側」）"
command -v terraform >/dev/null || die "terraform が無い（docs/setup.md「Terraform を打つ PC 側」。1.11 以上）"
if command -v python3 >/dev/null; then PY=(python3)
elif command -v uv >/dev/null; then PY=(uv run --python 3.13 python)
else die "python3 も uv も無い（docs/setup.md「Terraform を打つ PC 側」）"; fi
if [ -z "$SKIP_LAB" ] || [ -z "$SKIP_ANALYTICS" ]; then command -v curl >/dev/null || die "curl が無い（lab の rpm と analytics の jar を取るのに使う。sudo apt install curl）"; fi
# docker はイメージ（agent / lab の 3 つ / worker / temporal）を ECR に置くときだけ要る。土台だけなら要らない
NEED_DOCKER="$AGENT$WORKFLOW"; if [ -z "$SKIP_LAB" ]; then NEED_DOCKER=1; fi
if [ -n "$NEED_DOCKER" ]; then
  command -v docker >/dev/null || die "docker が無い（イメージのビルドに使う。docs/setup.md「Terraform を打つ PC 側」）"
  docker buildx version >/dev/null 2>&1 || die "docker buildx が無い（Ubuntu の docker.io には入っていない。docs/setup.md「Terraform を打つ PC 側」）"
fi
# 最後のポートフォワーディング（手順 10）で要る。40〜60 分かけた後で落ちないよう、ここで見る
if [ -z "$NO_PORTFORWARD" ]; then
  command -v session-manager-plugin >/dev/null || die "Session Manager plugin が無い（手順 10 のポートフォワーディングに使う。docs/setup.md「Terraform を打つ PC 側」。開かないなら NO_PORTFORWARD=1）"
fi
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text) || die "認証が通っていない（aws configure か aws login で入り直す）"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
case "$CALLER_ARN" in
  arn:aws:iam::*:user/*)
    if [ -n "${AWS_SESSION_TOKEN:-}" ]; then
      die "IAM ユーザーの一時セッション（get-session-token）で入っている。IAM の API が呼べないので、一時セッションを挟まず長期キーのまま入り直す"
    fi ;;
  arn:aws:sts::*:assumed-role/*) ;;
  *) if [ -n "$CREATE_KB" ] && [ -z "${ADMIN_ARN:-}" ]; then
       die "この認証情報の形（${CALLER_ARN}）では kb_admin_principal_arn を決められない。deploy.env に ADMIN_ARN=arn:aws:iam::<アカウント ID>:role/<ロール名> を書く"
     fi ;;
esac
tf_use_cli_credentials
CACERT="${OPENSEARCH_CACERT_FILE:-${AWS_CA_BUNDLE:-}}"
# 共用のエンドポイント（ecr.api / ecr.dkr / logs）は Runtime・lab の EC2（docker pull）・Spark（ドライバーのログ）・workflow の Fargate が使う。
# 2026-09-17 までは terraform/agent にあり、AGENT=0 PIPELINE=1 だと lab がイメージを取れず Spark のジョブも落ちた。使う機能が 1 つも無いときだけ作らない
SHARED_ENDPOINTS=""
if [ -n "$AGENT" ] || [ -z "$SKIP_LAB" ] || [ -z "$SKIP_ANALYTICS" ]; then SHARED_ENDPOINTS=1; fi
ROOTS="base/ecr base/core"
if [ -n "$AGENT" ]; then ROOTS="$ROOTS agent"; fi
if [ -z "$SKIP_LAB" ]; then ROOTS="$ROOTS pipeline/lab"; fi
if [ -z "$SKIP_STREAM" ]; then ROOTS="$ROOTS pipeline/stream"; fi
if [ -z "$SKIP_ANALYTICS" ]; then ROOTS="$ROOTS pipeline/analytics"; fi
if [ -z "$SKIP_GRAPH" ]; then ROOTS="$ROOTS pipeline/graph"; fi
if [ -n "$WORKFLOW" ]; then ROOTS="$ROOTS workflow"; fi
echo "ACCOUNT_ID=$ACCOUNT_ID"
echo "CALLER_ARN=$CALLER_ARN"
echo "IMAGE_TAG=$IMAGE_TAG"
echo "AGENT=${AGENT:-0} PIPELINE=${PIPELINE:-0} WORKFLOW=${WORKFLOW:-0} CREATE_KB=${CREATE_KB:-0}"
echo "作るルート: $ROOTS"
# 待機時の 1 時間あたりの目安（セント。東京リージョンの税抜。単価は 2026-09-14〜15 に Price List API で確認。README の「作るもの」と docs/deploy.md の金額はここから出している）。
# 土台 = 5（ssm / ssmmessages のエンドポイント 2 本 + Web の EC2）
#   + 共用のエンドポイント 8（ecr.api / ecr.dkr / logs の 3 本 × 2 AZ。AGENT か lab か analytics を作るときだけ。2026-09-18 に agent から土台へ移した）、
# agent = 5（bedrock-runtime × 2 AZ + bedrock-agentcore 1 本）
#   + CREATE_KB なら 36（OpenSearch Serverless の OCU 33 + bedrock-agent-runtime のエンドポイント 3）、
# lab = 9、graph = 14、stream = 71（MSK Connect の S3 sink 無しなら 57）+ Telegraf の EC2 1（terraform/pipeline/lab が作る t4g.micro。
#   公表単価 $0.0108/h からで、Price List API では確かめていない）、
# analytics = 17（ストリーミングのジョブが動いている間の EMR Serverless の 2 vCPU + 異常検知の events エンドポイント 2 本。単価は 2026-09-17 に確認）
#   + SINK_S3 なら 3（s3tables のエンドポイント 2 本。テーブルは無料）
#   + SINK_PROMETHEUS なら 3（aps-workspaces のエンドポイント 2 本。取り込みのサンプル課金は別）
#   + SINK_OPENSEARCH なら 33（logs コレクションの OCU。KB のコレクションと共有されるか確認できていないので最大値で数える。
#     共有されれば 0 に近づく）、
# workflow = 6（Fargate ARM 1 vCPU / 2 GB のタスク 1 つ + sqs エンドポイント 1 本。Gateway と Lambda と DynamoDB と SQS は使った分だけ。単価は 2026-09-17 に確認）。
# ここを変えたら README の「作るもの」と docs/deploy.md の金額も変える
COST_CENTS=5
if [ -n "$SHARED_ENDPOINTS" ]; then COST_CENTS=$((COST_CENTS + 8)); fi
if [ -n "$AGENT" ]; then
  COST_CENTS=$((COST_CENTS + 5))
  if [ -n "$CREATE_KB" ]; then COST_CENTS=$((COST_CENTS + 36)); fi
fi
if [ -z "$SKIP_LAB" ]; then COST_CENTS=$((COST_CENTS + 9)); fi
if [ -z "$SKIP_GRAPH" ]; then COST_CENTS=$((COST_CENTS + 14)); fi
if [ -z "$SKIP_STREAM" ]; then
  # MSK は kafka.m5.large × 2 で 0.542（Kafka 4 は t3.small を受け付けない。2026-09-18）。MSK Connect 1 MCU で +0.14
  if [ "$CREATE_S3_SINK" = 1 ]; then COST_CENTS=$((COST_CENTS + 71)); else COST_CENTS=$((COST_CENTS + 57)); fi
  COST_CENTS=$((COST_CENTS + 1))   # Telegraf の EC2
fi
if [ -z "$SKIP_ANALYTICS" ]; then
  COST_CENTS=$((COST_CENTS + 17))
  if [ -n "$SINK_S3" ]; then COST_CENTS=$((COST_CENTS + 3)); fi
  if [ -n "$SINK_PROMETHEUS" ]; then COST_CENTS=$((COST_CENTS + 3)); fi
  if [ -n "$SINK_OPENSEARCH" ]; then COST_CENTS=$((COST_CENTS + 33)); fi
fi
if [ -n "$WORKFLOW" ]; then COST_CENTS=$((COST_CENTS + 6)); fi
COST_NOTE=$(printf '待機だけで約 $%d.%02d/h（約 %d 円/h。チャットの分は別）の時間課金。使い終わったら当日中に ops/down.sh を打つ' \
  $((COST_CENTS / 100)) $((COST_CENTS % 100)) $(((COST_CENTS * 150 + 50) / 100)))
printf '\033[1;33m%s\033[0m\n' "$COST_NOTE"
case ",$SINKS," in
  *,opensearch,*) if [ -z "$SKIP_ANALYTICS" ]; then printf '\033[1;33m%s\033[0m\n' "SINK_OPENSEARCH=1（既定）: OpenSearch Serverless の logs コレクションを作る。OCU が KB のコレクションと共有されなければ最大 \$0.33/h で、上の目安はそれを含んでいる"; fi ;;
esac

# ---- 1. ECR --------------------------------------------------------------------
log "1. ECR リポジトリ（terraform/base/ecr）"
tf_apply base/ecr
REPO=$(tf base/ecr output -raw agent_repository_url); echo "REPO=$REPO"
REG="${REPO%%/*}"
TEMPORAL_TAG=1.9.1   # terraform/workflow の temporal_image_tag の既定値。変えるときは両方を変える

# ---- 2. イメージ ----------------------------------------------------------------
log "2. イメージ（ECR に無いタグだけ作る）"
NEED_AGENT=""; NEED_FRR=""; NEED_MULTITOOL=""; NEED_SNMPD=""; NEED_WORKER=""; NEED_TEMPORAL=""
if [ -n "$AGENT" ]; then
  if ecr_has "$PREFIX-agent" "$IMAGE_TAG"; then echo "agent:$IMAGE_TAG はある（作り直すなら IMAGE_TAG を変える）"; else NEED_AGENT=1; fi
fi
if [ -z "$SKIP_LAB" ]; then
  if ecr_has "$PREFIX-lab-frr" "$FRR_TAG"; then echo "lab-frr:$FRR_TAG はある"; else NEED_FRR=1; fi
  if ecr_has "$PREFIX-lab-multitool" "$MULTITOOL_TAG"; then echo "lab-multitool:$MULTITOOL_TAG はある"; else NEED_MULTITOOL=1; fi
  if ecr_has "$PREFIX-lab-snmpd" "$SNMPD_TAG"; then echo "lab-snmpd:$SNMPD_TAG はある"; else NEED_SNMPD=1; fi
fi
if [ -n "$WORKFLOW" ]; then
  if ecr_has "$PREFIX-worker" "$IMAGE_TAG"; then echo "worker:$IMAGE_TAG はある"; else NEED_WORKER=1; fi
  if ecr_has "$PREFIX-temporal" "$TEMPORAL_TAG"; then echo "temporal:$TEMPORAL_TAG はある"; else NEED_TEMPORAL=1; fi
fi
NEED_LAB="$NEED_FRR$NEED_MULTITOOL$NEED_SNMPD"
if [ -z "$NEED_AGENT$NEED_LAB$NEED_WORKER$NEED_TEMPORAL" ]; then
  echo "作るイメージは無い"
else
  docker info >/dev/null 2>&1 || die "dockerd に接続できない（WSL なら sudo service docker start。docs/setup.md「Terraform を打つ PC 側」）"
  # agent と snmpd は RUN があるので、x86_64 の PC では QEMU（binfmt）が要る
  if [ -n "$NEED_AGENT$NEED_SNMPD$NEED_WORKER" ] && ! docker buildx ls | grep -q 'linux/arm64'; then
    die "docker buildx ls の Platforms に linux/arm64 が無い（docs/setup.md「WSL2（Ubuntu）」の binfmt の行）"
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
    docker pull --platform linux/arm64 "ghcr.io/srl-labs/network-multitool:$MULTITOOL_TAG"
    docker tag "ghcr.io/srl-labs/network-multitool:$MULTITOOL_TAG" "$REG/$PREFIX-lab-multitool:$MULTITOOL_TAG"
    docker push "$REG/$PREFIX-lab-multitool:$MULTITOOL_TAG"
  fi
  if [ -n "$NEED_SNMPD" ]; then
    docker buildx build --platform linux/arm64 -t "$REG/$PREFIX-lab-snmpd:$SNMPD_TAG" --push lab/snmpd/
  fi
  if [ -n "$NEED_WORKER" ]; then
    docker buildx build --platform linux/arm64 -t "$REG/$PREFIX-worker:$IMAGE_TAG" --push workflow/
  fi
  if [ -n "$NEED_TEMPORAL" ]; then
    # Temporal の CLI 入りイメージ（temporal server start-dev。arm64 あり）。Fargate は ECR からしか安定して引けないのでミラーする
    docker pull --platform linux/arm64 "temporalio/temporal:$TEMPORAL_TAG"
    docker tag "temporalio/temporal:$TEMPORAL_TAG" "$REG/$PREFIX-temporal:$TEMPORAL_TAG"
    docker push "$REG/$PREFIX-temporal:$TEMPORAL_TAG"
  fi
fi

# ---- 3. 本体 --------------------------------------------------------------------
log "3. 土台（terraform/base/core。VPC / Web の EC2 / バケット / ロール。初回は 3〜5 分）"
MAIN_VARS=()
if [ -n "${VPC_CIDR:-}" ];    then MAIN_VARS+=(-var "vpc_cidr=$VPC_CIDR"); fi
if [ -n "${CLIENT_CIDR:-}" ]; then MAIN_VARS+=(-var "client_cidr=$CLIENT_CIDR"); fi
if [ -n "$SHARED_ENDPOINTS" ]; then MAIN_VARS+=(-var create_shared_endpoints=true); else MAIN_VARS+=(-var create_shared_endpoints=false); fi
# 2026-09-17 までの配置（ecr / logs のエンドポイントが terraform/agent にある）が残っていると、同じサービスのエンドポイントを
# 同じ VPC に 2 本は作れない（private DNS がぶつかる）ので base/core の apply が落ちる。作る前に止める
if [ -n "$SHARED_ENDPOINTS" ] && [ -f terraform/agent/terraform.tfstate ]; then
  tf_init agent
  if tf agent state list 2>/dev/null | grep -qF 'aws_vpc_endpoint.runtime["ecr-api"]'; then
    die "terraform/agent に前の配置のエンドポイント（ecr.api / ecr.dkr / logs）が残っている。いまは土台（terraform/base/core）が作るので、ops/down.sh で一度消してから ops/up.sh を打ち直す（KEEP_ECR=1 ならイメージは残る）。まだ何も変えていない"
  fi
fi
tf_apply base/core ${MAIN_VARS[@]+"${MAIN_VARS[@]}"}
INSTANCE_ID=$(tf base/core output -raw web_instance_id)
KB_BUCKET=$(tf base/core output -raw kb_bucket_name)
echo "INSTANCE_ID=$INSTANCE_ID KB_BUCKET=$KB_BUCKET"

# graph は base/core の state しか読まないので、ここで裏で始めて待ち時間を重ねる（Neptune は 10〜15 分）
if [ -z "$SKIP_GRAPH" ]; then
  log "3-2. graph（Neptune）の apply を裏で始める（10〜15 分。待たずに次へ進む）"
  mkdir -p ops/logs
  tf_init pipeline/graph   # init は前で済ませる（provider のキャッシュを 2 つの init で同時に触らない）
  ( tf_apply_only pipeline/graph ) >"$GRAPH_LOG" 2>&1 &
  GRAPH_PID=$!
  echo "進み具合: tail -f $GRAPH_LOG"
fi

# ---- 3-3. agent ----------------------------------------------------------------
KB_ID=""; DS_ID=""; LOG_GROUP=""
if [ -n "$AGENT" ]; then
  if [ -n "$CREATE_KB" ]; then
    log "3-3. agent（terraform/agent。Runtime + ガードレール + Knowledge Base。初回は 10〜20 分。OpenSearch Serverless の作成が長い）"
  else
    log "3-3. agent（terraform/agent。Runtime + ガードレール + bedrock のエンドポイント。初回は 5〜10 分）"
  fi
  AGENT_VARS=(-var "agent_image_tag=$IMAGE_TAG")
  if [ -n "$CREATE_KB" ];       then AGENT_VARS+=(-var create_knowledge_base=true); fi
  if [ -n "${ADMIN_ARN:-}" ];   then AGENT_VARS+=(-var "kb_admin_principal_arn=$ADMIN_ARN"); fi
  if [ -n "$CACERT" ];          then AGENT_VARS+=(-var "opensearch_cacert_file=$CACERT"); fi
  tf_apply agent "${AGENT_VARS[@]}"
  LOG_GROUP=$(tf agent output -raw runtime_log_group_name)
  if [ -n "$CREATE_KB" ]; then
    KB_ID=$(tf agent output -raw knowledge_base_id)
    DS_ID=$(tf agent output -raw data_source_id)
    echo "KB_ID=$KB_ID DS_ID=$DS_ID"
  fi
  echo "Runtime の ARN は SSM の $(tf agent output -raw runtime_arn_parameter_name) に置いた（Web は 60 秒以内に拾う）"
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
# app.py が import する web/ の .py（chat / config / incident_view / topology_view）も全部置く。app.py だけだと Web が起動のたびに落ちる
for f in web/*.py; do aws s3 cp --only-show-errors "$f" "s3://$KB_BUCKET/web/${f#web/}"; done
aws s3 cp --only-show-errors web/requirements.txt "s3://$KB_BUCKET/web/requirements.txt"
for f in toolkit topology anomalies graph proposals; do aws s3 cp --only-show-errors "agent/$f.py" "s3://$KB_BUCKET/web/$f.py"; done
aws s3 cp --only-show-errors agent/data/ "s3://$KB_BUCKET/web/data/" --recursive
aws s3 sync --only-show-errors wheels/ "s3://$KB_BUCKET/web/wheels/"

if [ -n "$CREATE_KB" ]; then
log "4-3. 手順書を置いて取り込む（CREATE_KB=1）"
aws s3 cp --only-show-errors kb-docs/ "s3://$KB_BUCKET/docs/" --recursive --exclude "*" --include "*.md"
# 索引を作った直後は StartIngestionJob が「no such index」の ValidationException を返す（OpenSearch Serverless 側の反映待ち。
# 2026-09-17 に索引の置き換えの 2 秒後で実測）。10 秒おきに最大 12 回（2 分）まで打ち直す
JOB_ID=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  JOB_ID=$(aws bedrock-agent start-ingestion-job --region "$REGION" \
    --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
    --query ingestionJob.ingestionJobId --output text 2>"${TMPDIR:-/tmp}/ingest-err.$$") && break
  if grep -q "no such index" "${TMPDIR:-/tmp}/ingest-err.$$"; then
    echo "索引がまだ見えない（${i} 回目）。10 秒待つ"; sleep 10; JOB_ID=""
  else
    cat "${TMPDIR:-/tmp}/ingest-err.$$" >&2; rm -f "${TMPDIR:-/tmp}/ingest-err.$$"; die "取り込みジョブを開始できない"
  fi
done
rm -f "${TMPDIR:-/tmp}/ingest-err.$$"
[ -n "$JOB_ID" ] || die "索引が 2 分たっても見えない。aws opensearchserverless で kb コレクションの状態を確かめる"
while :; do
  ST=$(aws bedrock-agent get-ingestion-job --region "$REGION" \
    --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" --ingestion-job-id "$JOB_ID" \
    --query 'ingestionJob.[status,statistics.numberOfDocumentsFailed]' --output text)
  case "$ST" in
    COMPLETE*) echo "取り込み ${ST}（status failed）"; [ "${ST#COMPLETE}" = $'\t0' ] || die "取り込みに失敗した md がある。get-ingestion-job の failureReasons を見る"; break ;;
    FAILED*|STOPPED*) die "取り込みジョブが $ST" ;;
    *) sleep 10 ;;
  esac
done
fi

log "4-4. EC2 を再起動して Web を立てる（初回の apply 時点では web/ が無いため）"
wait_ssm_online "$INSTANCE_ID"
run_on_instance "$INSTANCE_ID" "true"          # 初回の user_data が終わるのを待ってから再起動する
aws ec2 reboot-instances --region "$REGION" --instance-ids "$INSTANCE_ID"
sleep 30
wait_ssm_online "$INSTANCE_ID"
# 起動の失敗（環境変数や依存の不足）は数秒後に落ちる。is-active は落ちて再起動するまでの数秒も active と読むので、Gradio が 8080 を
# 聞いているかで見る。2 分待って聞いていなければ status と journald を出して止まる
WEB_ACTIVE="for i in \$(seq 1 24); do ss -ltn 'sport = :8080' | grep -q LISTEN && exit 0; sleep 5; done; systemctl --no-pager status $PREFIX-web.service; journalctl --no-pager -u $PREFIX-web.service -n 50; exit 1"
run_on_instance "$INSTANCE_ID" "$WEB_ACTIVE"
echo "Web が動いている"

# ---- 5. lab と stream の材料 --------------------------------------------------------
# lab の EC2 は起動のたびに s3://<バケット>/lab/ を、Telegraf の EC2 は s3://<バケット>/telegraf/ を読む。apply より前に置けば、最初の起動で入る（再起動が要らない）
# Telegraf の EC2（terraform/pipeline/lab の create_telegraf）は stream を作るときに作る。SKIP_STREAM=1 でも stream が残っていれば残す
# （stream が Telegraf のロールにポリシーを付けているので、先にロールを消せない。stream を消すのは ops/down.sh）
LAB_VARS=(-var create_telegraf=false)
if [ -z "$SKIP_LAB" ]; then
  if [ -z "$SKIP_STREAM" ]; then
    LAB_VARS=(-var create_telegraf=true)
  elif [ -f terraform/pipeline/stream/terraform.tfstate ]; then
    tf_init pipeline/stream
    if has_resources pipeline/stream; then
      echo "SKIP_STREAM=1 だが terraform/pipeline/stream が残っているので、Telegraf の EC2 は残す"
      LAB_VARS=(-var create_telegraf=true)
    fi
  fi
  log "5-1. lab の材料（containerlab の rpm とトポロジ）を s3://$KB_BUCKET/lab/ に置く"
  fetch "https://github.com/srl-labs/containerlab/releases/download/v$CONTAINERLAB_VERSION/$CONTAINERLAB_RPM" "$CONTAINERLAB_RPM" \
    || die "containerlab の rpm が取れない（社内 PC なら docs/setup.md「社内 PC の CA」）"
  aws s3 sync --only-show-errors lab/ "s3://$KB_BUCKET/lab/" --exclude "wanlab.clab.yml" --exclude "snmpd/certs/*"
  aws s3 cp --only-show-errors "$CONTAINERLAB_RPM" "s3://$KB_BUCKET/lab/"
  if [ "${LAB_VARS[1]}" = create_telegraf=true ]; then
    log "5-1b. Telegraf の材料（telegraf/ と Telegraf の rpm）を s3://$KB_BUCKET/telegraf/ に置く"
    fetch "https://dl.influxdata.com/telegraf/releases/$TELEGRAF_RPM" "$TELEGRAF_RPM" || die "Telegraf の rpm が取れない（社内 PC なら docs/setup.md「社内 PC の CA」）"
    aws s3 sync --only-show-errors telegraf/ "s3://$KB_BUCKET/telegraf/"
    aws s3 cp --only-show-errors "$TELEGRAF_RPM" "s3://$KB_BUCKET/telegraf/"
  fi
fi
STREAM_VARS=()
if [ -z "$SKIP_STREAM" ]; then
  if [ "$CREATE_S3_SINK" = 0 ]; then
    echo "CREATE_S3_SINK=0 なので S3 sink は作らない"
    STREAM_VARS+=(-var create_s3_sink=false)
  else
    log "5-2. S3 sink のプラグイン（Confluent の zip）を s3://$KB_BUCKET/stream/ に置く"
    if ! fetch "$S3_SINK_URL" "$S3_SINK_ZIP" || ! is_zip "$S3_SINK_ZIP"; then
      rm -f "$S3_SINK_ZIP"
      die "Confluent の zip が取れない（利用条件への同意が要るとページが返る）。ブラウザで $S3_SINK_URL を開いて取り、展開したフォルダの直下に $S3_SINK_ZIP の名前で置いて打ち直す。S3 sink が要らなければ deploy.env に CREATE_S3_SINK=0 を書いて打ち直す"
    fi
    aws s3 cp --only-show-errors "$S3_SINK_ZIP" "s3://$KB_BUCKET/stream/$S3_SINK_ZIP"
  fi
fi
if [ -z "$SKIP_ANALYTICS" ]; then
  log "5-3. Spark のスクリプトと jar（Kafka / MSK IAM / S3 Tables カタログ）を s3://$KB_BUCKET/analytics/ に置く"
  mkdir -p "$JARS_DIR"
  for url in "${JAR_URLS[@]}"; do
    fetch "$url" "$JARS_DIR/${url##*/}" || die "jar が取れない: $url （社内 PC なら docs/setup.md「社内 PC の CA」）"
  done
  "${PY[@]}" -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$SPARK_SCRIPT" || die "$SPARK_SCRIPT が Python として読めない"
  aws s3 cp --only-show-errors "$SPARK_SCRIPT" "s3://$KB_BUCKET/analytics/"
  aws s3 sync --only-show-errors "$JARS_DIR/" "s3://$KB_BUCKET/analytics/jars/" --exclude "*" --include "*.jar"
fi

# ---- 6. lab ---------------------------------------------------------------------
LAB_INSTANCE_ID=""; TELEGRAF_INSTANCE_ID=""; LAB_WARN=""
LAB_NODES=$(grep -c '^ *kind: linux' lab/wanlab.clab.yml.in)   # containerlab のノードの数（14）
if [ -z "$SKIP_LAB" ]; then
  log "6. lab（terraform/pipeline/lab。EC2 の中でトポロジが上がるまで 5 分ほど。${LAB_VARS[1]}）"
  tf_apply pipeline/lab "${LAB_VARS[@]}"
  LAB_INSTANCE_ID=$(tf pipeline/lab output -raw lab_instance_id); echo "LAB_INSTANCE_ID=$LAB_INSTANCE_ID"
  TELEGRAF_INSTANCE_ID=$(tf pipeline/lab output -raw telegraf_instance_id); echo "TELEGRAF_INSTANCE_ID=${TELEGRAF_INSTANCE_ID:-（作っていない）}"
fi

# ---- 7. stream ------------------------------------------------------------------
if [ -z "$SKIP_STREAM" ]; then
  log "7. stream（terraform/pipeline/stream。MSK の作成に 20〜30 分）"
  tf_apply pipeline/stream ${STREAM_VARS[@]+"${STREAM_VARS[@]}"}
fi

# ---- 7-2. lab と Telegraf の中を確かめる ------------------------------------------------
if [ -n "$LAB_INSTANCE_ID" ]; then
  # lab の EC2 の中を見る。ユニットの有無だけで見ると、イメージが取れずにトポロジが上がっていなくても「入っている」と読む（2026-09-17）
  log "7-2. lab のトポロジを確かめる"
  wait_ssm_online "$LAB_INSTANCE_ID"
  LAB_STATE=$(ssm_run "$LAB_INSTANCE_ID" "echo lab=\$(systemctl is-active $PREFIX-lab.service) containers=\$(docker ps -q --filter name=clab- | wc -l)" | tail -n 1) || LAB_STATE=""
  echo "lab の EC2: ${LAB_STATE:-（読めなかった）}"
  case " $LAB_STATE " in
    *" lab=active containers=$LAB_NODES "*) echo "トポロジは $LAB_NODES コンテナとも動いている" ;;
    *)
      LAB_WARN="lab のトポロジが上がっていない（${LAB_STATE:-状態を読めなかった}。$LAB_NODES コンテナが動いて lab=active になるはず）。SSM セッションで入り、sudo tail -n 50 /var/log/cloud-init-output.log と sudo journalctl -u $PREFIX-lab -n 50 --no-pager を見る（docs/pipeline.md の「動かないとき」）"
      printf '\033[1;33m%s\033[0m\n' "$LAB_WARN" ;;
  esac
  if [ -n "$TELEGRAF_INSTANCE_ID" ]; then
    # Telegraf の EC2 を lab の EC2 より後に作ったとき（lab の EC2 は作り直さない）は、lab の起動時の forward が Telegraf のアドレスを
    # 読めていない。何度打っても同じ規則になるので毎回打つ（トポロジが上がっていなければ lab.sh の up がまた打つ）
    log "7-2b. lab の EC2 から Telegraf の EC2 へ SNMP / trap / FRR のログを通す（lab forward）"
    ssm_run "$LAB_INSTANCE_ID" "[ ! -x /usr/local/bin/lab ] || /usr/local/bin/lab forward" \
      || printf '\033[1;33m%s\033[0m\n' "lab forward が失敗した。lab の EC2 で sudo lab forward-status を見る（docs/pipeline.md）"
    log "7-2c. Telegraf の EC2 を確かめる"
    wait_ssm_online "$TELEGRAF_INSTANCE_ID"
    TG_STATE=$(ssm_run "$TELEGRAF_INSTANCE_ID" "echo telegraf=\$(systemctl is-active $PREFIX-telegraf.service) unit=\$(systemctl cat $PREFIX-telegraf.service >/dev/null 2>&1 && echo yes || echo no)" | tail -n 1) || TG_STATE=""
    echo "Telegraf の EC2: ${TG_STATE:-（読めなかった）}"
    case " $TG_STATE " in
      *" unit=no "*)
        # telegraf/ と rpm を置く前に起動した EC2 には Telegraf が入っていない。起動のたびに入れるので再起動で足りる
        aws ec2 reboot-instances --region "$REGION" --instance-ids "$TELEGRAF_INSTANCE_ID"
        echo "Telegraf が無かったので Telegraf の EC2 を再起動した（起動時に rpm を入れる。数分）" ;;
      *" telegraf=active "*) echo "Telegraf は動いている" ;;
      *" unit=yes "*) echo "Telegraf は入っている（MSK のブローカーは 60 秒ごとに読み直すので、stream ができればつながる）" ;;
    esac
  fi
fi

# ---- 7-3. analytics ------------------------------------------------------------------
if [ -z "$SKIP_ANALYTICS" ]; then
  log "7-3. analytics（terraform/pipeline/analytics。EMR Serverless と格納先: ${SINKS}。数分）"
  # ドライバーのログは CloudWatch Logs へ出す（logs のエンドポイントは土台の共用のもの。analytics を作るなら必ずある）
  ANALYTICS_VARS=(-var "sinks=[$SINKS_TF]")
  tf_apply pipeline/analytics "${ANALYTICS_VARS[@]}"
  APP_ID=$(tf pipeline/analytics output -raw application_id); echo "APP_ID=$APP_ID"
  log "7-4. Spark のストリーミングジョブ（Kafka → ${SINKS}）を起こす（動いていれば何もしない）"
  RUNNING=$(aws emr-serverless list-job-runs --region "$REGION" --application-id "$APP_ID" \
    --states SUBMITTED PENDING SCHEDULED RUNNING --query 'jobRuns[].id' --output text)
  if [ -n "$RUNNING" ] && [ "$RUNNING" != None ]; then
    echo "ジョブが動いている（${RUNNING}）"
  else
    JOB_RUN_ID=$(aws emr-serverless start-job-run --region "$REGION" --application-id "$APP_ID" \
      --execution-role-arn "$(tf pipeline/analytics output -raw runtime_role_arn)" \
      --name snmp-sinks --mode STREAMING \
      --job-driver "$(tf pipeline/analytics output -raw job_driver_json)" \
      --configuration-overrides "$(tf pipeline/analytics output -raw configuration_overrides_json)" \
      --tags "Project=$PREFIX,owner=$OWNER" \
      --query jobRunId --output text)
    echo "JOB_RUN_ID=$JOB_RUN_ID （起動に 2〜5 分。様子は: $(tf pipeline/analytics output -raw list_job_runs_command)）"
  fi
fi

# ---- 8. graph と投入 --------------------------------------------------------------
if [ -n "$GRAPH_PID" ]; then
  log "8. graph の apply が終わるのを待つ"
  rc=0; wait "$GRAPH_PID" || rc=$?; GRAPH_PID=""
  if [ "$rc" -ne 0 ]; then
    tail -n 40 "$GRAPH_LOG" >&2
    die "terraform/pipeline/graph の apply に失敗した（全文: ${GRAPH_LOG}）。直したらもう一度 ops/up.sh"
  fi
  tail -n 3 "$GRAPH_LOG"
  log "8-2. Neptune が空なら lab の定義からトポロジを入れる（初期ロード。入っていれば何もしない。入れ直すのは ops/sync-graph.sh --replace）"
  # lab/lab_topology.py が lab/wanlab.clab.yml.in と lab/frr/*.conf から機器と回線を作り（手元で打つ）、ops/seed_graph.py を Web の EC2 の上で
  # Web と同じ環境変数と依存で動かして Neptune に入れる。コマンドに記号を入れないよう、スクリプトもトポロジも base64 で渡す
  LAB_TOPOLOGY_B64=$("${PY[@]}" lab/lab_topology.py lab | base64 | tr -d '\n') || die "lab/lab_topology.py が lab の定義を読めなかった"
  run_on_instance "$INSTANCE_ID" "echo $(base64 < ops/seed_graph.py | tr -d '\n') | base64 -d | NAME_PREFIX=$PREFIX LAB_TOPOLOGY_B64=$LAB_TOPOLOGY_B64 /usr/bin/python3.13 -"
fi
if [ -z "$SKIP_STREAM" ] || [ -z "$SKIP_GRAPH" ]; then
  log "8-3. Web を再起動する（起動時に SSM の異常テーブルと Neptune を読むため）"
  run_on_instance "$INSTANCE_ID" "systemctl restart $PREFIX-web.service; $WEB_ACTIVE"
  echo "Web が動いている"
fi

# ---- 8-5. workflow（機能 WORKFLOW）--------------------------------------------------------
if [ -n "$WORKFLOW" ]; then
  log "8-5. workflow（terraform/workflow。Temporal のワーカーと AgentCore Gateway。数分）"
  tf_apply workflow -var "worker_image_tag=$IMAGE_TAG"
  WF_CLUSTER=$(tf workflow output -raw cluster_name); WF_SERVICE=$(tf workflow output -raw service_name)
  echo "ECS のサービスが安定するのを待つ（イメージの取得と Temporal の起動。1〜3 分）"
  aws ecs wait services-stable --region "$REGION" --cluster "$WF_CLUSTER" --services "$WF_SERVICE"
  WF_TASK=$(aws ecs list-tasks --region "$REGION" --cluster "$WF_CLUSTER" --service-name "$WF_SERVICE" --query 'taskArns[0]' --output text)
  WF_TASK_IP=$(aws ecs describe-tasks --region "$REGION" --cluster "$WF_CLUSTER" --tasks "$WF_TASK" \
    --query 'tasks[0].attachments[0].details[?name==`privateIPv4Address`].value | [0]' --output text)
  echo "WF_TASK=${WF_TASK##*/} WF_TASK_IP=$WF_TASK_IP"
  echo "ワーカーのログ: $(tf workflow output -raw worker_logs_command)"
  echo "Temporal の UI（Web の EC2 経由でタスクの 8233 へ。PC の http://localhost:8233/ ）:"
  echo "  aws ssm start-session --region $REGION --target $INSTANCE_ID --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters '{\"host\":[\"$WF_TASK_IP\"],\"portNumber\":[\"8233\"],\"localPortNumber\":[\"8233\"]}'"
  log "8-6. Web を再起動する（起動時に SSM の修復案テーブルを読むため。エージェントは Gateway を 5 分以内に拾う）"
  run_on_instance "$INSTANCE_ID" "systemctl restart $PREFIX-web.service; $WEB_ACTIVE"
  echo "Web が動いている"
fi

# ---- 9. Runtime のロググループ -------------------------------------------------------
# AgentCore が最初の呼び出しで作るもので、Terraform の管理外。保持期間とタグだけ付け、ops/down.sh が消す
if [ -n "$AGENT" ]; then
  log "9. ロググループ ${LOG_GROUP}（保持 7 日 + タグ）"
  if ! aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7 2>/dev/null; then
    aws logs create-log-group --region "$REGION" --log-group-name "$LOG_GROUP"
    aws logs put-retention-policy --region "$REGION" --log-group-name "$LOG_GROUP" --retention-in-days 7
  fi
  aws logs tag-resource --region "$REGION" \
    --resource-arn "arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:$LOG_GROUP" \
    --tags "Project=$PREFIX,owner=$OWNER"
fi

# ---- 10. ポートフォワーディング -------------------------------------------------------------
log "できた（${ROOTS}）。利用者に配るコマンド:"
tf base/core output -raw start_session_command; echo
if [ -n "$LAB_INSTANCE_ID" ]; then
  echo "lab に入るコマンド:"
  tf pipeline/lab output -raw start_session_command; echo
fi
if [ -n "$TELEGRAF_INSTANCE_ID" ]; then
  echo "Telegraf に入るコマンド（sudo tg status）:"
  tf pipeline/lab output -raw telegraf_start_session_command; echo
fi
if [ -n "$LAB_WARN" ]; then printf '\033[1;33m%s\033[0m\n' "$LAB_WARN"; fi
printf '\033[1;33m%s\033[0m\n' "$COST_NOTE"
if [ -n "$NO_PORTFORWARD" ]; then exit 0; fi
log "10. ポートフォワーディング（http://localhost:$LOCAL_PORT/ 。Ctrl+C で閉じる）"
trap - EXIT
if [ -n "$TF_AWS_CONFIG" ]; then rm -f "$TF_AWS_CONFIG"; fi
exec aws ssm start-session --region "$REGION" --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
