# lab の材料（イメージの版・containerlab の rpm・S3 に置く lab/）。ops/up.sh（terraform/pipeline/lab と stream の Telegraf）と
# ops/lab-debug.sh（cloudformation/lab-debug.yaml のデバッグ用の EC2。up.sh とは別のスタックで、バケットと ECR もスタックが持つ）が source する。版と作り方をここ 1 か所にして、2 つの EC2 がずれないようにする。
# 呼ぶ側が REGION と PY（python の起動の配列）を先に決めておく。
#
# SRLINUX_TAG / MULTITOOL_TAG / CONTAINERLAB_VERSION は terraform/pipeline/lab の変数の既定値（*_image_tag / containerlab_version）と
# cloudformation/lab-debug.yaml のパラメータの既定値に、TELEGRAF_VERSION は telegraf/Dockerfile の ARG の既定値に合わせてある。
# 変えるときは全部を変える（tests/test_lab_debug.py が見る）
SRLINUX_TAG=26.7.2   # ghcr.io/nokia/srlinux はマルチアーキ。arm64 を引く
MULTITOOL_TAG=v0.10.0
CONTAINERLAB_VERSION=0.79.0
TELEGRAF_VERSION=1.40.0
CONTAINERLAB_RPM="containerlab_${CONTAINERLAB_VERSION}_linux_arm64.rpm"
SRLINUX_UPSTREAM=ghcr.io/nokia/srlinux
MULTITOOL_UPSTREAM=ghcr.io/srl-labs/network-multitool
# lab の SR Linux が送る syslog の形式。ops/up.sh の SYSLOG_STANDARD（既定は本番の Cisco に合わせた RFC3164）がこれと違えば up.sh が注意を出す。
# lab/lab.sh の LOG_STANDARD（デバッグ用の EC2 の Telegraf に渡す）と同じ
LAB_SYSLOG_STANDARD=RFC5424

cfn_stack_status() {  # cfn_stack_status <スタック名>  状態を出す。無ければ空。読めない（認証切れ・スロットリングなど）なら理由を stderr に出して 1
  # 「無い」と「読めない」を分ける（読めないのを無いと扱うと、ops/lab-debug.sh down が消さずに終わり、up が器を作り直そうとする）
  local out
  if out=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>&1); then
    printf '%s\n' "$out"
    return 0
  fi
  case "$out" in *"does not exist"*) return 0 ;; esac
  printf '%s\n' "$out" >&2
  return 1
}
ecr_has() {  # ecr_has <リポジトリ名> <タグ>
  aws ecr describe-images --region "$REGION" --repository-name "$1" --image-ids imageTag="$2" >/dev/null 2>&1
}
fetch() {  # fetch <URL> <ファイル名>  展開したフォルダの直下に無いときだけ取る。翌日からは取り直さない
  if [ -s "$2" ]; then echo "$2: 手元にあるので取らない"; return 0; fi
  curl -fL --retry 3 -o "$2.part" "$1" || { rm -f "$2.part"; return 1; }
  mv "$2.part" "$2"
}
dir_tag() {  # dir_tag <版> <ディレクトリ>  "<版>-<ディレクトリの中身のハッシュ 12 桁>"。ECR のタグは上書きできないので、中身を変えたら別のタグにする
  "${PY[@]}" - "$1" "$2" <<'PY'
import hashlib, os, sys
ver, root = sys.argv[1], sys.argv[2]
h = hashlib.sha256()
for d, dirs, files in os.walk(root):
    dirs[:] = sorted(x for x in dirs if x != "__pycache__")
    for f in sorted(files):
        if f == ".DS_Store":
            continue
        path = os.path.join(d, f)
        h.update(os.path.relpath(path, root).encode() + b"\0")
        with open(path, "rb") as fh:
            h.update(fh.read())
print(f"{ver}-{h.hexdigest()[:12]}")
PY
}
mirror_image() {  # mirror_image <上流のイメージ:タグ> <ECR のイメージ:タグ>  上流の arm64 を ECR に置き直す（EC2 は VPC の中から ECR しか引けない）
  docker pull --platform linux/arm64 "$1"
  docker tag "$1" "$2"
  docker push "$2"
}
mirror_lab_images() {  # mirror_lab_images <レジストリ> <接頭辞>  lab の 2 つ（SR Linux 約 1 GB と VM の multitool）のうち ECR に無いタグだけ
  if ecr_has "$2-lab-srlinux" "$SRLINUX_TAG"; then echo "lab-srlinux:$SRLINUX_TAG はある"
  else mirror_image "$SRLINUX_UPSTREAM:$SRLINUX_TAG" "$1/$2-lab-srlinux:$SRLINUX_TAG" || return 1; fi
  if ecr_has "$2-lab-multitool" "$MULTITOOL_TAG"; then echo "lab-multitool:$MULTITOOL_TAG はある"
  else mirror_image "$MULTITOOL_UPSTREAM:$MULTITOOL_TAG" "$1/$2-lab-multitool:$MULTITOOL_TAG" || return 1; fi
}
telegraf_tag() {  # telegraf_tag  telegraf/ の中身からタグを作る（stream の ECS もデバッグ用の EC2 もこのタグを引く）
  dir_tag "$TELEGRAF_VERSION" telegraf
}
build_telegraf() {  # build_telegraf <ECR のイメージ:タグ>  COPY だけなので x86_64 の PC でも QEMU は要らない
  docker buildx build --platform linux/arm64 --build-arg "TELEGRAF_VERSION=$TELEGRAF_VERSION" -t "$1" --push telegraf
}
upload_lab() {  # upload_lab <バケット>  lab の EC2 は起動のたびに s3://<バケット>/lab/ を読む（lab/setup.sh）。リポジトリの直下で呼ぶ
  fetch "https://github.com/srl-labs/containerlab/releases/download/v$CONTAINERLAB_VERSION/$CONTAINERLAB_RPM" "$CONTAINERLAB_RPM" \
    || { echo "containerlab の rpm が取れない（社内 PC なら docs/setup.md「社内 PC の CA」）" >&2; return 1; }
  aws s3 sync --only-show-errors lab/ "s3://$1/lab/" --exclude "splab.clab.yml" --exclude "__pycache__/*" --exclude "*.DS_Store" || return 1
  aws s3 cp --only-show-errors "$CONTAINERLAB_RPM" "s3://$1/lab/"
}
