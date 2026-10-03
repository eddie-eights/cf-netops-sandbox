# 構成

← [README](../../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

スライドの構成図は [architecture.pptx](../architecture.pptx)。5 枚で、全体像、① 〜 ③ 収集から格納まで、④ 〜 ⑤ 検知から修復まで、Nautobot 連携、どこで何が動くか（配置と閉域）の順。

構成の説明は terraform のルートに合わせて 4 つに分けてある。

| ファイル | terraform | 中身 |
|---|---|---|
| [core.md](core.md) | `base/core`、`base/ecr` | 土台: VPC、Web の EC2、アラートの SNS トピック、閉域（エンドポイント + Deny）、SG（通信の表） |
| [agent.md](agent.md) | `agent/`（`AGENT=1`） | チャットの経路: Web → AgentCore Runtime → Nova 2 Lite・ガードレール・KB・ツール |
| [pipeline.md](pipeline.md) | `pipeline/`（`PIPELINE=1`） | lab → Telegraf → MSK → Spark → 格納先、Grafana と Splunk のアラート、Neptune のトポロジ |
| [workflow.md](workflow.md) | `workflow/`（`WORKFLOW=1`） | アラート（SNS → SQS）→ Temporal の調査・承認・修復、Gateway（MCP） |

使い方は別のファイル: パイプラインは [pipeline.md](../pipeline.md)、機器から集めるデータは [collection.md](../collection.md)、承認の流れは [workflow.md](../workflow.md)、データの置き場は [data-stores.md](../data-stores.md)。

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
| `spark/` | Spark のジョブ（`snmp_sinks.py`。格納先へ流すだけで、検知はしない） |
| `lab/` | containerlab の構成、SR Linux の設定（`srlinux/*.cli`）、EC2 の支度（`setup.sh`。lab とデバッグ用の EC2 で共通）、Telegraf（ECS）への転送（`lab forward`）、デバッグ用の EC2 の Telegraf（`lab telegraf`） |
| `telegraf/` | Telegraf の `Dockerfile`、設定（`telegraf.conf.in`）と `tg`（stream の ECS のタスクで動く。デバッグ用の EC2 でも docker で `SINK=stdout`） |
| `grafana/` | Grafana の `Dockerfile` と provisioning（データソース、ダッシュボード、アラート（`alerting/netops.yaml`）。analytics の ECS のタスクで動く） |
| `splunk/` | Splunk の `Dockerfile`（公式イメージ + 検知のアプリ）と、アプリ `netops_alerts`（保存済みサーチと、SNS へ publish するアラートアクション。analytics の ECS のタスクで動く） |
| `nautobot/` | Nautobot の `Dockerfile`（公式イメージ + boto3）、Job（`jobs/netops_jobs.py`）と、その中身（`netops/`。対応付け `nb_map.py`、同期 `nb_sync.py`、起動時の `bootstrap.py`）。`NAUTOBOT=1` のとき ECS で動く |
| `graph/` | アラート（SNS）を受けて Neptune の `status` を書く Lambda |
| `kb-docs/` | ナレッジベースに入れる手順書 |
| `ops/` | `up.sh` / `down.sh` / `check.sh` など |
| `tests/` | 模擬テスト（AWS を呼ばない） |

```
terraform/
├── base/
│   ├── ecr/         ECR リポジトリ
│   └── core/        VPC / VPC エンドポイント / 閉域の Deny（perimeter.tf）/ SG（ワークロードごと。通信の表は security_groups.tf）/ フローログ / バケット / アラートの SNS トピック（alerts.tf）/ ロール / Web の EC2
├── agent/         AGENT=1     Runtime / ガードレール / KB
├── pipeline/      PIPELINE=1
│   ├── lab/         containerlab の EC2（stream を作るときは Telegraf への転送も）
│   ├── stream/      MSK / Telegraf（ECS Fargate + 内部 NLB）
│   ├── analytics/   EMR Serverless / S3 Tables / OpenSearch / Prometheus / Grafana と Splunk（ECS Fargate）
│   └── graph/       Neptune / status の Lambda（SNS の購読）
└── workflow/      WORKFLOW=1  Temporal on ECS / Gateway（MCP）/ SQS（SNS の購読）
```

デバッグ用の EC2（lab + Telegraf を 1 台）だけは terraform ではなく CloudFormation の `cloudformation/lab-debug.yaml`（スタック `<prefix>-lab-debug`）。作るのも消すのも `ops/lab-debug.sh up` / `down` だけで、`ops/up.sh` / `ops/down.sh` は触らない（2026-10-04 から）。土台（base/core）は使わず、自分の VPC（既定 `10.20.0.0/24`。どこともつながないので base/core と重なってよい。IGW / NAT は無い）、インターフェース型エンドポイント 4 本（ssm / ssmmessages / ecr.api / ecr.dkr）と S3 の gateway、バケット `<prefix>-lab-debug-<アカウント>`、ECR のリポジトリ 3 つ（`<prefix>-debug-lab-srlinux` / `-lab-multitool` / `-telegraf`。スタックと一緒に消える）を持つ。

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
| Telegraf | CloudWatch Logs `/ecs/<prefix>-telegraf`（stream の出力 `telegraf_log_group_name`） |
| Grafana / ECS の Splunk | CloudWatch Logs `/ecs/<prefix>-grafana` / `/ecs/<prefix>-splunk` |
| デバッグ用の EC2 の Telegraf | EC2 の中の `sudo lab telegraf logs`（docker logs。CloudWatch には出さない） |
| 誰がいつ入ったか | CloudTrail の `StartSession` |

ポートフォワーディングの中身は Session Manager のセッションログに残らない。会話の中身もどこにも保存しない。
