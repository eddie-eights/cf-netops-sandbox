#!/usr/bin/env bash
# docs/vscode/extensions.txt に並べた VS Code の拡張を入れる。AWS には触らない。
#
#   bash ops/vscode-setup.sh
#
# 設定そのもの（ユーザー設定・キー割り当て）は貼り付けで入れる。docs/development.md「VS Code の設定」を見る。
set -euo pipefail
cd "$(dirname "$0")/.."

LIST=docs/vscode/extensions.txt

if ! command -v code >/dev/null 2>&1; then
  echo "code コマンドが見つからない。" >&2
  echo "WSL のフォルダを VS Code で開いてから、その中のターミナルで打つ。" >&2
  echo "それでも出ないときは VS Code で Ctrl+Shift+P →「シェル コマンド: PATH 内に code コマンドをインストール」。" >&2
  exit 1
fi

# 拡張 ID だけ取り出す（# 以降のコメントと空行を落とす）
WANT=$(sed 's/#.*//; s/[[:space:]]//g' "$LIST" | grep -v '^$' | sort -u)

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
