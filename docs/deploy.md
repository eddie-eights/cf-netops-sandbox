# デプロイの詳しい説明（`ops/up.sh` / `ops/down.sh`）

← [README](../README.md)

業務終了後に全部消し、翌朝また作る運用なら、この 2 本を使う。**`ops/up.sh` は `deploy.env` で `1` にした機能を 1 本で作る**: 土台（`terraform/base/ecr` → `terraform/base/core`。手順 1〜4・7）は必ず作り、`AGENT=1`（既定。agent での分析）は `terraform/agent`（Runtime とガードレール。手順 3 の後半と 5）を、`PIPELINE=1`（データパイプラインとトポロジ）は lab → stream（Telegraf → MSK）→ analytics（Spark → S3 Tables / OpenSearch Serverless / Prometheus と、異常検知 → DynamoDB の「異常一覧」+ EventBridge）と graph（Neptune と静的トポロジの投入）を、`WORKFLOW=1`（Temporal での実行）は workflow（EventBridge → SQS、Temporal on ECS Fargate のワーカー、AgentCore Gateway（MCP））を足す。機能は互いに独立で、翌日に別の機能を `1` にして打ち直せばその機能だけ足される。PIPELINE の一部だけ要らないときは `SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` / `SKIP_GRAPH`。WORKFLOW は AGENT と lab / stream / analytics が要るので、`WORKFLOW=1` なら `AGENT=1` と `PIPELINE=1` にし、`SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` は書けない。
中身は下の手順のコマンドそのもので、**できているものは飛ばす**（Terraform は差分だけ作る、ECR に同じタグのイメージがあればビルドしない、`wheels/`・rpm・zip が手元にあれば取り直さない、Neptune に機器が入っていれば投入しない）ので、途中で落ちても同じコマンドを打ち直せばよい。
手順 0 の環境変数は要らない（スクリプトが認証情報と Terraform の出力から取る）。**IAM ユーザーの一時セッション（`sts get-session-token`）で入っていると、その旨を出して止まる**（IAM の API が呼べず、ロールを作る apply が落ちるため）。社内 PC は「[社内 PC で使うとき](setup.md)」の設定を入れたターミナルで打つ。

**待機の時間課金は、土台 0.05 + 共用のエンドポイント 0.08（AGENT / lab / analytics のどれかを作るとき）+ AGENT 0.05（`CREATE_KB=1` なら +0.36）+ PIPELINE の lab（t4g.large）0.09 + stream（MSK kafka.m5.large × 2 / MSK Connect）0.71 + analytics（EMR Serverless / S3 Tables / エンドポイント）0.20 + OpenSearch Serverless の logs コレクション最大 0.33 + Prometheus 0.03 + graph（Neptune）0.14 + WORKFLOW 0.06 で、全部作ると約 $1.74/h**（「[1 時間起動したときの試算](cost.md)」。`ops/up.sh` も手順 0 で目安を出す）。**使い終わったら当日中に `ops/down.sh` を打つ。**

初回だけ、設定のファイルを写す（`deploy.env` は配布物には入っていない）。写したら **`OWNER`（デプロイする人の名前。必須）の行の `#` を外して自分の名前に書き換える**:

```bash
cp deploy.env.example deploy.env
```

データパイプラインまで作る日は `deploy.env` の `PIPELINE=0` を `PIPELINE=1` に書き換えてから打つ（要らないルートがあれば `#SKIP_…=1` の行頭の `#` を外す）。機能を何も書かなければ土台と AGENT を作る:

```bash
ops/up.sh
```

初回は `PIPELINE=1` で 40〜60 分かかる（MSK の作成だけで 20〜30 分。`SKIP_STREAM=1` なら 30〜40 分）。AGENT だけなら 10〜15 分。
`deploy.env` を書き換えずにその回だけ変えるなら、同じ名前の環境変数を付けて打つ（空でない環境変数が `deploy.env` より優先）:

```bash
PIPELINE=1 ops/up.sh
```

`AGENT` / `PIPELINE` / `WORKFLOW` / `CREATE_KB` に書けるのは `1` / `0`（`true` / `false`、`yes` / `no` でもよい）。知らないキーや値の誤りがあれば、何も作らずに止まる。ほかに書けるものは下の「`deploy.env` に書けるもの」の表。
**機能を `0` に戻して打っても、前に作ったルートは消さない。**`PIPELINE=1` で作った翌日に `PIPELINE=0` で打つと、lab / stream / analytics / graph は残ったまま課金が続く。消すのは `ops/down.sh`。

| 順 | 何をする | 対応する手順 |
|---|---|---|
| 0 | `deploy.env` を読む（無ければ環境変数と既定値で動く。知らないキーや値の誤りがあれば止まる）。`aws` / `terraform` / `python3`（無ければ `uv`）/ `curl` / `docker` と `docker buildx` / `session-manager-plugin`（`NO_PORTFORWARD` が空のとき）があるか、認証が通っているかを確かめる。IAM ユーザーの一時セッション（`sts get-session-token`）なら止まる。鍵が環境変数に無ければ（`aws login` など）、Terraform には AWS CLI 経由（`credential_process`）で認証情報を渡す（0-1 の「`aws login` で入っているとき」を一時ファイルで行う）。作る機能とルートと、待機の時間課金の目安を表示する（`AGENT` / `PIPELINE` / `WORKFLOW` / `CREATE_KB` が `1` / `0` 以外、`SKIP_LAB=1` で `SKIP_STREAM` が空、`WORKFLOW=1` で `AGENT=0` か `PIPELINE=0` か `SKIP_LAB` か `SKIP_STREAM` か `SKIP_ANALYTICS` のときは、何も作らずに止まる。`SKIP_STREAM=1` なら `SKIP_ANALYTICS=1` に自動でなる） | 0 |
| 1 | `terraform/base/ecr` を init / apply | 1 |
| 2 | ECR に**無いタグだけ** arm64 でビルドして push する（`AGENT=1` ならエージェント。lab を作るときは lab の frr / multitool / snmpd も。`WORKFLOW=1` ではワーカーもビルドし、Temporal（`temporalio/temporal`）は arm64 のイメージをそのまま ECR にミラーする）。PC の `docker buildx` で作る（dockerd が動いていないとき、agent か snmpd を作るのに `docker buildx ls` に `linux/arm64` が無いときは止まる） | 2 / lab-1 |
| 3 | `terraform/base/core` を init / apply（土台。初回 3〜5 分）。graph を作るとき（`PIPELINE=1` で `SKIP_GRAPH` が空）は、終わったら `terraform/pipeline/graph` の apply を**裏で**始める（10〜15 分。ログは `ops/logs/graph-apply.log`） | 3 / g-1 |
| 3-3 | **`AGENT=1` のときだけ。**`terraform/agent` を init / apply（Runtime とガードレール。5〜10 分。`CREATE_KB=1` なら OpenSearch Serverless のコレクションも作るので 10〜20 分） | 3 |
| 4 | wheel を取り（`wheels/` が空のときだけ）、Web の部品を S3 に置く。`CREATE_KB=1` なら手順書も置いて、取り込みが `COMPLETE` になるまで待つ。EC2 の初回の user_data が終わるのを待ってから再起動し、Web のサービスが `active` になるまで待つ | 4 |
| 5 | **lab を作るときだけ**（`PIPELINE=1` で `SKIP_LAB` が空）。containerlab の rpm（stream を作るなら Telegraf の rpm と S3 sink の zip も。`CREATE_S3_SINK=0` なら zip は取らない）を展開したフォルダの直下に取り（無いときだけ）、lab の設定と一緒に S3 に置く。lab の EC2 を作る前に置くので、Telegraf まで最初の起動で入る。**analytics を作るときは**、Spark の jar 6 本（Maven Central）を `jars/` に取り（無いときだけ）、`spark/snmp_sinks.py` と一緒に S3 の `analytics/` に置く | lab-2 / s-1 / a-1 |
| 6 | **lab を作るときだけ。**`terraform/pipeline/lab` を init / apply | lab-3 |
| 7 | **stream を作るときだけ**（`SKIP_STREAM` が空）。`terraform/pipeline/stream` を init / apply（MSK の作成に 20〜30 分）。lab が前の実行から残っていて Telegraf が入っていなければ、lab の EC2 を再起動する | s-2 / s-3 |
| 7-3 | **analytics を作るときだけ**（`SKIP_ANALYTICS` が空）。`terraform/pipeline/analytics` を init / apply（数分） | a-2 |
| 7-4 | **analytics を作るときだけ。**Spark のストリーミングジョブ（Kafka → S3 Tables / OpenSearch / Prometheus と異常検知）が動いていなければ `start-job-run` で起こす（起動に 2〜5 分。動いていれば何もしない） | a-3 |
| 8 | **graph を作るときだけ。**graph の apply が終わるのを待ち、Neptune が空なら lab の定義（`lab/wanlab.clab.yml.in` と `lab/frr/*.conf`）から作ったトポロジを入れる（`lab/lab_topology.py` の出力を `ops/seed_graph.py` に渡して Web の EC2 の上で打つ。「[Neptune のトポロジを lab から作る](pipeline.md)」）。graph か stream を作ったときは、Web を再起動して `active` になるまで待つ（起動時に異常テーブルと Neptune の場所を読むため） | g-2 |
| 8-5 | **`WORKFLOW=1` のときだけ。**`terraform/workflow` を init / apply（数分）し、ECS のサービスが安定する（イメージの取得と Temporal の起動。1〜3 分）まで待つ。ワーカーのログを追うコマンドと、Temporal の UI を PC で開くポートフォワーディングのコマンド（Web の EC2 経由でタスクの 8233 へ）を表示する | w-2 / w-3 |
| 8-6 | **`WORKFLOW=1` のときだけ。**Web を再起動して `active` になるまで待つ（起動時に修復案テーブルの場所を読むため。Runtime は再起動せず、5 分以内に Gateway を拾う） | w-4 |
| 9 | **`AGENT=1` のときだけ。**Runtime のロググループに保持 7 日とタグ。まだ無ければ先に同じ名前で作る（AgentCore が既存のロググループをそのまま使うかは 2026-09-15 時点で未確認。使わず別名で作った場合は手順 5 を手で打つ） | 5 |
| 10 | 利用者に配る `start_session_command`（lab を作ったら lab に入るコマンドも）を表示し、ポートフォワーディングを開いたまま止まる（`Ctrl+C` で閉じる） | 7 |

手順 6（利用者への権限）は人に渡す作業なので入れていない。
Terraform の確認プロンプトは出さずに進む（スクリプトの中は `-auto-approve`）。手で打つ下の手順では、apply のたびに差分が出て `yes` と打つまで止まる。
途中で落ちたときは、裏の graph の apply が動いていればそれが終わるまで待ってから止まる（打ち直したときに state のロックでぶつからないように）。その間ターミナルを閉じない。

**S3 sink の zip が取れないとき。**zip は Confluent Hub から取り、ダウンロードに利用条件への同意が要ることがある。zip でないものが返ったら、`ops/up.sh` は案内を出して止まる。
ブラウザで s-1 の URL から取って展開したフォルダの直下に同じ名前（`confluentinc-kafka-connect-s3-12.1.11.zip`）で置いて打ち直すか、S3 sink（MSK Connect）無しでよければ `deploy.env` に `CREATE_S3_SINK=0` を書いて打ち直す。

`deploy.env` に書けるもの（**`OWNER` だけ必須**で、ほかは任意。同じ名前の環境変数でも渡せて、空でない環境変数が `deploy.env` より優先。見本と説明は `deploy.env.example`）:

| キー | 意味 |
|---|---|
| `OWNER` | **デプロイする人の名前。必須**（既定は無い。書かないと `ops/up.sh` / `ops/down.sh` が先頭で止まり、手で `terraform apply` を打つと値を聞かれる。英小文字で始まる 14 文字までの英小文字・数字・ハイフンで、ハイフンは連続させず末尾にも置かない）。リソース名（`<owner>-nwc-poc-vpc` など）・SSM のパス（`/<owner>-nwc-poc/…`）・ECR のリポジトリ名・EC2 の systemd ユニット名・`Project` タグの接頭辞はこの名前から `<owner>-nwc-poc` として作られ、`owner` タグにはこの名前がそのまま入る。1 つの AWS アカウントを何人かで使うときに、自分の名前で自分のリソースを探せるようにするための値（14 文字なのは、接頭辞が OpenSearch Serverless の data access policy 名 `<接頭辞>-logs-read` の 32 文字に収まる長さだから。ハイフンの制限は ECR のリポジトリ名）。AgentCore Runtime の名前だけはハイフンが使えないので `-` を `_` にした `<owner>_nwc_poc_agent` になる。**作ったあとで変えない**（名前が変わったリソースを Terraform は別のものと見るので、打ち直すと全部作り直しになる。変えるなら先に `ops/down.sh`）。`ops/down.sh` も同じ `deploy.env` を読むので、書き換えずに打てば消す相手は揃う |
| `AGENT` | agent での分析を作るか。`1`（既定）で `terraform/agent`（AgentCore Runtime・ガードレール・bedrock のエンドポイント 3 本）を足す。Web の「チャット」タブが使える。約 $0.05/h（ecr.api / ecr.dkr / logs の共用のエンドポイント 約 $0.08/h は土台の側で、AGENT か lab か analytics を作るときにかかる） |
| `PIPELINE` | データパイプラインとトポロジを作るか。`1` で lab / stream / analytics / graph を足す（既定 `0`）。Web の「トポロジ」「異常一覧」タブが動く。約 $1.50/h（`SINK_*` が既定のとき。`SKIP_*` / `SINK_*` で減らせる） |
| `WORKFLOW` | Temporal での実行を作るか。`1` で workflow を足す（既定 `0`）。AGENT と lab / stream / analytics が要るので、`AGENT=1` と `PIPELINE=1` にし、`SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` とは一緒に書けない。約 $0.06/h |
| `CREATE_KB` | `AGENT=1` でナレッジベース（S3 の md → Titan Embeddings → OpenSearch Serverless。手順 4 の手順書の取り込み）も作るか。既定 `0`（エージェントはモデルとツールだけで答える）。`1` で +$0.36/h（OpenSearch Serverless の最小 OCU と bedrock-agent-runtime のエンドポイント） |
| `SKIP_LAB=1` | `PIPELINE=1` で lab を作らない。約 $0.09/h 下がる。stream は lab の Telegraf から流すので、`SKIP_STREAM=1` も書く（無いと止まる） |
| `SKIP_STREAM=1` | `PIPELINE=1` で stream（Telegraf → MSK と、MSK Connect の S3 sink）を作らない。読む Kafka が無くなるので analytics も作らない。「異常一覧」は使えない。約 $1.27/h 下がる（`SINK_*` が既定のとき） |
| `SKIP_ANALYTICS=1` | `PIPELINE=1` で analytics（EMR Serverless の Spark、`SINK_*` の格納先、異常検知）を作らない。「異常一覧」は使えない（検知は Spark がする）。約 $0.56/h 下がる（`SINK_*` が既定のとき） |
| `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS` | analytics の Spark の格納先を 1 つずつ作る（`1`）/ 作らない（`0`）。既定は 3 つとも `1`。`0` にした格納先は Spark が書かないだけでなく、リソースごと作らない（前に作ってあれば、打ち直した `ops/up.sh` が消す。S3 Tables のテーブルの中身も消える）。3 つとも `0` は止まる（ジョブは格納先が 1 つ以上要る。analytics ごと要らないなら `SKIP_ANALYTICS=1`）。`SINK_S3` = 全トピック → S3 Tables（Iceberg）。テーブルは無料、`s3tables` のエンドポイント 2 本で +$0.03/h。stream の S3 sink（`CREATE_S3_SINK`）とは別物。`SINK_OPENSEARCH` = `traps` と `logs` → OpenSearch Serverless の TIMESERIES コレクション（VPC の中からだけ届く。OCU が `CREATE_KB=1` のコレクションと共有されるか確認できていないので最大 +$0.33/h）。`SINK_PROMETHEUS` = `metrics` → Amazon Managed Service for Prometheus（エンドポイント 2 本で +$0.03/h と取り込みのサンプル課金）。`terraform/pipeline/analytics` の `sinks`（`iceberg` / `opensearch` / `prometheus`）に組んで渡す |
| `SKIP_GRAPH=1` | `PIPELINE=1` で graph（Neptune）を作らない。約 $0.14/h 下がる。「トポロジ」タブは静的データを出す（編集はできない） |
| `CREATE_S3_SINK=0` | stream の S3 sink（MSK Connect）を作らない。約 $0.14/h 下がる。履歴の正本は analytics の S3 Tables なので、外してもデータは残る。`ops/down.sh` には要らない（state から読む） |
| `IMAGE_TAG` | エージェントのイメージのタグ（`WORKFLOW=1` ではワーカーも同じタグ）。既定 `v1`。`agent/` や `workflow/` を変えたら `v2` などに書き換える（`deploy.env` に書けば毎回付けなくてよい。行を消すと `v1` に戻す差分になる） |
| `ADMIN_ARN` | `terraform/agent` の `kb_admin_principal_arn`（`CREATE_KB=1` のときだけ使う）。自動で取れない認証の形のときだけ（スクリプトが止まって言う） |
| `VPC_CIDR` / `CLIENT_CIDR` | 手順 3 の `vpc_cidr` / `client_cidr` |
| `AWS_CA_BUNDLE` / `OPENSEARCH_CACERT_FILE` | 「[社内 PC で使うとき](setup.md)」の CA の PEM。`OPENSEARCH_CACERT_FILE` が無ければ `AWS_CA_BUNDLE` を使う |
| `AWS_PROFILE` | AWS CLI のプロファイル名。鍵を環境変数（`AWS_ACCESS_KEY_ID` など）で渡しているときは書かない |
| `LOCAL_PORT` | PC 側のポート。既定 8080 |
| `NO_PORTFORWARD=1` | ポートフォワーディングを開かずに終わる |
| `KEEP_ECR=1` | `ops/down.sh` で ECR（イメージ）を残す（下の表。`ops/up.sh` は見ない） |
| `TF_VERBOSE=1` | `ops/up.sh` / `ops/down.sh` が terraform の出力を全部そのまま出す。既定は `Plan:`、できた（消えた）リソース、5 分ごとの経過、エラーだけを出し、全文は `ops/logs/tf-<ルート>-apply.log`（`-destroy.log`）に残す |

- `AGENT` / `PIPELINE` / `WORKFLOW` / `CREATE_KB` / `SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` / `SKIP_GRAPH` / `NO_PORTFORWARD` は `1` / `0` のほか `true` / `false`、`yes` / `no` でもよい。`CREATE_S3_SINK` と `KEEP_ECR` は `1` か `0` だけ。
- `deploy.env` はシェルとして実行しない。`$HOME` や `$(…)` は展開せず、値の先頭の `~/` だけ読み替える。
- 知らないキー（打ち間違い）や、同じキーの 2 回目があると、手順 0 で何も作らずに止まる。
- `deploy.env` に書いた `1` をその回だけ打ち消すときは、空ではなく `0` を渡す（`SKIP_GRAPH=0 ops/up.sh`）。
- 別の場所のファイルを使うときは、環境変数 `DEPLOY_ENV_FILE` にそのファイルのパスを入れて打つ（相対パスは打った場所から見る）。

```bash
ops/down.sh
```

「[片付け](deploy-manual.md)」と同じ順（workflow → analytics（Spark のジョブを止めてから）→ graph → stream → lab → agent → main → ecr → Runtime のロググループ）で、**state にリソースが載っているルートだけ** destroy する（作っていないルートは飛ばす。`deploy.env` の `AGENT` / `PIPELINE` / `WORKFLOW` / `CREATE_KB` / `SKIP_*` / `CREATE_S3_SINK` は見ないので、`PIPELINE=0` に戻した後でも前に作った lab / stream / analytics / graph / workflow まで消す）。
バケットは中身ごと、ECR はイメージごと消える（`KEEP_ECR=1` のときは ECR を残す）。最後に `Project=netops-nwc-poc` のタグが付いたものが残っていないかを出す（何も出なければ全部消えている）。
graph と workflow は VPC の中に Lambda を持つ。Lambda は関数を消しても ENI が `available` のまま 20〜40 分残ることがあり、その間は SG とサブネットが消えない（2026-09-18 に graph の destroy が 20 分以上止まった）。`ops/down.sh` はこの 2 つを消している間、その関数の `available` な ENI だけを裏で消し続ける（実機では未確認）。
ECR を残すか消すかは `deploy.env` の `KEEP_ECR`（環境変数でもよい）で選ぶ。残すと翌朝の `ops/up.sh` がビルドを飛ばせる（保管料は月数円。Runtime はイメージが無いと作れないので、翌朝ビルドし直す時間が惜しいならこちら）。

| `KEEP_ECR` | ECR |
|---|---|
| 書かない / `0` | イメージごと消す（既定） |
| `1` | 残す |
| それ以外（`yes` など） | 手順 0 で止まる。何も消さない |

```bash
KEEP_ECR=1 ops/down.sh
```

**state の注意（ローカル state なので大事）。**

- `terraform/<ルート>/terraform.tfstate` を**消さない**。消すと Terraform は作ったものを忘れ、`ops/down.sh` が「無い」と言って飛ばし、AWS にリソースと課金が残る。次の `ops/up.sh` は同じ名前がぶつかって `AlreadyExists` で落ちる。
- **up と down は同じ PC で打つ。**別の PC には state が無いので、同じことが起きる。PC を替えるときは、元の PC で `ops/down.sh` を済ませてから。
- 消してしまったときは、コンソールで `netops-nwc-poc` の名前とタグのリソースを手で消す（「[名前とタグ](architecture.md)」の `get-resources` で探す）。

どちらも bash スクリプトなので、Windows は WSL のシェルから打つ。以下は、スクリプトの中身を 1 つずつ手で打つときの説明でもある。

コマンドの中の値は 3 種類ある。**書き方で見分けられるようにしてある。**

| 書き方 | 意味 | 例 |
|---|---|---|
| そのままの文字 | **実際の値。置き換えない** | `netops-nwc-poc`、`owner=netops`、`ap-northeast-1`、`v1`、ルートのディレクトリ名（`terraform/base/core` など） |
| `$ACCOUNT_ID` `$KB_BUCKET` `$INSTANCE_ID` のように `$` で始まる | **あなたの環境の値が入った環境変数。**`$ACCOUNT_ID`（と、要るときだけ `$ADMIN_ARN`）は手順 0 で入れる。それ以外（`$REPO` `$KB_BUCKET` `$INSTANCE_ID` `$KB_ID` `$DS_ID` `$JOB_ID` `$LOG_GROUP` `$RUNTIME_ARN` `$LAB_INSTANCE_ID`）は **`terraform output` で取る値**で、使う枠の 1 行目に「取るコマンド + `echo`」を置いてある。枠ごと上から順にコピーして打てば、値を書き換える場所は無い（シェルが `$ACCOUNT_ID` を 12 桁の数字に置き換えて実行する） | `$ACCOUNT_ID` → `123456789012` のような 12 桁。`$INSTANCE_ID` → `i-0` で始まるインスタンス ID |
| `<日本語>` の山括弧 | 手で書き換える場所（手順 0-1 と 0-3、「[社内 PC で使うとき](setup.md)」の 1、手順 6 の JSON だけ） | `<ロール名>` `<プロファイル>` `<アカウント ID>` |

**コマンドの枠の中に、ID・ARN・バケット名の実物は 1 つも書いていない**（環境ごとに違うので書けない）。`i-0…` や `arn:aws:…` の形が本文に出てきたら、それは「こういう形の値が出る」という説明で、打つものではない。
`terraform output` は**その PC の state を読む**ので、apply した PC で打つ。

