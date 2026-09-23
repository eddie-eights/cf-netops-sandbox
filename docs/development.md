# 手元で変える・確かめる

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

## 変更するとき

| 変えたもの | やること |
|---|---|
| `web/` の `.py`、手順書 | `ops/up.sh` を打つ（手順 4 で S3 に置き直して Web を再起動する）。apply は要らない |
| `agent/`（`agent/data/` を含む）、`workflow/` | `deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh`。同じタグのままだとビルドを飛ばす |
| ガードレール（`terraform/agent/kb.tf`） | `aws_bedrock_guardrail_version.r1` の `description` の末尾を `r2` のように上げて `ops/up.sh`。上げないと Runtime は古い版のまま判定する |
| `templates/*.sh.tftpl` | シェルの `${…}` は `$${…}`、`%{` は `%%{` と書く（`templatefile` を通るため）。user_data は 16 KB まで |
| 変数の既定 | `terraform/<ルート>/terraform.tfvars.example` を `terraform.tfvars` に写して書く |

- user_data や AMI（apply のたびに最新の AL2023 を引く）が変わると、**EC2 が作り直されてインスタンス ID が変わる。**利用者に配った `start_session_command` は配り直す。

## 手元で確かめる

AWS に触らずに、Terraform の構文検査と模擬テストを打てる。**変更したら、まずこれを打つ。**

```bash
uv sync --group dev
```

```bash
bash ops/check.sh
```

最後の行が `すべて通過` なら健全。中身は `terraform fmt`、8 ルートの `terraform validate`、`bash -n`、`tests/` の 6 本（`test_app` 51 項目、`test_graph` 23、`test_stream` 47、`test_sync` 28、`test_analytics` 169、`test_workflow` 141）。途中で落ちたらそこで止まる。

## Web を手元で動かす

画面だけ見たいとき、EC2 で Web が立たない原因を切り分けるとき。チャットには `terraform/agent` の apply が済んでいることが要る（無ければチャットだけエラー表示になる）。

```bash
cp .env.example .env
```

```bash
RUNTIME_ARN=$(terraform -chdir=terraform/agent output -raw agent_runtime_arn); echo "$RUNTIME_ARN"; echo "RUNTIME_ARN=$RUNTIME_ARN" >> .env
```

```bash
uv sync --group web
```

```bash
uv run python web/app.py
```

ブラウザで http://127.0.0.1:8080 を開く。環境変数の意味は `.env.example` に書いてある。

## VS Code（任意）

`.vscode/` はフォルダを開くだけで効く。拡張を入れるなら WSL のターミナルで打つ。

```bash
bash ops/vscode-setup.sh
```

ユーザー設定とキー割り当ての例は `docs/vscode/user-settings.json` と `docs/vscode/keybindings.json`。Ctrl+Shift+P の「ユーザー設定を開く (JSON)」「キーボードショートカットを開く (JSON)」に、要るところだけ貼る。

## 入っていないもの

- 会話の永続化。履歴は Runtime のセッションの中にだけあり、画面を再読み込みすると消える。
- Temporal の永続化と UI の認証。履歴はタスクと一緒に消え、UI にはポートフォワーディングでしか届かない。
- 実機への修復。打てるのは lab の `sudo lab heal-main` と `sudo lab check` だけ。
- 履歴の検索（`query_history`）。Athena をつないでいないので案内だけ返す。
- Grafana などの可視化。異常一覧と修復案は Neptune の頂点をそのまま表に出す。
- 証跡（S3 Tables の `anomaly_events` / `proposal_events`）を読む画面。書くだけで、読むには Athena などを足す。
- state の共有。1 人が 1 台の PC で打つ前提。
