# ops/up.sh と ops/down.sh が読み込む（単独では打たない）。deploy.env（ops/up.sh と ops/down.sh の設定）を読む関数を定義する。
# 呼ぶ側で log / die を定義し、展開したフォルダの直下に cd してから load_deploy_env を呼ぶ。
#
# ファイルはシェルとして実行しない（source しない）。1 行に 1 つの「キー=値」だけを読む:
#   - 空行と、# で始まる行は飛ばす。値の後ろの「 # …」（空白の後ろの #）もコメント
#   - 行頭の `export ` と、キーと値の前後の空白は無視する。CRLF（Windows で保存したファイル）も読める
#   - 値を "…" か '…' で囲むと、中身をそのまま使う（中の # もコメントにならない）
#   - $HOME や $(…) は展開しない。値の先頭の ~/ だけ $HOME/ に読み替える
#   - 値が空の行（`IMAGE_TAG=`）は書いていないのと同じ（既定値のまま。空の AWS_PROFILE などを環境に入れない）
#   - 同じ名前の環境変数が空でなければ、ファイルの値は使わない（`PIPELINE=1 ops/up.sh` はファイルの PIPELINE より優先）。
#     ファイルの 1 を環境変数で打ち消すときは空ではなく 0 を渡す（`SKIP_GRAPH=0 ops/up.sh`）
#   - 知らないキーと、同じキーの 2 回目は止まる（打ち間違いで違う機能を作らないため）
# ファイルの場所は既定で展開したフォルダ直下の deploy.env。DEPLOY_ENV_FILE=<パス> で変えられる（相対パスは打った場所から）。

# 読めるキー（意味は deploy.env.example）。OWNER だけ必須で、ほかは任意。これ以外のキーが書いてあれば止まる
DEPLOY_ENV_KEYS="OWNER PIPELINE AGENT WORKFLOW CREATE_KB SKIP_LAB SKIP_STREAM SKIP_ANALYTICS SKIP_GRAPH SINK_S3 SINK_OPENSEARCH SINK_PROMETHEUS
SINK_SPLUNK SPLUNK_HEC_URL SPLUNK_INDEX SPLUNK_SKIP_TLS_VERIFY IMAGE_TAG ADMIN_ARN
VPC_CIDR CLIENT_CIDR OPENSEARCH_CACERT_FILE LOCAL_PORT NO_PORTFORWARD KEEP_ECR TF_VERBOSE AWS_PROFILE AWS_CA_BUNDLE"

# DEPLOY_ENV_FILE の相対パスを、cd する前の場所から見た絶対パスにする。呼ぶ側が cd の前に打つ
resolve_deploy_env_file() {
  case "${DEPLOY_ENV_FILE:-}" in
    ''|/*) ;;
    *) DEPLOY_ENV_FILE="$PWD/$DEPLOY_ENV_FILE" ;;
  esac
}

load_deploy_env() {
  local file="${DEPLOY_ENV_FILE:-deploy.env}"
  local line key val q rest after n=0 seen=" " from_file="" from_env=""
  local keys
  keys=" $(echo $DEPLOY_ENV_KEYS) "  # 改行と連続した空白を 1 つにする
  if [ ! -f "$file" ]; then
    if [ -n "${DEPLOY_ENV_FILE:-}" ]; then die "DEPLOY_ENV_FILE のファイルが無い: $file"; fi
    echo "deploy.env: 無い（環境変数と既定値で動く。既定は AGENT=1 だけ。ただし OWNER は必須なので、cp deploy.env.example deploy.env で写して書く）"
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      ''|'#'*) continue ;;
      'export '*|'export	'*) line="${line#export}"; line="${line#"${line%%[![:space:]]*}"}" ;;
    esac
    case "$line" in
      *=*) ;;
      *) die "$file の $n 行目が「キー=値」の形でない" ;;
    esac
    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    val="${line#*=}"
    val="${val#"${val%%[![:space:]]*}"}"
    case "$key" in
      ''|*[!A-Z0-9_]*) die "$file の $n 行目のキー「${key}」は英大文字・数字・_ だけで書く" ;;
    esac
    case "$keys" in
      *" $key "*) ;;
      *) die "$file の $n 行目のキー「${key}」は使えない（使えるキーと意味は deploy.env.example）" ;;
    esac
    case "$seen" in
      *" $key "*) die "$file の $n 行目: $key が 2 回ある（どちらを使うか決められない）" ;;
    esac
    seen="$seen$key "
    q="${val:0:1}"
    if [ "$q" = '"' ] || [ "$q" = "'" ]; then
      rest="${val:1}"
      case "$rest" in
        *"$q"*) ;;
        *) die "$file の $n 行目: $key の値の引用符が閉じていない" ;;
      esac
      val="${rest%%"$q"*}"
      after="${rest#*"$q"}"
      after="${after#"${after%%[![:space:]]*}"}"
      case "$after" in
        ''|'#'*) ;;
        *) die "$file の $n 行目: $key の値の引用符の後ろに余計なものがある" ;;
      esac
    else
      case "$val" in
        '#'*) val="" ;;
        *[[:space:]]'#'*) val="${val%%[[:space:]]#*}" ;;
      esac
      val="${val%"${val##*[![:space:]]}"}"
    fi
    case "$val" in
      '~/'*) val="$HOME/${val#'~/'}" ;;
    esac
    if [ -n "${!key:-}" ]; then
      from_env="$from_env $key"
      continue
    fi
    if [ -z "$val" ]; then continue; fi
    printf -v "$key" '%s' "$val"
    export "${key?}"
    from_file="$from_file $key"
  done <"$file"
  echo "deploy.env: $file"
  echo "  ファイルから入れたキー:${from_file:- なし}"
  if [ -n "$from_env" ]; then echo "  環境変数が先にあったので、ファイルの値を使わなかったキー:$from_env"; fi
}

# resolve_name_prefix  deploy.env の OWNER（= デプロイする人の名前。**必須**）を確かめ、接頭辞 PREFIX=<owner>-nwc-poc を作る。
# load_deploy_env のあとに呼ぶ。OWNER は terraform の -var owner と AWS CLI の owner タグに渡り、
# PREFIX はリソース名の接頭辞であり Project タグの値で、terraform 側は同じものを locals.tf が var.owner から作る（渡さない）。
# 有無と形は各ルートの variables.tf の owner（既定値が無い + 同じ validation）と同じものをここでも見る（terraform を起こす前に、
# 値を聞かれて止まる代わりに何を書けばよいかを出して止まるため）。
#   接頭辞は OpenSearch Serverless の data access policy 名が 32 文字までで、一番長い接尾辞が terraform/workflow の
#   <接頭辞>-logs-read（10 文字）なので 22 文字まで。-nwc-poc の 8 文字を引いて owner は 14 文字まで。
#   ハイフンの連続と末尾のハイフンは ECR のリポジトリ名が受け付けない
resolve_name_prefix() {
  [ -n "${OWNER:-}" ] \
    || die "OWNER（デプロイする人の名前）が要る。cp deploy.env.example deploy.env で写して OWNER=<自分の名前> を書く（自分の名前でリソースを探せるようにするための値）"
  [[ "$OWNER" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ && ${#OWNER} -le 14 ]] \
    || die "OWNER は英小文字で始まる 14 文字までの英小文字・数字・ハイフンで、ハイフンは連続せず末尾にも置けない（いまは「${OWNER}」）"
  PREFIX="$OWNER-nwc-poc"
}

# flag_value <変数名>  1 / true / yes なら 1、0 / false / no / 空なら空にそろえる。それ以外の値は止まる
flag_value() {
  local name="$1" v
  v="${!name:-}"
  case "$v" in
    1|true|yes) printf -v "$name" '%s' 1 ;;
    ''|0|false|no) printf -v "$name" '%s' '' ;;
    *) die "$name は 1 か 0（いまは「${v}」）" ;;
  esac
}

# ---- terraform の出力を絞る（up.sh / down.sh 共通。呼ぶ側が tf <ルート> <引数…> を持っていること）
# 画面には Plan / 完了したリソース / 5 分ごとの経過 / エラーだけを出し、全文は ops/logs/ に残す。TF_VERBOSE=1 で全部そのまま出す。
TF_VERBOSE="${TF_VERBOSE:-}"
TF_KEEP='^(Plan:|Apply complete|Destroy complete|No changes)|Error|^[│╷╵]|: (Creation|Destruction|Modifications) complete|: Still (creating|destroying|modifying)\.\.\. \[[0-9]*[05]m0s elapsed\]'
tf_log_file() { echo "ops/logs/tf-${1//\//-}-$2.log"; }
tf_logged() { # <ルート> <apply|destroy> <引数…>。終了コードは terraform のもの（呼ぶ側の pipefail が前提）
  local root="$1" verb="$2"; shift 2
  local logf; logf=$(tf_log_file "$root" "$verb")
  mkdir -p ops/logs
  # TF_VERBOSE=1 でも全文をファイルに残す（画面に出すだけにすると、ops/down.sh がログから
  # DependencyViolation と掴んでいる SG を読めず、打ち直しが効かなくなる）
  if [ -n "$TF_VERBOSE" ]; then
    tf "$root" "$verb" -no-color "$@" 2>&1 | tee "$logf"
    return
  fi
  tf "$root" "$verb" -no-color -compact-warnings "$@" 2>&1 | tee "$logf" | { grep --line-buffered -E "$TF_KEEP" || true; }
}
