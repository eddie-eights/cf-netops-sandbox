#!/usr/bin/env bash
# デバッグ用の EC2（lab + Telegraf を 1 台に。CloudFormation のスタック <接頭辞>-lab-debug。cloudformation/lab-debug.yaml）を作る・消す。
# MSK / ECS / NLB を作らずに、機器の設定（lab/）と Telegraf の設定（telegraf/）を確かめる。Telegraf の出力は標準出力（sudo lab telegraf logs -f）。
#
# 使い方（展開したフォルダの直下で。deploy.env は OWNER と NETWORK_PERIMETER だけ読む。先に AWS CLI の認証を通しておく）:
#   ops/lab-debug.sh up       # スタック（VPC・エンドポイント・バケット・ECR）を作り、イメージ（ECR に無いタグだけ）と lab/ を置いて EC2 を起こす・変える。最後に SSM で入るコマンドを出す
#   ops/lab-debug.sh sync     # lab/ を置き直して EC2 を再起動する（lab/ の設定を変えたとき。telegraf/ を変えたときは up）
#   ops/lab-debug.sh status   # スタックと EC2 の状態
#   ops/lab-debug.sh down     # バケットを空にしてスタックを消す（ECR はイメージごと消える）
#
# ops/up.sh / ops/down.sh / terraform とは独立（2026-10-04 ユーザー決定: CloudFormation だけで扱う）。up.sh で何も作っていなくても動き、
# down.sh はこれを消さない。VPC もエンドポイントもバケットも ECR もスタックが持つので、土台（terraform/base/core）は要らない。
# terraform/pipeline/lab の EC2 とずれないよう、版とイメージと lab/ の置き方は ops/lab-common.sh、EC2 の中の支度は lab/setup.sh を共有する。
# 待機の費用は約 $0.15/h（EC2 t4g.xlarge 約 $0.09/h + インターフェース型エンドポイント 4 本 $0.056/h）。使い終わったら down。
set -euo pipefail

REGION=ap-northeast-1
. "$(dirname "$0")/deploy-env.sh"
. "$(dirname "$0")/lab-common.sh"
resolve_deploy_env_file
cd "$(dirname "$0")/.."

log()  { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mNG: %s\033[0m\n' "$*" >&2; exit 1; }
TEMPLATE=cloudformation/lab-debug.yaml

CMD="${1:-}"
case "$CMD" in up|sync|status|down) ;; *) sed -n '2,10p' "$0"; exit 1 ;; esac
load_deploy_env
resolve_name_prefix
STACK="$PREFIX-lab-debug"
command -v aws >/dev/null || die "aws CLI が無い（docs/setup.md「Terraform を打つ PC 側」）"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || die "認証が通っていない（aws configure か aws login で入り直す）"
REG="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
# スタックが持つバケットと ECR のリポジトリの名前（テンプレートと同じ作り方。down は Outputs が読めなくても消せるよう、ここで作る）
BUCKET="$PREFIX-lab-debug-$ACCOUNT_ID"
REPO_PREFIX="$PREFIX-debug"   # <これ>-lab-srlinux / -lab-multitool / -telegraf
# VPC の外からの呼び出しを拒む Deny（ops/up.sh の NETWORK_PERIMETER と同じキー。既定 1）
NETWORK_PERIMETER="${NETWORK_PERIMETER:-1}"
flag_value NETWORK_PERIMETER

stack_status() {  # 無ければ空。読めなければ止まる（$(…) の中なので、呼ぶ側は s=$(stack_status) で受けて set -e で止める）
  cfn_stack_status "$STACK" || die "$STACK の状態が読めない（上の出力。認証が切れていれば aws login で入り直す）"
}
stack_output() {  # stack_output <キー>
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
deploy() {  # deploy <CreateInstance> [<TelegrafImageTag>]  版は ops/lab-common.sh から渡す（テンプレートの既定値と同じ。tests/test_lab_debug.py）
  aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file "$TEMPLATE" \
    --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
    --parameter-overrides \
      "NamePrefix=$PREFIX" "Owner=$OWNER" "CreateInstance=$1" "TelegrafImageTag=${2:-}" \
      "NetworkPerimeter=$([ -n "$NETWORK_PERIMETER" ] && echo true || echo false)" \
      "ContainerlabVersion=$CONTAINERLAB_VERSION" "SrlinuxImageTag=$SRLINUX_TAG" "MultitoolImageTag=$MULTITOOL_TAG" \
    --tags "Project=$PREFIX" "owner=$OWNER" \
    || die "$STACK を作れなかった（aws cloudformation describe-stack-events --region $REGION --stack-name $STACK）"
}

instance_id() {  # EC2 の ID。まだ無い（初回の器だけの状態）なら空。読めなければ 1（$(…) の中では set -e が効かないので明示する）
  local id; id=$(stack_output InstanceId) || return 1
  case "$id" in None) id="" ;; esac
  printf '%s' "$id"
}

case "$CMD" in
  status)
    s=$(stack_status)
    if [ -z "$s" ]; then echo "$STACK: 無い"; exit 0; fi
    id=$(instance_id) || die "$STACK の出力が読めない（上の出力）"
    if [ -z "$id" ]; then echo "$STACK: $s / EC2 はまだ無い（ops/lab-debug.sh up）"; exit 0; fi
    echo "$STACK: $s / EC2 $id: $(aws ec2 describe-instances --region "$REGION" --instance-ids "$id" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo '?')"
    stack_output StartSessionCommand
    ;;

  down)
    s=$(stack_status)
    if [ -z "$s" ]; then echo "$STACK: 無いので消さない"; exit 0; fi
    log "$STACK を消す（EC2・VPC とエンドポイント・バケット・ECR のイメージ。数分）"
    # CloudFormation は中身のあるバケットを消せないので先に空にする（ECR は EmptyOnDelete でスタックが消す）
    if aws s3api head-bucket --region "$REGION" --bucket "$BUCKET" 2>/dev/null; then
      aws s3 rm --only-show-errors --recursive "s3://$BUCKET" || die "s3://$BUCKET を空にできなかった（まだスタックは消していない）"
    fi
    aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK" \
      || die "$STACK が消えなかった（aws cloudformation describe-stack-events --region $REGION --stack-name $STACK）"
    echo "$STACK を消した"
    ;;

  sync)
    s=$(stack_status)
    id=""; if [ -n "$s" ]; then id=$(instance_id) || die "$STACK の出力が読めない（上の出力）"; fi
    [ -n "$id" ] || die "$STACK の EC2 が無い。先に ops/lab-debug.sh up"
    command -v curl >/dev/null || die "curl が無い（containerlab の rpm を取るのに使う）"
    log "lab/ を s3://$BUCKET/lab/ に置き直して EC2 を再起動する（起動のたびに lab/setup.sh が置き直す）"
    upload_lab "$BUCKET" || die "lab の材料を s3://$BUCKET/lab/ に置けなかった"
    aws ec2 reboot-instances --region "$REGION" --instance-ids "$id"
    echo "$id を再起動した。トポロジが上がるまで 10 分ほど（sudo lab status）"
    ;;

  up)
    if command -v python3 >/dev/null; then PY=(python3)
    elif command -v uv >/dev/null; then PY=(uv run --python 3.13 python)
    else die "python3 も uv も無い（docs/setup.md「Terraform を打つ PC 側」）"; fi
    command -v curl >/dev/null || die "curl が無い（containerlab の rpm を取るのに使う）"

    s=$(stack_status)
    case "$s" in
      # 初回の deploy が変更セットを作っただけで止まった形（中身は無い）。deploy は無いものとして作り直せる
      REVIEW_IN_PROGRESS) s="" ;;
      ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
        die "$STACK が $s。作り直せないので先に ops/lab-debug.sh down" ;;
      *_IN_PROGRESS)
        die "$STACK が $s（CloudFormation が作業中）。終わってから打ち直す" ;;
    esac
    if [ -z "$s" ]; then
      # イメージと lab/ の置き場がまだ無いので、EC2 の無い器（VPC・エンドポイント・バケット・ECR）を先に作る
      log "1. $STACK の器（VPC・エンドポイント 4 本・バケット・ECR。EC2 はまだ作らない。数分）"
      deploy false
    else
      # 2026-10-04 より前の形（土台 terraform/base/core の VPC とバケットを使っていた）は器を持たないので、作り直す
      rp=$(stack_output RepositoryPrefix) || die "$STACK の出力が読めない（上の出力）"
      case "$rp" in ''|None) die "$STACK が前の形（土台の VPC を使う）のまま。先に ops/lab-debug.sh down" ;; esac
      log "1. $STACK はある（$s）"
    fi

    log "2. イメージ（ECR に無いタグだけ作る。lab の 2 つと、stream の ECS と同じ作り方の Telegraf）"
    TELEGRAF_TAG=$(telegraf_tag) || die "telegraf/ のタグを作れなかった"
    LOGGED_IN=""
    login() {
      [ -z "$LOGGED_IN" ] || return 0
      command -v docker >/dev/null || die "docker が無い（イメージを ECR に置くのに使う。docs/setup.md「Terraform を打つ PC 側」）"
      docker info >/dev/null 2>&1 || die "dockerd に接続できない（WSL なら sudo service docker start）"
      aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
      LOGGED_IN=1
    }
    if ! ecr_has "$REPO_PREFIX-lab-srlinux" "$SRLINUX_TAG" || ! ecr_has "$REPO_PREFIX-lab-multitool" "$MULTITOOL_TAG"; then login; fi
    mirror_lab_images "$REG" "$REPO_PREFIX" || die "lab のイメージを ECR に置けなかった"
    if ecr_has "$REPO_PREFIX-telegraf" "$TELEGRAF_TAG"; then echo "telegraf:$TELEGRAF_TAG はある"
    else
      login
      docker buildx version >/dev/null 2>&1 || die "docker buildx が無い（docs/setup.md「Terraform を打つ PC 側」）"
      build_telegraf "$REG/$REPO_PREFIX-telegraf:$TELEGRAF_TAG"
    fi

    log "3. lab の材料（containerlab の rpm とトポロジ）を s3://$BUCKET/lab/ に置く"
    upload_lab "$BUCKET" || die "lab の材料を s3://$BUCKET/lab/ に置けなかった"

    log "4. $STACK の EC2（$TEMPLATE。初回は EC2 の中でトポロジが上がるまで 10 分ほど）"
    BEFORE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
      --query 'Stacks[0].LastUpdatedTime' --output text 2>/dev/null || true)
    deploy true "$TELEGRAF_TAG"
    AFTER=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
      --query 'Stacks[0].LastUpdatedTime' --output text 2>/dev/null || true)
    if [ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ]; then
      echo "スタックは変わらなかった。置き直した lab/ を EC2 に入れるなら ops/lab-debug.sh sync（再起動する）"
    fi

    log "できた。デバッグ用の EC2 に入るコマンド（中で sudo lab status / sudo lab check / sudo lab telegraf logs -f / sudo lab telegraf test）:"
    stack_output StartSessionCommand
    echo "使い終わったら ops/lab-debug.sh down（待機だけで約 \$0.15/h。ops/down.sh では消えない）"
    ;;
esac
