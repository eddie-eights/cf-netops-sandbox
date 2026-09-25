# 構成

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

## チャットの経路

```mermaid
flowchart LR
  PC["利用者の PC<br/>localhost:8080"] -->|"SSM（ssmmessages、TLS）"| WEB["Web の EC2<br/>Gradio 127.0.0.1:8080<br/>チャット / トポロジ / 異常一覧 / 承認"]
  WEB -->|"invoke_agent_runtime"| RT["AgentCore Runtime<br/>agent/app.py"]
  RT -->|"Retrieve（HYBRID + Rerank、20 件 → 5 件）"| KB["ナレッジベース<br/>CREATE_KB=1 のときだけ"]
  RT -->|"Converse + Guardrail"| LLM["Nova 2 Lite（jp.）"]
  RT -->|"ツール（最大 5 往復）"| TOOLS["list_devices / neighbors / blast_radius / topology_graph<br/>list_anomalies / search_logs / query_metrics / query_history"]
  RT -.->|"WORKFLOW=1"| GW["Gateway（MCP）→ tools Lambda"]
```

- EC2 にパブリック IP も受信ルールも無い。SSM Agent が内側から ssmmessages へつなぐ。
- Runtime を呼ぶのは EC2 のインスタンスロール。ブラウザに AWS の認証情報は置かない。
- ツールは Neptune（トポロジ・異常・修復案。無ければトポロジは `agent/data/` の静的な 10 台）、OpenSearch Serverless、Prometheus を読む。

## パイプラインと WORKFLOW

```mermaid
flowchart LR
  LAB["lab の EC2<br/>containerlab + FRR"] -->|"SNMP ポーリング 10 秒 / trap / FRR のログ"| TG["Telegraf の EC2"] --> MSK["MSK<br/>metrics / traps / logs"]
  MSK --> SPARK["Spark（EMR Serverless）"]
  SPARK -->|"全トピック（正本）"| ICE["S3 Tables<br/>snmp_metrics"]
  SPARK -->|"traps / logs"| OS["OpenSearch<br/>snmp-logs"]
  SPARK -->|"metrics"| PROM["Prometheus"]
  SPARK -.->|"全トピック（SINK_SPLUNK=1 のとき）"| SPL["Splunk HEC<br/>AWS の外"]
  SPARK -->|"開いた / 閉じた"| AEV["S3 Tables<br/>anomaly_events（証跡）"]
  SPARK -->|"異常の「いま」"| NEP["Neptune<br/>トポロジ + 異常 + 修復案"]
  SPARK -->|"AnomalyOpened"| EB["EventBridge"]
  EB --> GL["graph の Lambda<br/>IF の status"] --> NEP
  EB --> SQS["SQS"] --> WF["Temporal（ECS Fargate）<br/>調査 → 承認 → 修復"]
  WF <-->|"修復案"| NEP
  WF -->|"作成・承認・却下・適用・確認"| PEV["S3 Tables<br/>proposal_events（証跡）"]
  WF -->|"SSM Run Command"| LAB
```

WORKFLOW の流れは [workflow.md](workflow.md)、データの置き場は [data-stores.md](data-stores.md)。

## どのファイルがどこで動くか

`app.py` が 2 つあり、動く場所が違う。

| ファイル | 動く場所 | 設定 |
|---|---|---|
| `web/app.py`（と `web/` の他のファイル） | Web の EC2（`<prefix>-web.service`） | `/etc/<prefix>-web.env`。Runtime の ARN は SSM の `<prefix>/runtime-arn` を 60 秒キャッシュで読む |
| `agent/app.py` | AgentCore Runtime のコンテナ | `terraform/agent/runtime.tf` の環境変数 |

**`agent/app.py` を EC2 に置かない。**`KeyError: 'MODEL_ID'` か `404` になり、cloud-init のログに `is not web/app.py` が出る。EC2 のロールに権限を足して直さない。

| パス | 中身 |
|---|---|
| `terraform/` | AWS にリソースを作るのはここだけ（下のツリー） |
| `agent/` | Runtime のエージェントとツール。`data/` は静的トポロジ |
| `web/` | Gradio の画面 |
| `workflow/` | Temporal のワークフローとワーカー |
| `tools/` | Gateway（MCP）の tools Lambda |
| `spark/` | Spark のジョブ（`snmp_sinks.py`） |
| `lab/` | containerlab の構成、FRR、snmpd、Telegraf の EC2 への転送（`lab forward`） |
| `telegraf/` | Telegraf の設定と `tg`（Telegraf の EC2 で動く） |
| `graph/` | Neptune の `status` を書く Lambda |
| `kb-docs/` | ナレッジベースに入れる手順書 |
| `ops/` | `up.sh` / `down.sh` / `check.sh` など |
| `tests/` | 模擬テスト（AWS を呼ばない） |

```
terraform/
├── base/
│   ├── ecr/         ECR リポジトリ
│   └── core/        VPC / SG / エンドポイント / バケット / ロール / Web の EC2
├── agent/         AGENT=1     Runtime / ガードレール / KB
├── pipeline/      PIPELINE=1
│   ├── lab/         containerlab の EC2 と Telegraf の EC2（stream を作るとき）
│   ├── stream/      MSK
│   ├── analytics/   EMR Serverless / S3 Tables / OpenSearch / Prometheus
│   └── graph/       Neptune
└── workflow/      WORKFLOW=1  Temporal on ECS / Gateway（MCP）
```

1 ディレクトリ = 1 state。state は各ルートの `terraform.tfstate`（ローカル）。変数を変えたいときは `terraform.tfvars.example` を `terraform.tfvars` に写す。

## 名前とタグ

- リソース名は `<prefix>-<何>`。Runtime だけはハイフンが使えないので `<owner>_nwc_poc_agent`。
- タグを付けられるものには全部 `Project=<prefix>` と `owner=<owner>` が付く（各ルートの `providers.tf` の `default_tags`）。
- 作ったものの一覧:

```bash
aws resourcegroupstaggingapi get-resources --region ap-northeast-1 \
  --tag-filters Key=Project,Values=<prefix> \
  --query 'ResourceTagMappingList[].ResourceARN' --output table
```

タグが付かないもの: ENI、Runtime のロググループ（`ops/up.sh` の手順 9 で付ける）、OpenSearch Serverless のポリシーとインデックス、KB のデータソース、ガードレールの版。

## ログ

| 何 | どこ |
|---|---|
| エージェントの実行ログ | CloudWatch Logs `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`（出力 `runtime_log_group_name`、保持 7 日） |
| Web の失敗 | Web の EC2 の `journalctl -u <prefix>-web` |
| ガードレールで止めたか | Runtime のログの `stop=guardrail_intervened` |
| 誰がいつ入ったか | CloudTrail の `StartSession` |

ポートフォワーディングの中身は Session Manager のセッションログに残らない。会話の中身もどこにも保存しない。
