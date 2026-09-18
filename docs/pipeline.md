# データパイプライン（PIPELINE）を手で打つ

← [README](../README.md)

## lab（PIPELINE）: containerlab + FRR を EC2 で動かす

`lab/` の containerlab の構成（本社・DC・支店 2 か所の CE、キャリア PE 2 台、snmpd、ホスト。すべて架空のアドレス）を、同じ VPC の EC2 1 台で動かす。
BGP の主副切替と SNMP の見え方を手で確かめるためのもので、**Web やエージェントとはつながっていない。**使わないときは止める。
`deploy.env` に `PIPELINE=1` を書いた `ops/up.sh` は lab-1〜lab-3 を打つ（lab を外すなら `SKIP_LAB=1`、lab だけ要って Neptune が要らなければ `SKIP_GRAPH=1`）。以下はその中身と、入ってからの使い方。

### lab-1. イメージを ECR に置く

手順 1 の `terraform/base/ecr` は既定（変数 `create_lab_repositories = true`）で `netops-nwc-poc-lab-frr` / `-lab-snmpd` / `-lab-multitool` も作っているので、ECR 側の準備は要らない。
インターネットに出られる端末で、**arm64 のイメージ**を取って push する。snmpd だけはビルドする。

```bash
REG="$ACCOUNT_ID.dkr.ecr.ap-northeast-1.amazonaws.com"
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin "$REG"
docker pull --platform linux/arm64 quay.io/frrouting/frr:10.2.1
docker tag quay.io/frrouting/frr:10.2.1 "$REG/netops-nwc-poc-lab-frr:10.2.1" && docker push "$REG/netops-nwc-poc-lab-frr:10.2.1"
docker pull --platform linux/arm64 ghcr.io/srl-labs/network-multitool:v0.10.0
docker tag ghcr.io/srl-labs/network-multitool:v0.10.0 "$REG/netops-nwc-poc-lab-multitool:v0.10.0" && docker push "$REG/netops-nwc-poc-lab-multitool:v0.10.0"
docker buildx build --platform linux/arm64 -t "$REG/netops-nwc-poc-lab-snmpd:v1" --push lab/snmpd/
```

snmpd のビルドは apk なので `--trusted-host` に当たるものが無い。社内ネットワークで `apk add` が証明書で落ちたら、WSL の `/usr/local/share/ca-certificates/corp-root.crt` を `lab/snmpd/certs/` にコピーして打ち直す（ビルド中だけ読む。配布物には入っていない）。

### lab-2. 設定と containerlab の rpm を S3 に置く

1 行目で `terraform/base/core` の出力 `kb_bucket_name` を `$KB_BUCKET` に入れる。

```bash
KB_BUCKET=$(terraform -chdir=terraform/base/core output -raw kb_bucket_name); echo "$KB_BUCKET"
curl -LO https://github.com/srl-labs/containerlab/releases/download/v0.79.0/containerlab_0.79.0_linux_arm64.rpm
aws s3 sync lab/ "s3://$KB_BUCKET/lab/" --exclude "wanlab.clab.yml"
aws s3 cp containerlab_0.79.0_linux_arm64.rpm "s3://$KB_BUCKET/lab/"
```

バケットは `terraform/base/core` のもの。ナレッジベースは `docs/` しか読まないので混ざらない。lab を apply した後なら、出力 `upload_lab_command` に同じ内容の完成形がある。

### lab-3. apply する

VPC / サブネット / エンドポイントの SG / バケットは `terraform/base/core` の state から読むので、ネットワークの値は要らない。
エンドポイントの SG には lab の EC2 から ssm / ssmmessages / ecr へ 443 を許すルール（`aws_vpc_security_group_ingress_rule.endpoints_from_lab`）が足される。

```bash
terraform -chdir=terraform/pipeline/lab init
```

```bash
terraform -chdir=terraform/pipeline/lab apply
```

起動時に Docker と containerlab を入れ、ECR からイメージを取り、トポロジを上げる（5 分ほど）。`-var auto_start_lab=false` を付けると上げずに待つ（付けたら以後の apply にも毎回付ける）。

### lab-4. 入って確かめる

管理者用のシェルセッション（手順 6 の `SSM-SessionManagerRunShell`）で入る。1 行目で `terraform/pipeline/lab` の出力 `lab_instance_id` を `$LAB_INSTANCE_ID` に入れる（Web の EC2 とは別のインスタンス。`echo` で `i-0` で始まる ID が出ること）。出力 `start_session_command` に ID を埋めた完成形もある。

```bash
LAB_INSTANCE_ID=$(terraform -chdir=terraform/pipeline/lab output -raw lab_instance_id); echo "$LAB_INSTANCE_ID"
aws ssm start-session --region ap-northeast-1 --target "$LAB_INSTANCE_ID"
```

セッションに入ったら、次を**セッションのタブに貼って**打つ（PC 側のシェルで打っても動かない。`lab` は EC2 の `/usr/local/bin/lab`）。

```bash
sudo lab status              # 14 コンテナが running か
sudo lab check               # BGP の隣接、経路、拠点間 ping、SNMP の ifOperStatus
sudo lab failover            # 本社の主回線を落として副回線に切り替わるのを見る（30〜60 秒）
sudo lab heal-main           # 主回線を戻す
sudo lab fail-main           # 主回線を落とすだけ（戻すまで落ちたまま）
sudo lab snmp hq-snmp-01     # 1 台の ifDescr と ifOperStatus
sudo lab logs                # 全機器の FRR のログの末尾（1 台だけなら sudo lab logs hq-ce-01、行数は LINES=50 を前に付ける）
sudo lab telegraf-status     # Telegraf のサービスの状態と直近のログ（stream を作ってから）
sudo lab clab inspect --all  # containerlab をそのまま呼ぶ
```

機器に直接入るとき（コンテナ名は `clab-wanlab-` + 機器名）。

```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}'
sudo docker exec -it clab-wanlab-hq-ce-01 vtysh          # FRR の CLI（show bgp summary / show ip route）
sudo docker exec -it clab-wanlab-hq-ce-01 vtysh -c 'show bgp summary'
```

サービスとして見るとき（lab と Telegraf は systemd のサービス）。

```bash
systemctl is-active netops-nwc-poc-lab netops-nwc-poc-telegraf
sudo journalctl -u netops-nwc-poc-lab -n 50 --no-pager
sudo journalctl -u netops-nwc-poc-telegraf -n 50 --no-pager
sudo tail -n 50 /var/log/cloud-init-output.log         # 起動時（Docker と containerlab の導入、イメージの取得）のログ
sudo systemctl restart netops-nwc-poc-lab              # トポロジを上げ直す
```

起動に失敗したら上の `cloud-init-output.log` と `journalctl -u netops-nwc-poc-lab` を見る。ECR から取れないとき（`docker pull` がタイムアウトする）は次の 2 つ。

- `terraform/base/core` に `ecr.api` / `ecr.dkr` のエンドポイントがあるか（`create_shared_endpoints`。既定は `true`。`ops/up.sh` は lab か analytics を作るなら `true` で打つ）。
- エンドポイントの SG に lab の SG からの 443 が足されているか（`terraform/pipeline/lab` が `terraform/base/core` の state から SG を取って足す。手で SG を直していないか）。

### lab-5. 止める・消す

1 行目は lab-4 と同じ（`$LAB_INSTANCE_ID` を入れる）。止める・起動するは出力 `stop_command` / `start_command` にも完成形がある。

```bash
LAB_INSTANCE_ID=$(terraform -chdir=terraform/pipeline/lab output -raw lab_instance_id); echo "$LAB_INSTANCE_ID"
aws ec2 stop-instances --region ap-northeast-1 --instance-ids "$LAB_INSTANCE_ID"    # 止める（EBS 16 GB の保管料だけ）
aws ec2 start-instances --region ap-northeast-1 --instance-ids "$LAB_INSTANCE_ID"   # 起動すると lab も上がる
```

ルートごと消すとき（`terraform/pipeline/stream` を作っているなら、先にそちらを消し終える。stream が lab の state を読むため）。

```bash
terraform -chdir=terraform/pipeline/lab destroy
```

- lab の設定（`lab/frr/` など）を変えたら lab-2 を置き直して再起動する。
- イメージを変えたら新しいタグで push し、`-var frr_image_tag=…`（`snmpd_image_tag` / `multitool_image_tag` も同じ）を付けて apply し直す。
- 止めたインスタンスに apply しても、user_data が変わる差分（イメージのタグや Telegraf の版を変えたとき）はインスタンスの作り直しになる。インスタンス ID が変わるので、lab-4 の 1 行目から取り直す。

## stream / analytics / graph（PIPELINE）: lab → MSK → Spark → S3 Tables / OpenSearch / Prometheus と異常検知、Neptune のトポロジ

stream は lab の SNMP（ポーリングと trap）と FRR のログを MSK に流し（トピック `metrics` / `traps` / `logs`）、DynamoDB の異常テーブルを持つ（書くのは analytics の Spark）。
analytics は MSK のトピックを Spark（EMR Serverless）のストリーミングジョブで読み、全部を S3 Tables（Iceberg）のテーブル `snmp_metrics` に、`traps` と `logs` を OpenSearch Serverless の `snmp-logs` に、`metrics` を Amazon Managed Service for Prometheus に 60 秒ごとに流す（`deploy.env` の `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS`。既定は 3 つ全部）。履歴の正本は S3 Tables。
同じジョブの detect が `link_down` を DynamoDB の異常テーブルに open / resolved で書き、開いた瞬間に EventBridge へ `AnomalyOpened` を出す（WORKFLOW の SQS が受ける）。Web の「異常一覧」とエージェントの `list_anomalies` がその表を読む。
graph はトポロジを Neptune に置く。静的な構成（機器と回線）は lab の定義から作って入れ、動的な状態（UP / DOWN）は Spark の検知を EventBridge → Lambda で書く（「Neptune のトポロジを lab から作る」）。Web の「トポロジ」タブから編集でき、エージェントのトポロジのツールもそこを読む。
**どれも時間課金なので、使う日に作って当日中に消す**（下の試算）。`terraform/base/core` はそのまま使い、stream は `terraform/pipeline/lab` も、analytics は `terraform/pipeline/stream` も使う。

順番: s-1 で rpm と zip を置く → `terraform/pipeline/stream` → lab EC2 の Telegraf を起動（再起動）→ a-1 で jar とスクリプトを置く → `terraform/pipeline/analytics` → Spark のジョブを起こす → `terraform/pipeline/graph` → Web の再起動と投入。
`ops/up.sh` は `PIPELINE=1` ならこれを全部打つ（`SKIP_STREAM=1` / `SKIP_ANALYTICS=1` / `SKIP_GRAPH=1` で外す）。以下はその中身と、動いてからの確かめ方。

### s-1. Telegraf の rpm と S3 sink のプラグインを S3 に置く

1 行目で `terraform/base/core` の出力 `kb_bucket_name` を `$KB_BUCKET` に入れる。**プラグインの zip は s-2 の apply より前に置く**（無いと s-2 が止まる）。

```bash
KB_BUCKET=$(terraform -chdir=terraform/base/core output -raw kb_bucket_name); echo "$KB_BUCKET"
curl -LO https://dl.influxdata.com/telegraf/releases/telegraf-1.40.0-1.aarch64.rpm
aws s3 cp telegraf-1.40.0-1.aarch64.rpm "s3://$KB_BUCKET/lab/"
curl -LO https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-s3/versions/12.1.11/confluentinc-kafka-connect-s3-12.1.11.zip
aws s3 cp confluentinc-kafka-connect-s3-12.1.11.zip "s3://$KB_BUCKET/stream/"
```

プラグインの URL は Confluent Hub の形で、ダウンロードには利用条件への同意が要ることがある。取れなければブラウザで取って同じキー（`stream/confluentinc-kafka-connect-s3-12.1.11.zip`）に置く。
S3 sink が要らなければ zip は飛ばし、s-2 の apply に `-var create_s3_sink=false` を付ける（以後の apply と destroy にも毎回付ける）。

### s-2. terraform/pipeline/stream を apply する

VPC / サブネット（`terraform/base/core` の Runtime サブネット）/ ルートテーブル / エンドポイントの SG / バケット / ロール名は `terraform/base/core` の state から、lab の SG とロールは `terraform/pipeline/lab` の state から読む。**`terraform/pipeline/lab` を先に apply しておく。**

```bash
terraform -chdir=terraform/pipeline/stream init
```

```bash
terraform -chdir=terraform/pipeline/stream apply
```

plan の段階で次のどちらかが出たら、そのとおりに直してから打ち直す（何も作られていない）。

| 出たメッセージ（先頭） | やること |
|---|---|
| `terraform/pipeline/lab の state（terraform/pipeline/lab/terraform.tfstate）から lab_security_group_id / lab_role_name が読めない` | lab-3 を先に apply する（この PC で） |
| `s3://<バケット名>/stream/confluentinc-kafka-connect-s3-12.1.11.zip に Confluent S3 sink の zip が無い` | s-1 の zip を置く。シンク無しで立てるなら `-var create_s3_sink=false` |

MSK の作成に 20〜30 分かかる。出来上がると SSM の `/netops-nwc-poc/msk-bootstrap`（ブローカー）と `/netops-nwc-poc/anomaly-table` が書かれ、lab の Telegraf と Web / エージェントはそこから読む（`terraform/pipeline/analytics` はテーブル名を state から読む）。

### s-3. lab の Telegraf を動かす

lab の EC2 は起動のたびに s-1 で置いた rpm を入れて `netops-nwc-poc-telegraf` サービスを作るので、**すでに lab が動いていれば再起動するだけでよい**（1 行目は lab-4 と同じ）。lab をまだ作っていなければ lab-3 を打つ（変数 `telegraf_version` は既定の `1.40.0` のまま）。

```bash
LAB_INSTANCE_ID=$(terraform -chdir=terraform/pipeline/lab output -raw lab_instance_id); echo "$LAB_INSTANCE_ID"
aws ec2 reboot-instances --region ap-northeast-1 --instance-ids "$LAB_INSTANCE_ID"
```

Telegraf は起動のたびに `lab telegraf-render` で SSM のブローカーを設定に埋める。`terraform/pipeline/stream` より先に上げた場合は失敗して 60 秒ごとにやり直すので、そのまま待てばつながる。

```bash
sudo lab telegraf-status       # サービスの状態と直近のログ
sudo lab failover              # 主回線を落とす → 5 秒以内に trap、10 秒以内にポーリングで link_down
sudo lab heal-main             # 戻す → resolved
```

FRR のログ（BGP の隣接の up / down、zebra のインタフェースの変化）は、EC2 の `/var/log/netops-lab/<機器名>/frr.log` を Telegraf が tail してトピック `logs` に出す（measurement は `frr_log`、タグ `sysName` が機器名、`daemon` が BGP / ZEBRA など）。元のログは `sudo lab logs` で見る。`sudo lab failover` を打つと hq-ce-01 と carrier-pe-01 に隣接の変化が出る。

Web の「異常一覧」タブか、チャットで「今の異常は？」と聞く（異常一覧に出るのは analytics の Spark が動いてから。a-3）。S3 sink は 1 分ごとに `stream/topics/<トピック>/dt=.../hour=.../` に JSON を置く（出力 `sink_prefix`）。


### a-1. Spark の jar とスクリプトを S3 に置く

Spark が Kafka（MSK の IAM 認証）と S3 Tables を読み書きするための jar 6 本を Maven Central から取り、`spark/snmp_sinks.py` と一緒に `terraform/base/core` のバケットの `analytics/` に置く。**a-2 の apply より前に置く**（ジョブがここから読む）。
版は `ops/up.sh` の `JAR_URLS`（Spark 3.5.6 = EMR Serverless の `emr-7.13.0`）と揃える。`jars/` は配布物には入っていない（`ops/up.sh` が無いときだけ取る）。

```bash
KB_BUCKET=$(terraform -chdir=terraform/base/core output -raw kb_bucket_name); echo "$KB_BUCKET"
```

```bash
mkdir -p jars && cd jars && curl -LO https://repo1.maven.org/maven2/org/apache/spark/spark-sql-kafka-0-10_2.12/3.5.6/spark-sql-kafka-0-10_2.12-3.5.6.jar && curl -LO https://repo1.maven.org/maven2/org/apache/spark/spark-token-provider-kafka-0-10_2.12/3.5.6/spark-token-provider-kafka-0-10_2.12-3.5.6.jar && curl -LO https://repo1.maven.org/maven2/org/apache/kafka/kafka-clients/3.4.1/kafka-clients-3.4.1.jar && curl -LO https://repo1.maven.org/maven2/org/apache/commons/commons-pool2/2.11.1/commons-pool2-2.11.1.jar && curl -LO https://repo1.maven.org/maven2/software/amazon/msk/aws-msk-iam-auth/2.3.2/aws-msk-iam-auth-2.3.2-all.jar && curl -LO https://repo1.maven.org/maven2/software/amazon/s3tables/s3-tables-catalog-for-iceberg-runtime/0.1.8/s3-tables-catalog-for-iceberg-runtime-0.1.8.jar && cd ..
```

```bash
aws s3 cp spark/snmp_sinks.py "s3://$KB_BUCKET/analytics/"
```

```bash
aws s3 sync jars/ "s3://$KB_BUCKET/analytics/jars/" --exclude "*" --include "*.jar"
```

### a-2. terraform/pipeline/analytics を apply する

VPC / サブネット / バケットは `terraform/base/core` の state から、MSK のクラスターと SG とブローカーは `terraform/pipeline/stream` の state から読む（stream が無いと precondition で止まる）。
作るのは S3 Tables のテーブルバケット `netops-nwc-poc-tables`（namespace `netops`、テーブル `snmp_metrics`）、EMR Serverless の Spark アプリケーション（ARM64、`emr-7.13.0`、アイドル 15 分で止まる）、ジョブの実行ロール、EMR の SG（MSK の SG に 9098 の受信を足す）、`s3tables` と `events`（EventBridge の PutEvents）の interface エンドポイント（2 AZ）、DynamoDB の異常テーブルへの書き込み権限。数分。

格納先は変数 `sinks`（既定 `["iceberg", "opensearch", "prometheus"]` の 3 つ全部）で選ぶ。`opensearch` があると OpenSearch Serverless の TIMESERIES コレクション `netops-nwc-poc-logs`（VPC エンドポイント経由だけ。インデックス `snmp-logs` は最初の書き込みで作られる）、`prometheus` があると Amazon Managed Service for Prometheus のワークスペース `netops-nwc-poc-metrics` と `aps-workspaces` の interface エンドポイント（2 AZ）も作る。
`ops/up.sh` は `deploy.env` の `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS` をこの変数に組んで渡す。`iceberg` を外すと S3 Tables のテーブルバケットと `s3tables` のエンドポイントも作らない。手で打つときは既定のままなら `-var` は要らず、減らすなら apply に `-var 'sinks=["iceberg"]'` を付ける（`--metric-topics` / `--log-topics` は変数 `metric_topics` / `log_topics`。既定 `metrics` / `traps` と `logs`）。

```bash
terraform -chdir=terraform/pipeline/analytics init
```

```bash
terraform -chdir=terraform/pipeline/analytics apply
```

### a-3. Spark のジョブを起こす

アプリケーションは器だけで、ジョブを起こすまで課金されない。ジョブは Structured Streaming で、止めるまで動き続ける（`--mode STREAMING`。落ちても EMR Serverless が再開する）。
`start-job-run` に渡す JSON は Terraform の出力にある（`job_driver_json` が Kafka のブローカー・格納先（`--sinks` と格納先ごとのテーブル名 / エンドポイント / URL）・チェックポイントの引数と Iceberg / S3 Tables の設定、`configuration_overrides_json` がドライバーのログを CloudWatch Logs に出す設定）。

```bash
APP_ID=$(terraform -chdir=terraform/pipeline/analytics output -raw application_id); echo "$APP_ID"
```

```bash
ROLE_ARN=$(terraform -chdir=terraform/pipeline/analytics output -raw runtime_role_arn); echo "$ROLE_ARN"
```

```bash
JOB_DRIVER=$(terraform -chdir=terraform/pipeline/analytics output -raw job_driver_json); echo "$JOB_DRIVER"
```

```bash
OVERRIDES=$(terraform -chdir=terraform/pipeline/analytics output -raw configuration_overrides_json); echo "$OVERRIDES"
```

```bash
aws emr-serverless start-job-run --region ap-northeast-1 --application-id "$APP_ID" --execution-role-arn "$ROLE_ARN" --name snmp-sinks --mode STREAMING --job-driver "$JOB_DRIVER" --configuration-overrides "$OVERRIDES" --tags Project=netops-nwc-poc,owner=netops
```

起動に 2〜5 分。様子は `list_job_runs_command` の出力のコマンドで見る（`RUNNING` になれば読んでいる。`FAILED` ならロググループ `/aws/emr-serverless/netops-nwc-poc` のドライバーの stderr）。テーブルに行が入ったかは `list_tables_command` と Athena（S3 Tables のカタログ `s3tablescatalog`）で見る。

```bash
terraform -chdir=terraform/pipeline/analytics output -raw list_job_runs_command; echo
```

```bash
terraform -chdir=terraform/pipeline/analytics output -raw list_tables_command; echo
```

#### Spark の UI（GUI）を開く

EMR Studio は要らない。動いているジョブの ID を取り、`get-dashboard-for-job-run` が返す URL をブラウザで開く（動いているジョブは live の Spark UI、終わったジョブは Spark History Server）。
**URL は一時的な認証を含み、約 1 時間で切れる。チャットやチケットに貼らない**（切れたら同じコマンドを打ち直す）。

```bash
APP_ID=$(terraform -chdir=terraform/pipeline/analytics output -raw application_id); echo "$APP_ID"
```

```bash
JOB_RUN_ID=$(aws emr-serverless list-job-runs --region ap-northeast-1 --application-id "$APP_ID" --states RUNNING --query 'jobRuns[0].id' --output text); echo "$JOB_RUN_ID"
```

```bash
aws emr-serverless get-dashboard-for-job-run --region ap-northeast-1 --application-id "$APP_ID" --job-run-id "$JOB_RUN_ID" --query url --output text
```

`$JOB_RUN_ID` が `None` なら動いているジョブが無い（`--states RUNNING` を外すと終わったものも出る）。見るのは「Structured Streaming」タブ（バッチごとの入力行数と処理時間）と「Executors」タブ。CLI だけで様子を見るなら次の 2 つ。

```bash
aws emr-serverless get-job-run --region ap-northeast-1 --application-id "$APP_ID" --job-run-id "$JOB_RUN_ID" --query 'jobRun.[state,stateDetails,totalExecutionDurationSeconds]' --output table
```

```bash
LOG_GROUP=$(terraform -chdir=terraform/pipeline/analytics output -raw log_group_name); aws logs tail "$LOG_GROUP" --region ap-northeast-1 --since 10m --follow
```

止めるときは `cancel-job-run`（`ops/down.sh` は analytics を消す前に打つ）。

```bash
aws emr-serverless cancel-job-run --region ap-northeast-1 --application-id "$APP_ID" --job-run-id "$JOB_RUN_ID"
```

ジョブは同時に 1 本だけにする（同じチェックポイントを 2 本で書くと壊れる）。`ops/up.sh` は動いているジョブがあれば起こさない。

### g-1. terraform/pipeline/graph を apply する

VPC / サブネット（`terraform/base/core` の Runtime サブネット）/ Runtime と Web の SG / ロール名は `terraform/base/core` の state から読む。

```bash
terraform -chdir=terraform/pipeline/graph init
```

```bash
terraform -chdir=terraform/pipeline/graph apply
```

10〜15 分。出来上がると SSM の `/netops-nwc-poc/neptune-endpoint` が書かれる。次にやることは出力 `next_step` にも出る。

### g-2. Web を再起動して静的データを投入する

Web は起動時に SSM を読むので、管理者のシェルで `sudo systemctl restart netops-nwc-poc-web`（`s-2` の後にも一度）。エージェントは呼び出しのたびに読む（60 秒キャッシュ）。
`ops/up.sh` の手順 8 は lab の定義から作った 10 台と 10 本を入れる（「Neptune のトポロジを lab から作る」）。手で入れるなら「トポロジ」タブの「Neptune で編集」を開き、「静的データを投入」で 10 台と 10 本を入れる（Neptune の中身を全部消してから `agent/data/` を入れ直すので、編集をやり直すときにも使う）。以後はリンクの追加（機器 A / B は一覧から選び、インタフェースはその機器で使用中の名前から選ぶか新しい名前を打つ）と削除（既存リンクの一覧から 1 本選ぶ）がそこでできて、エージェントの答えにも反映される（次の質問から）。機器の追加・削除は画面に無いので、`agent/data/` を直して投入し直す。Neptune を消すと静的データに戻る。

### Neptune のトポロジを lab から作る

Neptune に入る静的な構成は、lab の定義そのもの（`lab/wanlab.clab.yml.in` の `links:` と `lab/frr/<機器>.conf` の `router bgp` / `interface` の `description`）から `lab/lab_topology.py` が作る。
機器の site と role は名前（`hq-ce-01` → `hq` / `ce`）、asn は `router bgp`、mgmt_ip は containerlab の `mgmt-ipv4`、監視対象かどうかは snmpd のサイドカーの相乗り先か、回線の種別は両端の role（host なら l2、pe 同士なら ibgp、ほかは ebgp）、主副と帯域は description（`primary` / `secondary` と `1G` / `100M` / `10G`）で決める。
`agent/data/` の静的データと同じ内容になることを `tests/test_sync.py` が見ている（Neptune が無いとき（PIPELINE 無し、または `SKIP_GRAPH=1`）は静的データのまま動く）。

- **初回**: `ops/up.sh` の手順 8 が、Neptune が空のときだけ入れる。手で打つなら次（`lab/lab_topology.py` の JSON を base64 で `ops/seed_graph.py` に渡す。PyYAML は要らない）。

```bash
ops/sync-graph.sh
```

- **lab の定義を変えたとき**（機器や回線を足した・description を直した）: `--replace` で全部消して入れ直す。Web の「静的データを投入」（`agent/data/`）と違い lab の定義が正本になる。`--dry-run` は作った JSON を出すだけで Neptune に触らない。

```bash
ops/sync-graph.sh --replace
```

動的な状態は別の経路で書く。Spark の検知が EventBridge に出す `AnomalyOpened` / `AnomalyResolved`（source `<接頭辞>.spark`）を `terraform/pipeline/graph` のルールが受け、VPC の中の Lambda（`graph/status_handler.py`。`netops-nwc-poc-graph-status`）が Gremlin で `status` を書く。
linkDown / linkUp（`kind` が `link_down`）はその機器のそのインタフェースが付く回線の辺に `DOWN` / `UP`、ほかの trap は機器の頂点に `ALARM` / `UP`。
機器一覧・隣接・影響範囲のツールの答えに `status` が付き、Web の「トポロジ」タブでは赤い回線・赤い枠で出る（表の「状態」列も）。`ops/sync-graph.sh --replace` や「静的データを投入」で入れ直すと状態は消えて全部 UP に戻る（静的な構成だけを入れる）。
Lambda のログは出力 `status_log_group_name` のロググループ（保持は `log_retention_days`、既定 7 日）。EventBridge は 1 分のあいだに Lambda が落ちれば 15 分・3 回まで再送する。

### 消す

workflow を作っていれば、**最初に**消す（lab と stream の state を読む。「[workflow（WORKFLOW）](workflow.md)」の「消す」）。analytics は stream の state を読むので、**stream より先に**消す。先に Spark のジョブを止めないと destroy がアプリケーションで止まる（動いているジョブの ID は a-3 の `list_job_runs_command` で分かる）。

```bash
APP_ID=$(terraform -chdir=terraform/pipeline/analytics output -raw application_id); echo "$APP_ID"
```

```bash
JOB_RUN_ID=$(aws emr-serverless list-job-runs --region ap-northeast-1 --application-id "$APP_ID" --states SUBMITTED PENDING SCHEDULED RUNNING --query 'jobRuns[0].id' --output text); echo "$JOB_RUN_ID"
```

```bash
aws emr-serverless cancel-job-run --region ap-northeast-1 --application-id "$APP_ID" --job-run-id "$JOB_RUN_ID"
```

```bash
aws emr-serverless stop-application --region ap-northeast-1 --application-id "$APP_ID"
```

```bash
terraform -chdir=terraform/pipeline/analytics destroy
```

S3 Tables のテーブルはバケットごと消える（**Iceberg の履歴も消える。**残すなら先に Athena などで別のバケットへ写す）。S3 の `analytics/` に置いた jar とスクリプトは残る。

```bash
terraform -chdir=terraform/pipeline/graph destroy
```

```bash
terraform -chdir=terraform/pipeline/stream destroy
```

S3 sink 無しで立てた（`-var create_s3_sink=false`）なら、destroy にも同じ `-var` を付ける（`ops/down.sh` は state を見て自分で付ける）。
`stream` は MSK Connect → MSK の順に消えるので 15 分ほど。DynamoDB のテーブルも一緒に消える。S3 の `stream/` に置いた生データとプラグインは残る（`terraform/base/core` を destroy すればバケットごと消える）。
生データだけ消したいときは、**stream を消し終えてから**次を打つ。プラグインの zip も消えるので、次に stream を立てる前に s-1 で置き直す。

```bash
KB_BUCKET=$(terraform -chdir=terraform/base/core output -raw kb_bucket_name); echo "$KB_BUCKET"
aws s3 rm "s3://$KB_BUCKET/stream/" --recursive
```

