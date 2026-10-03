#!/usr/bin/env bash
# デバッグ用の EC2（lab + Telegraf を 1 台に。CloudFormation のスタック <接頭辞>-lab-debug。cloudformation/lab-debug.yaml）を作る・消す。
# MSK / ECS / NLB を作らずに、機器の設定（lab/）と Telegraf の設定（telegraf/）を確かめる。Telegraf の出力は標準出力（sudo lab telegraf logs -f）。
#
# 使い方（展開したフォルダの直下で。ops/up.sh と同じ deploy.env を読む。先に AWS CLI の認証を通しておく）:
#   ops/lab-debug.sh up       # イメージ（ECR に無いタグだけ）と lab/ を置き、スタックを作る・変える。最後に SSM で入るコマンドを出す
#   ops/lab-debug.sh sync     # lab/ を置き直して EC2 を再起動する（lab/ の設定を変えたとき。telegraf/ を変えたときは up）
#   ops/lab-debug.sh status   # スタックと EC2 の状態
#   ops/lab-debug.sh down     # スタックを消す（ops/down.sh も土台より先にこれを呼ぶ）
#
# 土台（terraform/base/core）が要り、ECR の ecr.api / ecr.dkr のインターフェース型エンドポイントも要る。
# deploy.env に LAB_DEBUG=1 を書いて ops/up.sh を打てば、エンドポイントを足して最後にこれ（up）を呼ぶ。
# terraform/pipeline/lab の EC2 とずれないよう、版とイメージと lab/ の置き方は ops/lab-common.sh、EC2 の中の支度は lab/setup.sh を共有する。
# 待機の費用は lab の EC2 と同じ約 $0.09/h（t4g.xlarge）。使い終わったら down か ops/down.sh。
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

stack_status() {  # 無ければ空。読めなければ止まる（$(…) の中なので、呼ぶ側は s=$(stack_status) で受けて set -e で止める）
  cfn_stack_status "$STACK" || die "$STACK の状態が読めない（上の出力。認証が切れていれば aws login で入り直す）"
}
stack_output() {  # stack_output <キー>
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
core_output() {  # core_output <名前>  terraform/base/core の output（state を読むだけ。AWS は呼ばない）
  terraform -chdir=terraform/base/core output -raw "$1" 2>/dev/null
}

case "$CMD" in
  status)
    s=$(stack_status)
    if [ -z "$s" ]; then echo "$STACK: 無い"; exit 0; fi
    id=$(stack_output InstanceId)
    echo "$STACK: $s / EC2 $id: $(aws ec2 describe-instances --region "$REGION" --instance-ids "$id" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo '?')"
    stack_output StartSessionCommand
    ;;

  down)
    s=$(stack_status)
    if [ -z "$s" ]; then echo "$STACK: 無いので消さない"; exit 0; fi
    log "$STACK を消す（EC2 とロール。数分）"
    aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK" \
      || die "$STACK が消えなかった（aws cloudformation describe-stack-events --region $REGION --stack-name $STACK）"
    echo "$STACK を消した"
    ;;

  sync|up)
    command -v terraform >/dev/null || die "terraform が無い（土台 terraform/base/core の output を読む）"
    if command -v python3 >/dev/null; then PY=(python3)
    elif command -v uv >/dev/null; then PY=(uv run --python 3.13 python)
    else die "python3 も uv も無い（docs/setup.md「Terraform を打つ PC 側」）"; fi
    command -v curl >/dev/null || die "curl が無い（containerlab の rpm を取るのに使う）"
    BUCKET=$(core_output kb_bucket_name) && [ -n "$BUCKET" ] \
      || die "terraform/base/core の output が読めない。先に土台を作る（deploy.env に LAB_DEBUG=1 を書いて ops/up.sh）"

    if [ "$CMD" = sync ]; then
      s=$(stack_status)
      [ -n "$s" ] || die "$STACK が無い。先に ops/lab-debug.sh up"
      log "lab/ を s3://$BUCKET/lab/ に置き直して EC2 を再起動する（起動のたびに lab/setup.sh が置き直す）"
      upload_lab "$BUCKET" || die "lab の材料を s3://$BUCKET/lab/ に置けなかった"
      id=$(stack_output InstanceId)
      aws ec2 reboot-instances --region "$REGION" --instance-ids "$id"
      echo "$id を再起動した。トポロジが上がるまで 10 分ほど（sudo lab status）"
      exit 0
    fi

    log "1. 土台（terraform/base/core の output）とエンドポイント"
    VPC_ID=$(core_output vpc_id)
    SUBNET_ID=$(core_output instance_subnet_id)
    SG_ID=$(terraform -chdir=terraform/base/core output -json security_group_ids \
      | "${PY[@]}" -c 'import json, sys; print(json.load(sys.stdin)["lab"])')
    PERIMETER=$(core_output network_perimeter_policy_arn || true)
    echo "VPC=$VPC_ID SUBNET=$SUBNET_ID SG(lab)=$SG_ID BUCKET=$BUCKET PERIMETER=${PERIMETER:-（無し。NETWORK_PERIMETER=0）}"
    # EC2 は VPC の外へ出られないので、イメージは ECR のエンドポイント越しに引く。無ければ docker pull が接続のタイムアウトで止まる
    n=$(aws ec2 describe-vpc-endpoints --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=vpc-endpoint-state,Values=available" \
                "Name=service-name,Values=com.amazonaws.$REGION.ecr.api,com.amazonaws.$REGION.ecr.dkr" \
      --query 'length(VpcEndpoints)' --output text)
    [ "$n" = 2 ] || die "VPC に ecr.api / ecr.dkr のエンドポイントが無い（$n 本）。deploy.env に LAB_DEBUG=1 を書いて ops/up.sh を打つ"

    log "2. イメージ（ECR に無いタグだけ作る。lab の 2 つと、stream の ECS と同じ Telegraf）"
    TELEGRAF_TAG=$(telegraf_tag) || die "telegraf/ のタグを作れなかった"
    LOGGED_IN=""
    login() {
      [ -z "$LOGGED_IN" ] || return 0
      command -v docker >/dev/null || die "docker が無い（イメージを ECR に置くのに使う。docs/setup.md「Terraform を打つ PC 側」）"
      docker info >/dev/null 2>&1 || die "dockerd に接続できない（WSL なら sudo service docker start）"
      aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
      LOGGED_IN=1
    }
    if ! ecr_has "$PREFIX-lab-srlinux" "$SRLINUX_TAG" || ! ecr_has "$PREFIX-lab-multitool" "$MULTITOOL_TAG"; then login; fi
    mirror_lab_images "$REG" "$PREFIX" || die "lab のイメージを ECR に置けなかった"
    if ecr_has "$PREFIX-telegraf" "$TELEGRAF_TAG"; then echo "telegraf:$TELEGRAF_TAG はある"
    else
      login
      docker buildx version >/dev/null 2>&1 || die "docker buildx が無い（docs/setup.md「Terraform を打つ PC 側」）"
      build_telegraf "$REG/$PREFIX-telegraf:$TELEGRAF_TAG"
    fi

    log "3. lab の材料（containerlab の rpm とトポロジ）を s3://$BUCKET/lab/ に置く"
    upload_lab "$BUCKET" || die "lab の材料を s3://$BUCKET/lab/ に置けなかった"

    log "4. $STACK（$TEMPLATE。初回は EC2 の中でトポロジが上がるまで 10 分ほど）"
    s=$(stack_status)
    case "$s" in
      ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
        die "$STACK が $s。作り直せないので先に ops/lab-debug.sh down" ;;
    esac
    BEFORE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
      --query 'Stacks[0].LastUpdatedTime' --output text 2>/dev/null || true)
    # ContainerlabVersion などの版は ops/lab-common.sh から渡す（テンプレートの既定値と同じ。tests/test_lab_debug.py）
    aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file "$TEMPLATE" \
      --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
      --parameter-overrides \
        "NamePrefix=$PREFIX" "Owner=$OWNER" "SubnetId=$SUBNET_ID" "SecurityGroupId=$SG_ID" "BucketName=$BUCKET" \
        "PerimeterPolicyArn=$PERIMETER" "ContainerlabVersion=$CONTAINERLAB_VERSION" "SrlinuxImageTag=$SRLINUX_TAG" \
        "MultitoolImageTag=$MULTITOOL_TAG" "TelegrafImageTag=$TELEGRAF_TAG" \
      --tags "Project=$PREFIX" "owner=$OWNER" \
      || die "$STACK を作れなかった（aws cloudformation describe-stack-events --region $REGION --stack-name $STACK）"
    AFTER=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
      --query 'Stacks[0].LastUpdatedTime' --output text 2>/dev/null || true)
    if [ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ]; then
      echo "スタックは変わらなかった。置き直した lab/ を EC2 に入れるなら ops/lab-debug.sh sync（再起動する）"
    fi

    log "できた。デバッグ用の EC2 に入るコマンド（中で sudo lab status / sudo lab check / sudo lab telegraf logs -f / sudo lab telegraf test）:"
    stack_output StartSessionCommand
    echo "使い終わったら ops/lab-debug.sh down（待機だけで約 \$0.09/h）"
    ;;
esac
