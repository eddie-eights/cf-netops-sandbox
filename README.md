# fukuda-nwc-poc — NetOps PoC（閉域ネットワーク版 / Terraform）

ブラウザのチャット画面から AgentCore Runtime 上のエージェントと話し、エージェントが Bedrock（Amazon Nova 2 Lite）で答える。
答える前に **Bedrock Knowledge Base**（OpenSearch Serverless、ベクトル検索とキーワード検索のハイブリッド）で手順書の md を引き、**Bedrock Guardrails** で質問と回答を判定する。
これを、**インターネットに出口の無い VPC** で動かすための Terraform 一式（state はローカル）。

ブラウザからは **SSM Session Manager のポートフォワーディング**で入る。NAT Gateway・EIP・パブリック IP・ロードバランサ・証明書は使わない。
EC2 のセキュリティグループに**受信ルールは 1 つも無い**。

**何を作るかは `deploy.env` の 3 つの機能で選ぶ**（`cp deploy.env.example deploy.env` で写して書く。gitignore 済み）。機能は互いに独立で、要るものだけ `1` にする。`deploy.env` が無ければ土台と AGENT を作る。

| 何 | できること | 作るもの（`terraform/` の下） | 待機の時間課金（東京） |
|---|---|---|---|
| 土台（必ず作る） | 閉域の VPC と Web の EC2（Gradio の画面）、S3 バケット、Runtime / Web のロール | `base/ecr` / `base/core` | 約 $0.05/h（約 8 円） |
| `AGENT=1`（既定） | agent での分析。AgentCore Runtime + ガードレール（チャットがモデルとトポロジのツールで答える）。`CREATE_KB=1` でナレッジベース（手順書の検索）も足す | + `agent` | 約 $0.13/h（約 20 円）。`CREATE_KB=1` なら +$0.36/h |
| `PIPELINE=1` | データパイプライン（lab の containerlab → Telegraf → Kafka → Spark → S3 Tables / OpenSearch Serverless（ログ）/ Prometheus（メトリクス）。3 つとも既定で作り、`SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS` の `1` / `0` で 1 つずつ外せる。Spark が異常を検知して DynamoDB の「異常一覧」と EventBridge に出す）と、トポロジの操作（Neptune。Web の「トポロジ」タブで編集する） | + `lab` / `stream` / `analytics` / `graph` | 約 $1.08/h（約 162 円。OpenSearch Serverless の最大 0.33 を含む）。`SKIP_*` / `SINK_*` で一部を外せる |
| `WORKFLOW=1` | Temporal での実行。Spark の検知が EventBridge → SQS で届き、エージェントが Neptune / OpenSearch / Prometheus を見て原因を Temporal のワークフローで調べて修復案を出し、人が Web の「承認」タブで承認すると Temporal が lab で直して確かめる。エージェントのツールは AgentCore Gateway（MCP）経由。**AGENT と PIPELINE が要る** | + `workflow` | 約 $0.06/h（約 9 円） |

PIPELINE は、組織の SCP / IAM で止められていることがある（「[前提](docs/setup.md)」の「AWS 側」）。フェーズの番号（`PHASE=1 / 2 / 3`）は 2026-09-17 に機能の名前に替えた（1 → `AGENT=1`、2 → `AGENT=1` + `PIPELINE=1`、3 → 全部 `1`。`PHASE` を書いたままなら `ops/up.sh` が読み替えて注意を出す。[docs/phases.md](docs/phases.md)）。

## ディレクトリ構成

| パス | 中身 |
|---|---|
| `terraform/` | **AWS にリソースを作るのはここだけ**（下のツリー） |
| `agent/` | AgentCore Runtime で動くエージェント（`app.py` とツール 4 本: topology / anomalies / evidence / proposals） |
| `tools/` | AgentCore Gateway（MCP）の tools Lambda（`handler.py` + `tools.json`）。`agent/` のツールを同じ引数で外から呼べるようにする |
| `web/` | Web の EC2 で動く Gradio の画面（チャット / トポロジ / 異常一覧 / 承認） |
| `workflow/` | Temporal のワークフローとワーカー（調査 → 修復案 → 承認 → 実行 → 確認） |
| `spark/` | EMR Serverless で動く Spark のジョブ（Kafka → S3 Tables / OpenSearch / Prometheus と異常検知） |
| `lab/` | containerlab の構成（FRR の機器と Telegraf） |
| `graph/` | Neptune に入れるトポロジ（静的データと投入スクリプト） |
| `kb-docs/` | ナレッジベースに入れる手順書の md |
| `ops/` | **打つもの**。`up.sh`（作る）/ `down.sh`（消す）/ `check.sh`（検査） |
| `tests/` | `ops/check.sh` が回す模擬テスト（AWS を呼ばない） |
| `docs/` | **読むもの**（下の「[ドキュメント](#ドキュメント)」） |
| `deploy.env.example` | `deploy.env` の見本。写して使う |

Terraform のルートは、土台（`base`）と機能ごと（`agent` / `pipeline` / `workflow`）に分けてある（2026-09-18。それまでの `terraform/main` は `terraform/base/core`）。1 ディレクトリ = 1 state で、`deploy.env` の機能のキーとそのまま対応する。

```
terraform/
├── base/          土台（必ず作る）
│   ├── ecr/         ECR リポジトリ
│   └── core/        VPC / SG / エンドポイント / バケット / ロール / Web の EC2
├── agent/         AGENT=1     AgentCore Runtime / ガードレール / KB
├── pipeline/      PIPELINE=1
│   ├── lab/         containerlab の EC2
│   ├── stream/      MSK / MSK Connect / 異常テーブル
│   ├── analytics/   EMR Serverless（Spark）/ S3 Tables / OpenSearch / Prometheus
│   └── graph/       Neptune
└── workflow/      WORKFLOW=1  Temporal on ECS / Gateway（MCP）
```

どのファイルがどこで動くか、なぜこの形にしたかは「[構成](docs/architecture.md)」。

## デプロイ手順

コマンドは全部 bash 用で、**Mac（Apple Silicon）と Windows の WSL2 で同じものを打つ**。違うのは道具の入れ方だけ（「[前提](docs/setup.md)」）。
**コマンドはすべてリポジトリの直下で打つ**（`terraform -chdir=terraform/<ルート>` と `web/` などのパスが直下から見た位置になっている）。

1. 道具を入れる。AWS CLI v2 / Terraform 1.11 以上 / Docker（arm64 のビルドができる buildx）/ Session Manager plugin / python3 か uv（「[前提](docs/setup.md)」）。
2. AWS に入る。`aws login --profile <プロファイル>` か `aws configure sso`、または IAM ユーザーの長期キー（`aws configure`）。スクリプトは `credential_process` で Terraform に渡すので、環境変数に鍵を出さなくてよい。**IAM ユーザーの一時セッション（`sts get-session-token`）では IAM の API が呼べず apply が落ちる**ので、`ops/up.sh` が見つけて先頭で止まる。
3. リポジトリを **Linux 側のホーム**に置く（WSL2 は `/mnt/c` ではなく `~`。Docker のビルドと `wheels/` の展開が遅くなる）。
4. `cp deploy.env.example deploy.env` で写し、要る機能を `1` にする。
5. `ops/up.sh` を打つ。手順 0 で作るルートと 1 時間あたりの目安を出し、土台 → 機能の順に apply する。終わると Web への SSM ポートフォワーディングが開く（`http://localhost:8080`）。
6. 使い終わったら **当日中に** `ops/down.sh`。

```bash
git clone https://github.com/eddie-eights/cf-netops-sandbox.git ~/cf-netops-sandbox && cd ~/cf-netops-sandbox
```

```bash
cp deploy.env.example deploy.env
```

```bash
ops/up.sh
```

```bash
ops/down.sh
```

`ops/up.sh` は**できているものを飛ばす**（Terraform は差分だけ、ECR に同じタグのイメージがあればビルドしない、`wheels/`・rpm・zip が手元にあれば取り直さない、Neptune に機器が入っていれば投入しない）ので、途中で落ちても同じコマンドを打ち直せばよい。
初回は `PIPELINE=1` で 40〜60 分かかる（MSK の作成だけで 20〜30 分）。AGENT だけなら 10〜15 分。
`deploy.env` を書き換えずにその回だけ変えるなら、同じ名前の環境変数を付けて打つ（`PIPELINE=1 ops/up.sh`。空でない環境変数が `deploy.env` より優先）。

### `deploy.env` のよく使うキー

| キー | 何 |
|---|---|
| `AGENT` / `PIPELINE` / `WORKFLOW` | 作る機能（上の表）。既定は `AGENT=1` だけ |
| `CREATE_KB` | ナレッジベース（手順書の検索）を作る。+$0.36/h |
| `SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` / `SKIP_GRAPH` | `PIPELINE=1` のうち、このルートを作らない |
| `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS` | Spark の格納先を 1 つずつ作る（`1`）/ 作らない（`0`）。既定は 3 つとも `1` |
| `IMAGE_TAG` | エージェント（と worker）のイメージのタグ。既定 `v1`。`agent/` や `workflow/` を変えたら `v2` などにする |
| `KEEP_ECR` | `ops/down.sh` で ECR（イメージ）を残す。翌朝のビルドを飛ばせる（保管料は月数円） |
| `AWS_PROFILE` / `LOCAL_PORT` / `NO_PORTFORWARD` | プロファイル名 / PC 側のポート（既定 8080）/ ポートフォワーディングを開かない |

残りのキー、値の書き方の決まり、`ops/up.sh` と `ops/down.sh` が何を順に打つかは「[デプロイの詳しい説明](docs/deploy.md)」。スクリプトを使わず 1 ルートずつ手で打つなら「[手で打つ手順](docs/deploy-manual.md)」。

### 待機の時間課金

土台 0.05 + 共用のエンドポイント 0.08（AGENT / lab / analytics のどれかを作るとき）+ AGENT 0.05（`CREATE_KB=1` なら +0.36）+ PIPELINE の lab 0.09 + stream 0.71 + analytics 0.20 + OpenSearch Serverless の logs コレクション最大 0.33 + Prometheus 0.03 + graph（Neptune）0.14 + WORKFLOW 0.06 で、**全部作ると約 $1.75/h**（`ops/up.sh` も手順 0 で目安を出す）。
**使い終わったら当日中に `ops/down.sh` を打つ。**PIPELINE / WORKFLOW を置いておくと 1 か月で約 $800 になる（内訳は「[費用の試算](docs/cost.md)」）。

### state の注意（ローカル state なので大事）

- `terraform/<ルート>/terraform.tfstate` を**消さない**。`git clean -fdx` も打たない（gitignore したファイルごと消える）。消すと Terraform は作ったものを忘れ、`ops/down.sh` が「無い」と言って飛ばし、AWS にリソースと課金が残る。次の `ops/up.sh` は同じ名前がぶつかって `AlreadyExists` で落ちる。
- **up と down は同じ PC で打つ。**別の PC には state が無いので、同じことが起きる。PC を替えるときは、元の PC で `ops/down.sh` を済ませてから。
- 消してしまったときは、コンソールで `fukuda-nwc-poc` の名前とタグのリソースを手で消す（「[名前とタグ](docs/architecture.md)」の `get-resources` で探す）。
- **機能を `0` に戻して打っても、前に作ったルートは消さない。**消すのは `ops/down.sh` だけ。

うまく動かないときは「[うまくいかないとき](docs/troubleshooting.md)」。

## ドキュメント

| 何 | どこ |
|---|---|
| 構成（何がどこで動くか、ファイル一覧、なぜこの形にしたか、名前とタグ、ログ） | [docs/architecture.md](docs/architecture.md) |
| 前提（AWS 側 / 利用者の PC / Terraform を打つ PC、WSL2 と Mac の準備、社内 PC のプロキシと CA） | [docs/setup.md](docs/setup.md) |
| デプロイの詳しい説明（`ops/up.sh` / `ops/down.sh` が打つ順、`deploy.env` の全キー） | [docs/deploy.md](docs/deploy.md) |
| 手で打つ手順 0〜7 と片付け（CloudFormation 版から移るときも） | [docs/deploy-manual.md](docs/deploy-manual.md) |
| データパイプライン（lab / stream / analytics / graph）を手で打つ | [docs/pipeline.md](docs/pipeline.md) |
| Temporal での実行（workflow）を手で打つ | [docs/workflow.md](docs/workflow.md) |
| うまくいかないとき | [docs/troubleshooting.md](docs/troubleshooting.md) |
| 費用の試算（1 時間起動したとき） | [docs/cost.md](docs/cost.md) |
| 手元で変える・確かめる（テスト、Web のローカル起動、VS Code、確認できていないこと） | [docs/development.md](docs/development.md) |
| フェーズごとの概要 | [docs/phases.md](docs/phases.md) |
| Spark と MSK の offset は誰が持っているか | [docs/spark-msk-offset.md](docs/spark-msk-offset.md) |
| 構成図（HTML） | [docs/20260914-fukuda-nwc-poc-architecture.html](docs/20260914-fukuda-nwc-poc-architecture.html) |
