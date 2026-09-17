# ops/up.sh と ops/down.sh が読み込む（単独では打たない）。deploy.env（ops/up.sh と ops/down.sh の設定）を読む関数を定義する。
# 呼ぶ側で log / die を定義し、リポジトリの直下に cd してから load_deploy_env を呼ぶ。
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
# ファイルの場所は既定でリポジトリ直下の deploy.env。DEPLOY_ENV_FILE=<パス> で変えられる（相対パスは打った場所から）。

# 読めるキー。機能は PIPELINE / AGENT / WORKFLOW の 3 つ（2026-09-17）。PHASE は古い書き方で、ops/up.sh が機能に読み替えて注意を出す。
# WITH_LAB（2026-09-16）と WITH_STREAM（2026-09-17）は無くなったキーで、ops/up.sh が案内を出して止まる
DEPLOY_ENV_KEYS="PIPELINE AGENT WORKFLOW CREATE_KB PHASE SKIP_LAB SKIP_STREAM SKIP_ANALYTICS SKIP_GRAPH CREATE_S3_SINK SINKS IMAGE_TAG ADMIN_ARN
VPC_CIDR CLIENT_CIDR OPENSEARCH_CACERT_FILE LOCAL_PORT NO_PORTFORWARD KEEP_ECR AWS_PROFILE AWS_CA_BUNDLE WITH_LAB WITH_STREAM"

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
    echo "deploy.env: 無い（環境変数と既定値で動く。既定は AGENT=1 だけ。作るなら cp deploy.env.example deploy.env）"
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
