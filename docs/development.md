# 手元で変える・確かめる

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

## 変更するとき

| 変えたもの | やること |
|---|---|
| `web/` の `.py`、手順書 | `ops/up.sh` を打つ（手順 4 で S3 に置き直して Web を再起動する）。apply は要らない |
| `agent/`（`agent/data/` を含む）、`workflow/` | `deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh`。同じタグのままだとビルドを飛ばす |
| `telegraf/`、`grafana/`（アラートのルール `provisioning/alerting/` を含む）、`splunk/`（保存済みサーチとアラートアクションを含む） | `ops/up.sh` を打つ。イメージのタグがディレクトリの中身から決まるので、作り直してタスクが入れ替わる（タグを上げる操作は要らない） |
| ガードレール（`terraform/agent/kb.tf`） | `aws_bedrock_guardrail_version.r1` の `description` の末尾を `r2` のように上げて `ops/up.sh`。上げないと Runtime は古い版のまま判定する |
| `templates/*.sh.tftpl` | シェルの `${…}` は `$${…}`、`%{` は `%%{` と書く（`templatefile` を通るため）。user_data は 16 KB まで |
| 変数の既定 | `terraform/<ルート>/terraform.tfvars.example` を `terraform.tfvars` に写して書く |
| lab と Telegraf の版 | `ops/lab-common.sh` を正本に、`terraform/pipeline/lab` の変数の既定値・`cloudformation/lab-debug.yaml` のパラメータの既定値（Telegraf は `telegraf/Dockerfile` の ARG も）を全部そろえる（`tests/test_lab_debug.py` が見る） |
| `cloudformation/lab-debug.yaml` の UserData | `Fn::Sub` を通るので、シェルの変数は `${…}` でなく `$LAB` の形で書く。EC2 の中の支度は `lab/setup.sh` に書き、UserData には足さない |

- user_data や AMI（apply のたびに最新の AL2023 を引く）が変わると、**EC2 が作り直されてインスタンス ID が変わる。**利用者に配った `start_session_command` は配り直す。

## 手元で確かめる

AWS に触らずに、Terraform の構文検査と模擬テストを打てる。**変更したら、まずこれを打つ。**

```bash
uv sync --group dev
```

```bash
bash ops/check.sh
```

最後の行が `すべて通過` なら健全。中身は `terraform fmt`、8 ルートの `terraform validate`、`bash -n`、`tests/` の 9 本（`test_app` 55 項目、`test_graph` 48、`test_stream` 44、`test_sync` 59、`test_analytics` 249、`test_workflow` 238、`test_alerts` 89、`test_kb_index` 7、`test_lab_debug` 66）。途中で落ちたらそこで止まる。

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

## 入っていないもの

- 会話の永続化。履歴は Runtime のセッションの中にだけあり、画面を再読み込みすると消える。
- Temporal の永続化と UI の認証。履歴はタスクと一緒に消え、UI にはポートフォワーディングでしか届かない。
- 実機への修復。打てるのは lab の `sudo lab heal-main`（`dc1-leaf-01 ethernet-1/1` の fabric を戻す）と `sudo lab check` だけ。
- 履歴の検索（`query_history`）。Athena をつないでいないので案内だけ返す。
- Web の画面の中のグラフ。修復案は Neptune の頂点をそのまま表に出す（メトリクスとログのグラフは 2026-09-28 から Grafana（`GRAFANA=1`）で見る）。
- 障害の履歴の置き場と、Web の異常一覧。2026-10-02 に検知を Grafana と Splunk へ移したときにやめ、置き場はまだ決めていない（[data-stores.md](data-stores.md) の「5. 経緯」）。いまの異常は「トポロジ」タブの `status`、アラートは Grafana / Splunk の画面で見る。
- 複数の機器にまたがるアラートの相関。まとめるのは同じ機器・種類・対象のアラートだけ（送り手が違っても異常の id が同じになる）で、「Spine が落ちたので配下の Leaf のアラートを 1 つの障害にする」ようなルールは入れていない。
- 証跡（S3 Tables の `proposal_events`）を読む画面。書くだけで、読むには Athena などを足す。
- state の共有。1 人が 1 台の PC で打つ前提。
