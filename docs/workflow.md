# 調査と修復（WORKFLOW）

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。`deploy.env` に `AGENT=1`、`PIPELINE=1`、`WORKFLOW=1` を書いて `ops/up.sh` を打つと、下の全部ができる。

## 流れ

```mermaid
flowchart LR
  SPARK["Spark<br/>異常を開く"] -->|"AnomalyOpened"| EB["EventBridge"] --> SQS["SQS<br/>prefix-anomalies"]
  SQS -->|"long polling 20 秒"| WK["worker<br/>異常ごとにワークフロー"]
  WK -->|"原因と修復案を聞く"| RT["AgentCore Runtime"]
  WK -->|"pending で置く"| PT["修復案テーブル<br/>DynamoDB"]
  WEB["Web の「承認」タブ"] -->|"承認 / 却下"| PT
  PT -->|"approved を見る"| WK
  WK -->|"SSM Run Command<br/>sudo lab heal-main"| LAB["lab の EC2"]
```

- temporal（`start-dev`、データは SQLite）と worker は、ECS Fargate の 1 タスクに入っている。Temporal の履歴はタスクと一緒に消える。
- Web と worker は修復案テーブルの `status` だけでやり取りする。
- 同じルートで AgentCore Gateway（MCP）と tools Lambda も作る。Runtime はツールを Gateway 経由で呼ぶ（届かなければコンテナの中のツールで答える）。

## 修復案の状態

```mermaid
stateDiagram-v2
  [*] --> pending: 調査が終わる
  pending --> approved: 承認
  pending --> rejected: 却下（何もしない）
  pending --> expired: 120 分待っても決まらない
  approved --> applied: lab でコマンドを打つ
  applied --> verified: 30 秒おきに 6 回見て resolved になった
  applied --> failed: resolved にならなかった
  rejected --> [*]
  expired --> [*]
  verified --> [*]
  failed --> [*]
```

## 試す

1. lab に入り（[pipeline.md](pipeline.md) の「lab に入る」）、`sudo lab fail-main` で主回線を落とす。
2. 60 秒ほどで Web の「異常一覧」に出て、数十秒で「承認」タブに修復案（原因・打つコマンド・理由）が `pending` で並ぶ。
3. 「承認して直す」を押すと `approved` → `applied` → `verified` / `failed` と進む。

## Temporal UI を開く

UI（8233）はプライベートサブネットのタスクにあるので、Web の EC2 を踏み台にしてポートフォワーディングする。`ops/up.sh` の手順 8-5 が同じコマンドを表示する。

```bash
INSTANCE_ID=$(terraform -chdir=terraform/base/core output -raw web_instance_id); echo "$INSTANCE_ID"
WF_CLUSTER=$(terraform -chdir=terraform/workflow output -raw cluster_name); echo "$WF_CLUSTER"
WF_SERVICE=$(terraform -chdir=terraform/workflow output -raw service_name); echo "$WF_SERVICE"
WF_TASK=$(aws ecs list-tasks --region ap-northeast-1 --cluster "$WF_CLUSTER" --service-name "$WF_SERVICE" --query 'taskArns[0]' --output text); echo "$WF_TASK"
WF_TASK_IP=$(aws ecs describe-tasks --region ap-northeast-1 --cluster "$WF_CLUSTER" --tasks "$WF_TASK" --query 'tasks[0].attachments[0].details[?name==`privateIPv4Address`].value | [0]' --output text); echo "$WF_TASK_IP"
aws ssm start-session --region ap-northeast-1 --target "$INSTANCE_ID" --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters "{\"host\":[\"$WF_TASK_IP\"],\"portNumber\":[\"8233\"],\"localPortNumber\":[\"8233\"]}"
```

このあと http://localhost:8233/ を開く。UI に認証は無い（届くのはこのセッションだけ）。

## うまくいかないとき

ワーカーのログ:

```bash
terraform -chdir=terraform/workflow output -raw worker_logs_command; echo
```

| 症状 | 原因と直し方 |
|---|---|
| apply の直後に worker が落ちる | Temporal が上がるまで 1〜3 分かかる。数回落ちてから上がる |
| 承認しても approved のまま進まない（UI で `TimeoutError`） | ワーカーのイメージが古い。`deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh` |
| Runtime のログに `gateway tools/list failed, using local tools` | Gateway に届いていない。答えはコンテナの中のツールで返る |
| 修復案が出ない | Spark のジョブが古いまま動いている（[troubleshooting.md](troubleshooting.md) の「パイプラインと WORKFLOW」） |
