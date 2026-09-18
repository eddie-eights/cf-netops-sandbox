# Temporal での実行（WORKFLOW）を手で打つ

← [README](../README.md)

## workflow（WORKFLOW）: Temporal on ECS Fargate のワーカーと AgentCore Gateway（MCP）

`WORKFLOW=1`（`AGENT=1` と `PIPELINE=1` も）の `ops/up.sh` が w-1 〜 w-4 を打つ。手で打つときは agent と PIPELINE の lab / stream / analytics が上がってから（graph が無ければトポロジは静的データ、`SINK_*` を 0 にするとそのツールは「配備されていない」を返す）。
**しくみ。**ECS on Fargate の 1 タスクに temporal コンテナ（`temporal server start-dev`。データは SQLite でタスクの中）と worker コンテナ（`workflow/worker.py`）を入れる。
Spark が異常を開くと EventBridge に `AnomalyOpened` が出て、ルールが SQS `fukuda-nwc-poc-anomalies` に流す。worker の starter がそれを long polling（20 秒）で受け、異常ごとに Temporal のワークフロー `investigate-<anomaly_id>` を起こす（SQS が無ければ 60 秒ごとに DynamoDB の `open` を見る）。ワークフローは AgentCore Runtime に原因と修復案を JSON で答えさせ、
修復案テーブル（`terraform/workflow` の DynamoDB）に `pending` で置く。人が Web の「承認」タブで承認すると、SSM Run Command で lab の EC2 に `sudo lab heal-main`（か `sudo lab check`）を打ち、異常が `resolved` になるまで 30 秒おきに 6 回確かめて `verified` / `failed` にする。
承認待ちのまま 2 時間（変数 `approval_timeout_minutes`）で `expired`。却下（`rejected`）なら何もしない。Web と worker は Temporal でつながず、修復案テーブルの `status` だけでやり取りする（worker がポーリング）。
同じルートで AgentCore Gateway（MCP、IAM 認証）と tools Lambda を作り、Runtime は起動後 5 分以内に SSM の `gateway-url` を拾って、ツールの一覧と呼び出しを Gateway に投げる（`agent/mcp_client.py`。Gateway に届かなければコンテナの中のツールに戻る）。Gateway が要らなければ `-var create_gateway=false`。

### w-1. ワーカーのイメージをビルドし、Temporal のイメージをミラーする

`terraform/base/ecr` にリポジトリ `worker` と `temporal` がある（手順 1 で作られる。前に作ったなら apply を打ち直すと 2 つ増える）。ワーカーはエージェントと同じく arm64 で作る（Fargate のタスク定義が `ARM64`）。Temporal は Docker Hub のイメージを arm64 で引いて ECR に置き直すだけ（閉域の Fargate は ECR からしか引けない）。

```bash
WORKER_REPO=$(terraform -chdir=terraform/base/ecr output -raw worker_repository_url); echo "$WORKER_REPO"
```

```bash
TEMPORAL_REPO=$(terraform -chdir=terraform/base/ecr output -raw temporal_repository_url); echo "$TEMPORAL_REPO"
```

```bash
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin "${WORKER_REPO%%/*}"
docker buildx build --platform linux/arm64 -t "$WORKER_REPO:v1" --push workflow/
```

```bash
docker pull --platform linux/arm64 temporalio/temporal:1.9.1
```

```bash
docker tag temporalio/temporal:1.9.1 "$TEMPORAL_REPO:1.9.1"
```

```bash
docker push "$TEMPORAL_REPO:1.9.1"
```

タグは手順 2 と同じ付け方（`ops/up.sh` は `deploy.env` の `IMAGE_TAG`、既定 `v1`。`workflow/` を変えたら `v2` などに上げる）。Temporal の版を変えるなら `terraform/workflow` の変数 `temporal_image_tag`（既定 `1.9.1`）も合わせる。

### w-2. terraform/workflow を apply する

```bash
terraform -chdir=terraform/workflow init -input=false
```

```bash
terraform -chdir=terraform/workflow apply -var worker_image_tag=v1
```

`-var` は destroy にも同じものを付ける。`terraform/base/core` / `lab` / `stream` の state を読むので、その 3 つが apply 済みでないと止まる（`graph` / `analytics` の state は有れば読み、tools Lambda に Neptune / OpenSearch / Prometheus の接続先と権限を付ける）。数分で終わるが、ECS のサービスがタスクを起こしてイメージを引き、Temporal が上がるまで 1〜3 分かかる。

```bash
WF_CLUSTER=$(terraform -chdir=terraform/workflow output -raw cluster_name); echo "$WF_CLUSTER"
```

```bash
WF_SERVICE=$(terraform -chdir=terraform/workflow output -raw service_name); echo "$WF_SERVICE"
```

```bash
aws ecs wait services-stable --region ap-northeast-1 --cluster "$WF_CLUSTER" --services "$WF_SERVICE"
```

上がらないときはログ（`worker_logs_command` の出力）を見る。temporal コンテナが `Temporal server is running` の前に落ちていればイメージか `--ip 0.0.0.0` の問題、worker が `TEMPORAL_ADDRESS` に付けずに落ち続けていれば temporal の起動待ち（`START` 条件なので数回は落ちてよい）。

```bash
terraform -chdir=terraform/workflow output -raw worker_logs_command
```

### w-3. Temporal の UI を PC で開く

タスクはプライベートサブネットにあり、UI（8233）に届くのは同じ VPC の中だけ。Web の EC2 を踏み台にして SSM のポートフォワーディング（リモートホスト版）で PC の 8233 につなぐ。

```bash
WF_TASK=$(aws ecs list-tasks --region ap-northeast-1 --cluster "$WF_CLUSTER" --service-name "$WF_SERVICE" --query 'taskArns[0]' --output text); echo "$WF_TASK"
```

```bash
WF_TASK_IP=$(aws ecs describe-tasks --region ap-northeast-1 --cluster "$WF_CLUSTER" --tasks "$WF_TASK" --query 'tasks[0].attachments[0].details[?name==`privateIPv4Address`].value | [0]' --output text); echo "$WF_TASK_IP"
```

```bash
aws ssm start-session --region ap-northeast-1 --target "$INSTANCE_ID" --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters "{\"host\":[\"$WF_TASK_IP\"],\"portNumber\":[\"8233\"],\"localPortNumber\":[\"8233\"]}"
```

ブラウザで http://localhost:8233/ を開くと、ワークフロー `investigate-<anomaly_id>` の一覧と、各アクティビティの入出力・待ち状態が見える。`INSTANCE_ID` は手順 3 で取った Web の EC2（`web_instance_id`）。UI に認証は無い（届くのはこのセッションだけ）。

### w-4. Web を再起動し、承認する

Web は起動時に SSM の `proposal-table` を読むので、管理者のシェルで `sudo systemctl restart fukuda-nwc-poc-web`。Runtime は再起動しなくてよい（5 分以内に Gateway を拾う。すぐ使いたいなら Runtime を作り直す）。
lab で `sudo lab failover` などで異常を起こすと（lab-4）、Spark の次のマイクロバッチ（60 秒以内）で EventBridge → SQS を通ってワークフローが起き、数十秒でチャットの「承認」タブに修復案（原因・打つコマンド・理由）が `pending` で並ぶ。「承認して直す」を押すと `approved` → `applied` → `verified` / `failed` と進み、表の「状態」を変えて追える。
Gateway に届いていないときは Runtime のログに `gateway tools/list failed, using local tools` が出て、コンテナの中のツールで答える。

### 消す

`terraform/pipeline/lab` / `terraform/pipeline/stream` / `terraform/pipeline/analytics` / `terraform/pipeline/graph` より**先に**消す（それらの state を読む）。`ops/down.sh` は最初に消す。

```bash
terraform -chdir=terraform/workflow destroy -var worker_image_tag=v1
```

修復案テーブルはルートと一緒に消える（残すものは無い）。Temporal の実行履歴もタスクと一緒に消える。ECR の `worker` / `temporal` のイメージは `terraform/base/ecr` を消すまで残る。

