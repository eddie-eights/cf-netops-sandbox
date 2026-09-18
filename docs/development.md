# 手元で変える・確かめる

← [README](../README.md)

## 変更するとき

- **画面（`web/app.py`）を直すときは S3 に置いてインスタンスを再起動する**（手順 4）。apply は要らない。Web の起動のしかた（`templates/web_user_data.sh.tftpl`）を変えたときだけ `terraform/base/core` を apply する。**user_data が変わるとインスタンスが作り直され、インスタンス ID が変わる**（`user_data_replace_on_change`。Web は状態を持たないので中身は失われない）。手順 4・7 の枠は毎回出力から ID を取るのでそのまま打てばよいが、利用者に配った `start_session_command` は配り直す。
- **エージェントを更新するときは、新しいタグで push して `-var agent_image_tag=v2` で apply する**（`ops/up.sh` なら `IMAGE_TAG=v2`）。以後の apply にも毎回同じタグを付ける（付け忘れると既定の `v1` に戻す差分が出る）。`agent/data/` を変えたときも同じ（トポロジはイメージに入っている）。Web の「トポロジ」タブは S3 の `web/data/` を見るので、そちらも置き直す。
- **ガードレールを変えたら、`terraform/base/core/kb.tf` の `aws_bedrock_guardrail_version.r1` の `description` を `fukuda-nwc-poc r2` のように上げて apply する。**版は作ったときの中身で固定されるので、上げないと Runtime は古い版のまま判定する。
- 手順書を変えたら、手順 4 をやり直す。apply は要らない。
- AMI は apply のたびに SSM パラメータ（変数 `ami_ssm_parameter`）から最新の AL2023 を引く。新しい AMI が出ていると**インスタンスが作り直され、インスタンス ID が変わる**（上と同じ扱い）。apply の差分に `aws_instance.web` の `ami` が出ていたらこれ。
- user_data のテンプレート（`templates/*.sh.tftpl`）は `templatefile` を通るので、シェルの `${…}` をそのまま書くと Terraform の変数として解釈される。シェルの変数は `$${…}`、`%{` は `%%{` と書く。
- user_data の上限は 16 KB。Gradio の画面は S3 に置いているので、user_data には入れない。
- 変数の既定を変えたいときは `terraform/<ルート>/terraform.tfvars.example` を `terraform.tfvars` に写して書く（gitignore 済み。`-var` より弱く、既定より強い）。

## 手元で確かめる

AWS に触らずに、Terraform の構文検査と模擬テストを打てる。会社の PC（WSL2 + uv）でも Mac でも同じ。Python 3.13 は `.python-version` に書いてあり、無ければ uv が取ってくる。

```bash
uv sync --group dev
```

下の 4 つは `ops/check.sh` が同じ順番で打つ。**変更したら、まずこれを打つ。**最後の行が `すべて通過` なら健全で、途中で落ちたらそこで止まって何が失敗したかを出す。

```bash
bash ops/check.sh
```

```bash
terraform fmt -check -recursive terraform
```

```bash
for r in ecr main lab stream analytics graph workflow; do terraform -chdir=terraform/$r init -backend=false -input=false >/dev/null && terraform -chdir=terraform/$r validate || break; done
```

```bash
for t in test_app test_graph test_stream test_sync test_analytics test_workflow; do uv run --group dev python tests/$t.py || break; done
```

健全なら `fmt` は何も出さず、`validate` は 8 回 `Success! The configuration is valid.` を出し（workflow は非推奨の警告が付く）、テストはそれぞれ最後の行が `通過 48 / 失敗 0`、`通過 23 / 失敗 0`、`通過 44 / 失敗 0`、`通過 28 / 失敗 0`、`通過 173 / 失敗 0`、`通過 133 / 失敗 0` になる（`--group dev` は `agent/topology.py` が `devices.yaml` を読むための PyYAML）。
`init -backend=false` は provider を取るだけで、state には触らない（apply 済みの PC で打ってもよい）。
`ops/up.sh` と `ops/down.sh`、EC2 の上で打つ `ops/seed_graph.py` は AWS に触らないと動かせないので、`ops/check.sh` は構文だけを見る。
`pyproject.toml` と `uv.lock` はこの確認のためだけのもので、AWS に置く依存は `agent/requirements.txt` と `web/requirements.txt`。`.venv/` は gitignore してある。

### Web を手元で動かす

EC2 に置く前に画面だけ見たいとき、または EC2 で立たない原因を切り分けるとき。チャットは AgentCore Runtime を呼ぶので、手順 3 が済んでいて認証（手順 0-1）が通っていることが要る。トポロジのタブは `agent/data/` の静的データで出る（Runtime が無ければチャットだけエラー表示になる）。

環境変数は `.env.example` に全部並べてある（意味と、AWS 上で誰が入れるか）。写して `RUNTIME_ARN` だけ埋める。`.env` は gitignore 済みで、`web/app.py` がリポジトリ直下の `.env` を読む（`ENV_FILE=<パス>` で場所を変えられる。同じ名前は後の行が勝つ）。

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

ブラウザで http://127.0.0.1:8080 を開く。`RUNTIME_ARN` が空で SSM の `<prefix>/runtime-arn` も無ければ、チャットが「Runtime が配備されていない」のエラー表示になる（EC2 では `RUNTIME_ARN` を渡さず SSM を読む。「[うまくいかないとき](troubleshooting.md)」）。

## VS Code の設定（任意）

手元の PC の VS Code と同じ設定・拡張を社用 PC に入れるためのファイルを置いてある。AWS には触らないので、飛ばしてもよい。

| ファイル | 中身 | どう使うか |
|---|---|---|
| `.vscode/settings.json` | このリポジトリを開いたときだけ効く設定（改行 LF、保存で terraform fmt、`.terraform` を検索から外す） | 何もしなくてよい。フォルダを開けば効く |
| `.vscode/extensions.json` | このリポジトリに要る拡張の推奨 | フォルダを開くと「推奨する拡張機能をインストールしますか」と出る |
| `docs/vscode/user-settings.json` | 手元の PC のユーザー設定 | 中身を貼る（下の 2） |
| `docs/vscode/keybindings.json` | ターミナルで Shift+Enter を改行にするキー割り当て | 中身を貼る（下の 3） |
| `docs/vscode/extensions.txt` | 拡張の一覧。`[repo]` がこのリポジトリ用、`[extra]` は手元の PC に入れている残り | `ops/vscode-setup.sh` が読む |
| `ops/vscode-setup.sh` | 一覧を読んで `code --install-extension` を回すだけのスクリプト | 下の 1 |

1. 拡張を入れる（WSL のターミナルで打つ）。`code` が無いと言われたら、VS Code で WSL のフォルダを開いてから、その中のターミナルで打つ。

```bash
bash ops/vscode-setup.sh
```

手元の PC と同じものを全部入れるなら（PHP や Azure など、このリポジトリに要らないものも入る）:

```bash
ALL=1 bash ops/vscode-setup.sh
```

2. ユーザー設定: VS Code で Ctrl+Shift+P →「基本設定: ユーザー設定を開く (JSON)」→ `docs/vscode/user-settings.json` の中身を貼る。既に設定があるなら、丸ごと上書きせず要るところだけ足す。

3. キー割り当て: Ctrl+Shift+P →「基本設定: キーボードショートカットを開く (JSON)」→ `docs/vscode/keybindings.json` の中身を貼る。

**手元では入れているが、この控えから外した設定。**社用 PC に既定で持ち込むものではないと判断した。要るなら自分で足す。

| 設定 | 外した理由 |
|---|---|
| `claudeCode.allowDangerouslySkipPermissions` | 実行前の確認を飛ばす設定 |
| `security.workspace.trust.untrustedFiles` を `open` | 信頼していないフォルダのファイルをそのまま開く設定 |
| `security.promptForLocalFileProtocolHandling` を `false` | ローカルファイルを開くときの確認を消す設定 |
| `claudeCode.claudeProcessWrapper` | 手元の PC の絶対パス。このリポジトリは public なので置かない |

注意:

- テーマ `One Dark Modern Classic` を出す拡張は、手元の拡張一覧に見当たらなかった（`code --list-extensions` に出ない形で入っているらしい）。同じ見た目にしたいなら Marketplace で名前で探して入れる。入っていなくても既定のテーマになるだけで、壊れはしない。
- WSL では拡張が 2 か所に分かれる。Python や Terraform のように WSL 側に入るものと、テーマ・アイコン・日本語パックのように Windows 側に入るものがある。`ops/vscode-setup.sh` は WSL 側で打つ前提で、失敗したものは最後にまとめて出す。
- SSL 検査のある回線では Marketplace のダウンロードが証明書エラーになることがある。そのときは Windows 側の VS Code から入れるか、Marketplace から `.vsix` を落として「VSIX からのインストール」を使う。

## 入っていないもの

- 会話の永続化。履歴は Runtime のセッション（microVM）の中にだけあり、アイドル 5 分か 1 時間、または画面の再読み込みで消える。
- チャット Web のログの CloudWatch Logs 転送（CloudWatch エージェント）。必要なら AL2023 の `amazon-cloudwatch-agent` を入れる。logs エンドポイントは土台の共用のもの（`create_shared_endpoints`）が VPC 全体で使える。
- 複数人の同時利用を想定した作り。t4g.small で数人程度まで（Gradio の同時実行は 4）。
- BGP の状態の監視。stream で入るのはインタフェースの up/down（ポーリングと trap）だけで、BGP の隣接や経路の変化は異常にならない。
- Temporal の永続化。`temporal server start-dev` の SQLite はタスクの中にあり、タスクが入れ替わると（デプロイ・障害・`ops/down.sh`）実行履歴ごと消える。毎日消す運用なので置いていない。UI（8233）に認証も無く、SSM のポートフォワーディングでしか届かない。
- Temporal のワーカーは ECS on Fargate に置いている。ユーザー決定（2026-09-17「Temporal（EKS）だった。ただいまの段階では EKS ではなく ECS で OK」）のとおり EKS は後回し（クラスタだけで $0.10/h）。
- `query_history`（S3 Tables の履歴の検索）は Athena のワークグループとカタログの接続をまだ置いていないので、案内だけ返す。長期の履歴を調べるには Athena を足す。
- 修復の対象は lab の EC2 で、打てるのは `sudo lab heal-main` と `sudo lab check` だけ（`workflow/worker.py` の `ALLOWED_ACTIONS`。エージェントがそれ以外を返したら `none` にする）。実機には何も打たない。
- Grafana などの可視化は保留。異常一覧と修復案は DynamoDB の表をそのまま出す。
- Kafka から 4 つに分ける設計（2026-09-17）の 4 本目、log + metrics → Splunk。Spark を通さず Kafka の sink（MSK Connect の Splunk Connect for Kafka）にする予定で後回し。Splunk 自体も置いていない（Splunk Enterprise の公式コンテナイメージ `splunk/splunk` はあるが、arm64 のイメージがあるか、Free ライセンス（500 MB/日）で HEC が使えるかは確認できていない）。
- OpenSearch Serverless と Prometheus（`SINK_OPENSEARCH` / `SINK_PROMETHEUS`。既定で作る）の可視化。Grafana は置いていない（後回し）。中身は VPC の中からしか届かないので、見るなら Web の EC2 から curl するか Grafana を足す。
- Neptune のトポロジと lab の実配線の同期。Neptune は手で編集するもので、lab を変えても追随しない。
- 手順書の自動取り込み。S3 のイベントで取り込みジョブを流す仕組みは入れていない。
- 日本語向けの形態素解析（kuromoji など）。キーワード検索は OpenSearch の既定のアナライザで、日本語は細かく切られる。ログの文字列やコマンド名のような英数字の一致には効く。
- ガードレールの機微情報フィルタ（PII）、拒否トピック、単語フィルタ、コンテキストグラウンディング。PII は IP アドレスやホスト名を伏せて運用の回答を壊すので入れていない。単語フィルタとグラウンディングは日本語に対応していない。
- OpenSearch Serverless の閉域化。ネットワークポリシーは公開で、中身に触れるのはデータアクセスポリシーの 2 つ（ナレッジベースのロールと apply した人）だけ。閉域にすると、Terraform のインデックス作成が PC から届かなくなる。
- state の共有（S3 バックエンドとロック）。1 人が 1 台の PC で打つ前提。

## 確認したこと・確認できていないこと

確認したこと（2026-09-14、lab・graph・stream は 2026-09-15、Terraform への移行と `deploy.env` による選び方は 2026-09-16、analytics と、フェーズ 1 / 2 / 3 から機能（AGENT / PIPELINE / WORKFLOW）への付け直しと `terraform/agent` の切り出しは 2026-09-17）。

- `ops/check.sh` が最後まで `すべて通過` で終わる（2026-09-16）。中身は次の 2 つと `bash -n`。
- `deploy.env` の読み込みと機能の分け方を、偽の `aws` / `terraform` で `ops/up.sh` の手順 0 まで流した（2026-09-17）。ファイル無しで土台 + AGENT、`PIPELINE=1` で lab + stream + analytics + graph、`SKIP_STREAM` で analytics も外れ、`SKIP_ANALYTICS` / `SKIP_LAB` + `SKIP_STREAM` / `SKIP_GRAPH` で外れること。`WORKFLOW=1` で workflow が足され、`WORKFLOW=1` に `AGENT=0` / `PIPELINE=0` / `SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` を書くと止まること。古い `PHASE=1 / 2 / 3` が機能に読み替えられ、`2B` / `5A`、`WITH_LAB` / `WITH_STREAM`、値の誤りで何も作らずに止まること。環境変数がファイルより優先されること。`ops/down.sh` がファイルの `KEEP_ECR` を読むこと（`tests/test_workflow.py`）。
- 8 つのルート（`base/ecr` / `base/core` / `agent` / `pipeline/lab` / `pipeline/stream` / `pipeline/analytics` / `pipeline/graph` / `workflow`）で `terraform init -backend=false` と `terraform validate` が通り、`terraform fmt -check -recursive` に差分が無い（Terraform 1.16.0、hashicorp/aws 6.64.0、opensearch-project/opensearch 2.6.0、hashicorp/time 0.14.2、hashicorp/archive 2.8.1。2026-09-17）。`terraform/agent` と `terraform/workflow` の plan は `terraform/base/core`（workflow は agent も）を apply した後でないと remote state が読めずに止まる（設計どおり）。
- Spark の検知と graph の模擬テスト。`tests/test_stream.py`（34 項目、2026-09-17: `spark/snmp_sinks.py` の detect を pyspark 無しで動かし、機器名の引き方、ポーリングの open / resolved、`first_seen` を保つ、解消済みへの up を数えない、MIB 無しの trap から ifDescr を取る、linkUp で resolved、壊れたレコードを飛ばす、開いた瞬間だけ EventBridge に `AnomalyOpened` を、解消した瞬間だけ `AnomalyResolved` を出す。`terraform/pipeline/stream` から detector Lambda と lambda エンドポイントが消え、`anomaly_table_arn` を出すこと）、`tests/test_graph.py`（23 項目: SSM 未設定なら静的、GraphSON の読み替え、Neptune からの組み立て、失敗時と空のときの静的への切り戻し、`add_link` の正規化と重複拒否、`remove_link` / `add_device` / `seed` の Gremlin、`seed` が `status` を入れないこと、`set_status` が回線の両向きと機器に書くこと、`status` が機器一覧・隣接・影響範囲に出ること）、`tests/test_sync.py`（28 項目、2026-09-17: `lab/lab_topology.py` が lab の定義から作る機器と回線が `agent/data/` と同じであること（PyYAML があるときと無いとき）、帯域・主副・asn の読み取り、`ops/up.sh` / `sync-graph.sh` / `seed_graph.py` の受け渡し、`graph/status_handler.py` の `AnomalyOpened` / `AnomalyResolved` から `set_status` への写し（回線 / 機器 / 知らない型は無視）、`terraform/pipeline/graph` の zip・EventBridge のルール・VPC の Lambda・SG・IAM・ロググループ）。
- analytics の静的テスト `tests/test_analytics.py`（169 項目、2026-09-17: `terraform/pipeline/analytics` が読む main / stream の出力が実際に定義されていること、EMR の SG に CIDR の受信が無いこと、S3 Tables のテーブルの列が `spark/snmp_sinks.py` の select の列と同じ順で同じ型であること、`start-job-run` に渡す JSON の Iceberg / S3 Tables の設定と `--sinks` / `--metric-topics` / `--log-topics`、`sinks` で OpenSearch Serverless のコレクション（TIMESERIES、VPC エンドポイントだけ）と Prometheus のワークスペース + `aps-workspaces` のエンドポイントが count で生えること、実行ロールの `aoss:APIAccessAll` / `aps:RemoteWrite`、Kafka の MSK IAM 認証のオプション 4 つ、格納先ごとの checkpoint と 60 秒トリガー、`sinks` の既定が 3 つ全部で `events` のエンドポイントと DynamoDB への書き込み権限が付くこと、`SINK_S3=0` で S3 Tables と `s3tables` のエンドポイントと実行ロールの `S3TablesCatalog` と Spark のカタログの設定が外れること、`ops/up.sh` の `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS` の判定（up.sh から切り出して bash で実際に動かす。全部 0 と、古い `SINKS` との併記は止まる）と jar 6 本が Spark 3.5.6 と揃っていること、up / down の順番。スクリプトは pyspark 無しで import して、引数の検査・トピックの振り分け・メトリクス名とラベル名の規則・remote write の protobuf と snappy（テストの中で手で復号する）・`_bulk` の文書を実際に動かす）。
- workflow の静的テスト `tests/test_workflow.py`（131 項目、2026-09-17: ワーカーのプロンプトと JSON の読み取り、`ALLOWED_ACTIONS` が `lab/lab.sh` のサブコマンドにあること、起こす条件とワークフロー ID、DynamoDB の読み書きの形（GSI `status-updated_at-index`、承認は `pending` のときだけ通る条件式）、Runtime の呼び方（`qualifier=DEFAULT`、セッション ID 33 文字以上）、MCP クライアントの JSON / SSE の読み取りと `toolConfig` への変換、`tools/tools.json` が `agent/` のツール仕様と名前・引数・必須・説明まで同じであること、tools Lambda の振り分け、SQS のメッセージから `anomaly_id` を取ること、`terraform/workflow` の配線（EventBridge のルール → SQS と DLQ、ワーカーの `ANOMALY_QUEUE_URL` と受信 / 削除の権限、VPC の中の tools Lambda と `aoss` / `aps` の権限、ARM64 の Fargate、`start-dev` の SQLite、worker が temporal の起動を待つ、環境変数、IAM が `InvokeAgentRuntime` と `AWS-RunShellScript` だけ、Gateway の `AWS_IAM` / `MCP`、Lambda の zip の中身）、`ops/up.sh` / `down.sh` / `check.sh` と `deploy.env.example`）。
- `temporalio/temporal` 1.9.1 のイメージが arm64 を含むこと（マニフェスト、2026-09-17）。
- EMR Serverless の `emr-7.13.0` が Spark 3.5.6 であること、S3 Tables のカタログが `emr-7.5.0` 以上で使えること、jar 6 本が Maven Central にあること（HTTP 200）、S3 Tables のデータが `<uuid>--table-s3` という名前のバケットに置かれること、EMR Serverless が 0.0.0.0/0 の受信を持つ SG を拒否すること、`start-job-run` に `--mode STREAMING` があること（2026-09-17、AWS の文書）。
- Telegraf の `inputs.snmp` は数値 OID とフィールド名を明示すれば MIB 無しで動き、`inputs.snmp_trap` は v2c を MIB 無しで受ける（varbind の名前は数値 OID）。`agent_host` タグは `source` に替わっている。net-snmp の `monitor` には `iquerySecName` と内部ユーザーが要る。
- MSK は Kafka 4.1.x の KRaft モードで作る（`kafka_version = "4.1.x.kraft"`。Kafka 4 に ZooKeeper モードは無く、版の末尾の `.kraft` が KRaft の指定。Standard ブローカーで選べる最新が 4.1.x で、4.2.x は Express ブローカー専用。AWS の「推奨」の印は 3.9.x に付いたまま。`aws kafka list-kafka-versions` と MSK の supported versions のページで 2026-09-18 に確認。KRaft のコントローラーに追加料金は無い）。クライアントは Kafka 2.1 以降のプロトコルが要る（KIP-896）。MSK Connect 2.7.1、Spark の kafka-clients 3.x、Telegraf（sarama）はどれも満たすが、4.1.x.kraft では kafka.t3.small を `CreateCluster` が `Unsupported InstanceType specified. Valid values: [express.m7g.*, kafka.m5.*, kafka.m7g.*]` で拒否した（2026-09-18 実機）ので、ブローカーは kafka.m5.large にした（2026-09-18 ユーザー決定）。
- Neptune の最新が 1.4.8.0、Neptune の IAM アクションが `neptune-db:*DataViaQuery`、`aws_msk_configuration` の版を `latest_revision` で渡すこと、Lambda の MSK イベントソースが NAT 無しの VPC では lambda と sts のエンドポイントを要ること、MSK Connect の信頼先が `kafkaconnect.amazonaws.com` であること。
- `agent/app.py` と `agent/topology.py` を、boto3 と SDK を差し替えた模擬テスト（`tests/test_app.py`）で確かめた。46 項目（2026-09-17 に Web の編集画面の選択肢 `interfaces` / `link_choices` の 5 項目を足した）: ハイブリッド検索の指定、リランクの有無で `rerankingConfiguration` を付け外しする、質問だけを `guardContent` に入れる、ガードレールで止めた往復を履歴に残さない、参照元の付け方、検索とモデルの失敗、履歴の長さ、ツールの仕様が `toolConfig` に載ること、`toolUse` → `toolResult` の往復、往復の上限（5 回）、無い機器の扱い、トポロジ関数の結果、ツールが 8 つ（トポロジ 4 + 異常一覧 + 証拠 3）、`list_anomalies` が未配備で error を返す、振り分け。
- `web/app.py` を手元（Python 3.14、gradio 5.50.0）で起動し、画面が出ることと、Runtime の呼び出しが `AccessDenied` のときにエラー表示になることを確かめた。
- `web/requirements.txt` の依存が arm64 / cp313 の wheel で全部取れること（`pip download`、58 個、132 MB。numpy は manylinux_2_28 で、AL2023 の glibc 2.34 で動く）。
- FRR 10.2.1・network-multitool v0.10.0・alpine 3.20 のイメージが arm64 を含むこと（マニフェスト）。containerlab v0.79.0 に `linux_arm64.rpm` があること。
- `Retrieve` の `rerankingConfiguration` の形（`type` は `BEDROCK_RERANKING_MODEL` だけ、`numberOfResults` と `numberOfRerankedResults` は 1〜100）と、リランクに要る権限がナレッジベースのサービスロールの `bedrock:Rerank` とモデルへの `bedrock:InvokeModel` であること（https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-prereq.html ）。
- 東京で `amazon.rerank-v1:0` が使えること（https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html ）と、Amazon のモデルは AWS Marketplace を通さないこと（https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html ）。
- Amazon Nova 2 Lite の `jp.` 推論プロファイル（`jp.amazon.nova-2-lite-v1:0`。東京から東京と大阪へ）と、Converse・ガードレールへの対応（https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html ）。日本語が最適化の対象の 15 言語に入っていること（Amazon Nova のユーザーガイド）。
- 東京に `bedrock-agent-runtime` / `aoss` / `aoss-data` / `bedrock-agent` のエンドポイントサービスがある。
- ガードレールの APAC プロファイル `apac.guardrail.v1:0` の、東京からの行き先リージョン（https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-cross-region-support.html ）。
- Classic 階層が日本語に非対応で、Standard 階層がコンテンツフィルタ・プロンプト攻撃で日本語に対応していること。
- 東京で ssm / ssmmessages / bedrock-agentcore のエンドポイントサービスが 1a / 1c / 1d にあり、プライベート DNS に対応している。
- AL2023（2023.12.20260817）の AWS CLI は 2.33.15 で、`bedrock-agentcore invoke-agent-runtime` を持つ。
- AL2023 のパッケージ一覧に `python3.13` がある。`/usr/bin/python3` は 3.9 のまま（https://docs.aws.amazon.com/linux/al2023/ug/python.html ）。
- `bedrock-agentcore` 1.23.0 は Python 3.10 以上で、3.13 を対応版に挙げている（PyPI の `requires_python` と classifiers）。

確認できていないこと。

- **実環境への apply。**上はすべて手元の静的検査と模擬テストで、`terraform/agent` に切り出した後の形は AWS 上で apply していない（切り出す前の `terraform/base/core` 一体の形は 2026-09-16〜17 に動いた）。PIPELINE の 4 ルートも同じで、lab の起動、Telegraf → MSK の IAM 認証、MSK Connect、Spark のジョブ、S3 Tables / OpenSearch Serverless / Prometheus への書き込み、Spark の検知（DynamoDB と EventBridge）、Neptune への Gremlin は実環境で通していない。Mac からの通しの apply も打っていない。
- workflow の次の点。Gateway（MCP）に VPC モードの Runtime と Fargate のタスクから届くか（Gateway のエンドポイントは公開で、閉域からは `bedrock-agentcore` の interface エンドポイント経由になる想定。届かなければ Runtime はコンテナの中のツールに戻る）。組織の SCP / IAM が ECS / Fargate / AgentCore Gateway / Lambda を止めていないか。`temporal server start-dev` が Fargate の中で `--ip 0.0.0.0` で上がり、worker が `localhost:7233` に付けるか。`temporalio` 1.33.0 の SDK と Temporal サーバー 1.9.1 の組み合わせ。SSM Run Command が lab の EC2 で `sudo lab heal-main` を通し、異常が resolved に変わるまでの時間が確認の 6 回 × 30 秒に収まるか。Nova 2 Lite が求めた JSON の形で修復案を返すか（返さなければ `action=none` の案になる）。EMR Serverless の driver から `events` の interface エンドポイント経由で PutEvents が通るか。EventBridge のルールから SQS へ届き、ワーカーが `sqs` の interface エンドポイント経由で long polling できるか。VPC の中の tools Lambda から Neptune（SG の穴）/ OpenSearch Serverless（`aoss` エンドポイント、コレクションの network policy）/ Prometheus（`aps-workspaces` エンドポイントで `query_range`）に届くか。OpenSearch Serverless の logs コレクションの OCU が `CREATE_KB=1` のコレクションと共有されるか（されなければ +$0.33/h）。Spark が書く文書の `@timestamp` を `search_logs` がそのまま range で引けるか。
- analytics の次の点。`emr-7.13.0` に S3 Tables のカタログの jar が同梱されているか（同梱なら `s3-tables-catalog-for-iceberg-runtime` を足すと衝突する可能性がある）。Kafka の jar 6 本の組み合わせで Structured Streaming の Kafka ソースが動くか。閉域から S3 Tables に届くか（`s3tables` の interface エンドポイントと、S3 ゲートウェイエンドポイントの `*--table-s3` の許可）。Lake Formation の設定が要るか。EMR の SG に自分自身からの受信が要るか。組織の SCP / IAM が EMR Serverless / S3 Tables を止めていないか。
- `templatefile` で展開した user_data（`web_user_data.sh.tftpl` / `lab_user_data.sh.tftpl`）が `bash -n` を通るか。展開後のシェルを手元で取り出して確かめていない。
- `aws_mskconnect_connector` の `kafkaconnect_version` に `2.7.1` が入るか（許される値の一覧を文書で確認できていない）。MSK Connect が S3 とログに届くのに、S3 ゲートウェイと logs エンドポイント以外の経路が要るか。
- Telegraf 1.40.0 の `outputs.kafka` の `AWS-MSK-IAM` が、インスタンスロール（IMDS）の資格情報で動くか。もっと古い版で使えるかも未確認。
- Confluent の S3 sink 12.1.11 の zip を CustomPlugin として登録できるか（Confluent Community License。ダウンロードの URL と同意の要否）。
- `web/app.py` は手元で `uv run --group web` の gradio 5.50.0 で読み込んで Blocks が組み上がることと、編集画面のハンドラの入力チェック（未選択・同じ機器・IF 無し・Neptune 未配備）までを確かめた（2026-09-17）。ブラウザでの操作と Neptune への実書き込みは EC2 で見る。Gremlin の形は `tests/test_graph.py`。
- boto3 の `neptunedata` クライアントが VPC モードの Runtime からクラスターの DNS 名で届くか（プライベート DNS。エンドポイントは要らない想定）。
- `opensearch_index.kb` の作成が、データアクセスポリシーの反映待ちで 403 になることがあるか（ポリシーとコレクションの後に `time_sleep` で 60 秒待っているが、足りるかは未確認）。
- `data.aws_iam_session_context` の `issuer_arn` が、Identity Center のロール（パス付き）でデータアクセスポリシーに効く形で取れるか。
- opensearch provider（2.6.0）が、WSL の証明書ストア（`opensearch_cacert_file`）で社内の SSL 検査を通るか。
- ネットワークポリシーを非公開（`SourceServices: bedrock.amazonaws.com` と VPC エンドポイント）にしても、Terraform のインデックス作成が通るか。
- `aws_bedrock_guardrail_version` の `description` を変えたとき、版が作り直され Runtime の環境変数が新しい版を指すか（「変更するとき」）。
- 既定のアナライザで、日本語の質問にキーワード検索がどれだけ効くか。
- ハイブリッド検索がベクトルとキーワードの結果をどう合わせるか（それぞれ何件取るか、点数の合わせ方）。
- Amazon Nova 2 Lite が、この資料と質問でどれだけ日本語の回答を正しく書くか。`guardContent` とシステムプロンプトを付けた Converse を実環境で呼んでいない。
- Amazon Nova 2 Lite の提供終了日。モデルカードには「2026-12-02 より前には終わらない」とだけある。終わる前に後継のモデル ID へ替える（変数 `model_id` を変えるだけ）。
- Amazon Rerank 1.0 が日本語の手順書でどれだけ順位を良くするか。日本語に対応するかを AWS の文書で確認できていない。候補 20 件 → 5 件が妥当か。
- Converse の `guardContent` を使ったとき、履歴の過去の質問が判定されないこと（文書の説明どおりか）。
- 閉域の EC2 から、S3 ゲートウェイ経由で AL2023 のリポジトリに届き `dnf install python3.13 python3.13-pip` / `dnf install docker` が通るか（バケット名は AWS の文書の例から取った）。`python3.13-pip` というパッケージ名も未確認（無ければ `python3.13 -m ensurepip`）。
- t4g.small で Gradio（numpy / pandas 込み）が安定して動くか。手元の起動時は約 400 MB。
- containerlab の rpm が AL2023 で依存なしに入るか。lab 14 コンテナが t4g.large で問題なく動くか。
- t4g.small / t4g.large / gp3 の単価（Price List API を引けていない）。
- VPC モードの Runtime が、イメージの取得に VPC 内の ECR / S3 エンドポイントを使うのか、サービス側で取得するのか。安全側に倒してエンドポイントを作る設定を既定にした。
- SSM Agent のポートフォワーディングが `localhost` を IPv6（`::1`）で先に試すか。Web は `127.0.0.1` だけで待つ。つながらない場合は journald とセッションのエラーを見る。
- Session Manager plugin が社内プロキシの環境変数に従うか。
- Runtime のロググループが作られるタイミング（Runtime の作成時か、最初の呼び出し時か）。
- `InvokeAgentRuntime` が CloudTrail の管理イベントとして既定で記録されるか。
- VS Code の拡張が、WSL 側と Windows 側のどちらに入るか（テーマ・アイコン・日本語パックは Windows 側の想定）。SSL 検査のある回線で Marketplace からダウンロードできるか。`ops/vscode-setup.sh` は社用 PC で動かしていない。
