# 構成

← [README](../README.md)

図解（構成図・通信の順番・作るリソース・費用）は [`20260914-netops-poc-architecture.html`](20260914-netops-poc-architecture.html)。ブラウザで開く（`docs/design-system/` を同じ場所に置いたまま）。
機能ごとの概要（どこまで作ってあって、何が決まっていないか）は [`phases.md`](phases.md)。

```
利用者の PC
  │ aws ssm start-session（AWS-StartPortForwardingSession）
  │   ブラウザ → http://localhost:8080/ → Session Manager plugin
  │   ─ TLS（WebSocket）─▶ ssmmessages エンドポイント
  ▼
EC2（AL2023 arm64、プライベートサブネット、受信ルールなし）
  │ SSM Agent → 127.0.0.1:8080 の Web（Gradio。タブは「チャット」「トポロジ」「異常一覧」「承認」）
  │ boto3 invoke_agent_runtime（インスタンスロールで署名）
  ▼ bedrock-agentcore エンドポイント
AgentCore Runtime（VPC モード。terraform/agent。AGENT=1）
  │ 1. Retrieve（HYBRID + Rerank。CREATE_KB=1 のときだけ）─ bedrock-agent-runtime エンドポイント ─▶ Knowledge Base
  │                                                                 └▶ OpenSearch Serverless（Bedrock がサービス側から検索。候補 20 件）
  │                                                                 └▶ Amazon Rerank 1.0（候補を並べ替えて上位 5 件）
  │ 2. Converse + ガードレール + ツール ─ bedrock-runtime エンドポイント ─▶ Guardrail が質問を判定
  │      ↑ モデルが list_devices / neighbors / blast_radius を呼んだら        └▶ Amazon Nova 2 Lite（jp 推論プロファイル）
  │        トポロジ（Neptune があればそこから、無ければ agent/data/）で答えて往復（最大 5 回）  └▶ Guardrail が回答を判定
  │        list_anomalies を呼んだら DynamoDB の異常一覧（terraform/pipeline/stream のテーブルに terraform/pipeline/analytics の Spark が書く。PIPELINE=1 で SKIP_STREAM / SKIP_ANALYTICS が空のとき）を返す
  │        search_logs / query_metrics を呼んだら OpenSearch Serverless のログ / Prometheus のメトリクス（terraform/pipeline/analytics の sinks。deploy.env の SINK_*）を返す
  │        WORKFLOW=1 で Gateway があれば、ツールの一覧は Gateway（MCP の tools/list）から取り、呼び出しも Gateway（tools/call → tools Lambda）に投げる
  ▼ 回答の末尾に参照した md のファイル名を付けて返す

取り込み（CREATE_KB=1 のとき。利用者が手で行う）: kb-docs/*.md ─ aws s3 cp ─▶ S3 ─ start-ingestion-job ─▶ Titan Embeddings V2 ─▶ OpenSearch Serverless
Web の部品（利用者が手で行う）: web/*.py + agent/data/ + wheels/ ─ aws s3 sync ─▶ S3 の web/ ─ 起動時に EC2 が取る

データパイプライン（PIPELINE=1。terraform/pipeline/lab → stream → analytics と terraform/pipeline/graph の 4 ルート。使う日だけ作って当日中に消す）:
  lab: 同じ VPC の EC2 1 台で containerlab + FRR × 6 + snmpd × 4 + ホスト × 4 を動かす。
    SSM セッションで入って `sudo lab check` / `sudo lab failover`。イメージは ECR（terraform/base/ecr）、設定と rpm は S3 の lab/。
  lab EC2 の Telegraf ─ SNMP ポーリング（10 秒、CE 4 台）+ SNMP trap（linkUp/linkDown、snmpd → 203.0.113.1:162）+ FRR のログ（6 台。/var/log/netops-lab/<機器名>/frr.log を tail）
    ─ Kafka（IAM 認証、9098）─▶ MSK（2 ブローカー、terraform/pipeline/stream）─┬▶ MSK Connect（S3 sink）─▶ S3 の stream/（任意。CREATE_S3_SINK=0 で外す）
                                                                 ├▶ Spark（EMR Serverless、terraform/pipeline/analytics）─ 全トピック ─▶ S3 Tables（Iceberg）の snmp_metrics（履歴の正本。60 秒ごとに追記）
                                                                 ├▶ Spark ─ traps / logs（ログ）だけ ─▶ OpenSearch Serverless の snmp-logs（SINK_OPENSEARCH。既定で作る）
                                                                 ├▶ Spark ─ metrics だけ ─▶ Amazon Managed Service for Prometheus（SINK_PROMETHEUS。既定で作る）
                                                                 └▶ Spark の detect ─ link_down を見つける ─▶ DynamoDB の異常テーブル（terraform/pipeline/stream）─▶ Web の「異常一覧」/ エージェントの list_anomalies
                                                                                                        └▶ EventBridge に AnomalyOpened（source <接頭辞>.spark。WORKFLOW の SQS が受ける）
    Kafka から 4 つに分ける設計の 4 本目、log + metrics → Splunk は Kafka の sink（MSK Connect）にする予定で後回し。Grafana での可視化も後回し
  Neptune（terraform/pipeline/graph）─ Gremlin（boto3 neptunedata、IAM 認証）─▶ エージェントの topology.py と Web の「トポロジ」タブ（図・表・リンクの追加削除）

Temporal での実行（WORKFLOW=1。terraform/workflow の 1 ルート。AGENT と PIPELINE の lab / stream / analytics が要る。graph が無ければトポロジは静的、SINK_* を 0 にするとそのツールは「配備されていない」を返す）:
  ECS on Fargate（ARM64、1 タスク = temporal コンテナ（temporal server start-dev、SQLite）+ worker コンテナ）を同じ VPC のプライベートサブネットに置く
  EventBridge のルール（`<接頭辞>.spark` / AnomalyOpened）─▶ SQS ─ worker の starter が long polling（20 秒）─▶ 異常ごとに Temporal のワークフロー investigate-<anomaly_id>
    （SQS が無ければ 60 秒ごとに DynamoDB の異常テーブルの open を見る。5 回失敗したメッセージは DLQ へ）
    ─ AgentCore Runtime を呼んで原因と修復案（JSON）を得る ─▶ DynamoDB の修復案テーブル（status = pending）─▶ Web の「承認」タブ
    ─ 人が承認（approved）─▶ SSM Run Command で lab の EC2 に `sudo lab heal-main` / `sudo lab check` ─▶ 異常が resolved になるまで 30 秒おきに確かめる ─▶ verified / failed
  AgentCore Gateway（MCP、IAM 認証）─▶ tools Lambda（tools/handler.py。VPC の中。list_devices / neighbors / blast_radius を Neptune（無ければ静的）、list_anomalies を DynamoDB、
    search_logs を OpenSearch Serverless、query_metrics を Prometheus で答える。query_history（S3 Tables）は Athena をまだ置いていないので案内だけ）
    Runtime は起動後 5 分以内に SSM の gateway-url を拾い、以後ツールの一覧と呼び出しを Gateway に投げる（Gateway が無ければ今までどおりコンテナの中で答える）
  Temporal の UI（8233）は Web の EC2 経由の SSM ポートフォワーディングで PC から開く
```

**どのファイルがどこで動くか。**`app.py` という名前のファイルが 2 つあり、動く場所が違う。置き場所を取り違えると動かない。

| ファイル | 動く場所 | 何をするか | 環境変数を入れるもの |
|---|---|---|---|
| `web/app.py`（画面の組み立てだけ。中身は同じ `web/` の `config.py` / `chat.py` / `topology_view.py` / `incident_view.py`） | **EC2**（`netops-nwc-poc-web.service`、127.0.0.1:8080） | Gradio の画面（チャット・トポロジ・異常一覧・承認）。チャットは boto3 の `invoke_agent_runtime` で Runtime に投げるだけで、モデルもナレッジベースも直接は呼ばない | `terraform/base/core` の user_data（`templates/web_user_data.sh.tftpl`）が `/etc/netops-nwc-poc-web.env` に書く（`AWS_REGION` / `PARAM_PREFIX` など）。Runtime の ARN は `terraform/agent` が書く SSM の `<prefix>/runtime-arn` を 60 秒キャッシュで読む（agent を作り直しても EC2 は作り直さない） |
| `agent/app.py` | **AgentCore Runtime のコンテナ**（手順 2 で ECR に push したイメージ） | `BedrockAgentCoreApp`。`POST /invocations` を受けて Retrieve → Converse → ツールを回す。画面は無い | `terraform/agent/runtime.tf` の `environment_variables`（`MODEL_ID` / `GUARDRAIL_ID` など。`KNOWLEDGE_BASE_ID` は `CREATE_KB=1` のときだけ） |

**`agent/app.py` を EC2 に置かない**（S3 の `web/app.py` に上書きしない）。置くと `KeyError: 'MODEL_ID'` で落ちるか、立ってもブラウザが `404 Not Found` になり、EC2 のロールに `bedrock:Retrieve` / `bedrock:InvokeModel` が無いのでその先にも進めない。無いのは意図した形で、それらは Runtime のロール（`runtime.tf` の `aws_iam_role.runtime`）が持つ。EC2 のロールに足して直さない。`terraform/base/core` の user_data は起動時にこれを検出し、`is not web/app.py` と cloud-init のログに出して止まる。


| ファイル | 中身 |
|---|---|
| `terraform/base/ecr/` | エージェントと lab、WORKFLOW のワーカーと Temporal（ミラー）のイメージの ECR リポジトリ（タグは上書き不可、destroy でイメージごと消える）。**最初に apply する。**`terraform/agent` がリポジトリの URL をこのルートの state から読む |
| `terraform/base/core/` | 土台（必ず作る）。`vpc.tf`（VPC・サブネット 2 つ・ルートテーブル）/ `security_groups.tf`（Runtime / Web / エンドポイントの SG）/ `endpoints.tf`（ssm / ssmmessages、S3 ゲートウェイ、共用の ecr.api / ecr.dkr / logs）/ `bucket.tf`（S3 バケット）/ `runtime.tf`（Runtime と Web のロールだけ。ポリシーは各機能のルートが足す）/ `web.tf`（EC2）/ `templates/web_user_data.sh.tftpl`（起動時に S3 から Web を取って入れる）/ `locals.tf`。他のルートが VPC / サブネット / SG / バケット / ロール名をこの state から読む |
| `terraform/agent/` | AGENT。`runtime.tf`（AgentCore Runtime と Runtime のロールのポリシー、Web に ARN を渡す SSM の `runtime-arn`、Web のロールへの `InvokeAgentRuntime`）/ `network.tf`（Runtime 用のエンドポイント bedrock-runtime × 2 AZ と、Web 用の bedrock-agentcore × 1 AZ。ecr.api / ecr.dkr / logs は lab と Spark も使うので `terraform/base/core` が持つ）/ `kb.tf`（ガードレールと、`create_knowledge_base=true` のときだけ OpenSearch Serverless・インデックス・ナレッジベース）/ `locals.tf`。`terraform/base/ecr` と `terraform/base/core` の state を読む |
| `terraform/pipeline/lab/` | PIPELINE。containerlab + FRR の lab を動かす EC2 1 台。`network.tf`（SG とエンドポイントへの 443）/ `iam.tf`（インスタンスロール）/ `instance.tf`（EC2 と `templates/lab_user_data.sh.tftpl`）/ `locals.tf`。VPC / サブネット / SG / バケットは `terraform/base/core` の state から読む。stream を作るときは Telegraf も入れる |
| `terraform/pipeline/stream/` | PIPELINE。`network.tf`（SG と sts・dynamodb エンドポイント）/ `msk.tf`（MSK。2 ブローカー、IAM 認証）/ `anomalies.tf`（DynamoDB の異常テーブル。書くのは analytics の Spark）/ `sink.tf`（MSK Connect の S3 sink）/ `access.tf`（`terraform/base/core` と `terraform/pipeline/lab` のロールに足す権限）/ `locals.tf`。`terraform/base/core` と `terraform/pipeline/lab` の state を読む |
| `terraform/pipeline/analytics/` | PIPELINE。`tables.tf`（S3 Tables のテーブルバケット・namespace `netops`・Iceberg テーブル `snmp_metrics`）/ `emr.tf`（EMR Serverless の Spark アプリケーション。ARM64、アイドル 15 分で止まる）/ `access.tf`（ジョブの実行ロール）/ `sinks.tf`（OpenSearch Serverless の logs コレクションと Prometheus のワークスペース。`sinks` で生える）/ `network.tf`（EMR の SG、MSK への 9098、`s3tables` / `aps-workspaces` / `events` の interface エンドポイント）/ `locals.tf` / `outputs.tf`（`start-job-run` に渡す JSON）。`terraform/base/core` と `terraform/pipeline/stream` の state を読む |
| `spark/snmp_sinks.py` | analytics のジョブ本体。Kafka の `metrics` / `traps` / `logs` を読み、Telegraf の JSON を行にして格納先に 60 秒ごとに流す（Structured Streaming。格納先ごとに別のクエリ）。iceberg = 全トピック → S3 Tables、opensearch = `traps` と `logs`（FRR のログ）→ OpenSearch Serverless の `_bulk`（SigV4）、prometheus = `metrics` の数値の field → Amazon Managed Service for Prometheus の remote write（protobuf + snappy を手組み）。detect = `link_down` を DynamoDB の異常テーブルに open / resolved で書き、開いた瞬間に EventBridge へ `AnomalyOpened` を出す。`ops/up.sh` が jar 6 本と一緒に S3 の `analytics/` に置く（a-1） |
| `terraform/pipeline/graph/` | PIPELINE。Neptune（db.t4g.medium × 1、IAM 認証）と、`terraform/base/core` のロールへの Gremlin 権限、Spark の検知（EventBridge）を受けて機器と回線の `status` を書く Lambda（`graph/status_handler.py`）。`network.tf`（SG とサブネットグループ）/ `neptune.tf` / `access.tf`（ロールへの権限）/ `sync.tf`（Lambda と EventBridge）/ `locals.tf`。`terraform/base/core` の state を読む。無ければ静的データで動く |
| `terraform/workflow/` | WORKFLOW。`ecs.tf`（ECS クラスタ・タスク定義（temporal + worker の 2 コンテナ、ARM64、1 vCPU / 2 GB）・サービス・SG・ロググループ）/ `proposals.tf`（DynamoDB の修復案テーブルと SSM の `proposal-table`、Web と Runtime のロールへの読み書き権限）/ `iam.tf`（タスクのロール。Runtime の呼び出し、lab の EC2 への `AWS-RunShellScript` だけ）/ `events.tf`（EventBridge のルール `AnomalyOpened` → SQS `<prefix>-anomalies` と DLQ、`sqs` の interface エンドポイント）/ `gateway.tf`（AgentCore Gateway（MCP、IAM 認証）と VPC の中の tools Lambda、SSM の `gateway-url`。`create_gateway=false` で外せる）/ `locals.tf`。`terraform/base/ecr` / `main` / `agent` / `lab` / `stream` の state を読み、`graph` / `analytics` は有れば読む |
| `workflow/` | ワーカーのコンテナ（`worker.py` / `awsio.py` / `rules.py` / `Dockerfile` / `requirements.txt`。Python 3.13、`temporalio` SDK、arm64）。starter（SQS の `AnomalyOpened` を受けてワークフローを起こす。キューが無ければ DynamoDB を 60 秒ごとに見る）とワークフロー（調査 → 修復案 → 承認待ち → lab で修復 → 確認）が 1 プロセス。`worker.py` は Temporal の定義だけ、`awsio.py` が AWS の呼び出し、`rules.py` が判断だけの純粋関数（標準ライブラリしか読まないので Temporal のサンドボックスをそのまま通る） |
| `tools/` | Gateway のツール（`tools.json` が MCP のツール定義 9 つ、`handler.py` が Lambda の本体。`agent/` の `toolkit.py` / `topology.py` / `graph.py` / `anomalies.py` / `evidence.py` / `proposals.py` と `data/topology.json` を同じ zip に入れる。一覧は `terraform/workflow/gateway.tf` の `tools_files`） |
| `terraform/<ルート>/terraform.tfvars.example` | 変数と既定値の一覧。既定のままでよい。変えたいときだけ同じ場所の `terraform.tfvars` に写す（配布物には入っていない） |
| `terraform/<ルート>/terraform.tfstate` | apply すると PC にできる state（配布物には入っていない）。**Terraform が何を作ったかの記録で、これを消すと destroy できなくなる。**ARN などが平文で入るので共有しない。apply した PC に残るので、destroy もその PC で打つ（「[デプロイの詳しい説明](deploy.md)」の注意） |
| `agent/` | Runtime に載せるコンテナ（Python 3.13、`bedrock-agentcore` SDK、arm64）。`toolkit.py` が全モジュール共通の土台（リージョン・SSM パラメータの読み出し・boto3 クライアント。Runtime / tools Lambda / Web の EC2 / graph の status Lambda の 4 か所で動く）、`topology.py` がトポロジのツール（Neptune → 静的の順）、`graph.py` が Neptune の読み書き、`anomalies.py` が異常一覧のツール、`evidence.py` が調査の証拠のツール（`search_logs` = OpenSearch Serverless、`query_metrics` = Prometheus、`query_history` = S3 Tables（Athena 未配備なので案内だけ））、`mcp_client.py` が Gateway（MCP）の tools/list と tools/call（SigV4。無ければコンテナの中のツールに戻る）、`proposals.py` が修復案の読み書き（Web と共用）、`data/` が静的トポロジ（`devices.yaml` / `topology.json`、架空の 10 台） |
| `web/` | EC2 で動かす Gradio の画面と依存（`requirements.txt`）。`app.py` が画面の組み立て、`config.py` が環境変数と `agent/` へのパス通し、`chat.py` が「チャット」タブ、`topology_view.py` が「トポロジ」タブ、`incident_view.py` が「異常一覧」「承認」タブ。`agent/` の 5 モジュールと一緒に S3 に置く（出力 `upload_web_command`） |
| `.env.example` | 環境変数の一覧（Web / エージェント / lab。意味と AWS 上で誰が入れるか）。AWS 上では Terraform（user_data と Runtime の環境変数）が書くので手で用意しない。EC2 で Web が立たないときの見比べ先で、手元で `web/app.py` を動かすときは `.env` に写して使う（「[Web を手元で動かす](development.md)」） |
| `deploy.env.example` | `ops/up.sh` / `ops/down.sh` の設定の見本（デプロイする人の名前、どの機能を作るか、ECR を残すかなど）。`cp deploy.env.example deploy.env` で写して書く。`deploy.env` は配布物には入っていないので自分で作る。**`OWNER` は必須**で、機能を何も書かなければ土台と AGENT を作る |
| `lab/` | lab の材料。`wanlab.clab.yml.in`（containerlab の定義。イメージ名は起動時に埋める）、`frr/`、`snmpd/`（Dockerfile と設定。trap の送信も）、`telegraf.conf.in`（ポーリングと trap 受信、FRR のログの tail → MSK の `metrics` / `traps` / `logs`）、`lab.sh`、`lab_topology.py`（定義から Neptune に入れる機器と回線を作る。「[Neptune のトポロジを lab から作る](pipeline.md)」） |
| `graph/` | `status_handler.py`。`terraform/pipeline/graph` の Lambda で、Spark の検知（EventBridge の `AnomalyOpened` / `AnomalyResolved`）を受けて Neptune の機器と回線の `status` を書く。`agent/graph.py` と一緒に zip になる |
| `kb-docs/` | ナレッジベースに入れる手順書の例（架空の md 3 つ。`CREATE_KB=1` のときだけ使う） |
| `ops/` | `up.sh`（`deploy.env` の `AGENT` / `PIPELINE` / `WORKFLOW` で選んだ機能を、手順 1〜7・lab・graph・stream・analytics・workflow の順に 1 本で打つ）と `down.sh`（片付けをまとめて打つ）。毎日消して作り直す運用向け（「[デプロイの詳しい説明](deploy.md)」）。`deploy-env.sh` は 2 本が読む `deploy.env` の読み込み（シェルとしては実行しない）。`seed_graph.py` は `up.sh` が Web の EC2 の上で打つ Neptune への投入（`lab/lab_topology.py` が lab の定義から作った機器と回線を受け取る）。`sync-graph.sh` は起動後にトポロジを入れ直す（「[Neptune のトポロジを lab から作る](pipeline.md)」）。`check.sh` は AWS に触らない検査をまとめて打つ（「[手元で確かめる](development.md)」）。`vscode-setup.sh` は VS Code の設定を入れる（「VS Code の設定」） |
| `tests/` | 模擬テスト（AWS に触れない。打ち方は「[手元で確かめる](development.md)」）。`test_app.py`（エージェント）、`test_graph.py`（Neptune の読み書き・動的な状態・静的への切り戻し）、`test_sync.py`（lab の定義からのトポロジ、状態を書く Lambda、`terraform/pipeline/graph` の配線）、`test_stream.py`（Spark の検知（`spark/snmp_sinks.py` の detect）と `terraform/pipeline/stream` の配線）、`test_analytics.py`（`terraform/pipeline/analytics` と Spark のスクリプトの整合）、`test_workflow.py`（ワーカー・修復案・MCP クライアント・tools Lambda と `terraform/workflow` の配線） |

## なぜこの形にしたか

| 要件 | どう満たすか |
|---|---|
| インターネットから届かない | EC2 にパブリック IP も受信ルールも無い。SSM Agent が内側から ssmmessages へつなぎに行く |
| 通信が暗号化される | PC から AWS までは Session Manager の TLS。ブラウザは自分の PC の `localhost` を見るだけ |
| 人単位で絞れて、記録が残る | 入れるかどうかは IAM の `ssm:StartSession` で決まる。誰がいつ入ったかは CloudTrail に残る |
| 証明書が要らない | `localhost` はブラウザが安全なコンテキストとして扱う。hosts の書き換えも要らない |
| ブラウザに AWS の認証情報を置かない | Runtime を呼ぶのは EC2 のインスタンスロール |
| 画面は Gradio | チャット・図・表を Python だけで組める。EC2 はインターネットに出ないので、依存は arm64 / cp313 の wheel を S3 に置いて `pip install --no-index` で入れる。テレメトリは環境変数で止める（`GRADIO_ANALYTICS_ENABLED=False` / `HF_HUB_OFFLINE=1`） |
| トポロジは静的データをコンテナに同梱 | AGENT だけの範囲では機器から取らない。`agent/data/` の 10 台（架空）を Converse のツール（`list_devices` / `neighbors` / `blast_radius` / `topology_graph`）としてモデルに渡す。読むだけなので副作用が無く、Runtime に権限を足さなくてよい。Web の「トポロジ」タブも同じデータ。PIPELINE で graph を作ると Neptune のデータに替わり、Web から編集できる |
| lab は別ルート・EC2 1 台 | containerlab は veth と network namespace を使うので ECS / Fargate では動かない。同じ VPC に置くが Web やエージェントとはつながず、使うときだけ起動する。lab の中のアドレスは EC2 の中の docker network に閉じて VPC には出ない |
| state はローカル | 使う人が 1 人で、毎日 destroy して作り直す。state 用の S3 バケットとロックを別に用意しない。代わりに、apply と destroy は同じ PC で打つ |

### ナレッジベースとガードレール

ナレッジベースは `CREATE_KB=1`（`terraform/agent` の `create_knowledge_base=true`）のときだけ作る。既定では作らず、エージェントはモデルとトポロジのツールだけで答える（OpenSearch Serverless の最小 OCU $0.33/h を避けるため）。以下は作るときの形。

| 決めたこと | 理由 |
|---|---|
| 検索はハイブリッド（`overrideSearchType: HYBRID`） | `%BGP-5-ADJCHANGE` のようなログの文字列やコマンド名は、意味の近さ（ベクトル）より文字の一致（キーワード）で当たる。両方を混ぜる |
| ベクトルストアは OpenSearch Serverless | Bedrock のハイブリッド検索に対応するストアのうち、Terraform（opensearch provider）でインデックスまで作れる |
| インデックスは faiss / hnsw、1024 次元、テキストのフィールドを `index: true` | ハイブリッド検索の条件。1024 は Titan Text Embeddings V2 の既定の次元数 |
| 候補を 20 件取り、リランクで 5 件に絞る（変数 `number_of_results` / `number_of_reranked_results`） | ハイブリッド検索の順位は、ベクトルとキーワードの点数を合わせたもので、質問への答えになっているかまでは見ていない。リランクモデルが質問と各資料を読み比べて並べ替える。モデルに渡すのは 5 件のままなので、トークンは増えない |
| リランクは `Retrieve` の `rerankingConfiguration` で行い、モデルは Amazon Rerank 1.0 | 別の API を呼ぶ往復が増えない。Amazon のモデルは AWS Marketplace を通さないので、他社モデルの購読・EULA への同意・Marketplace 経由の請求が発生しない。単価も Cohere Rerank 3.5 の半分。止めるときは変数 `rerank_model_id` を空にする |
| モデルは Amazon Nova 2 Lite を `jp.` の推論プロファイルで呼ぶ | Amazon のモデルは AWS Marketplace を通さないので、購読・EULA への同意・初回利用フォームが要らず、請求も Bedrock の料金として来る（Claude などの他社モデルは Marketplace の料金になる）。推論は東京と大阪だけで走る（東京に In-Region の呼び出しは無い）。Converse とガードレールに対応し、日本語は最適化の対象。単価は Claude Haiku 4.5 の 3 分の 1 強 |
| リランクの権限はナレッジベースのロールに付ける | `Retrieve` の中のリランクはナレッジベースのサービスロールで動く（文書どおり）。Runtime のロールは `bedrock:Retrieve` のままでよい |
| Runtime は OpenSearch を直接呼ばず `Retrieve` を呼ぶ | Runtime に要る権限が `bedrock:Retrieve` だけになり、VPC から出るのは bedrock-agent-runtime エンドポイントだけで済む |
| ガードレールは Standard 階層 | Classic 階層は英語・フランス語・スペイン語だけで、日本語の質問を判定できない |
| 質問は `guardContent` に入れ、資料はふつうの `text` にする | 入力の判定を質問だけにする。資料まで判定すると、手順書の「攻撃」「遮断」などで止まりやすく、判定の文字数（課金）も増える。回答は全体を判定する |
| フィルタは MEDIUM、プロンプト攻撃だけ HIGH | 運用の語で誤検知しにくくする。止めすぎるなら `terraform/agent/kb.tf` の `aws_bedrock_guardrail.this` で下げる |
| ガードレールに止められた往復は履歴に残さない | 次の質問の文脈に混ぜない |

却下した案は次の通り。

| 案 | 却下した理由 |
|---|---|
| EC2 にプライベート IP で直接 HTTP | 平文で、ネットワークに届く人は誰でも開ける。HTTPS にするには証明書が要る |
| Private API Gateway + S3 | 名前解決（hosts かエンドポイントの DNS 名）が要り、利用者の認証も別に作る必要がある |
| ブラウザから AgentCore を直接呼ぶ | 認証情報をブラウザに置くか、インターネット上の IdP が要る |
| NLB / ALB | 時間課金が増える。証明書の問題は残る |

## 名前とタグ

- リソース名は `netops-nwc-poc-<何>`。接頭辞はデプロイする人の名前（変数 `owner`。`deploy.env` の `OWNER`）から、各ルートの `locals.tf` が `<owner>-nwc-poc` として作る。Runtime 名だけはハイフンが使えないので `netops_nwc_poc_agent`。
- タグを付けられるリソースには全部 `Project=netops-nwc-poc`・`owner=netops`（どちらも変数 `owner` から決まる）を付ける。各ルートの `providers.tf` の `default_tags` で付けるので、リソースごとに書き忘れることは無い。`Name` は個別に付けている。
- 作ったものの一覧はこれで出る。

```bash
aws resourcegroupstaggingapi get-resources --region ap-northeast-1 \
  --tag-filters Key=Project,Values=netops-nwc-poc \
  --query 'ResourceTagMappingList[].ResourceARN' --output table
```

**タグが付かないもの**: EC2 の ENI（エンドポイントのものを含む）、AgentCore が作る Runtime のロググループと ENI、
OpenSearch Serverless のセキュリティポリシー・アクセスポリシー・インデックス、ナレッジベースのデータソース、ガードレールの版。
ロググループは手順 5 で手で付ける。

## ログ

| 何のログ | どこ | 保持 |
|---|---|---|
| 誰がいつセッションを開いたか | CloudTrail の `StartSession` / `TerminateSession` | 組織の CloudTrail の設定 |
| エージェントの実行ログ | CloudWatch Logs `/aws/bedrock-agentcore/runtimes/<agent_runtime_id>-DEFAULT`（出力 `runtime_log_group_name`） | 手順 5 で 7 日に設定。付けないと無期限 |
| チャット Web の呼び出し失敗 | EC2 の journald（`journalctl -u netops-nwc-poc-web`） | インスタンスの中だけ。終了すると消える |
| 取り込みの結果（失敗したファイル） | `aws bedrock-agent get-ingestion-job` の `statistics` と `failureReasons` | ジョブの履歴として残る |
| ガードレールで止めたか | Runtime のログの `stop=guardrail_intervened` | Runtime のロググループと同じ |

- **ポートフォワーディングのセッションは、Session Manager のセッションログ（S3 / CloudWatch Logs）の対象外。**AWS のドキュメントに明記されている。記録されるのは接続したという事実（CloudTrail）だけ。
- 会話の中身はどこにも保存しない。残したいなら Bedrock のモデル呼び出しログ（アカウント単位の設定）を使う。
- Cost Explorer で `Project` 別に費用を見るには、Billing のコスト配分タグで `Project` を有効にする（組織の管理アカウントで行う設定）。

