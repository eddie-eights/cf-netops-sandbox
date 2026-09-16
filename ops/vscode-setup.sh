#!/usr/bin/env bash
# VS Code の拡張を入れる。AWS には触らない。
#
#   bash ops/vscode-setup.sh          … docs/vscode/extensions.txt の [repo]（このリポジトリに要るものだけ）
#   ALL=1 bash ops/vscode-setup.sh    … [repo] + [extra]（手元の PC と同じ全部）
#
# 設定そのもの（ユーザー設定・キー割り当て）は貼り付けで入れる。README「VS Code の設定」を見る。
set -euo pipefail
cd "$(dirname "$0")/.."

LIST=docs/vscode/extensions.txt
ALL="${ALL:-0}"

if ! command -v code >/dev/null 2>&1; then
  echo "code コマンドが見つからない。" >&2
  echo "WSL のフォルダを VS Code で開いてから、その中のターミナルで打つ。" >&2
  echo "それでも出ないときは VS Code で Ctrl+Shift+P →「シェル コマンド: PATH 内に code コマンドをインストール」。" >&2
  exit 1
fi

# [repo] だけ、または全部を取り出す（# 以降と空行を落とす）
if [ "$ALL" = "1" ]; then
  WANT=$(sed 's/#.*//; s/[[:space:]]//g' "$LIST" | grep -v '^\[' | grep -v '^$' | sort -u)
  echo "全部（[repo] + [extra]）を入れる"
else
  WANT=$(awk '/^\[repo\]/{f=1;next} /^\[/{f=0} f' "$LIST" | sed 's/#.*//; s/[[:space:]]//g' | grep -v '^$' | sort -u)
  echo "[repo] だけ入れる（全部入れるなら ALL=1 を頭に付ける）"
fi

INSTALLED=$(code --list-extensions 2>/dev/null | tr 'A-Z' 'a-z' || true)

ok=0; skip=0; ng=0
for id in $WANT; do
  if printf '%s\n' "$INSTALLED" | grep -qix "$id"; then
    skip=$((skip + 1))
    continue
  fi
  printf '入れる: %s ... ' "$id"
  if code --install-extension "$id" --force >/dev/null 2>&1; then
    echo "ok"
    ok=$((ok + 1))
  else
    echo "失敗"
    ng=$((ng + 1))
  fi
done

echo "入れた $ok / 既にあった $skip / 失敗 $ng"
if [ "$ng" -gt 0 ]; then
  echo
  echo "失敗したものがある。よくある理由は 2 つ:" >&2
  echo "  - テーマ・アイコン・日本語パックは Windows 側に入る拡張なので、WSL 側からは入らないことがある。Windows 側の VS Code で入れる。" >&2
  echo "  - SSL 検査のある回線では Marketplace のダウンロードが証明書エラーになることがある。Windows 側から入れるか、.vsix を落として「VSIX からのインストール」を使う。" >&2
  exit 1
fi
