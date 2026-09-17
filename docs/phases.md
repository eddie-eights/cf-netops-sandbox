# フェーズごとの概要（2026-09-17 時点）

いま決まっている範囲だけを書く。**決まっていないことは「未定」と書いてある。**
手順とコマンドの正本は [`../README.md`](../README.md)、構成図は [`20260914-fukuda-nwc-poc-architecture.html`](20260914-fukuda-nwc-poc-architecture.html)。
ここはその手前の「どこまで作ってあって、次に何が残っているか」だけを見るための頁。

**2026-09-17 ユーザー決定: フェーズの番号で積み上げる形をやめ、機能ごとに独立して作る形にした。**目的は費用を抑えること（「フェーズ 1 の上に 2 が乗るのではなく、機能ごとにデプロイできれば嬉しい」）。土台（`terraform/base/ecr` + `terraform/base/core`）を必ず作り、その上に 3 つの機能を要るものだけ載せる。`deploy.env` のキーは `AGENT` / `PIPELINE` / `WORKFLOW`。

| 機能（`deploy.env`） | 一言で | 作るルート |
|---|---|---|
| 土台（必ず） | 閉域の VPC、Web の EC2、S3 バケット、Runtime / Web のロール | `terraform/base/ecr` → `terraform/base/core` |
| `AGENT=1`（既定） | **agent での分析**: AgentCore Runtime + ガードレール。ナレッジベースは `CREATE_KB=1` のときだけ（既定は作らない。2026-09-17 ユーザー決定「ナレッジベースは今回は使わないかな。デフォルトは OFF でいい」） | `terraform/agent` |
| `PIPELINE=1` | **データパイプライン**: containerlab → Telegraf → Kafka → Spark → S3 Tables / OpenSearch / Prometheus。トポロジは Neptune（「graph はパイプライン側にする」） | `terraform/pipeline/lab` → `stream` → `analytics`、並行して `graph` |
| `WORKFLOW=1` | **Temporal での実行**: エージェントが原因を調査し、人が修復を承認するまで。AGENT と PIPELINE が要る | `terraform/workflow` |

古い番号との対応（`deploy.env` に `PHASE` が残っていれば `ops/up.sh` が読み替えて注意を出す。2B / 5A / 5B などは止まる）:

| 古い番号 | いまの書き方 |
|---|---|
| 1（LLM + RAG の対話） | `AGENT=1`。ナレッジベースは `CREATE_KB=1` を足す |
| 2（lab + graph）と 2 の任意（`WITH_STREAM=1`）、2B（Kafka → Spark → Iceberg） | `PIPELINE=1`（`AGENT=1` はそのまま）。stream / analytics は本体で、外すなら `SKIP_STREAM=1` / `SKIP_ANALYTICS=1` |
| 3（欠番）/ 4（ベクトル + 全文検索） | 4 は `CREATE_KB=1`（ナレッジベースの検索）。番号は使わない |
| 5A（エージェントの調査ワークフロー）+ 5B（人が承認して直す）、3 | `WORKFLOW=1`（`AGENT=1` と `PIPELINE=1` も） |

**3 つとも Terraform とスクリプトがある。**AWS 上の apply は、`terraform/agent` に切り出した後の形はどれも未確認（各機能の「決まっていないこと」）。以下の節の見出しは古い番号のままにしてある（1 = AGENT、2 = PIPELINE、3 = WORKFLOW と読む）。

## 全体

| 機能 | 到達点 | 作る Terraform ルート | 立てている間の費用（東京・税抜・$1 = 150 円） | 状態 |
|---|---|---|---|---|
| 土台（必ず作る） | 閉域の VPC に Web の EC2（Gradio）を置き、SSM のポートフォワーディングで開く | `terraform/base/ecr` → `terraform/base/core` | 約 $0.05/h（約 8 円）。AGENT / lab / analytics のどれかを作るなら共用のエンドポイント（ecr.api / ecr.dkr / logs）で +$0.08/h | **動く** |
| AGENT（`AGENT=1`、既定。旧 1） | agent での分析。閉域の VPC でチャットし、モデルとトポロジのツールで答える。`CREATE_KB=1` なら手順書も引く | 土台に `terraform/agent` | 約 $0.05/h（約 8 円。2026-09-18 に ecr.api / ecr.dkr / logs を土台へ移した。合わせると今までどおり約 $0.13/h）。`CREATE_KB=1` なら +$0.36/h | **動いた**（`terraform/base/core` 一体の形で 2026-09-16〜17。切り出した後の apply は未確認） |
| PIPELINE（`PIPELINE=1`。旧 2） | データパイプライン。EC2 の中の疑似ネットワーク（containerlab）の SNMP を Telegraf が Kafka に流し、Spark が S3 Tables（Iceberg）/ OpenSearch Serverless / Prometheus に書き続け、異常を検知して DynamoDB の一覧に出す（EventBridge にも出す）。トポロジは Neptune で持って Web から編集する | 土台に `terraform/pipeline/lab` → `terraform/pipeline/stream` → `terraform/pipeline/analytics`、並行して `terraform/pipeline/graph` | 約 $1.08/h（約 162 円。lab 0.09 + stream 0.28 + analytics 0.20 + OpenSearch 最大 0.33 + Prometheus 0.03 + graph 0.14） | 作ってある（使う日だけ作る。**AWS 上の apply は未確認**） |
| WORKFLOW（`WORKFLOW=1`。旧 3。AGENT と PIPELINE が要る） | Spark の検知が EventBridge → SQS で届き、エージェントが Neptune / OpenSearch / Prometheus を見て原因調査 → 修復案を Temporal のワークフローで回し、**人が Web の「承認」タブで承認**してから Temporal が lab で直して確かめる。エージェントのツールは AgentCore Gateway（MCP）経由 | AGENT + PIPELINE に `terraform/workflow` | 約 $0.06/h（約 9 円。Temporal のサーバーとワーカーを ECS on Fargate の 1 タスク（ARM、1 vCPU / 2 GB）+ sqs エンドポイント 1 本） | 作ってある（2026-09-17。使う日だけ作る。**AWS 上の apply は未確認**） |

費用は 1 時間立てたときの目安。内訳と前提は README の「1 時間起動したときの試算」にある（単価は土台と AGENT が 2026-09-14、lab・graph・stream が 2026-09-15、analytics が 2026-09-17 に AWS Price List API と料金ページで確認した値）。
何を作るかは `deploy.env` の `AGENT` / `PIPELINE` / `WORKFLOW`（と `CREATE_KB`）で選ぶ（README の「毎日の起動と片付けをスクリプトで打つ」）。機能は互いに独立で、あとから別の機能を `1` にして打ち直せばその機能だけ足される。PIPELINE の一部だけ要らないときは `SKIP_LAB` / `SKIP_STREAM` / `SKIP_ANALYTICS` / `SKIP_GRAPH`。

**時間課金のものは使う日に作って当日中に消す。**土台 + AGENT を 1 か月置くと約 $131（約 19,700 円。`CREATE_KB=1` なら +$263 で、その大半は OpenSearch Serverless の最小 OCU）、PIPELINE の 4 ルートは約 $789（約 118,000 円）。**EC2 を止めてもほとんど減らない。**

---

## フェーズ 1（いまの AGENT + 土台）— 閉域ネットワークで動くチャット

### 何ができる

- ブラウザのチャット画面（Gradio）から、AgentCore Runtime 上のエージェントと日本語で話せる。
- 答える前に **Bedrock Knowledge Base** で手順書の md を引き（ベクトル検索とキーワード検索のハイブリッド、候補 20 件を Rerank で 5 件に絞る）、回答の末尾に参照したファイル名が付く。
- 質問と回答を **Bedrock Guardrails** が判定する。
- 「トポロジ」タブで機器と結線を見る（PIPELINE の graph を作っていなければ静的データ）。
- エージェントは `list_devices` / `neighbors` / `blast_radius` を呼んで、影響範囲を答えられる。

### 決まっていること

| 項目 | 決めたこと |
|---|---|
| 出口 | **インターネットに出口を作らない**。NAT Gateway・EIP・パブリック IP・ロードバランサ・証明書を使わない |
| 入口 | **SSM Session Manager のポートフォワーディング**。EC2 のセキュリティグループに受信ルールを 1 つも置かない |
| モデル | Amazon Nova 2 Lite（`jp.` 推論プロファイル）。埋め込みは Titan Text Embeddings V2、並べ替えは Amazon Rerank 1.0 |
| 画面 | Gradio を EC2（AL2023 / arm64 / t4g.small）の 127.0.0.1:8080 で動かす |
| state | **ローカル**。ルート間は `terraform_remote_state` で読む（1 人が 1 台の PC で打つ前提） |
| 手順書 | `kb-docs/*.md` を S3 に置いて取り込みジョブを流す。**`docs/` は取り込まない** |
| 片付け | 毎日 `ops/down.sh` で消し、翌朝 `ops/up.sh` で作り直す（何を作るかは `deploy.env` の `AGENT` / `PIPELINE` / `WORKFLOW`。既定は土台 + AGENT） |

### 決まっていないこと・入れていないこと

- 会話の永続化（履歴は Runtime のセッションの中だけ。アイドル 5 分か 1 時間、画面の再読み込みで消える）。
- 複数人の同時利用（t4g.small で数人程度まで。Gradio の同時実行は 4）。
- 手順書の自動取り込み（S3 のイベントで取り込みジョブを流す仕組みは入れていない）。
- 日本語向けの形態素解析（kuromoji など）。キーワード検索は既定のアナライザのまま。
- ガードレールの機微情報フィルタ（PII）・拒否トピック・単語フィルタ・コンテキストグラウンディング。PII は IP アドレスやホスト名を伏せて運用の回答を壊すので入れていない。
- OpenSearch Serverless の閉域化（閉域にすると Terraform のインデックス作成が PC から届かなくなる）。
- state の共有（S3 バックエンドとロック）。
- チャット Web のログを CloudWatch Logs に送ること（CloudWatch エージェントを入れていない）。
- **MCP でツールを出すこと。**いまはエージェントが Runtime の中の Python 関数を直接呼んでいる。
  2026-09-16 の「道具を揃える」決定に MCP が入っているが、**どこに置くか**（AgentCore Gateway か、lab の EC2 に立てるか）は未定。
  閉域なので、置き場によっては VPC エンドポイントが要る。

---

## フェーズ 2（いまの PIPELINE）— データパイプライン（lab → stream → analytics）とトポロジ（graph）

**2026-09-17 のユーザー決定: フェーズ 2 = containerlab → Telegraf → Kafka → Spark → S3 Tables のデータパイプライン構築。**
それまで「2 の任意（`WITH_STREAM=1`）」だった stream と、「2B」だった analytics がフェーズ 2 の本体になった。トポロジの Neptune（graph）もこのフェーズのまま。
`deploy.env` に `PIPELINE=1` と書くと 4 ルートを作る。**使う日に作って当日中に消す。**`terraform/base/core` はそのまま使う。

```
lab（EC2 の containerlab: FRR × 6 + snmpd × 4 + ホスト × 4）
  └─ Telegraf（SNMP 10 秒ポーリング + trap + FRR のログの tail）─▶ stream（MSK、トピック metrics / traps / logs）
                                                 ├─▶ analytics の Spark（detect）─▶ DynamoDB の異常一覧（Web とエージェントが読む「いま」）─▶ EventBridge の AnomalyOpened（WORKFLOW の SQS へ）
                                                 ├─▶ MSK Connect（S3 sink）─▶ S3 の stream/（任意。CREATE_S3_SINK=0 で外す）
                                                 ├─▶ analytics（Spark on EMR Serverless）─ 全トピック ─▶ S3 Tables（Iceberg）の snmp_metrics（履歴の正本）
                                                 ├─▶ analytics の Spark ─ traps / logs（ログ）だけ ─▶ OpenSearch Serverless の snmp-logs（SINK_OPENSEARCH。既定で作る）
                                                 └─▶ analytics の Spark ─ metrics だけ ─▶ Amazon Managed Service for Prometheus（SINK_PROMETHEUS。既定で作る）
  4 本目の log + metrics → Splunk は Kafka の sink（MSK Connect）にする予定で後回し（2026-09-17 ユーザー決定「splunkは後回しでもOK」）
graph（Neptune のトポロジ。Web の「トポロジ」タブから編集）
```

作る順は `lab` → `stream` → `analytics`（stream の Kafka を読む）。`graph` は独立なので `ops/up.sh` は base/core の直後に裏で始める。
消す順は `analytics`（Spark のジョブを止めてから）→ `graph` → `stream` → `lab`（`ops/down.sh` がこの順で消す）。

一部だけ要らないとき: `SKIP_LAB=1`（stream は lab の SNMP が要るので `SKIP_STREAM=1` も書く）、`SKIP_STREAM=1`（analytics も作らない。読む Kafka が無い）、`SKIP_ANALYTICS=1`、`SKIP_GRAPH=1`。

### 何ができる

- EC2 1 台の中に **FRR × 6 + snmpd × 4 + ホスト × 4** の疑似ネットワークを作る（lab）。SSM で入って `sudo lab check` / `sudo lab failover` / `sudo lab heal-main` を打つと、主回線の切り替えを再現できる。
- lab の SNMP（10 秒ポーリング + linkUp/linkDown の trap）を Telegraf が MSK に流す（stream）。analytics の Spark（`spark/snmp_sinks.py` の detect。60 秒のマイクロバッチ）が `link_down` を DynamoDB に書き、開いた瞬間に EventBridge へ `AnomalyOpened` を出す。Web の「異常一覧」タブとエージェントの `list_anomalies` がその表を読む。チャットで「今の異常は？」と聞ける。
- 同じトピックを **Spark（EMR Serverless）のストリーミングジョブ**が 60 秒ごとに **S3 Tables の Iceberg テーブル `snmp_metrics`** に追記し続ける（analytics）。Telegraf の JSON をそのまま行にする（`ts` / `topic` / `measurement` / `agent_host` / `host` / `tags_json` / `fields_json` / `ingested_at`）。
- トポロジを **Neptune** に載せ（graph）、Web の「トポロジ」タブからリンクの追加・削除ができる。エージェントの答えにも反映される。

### 決まっていること

| 項目 | 決めたこと |
|---|---|
| lab | 機器のアドレスは **RFC 5737 の文書用アドレス**（`203.0.113.0/24`）。実機の値は写さない。イメージは `terraform/base/ecr`、設定と rpm は S3 の `lab/` に置く |
| stream | MSK は IAM 認証（9098）。異常の「いま」は **DynamoDB**（オンデマンド）。ブローカーとテーブル名は SSM パラメータ経由で lab と Web に渡す |
| S3 sink | 任意。`CREATE_S3_SINK=0` にすると MSK Connect を作らず約 $0.14/h 下がる。履歴の正本は analytics の S3 Tables なので、外してもデータは残る |
| analytics の実体 | **EMR Serverless**（Glue ではない。ジョブが無ければ 0、アプリケーションは器だけ）。ARM64、release `emr-7.13.0`（Spark 3.5.6。S3 Tables は 7.5.0 以上。2026-09-17 確認） |
| analytics の格納先 | **Kafka から 4 つに分ける**（2026-09-17 ユーザー決定）。Spark が 3 本: iceberg = 全トピック → S3 Tables（既定）、opensearch = ログ（`traps` と `logs`。`logs` は FRR の `log file` を EC2 に bind して Telegraf の `inputs.tail` で読む。FRR のコンテナに syslogd が無いので syslog にはしていない。2026-09-18）→ OpenSearch Serverless の TIMESERIES コレクション `<prefix>-logs`（VPC エンドポイント経由だけ）、prometheus = メトリクス（`metrics`）→ Amazon Managed Service for Prometheus `<prefix>-metrics`（remote write を SigV4 で）。格納先ごとに別のストリーミングクエリと checkpoint。4 本目の Splunk（log + metrics）は Spark を通さず MSK Connect の sink にする予定で後回し。`deploy.env` の `SINK_S3` / `SINK_OPENSEARCH` / `SINK_PROMETHEUS`（`1` / `0`。既定は 3 つとも `1`。2026-09-17 夜のユーザー決定「s3, open search, prometheus のデプロイをそれぞれ 1 と 0 でオンオフ」。`0` はリソースごと作らない。カンマ区切りの `SINKS` は古い書き方で読み替える。2026-09-17 ユーザー決定「SINKS に opensearch と prometheus を入れる。KB のコレクションと共有できなければこちらを優先」）→ `terraform/pipeline/analytics` の `var.sinks` |
| analytics のテーブル | **S3 Tables（Iceberg）**。テーブルバケット `<prefix>-tables`、namespace `netops`、テーブル `snmp_metrics`（namespace とテーブル名はアンダースコアだけ。ハイフン不可）。テーブルは Terraform で作る（destroy でバケットまで消せるように） |
| analytics のジョブ | Structured Streaming、`--mode STREAMING`、60 秒トリガー、driver 1 + executor 1 の 2 vCPU。Kafka / MSK IAM / S3 Tables カタログの jar 6 本は `ops/up.sh` が Maven Central から取って `s3://<バケット>/analytics/jars/` に置く。起動は `ops/up.sh` の start-job-run（動いていれば起こさない） |
| analytics のネットワーク | NAT が無いので S3 Tables の API は **interface エンドポイント `s3tables`（2 AZ）**、データ本体は main の S3 ゲートウェイエンドポイント。EMR の SG は inbound を自分自身からだけにする（0.0.0.0/0 の inbound があると EMR Serverless が拒否する） |
| graph | Neptune は Gremlin を boto3 の `neptunedata` から IAM 認証で呼ぶ。`ops/up.sh` は最後に静的トポロジを投入して Web を再起動する |
| 費用 | 約 $0.69/h（約 104 円）: lab 0.09（t4g.large + gp3）、stream 0.29（MSK 2 ブローカー + MSK Connect。sink 無しで 0.15）、analytics 0.17（2 vCPU / 8 GB のジョブ ≒ 0.15 + s3tables EP 2 AZ 0.028）、graph 0.14（db.t4g.medium）。1 か月置くと約 $504（約 75,600 円） |
| 権限 | 組織の SCP / IAM で t4g.large・MSK・Neptune・EMR Serverless・S3 Tables の作成が止められていることがある（README の「前提」→「AWS 側」） |

### 決まっていないこと（着手前・初回の apply で確かめる）

- **AWS 上の apply は未確認**（4 ルートとも。手元の validate と模擬テストまで）。
- analytics は特に未検証が多い（README「未確認のもの」にも同じ一覧）:
  - EMR Serverless 7.13.0 に S3 Tables カタログの jar が同梱されているか（同梱なら `s3-tables-catalog-for-iceberg-runtime` を足すと衝突する可能性）。
  - Kafka の jar 6 本の組み合わせで Structured Streaming の Kafka ソースが動くか。
  - 閉域から S3 Tables に届くか（interface エンドポイント + S3 ゲートウェイエンドポイントの `*--table-s3` の許可）。Lake Formation の設定が要るか。
  - EMR Serverless / S3 Tables が組織の SCP / IAM で止められていないか。
- lab の配線と Neptune のトポロジの同期（Neptune は手で編集するもので、lab を変えても追随しない）。
- **BGP の状態の監視。**拾うのはインタフェースの up/down（ポーリングと trap）だけで、隣接や経路の変化は異常にならない。
- Grafana などの可視化（**保留**。2026-09-17 ユーザー決定「OpenSearchとPrometheusはgrafanaで可視化したいけどそこは後回しでOK」）。異常一覧は DynamoDB の表をそのまま出す。OpenSearch Serverless / Prometheus の中身は VPC の中からしか届かない。
- 異常の重み付けや相関（同時に落ちた複数のリンクを 1 件にまとめる、など）。閾値判定は 2026-09-17 に stream の detector Lambda から Spark（`spark/snmp_sinks.py` の detect）に寄せた。
- **（失効）OpenSearch の全文検索は入れない（2026-09-17 朝のユーザー決定「いれなくてOK」）。同日午後の「spark から s3 iceburg, splunk, open search + prometheus この3パターンに格納したい」「Kafka から 4 つに分ける」で置き換わり、ログ（`traps`）だけを Spark から OpenSearch Serverless の TIMESERIES コレクションに入れる形で実装した（`SINKS` に `opensearch`。同日夕方のユーザー決定「SINKS に opensearch と prometheus を入れる。KB コレクションと共有できなければこちらを優先して」で既定に入れた）。**下の理由のうち「別に立てると OCU がもう 1 セット出る可能性」はそのまま残っている（確認できていない）。以下は当時の判断:
  Spark の後に Kafka のメッセージを OpenSearch にも入れる案（2026-09-16 の見直しの矢印にある「+ OpenSearch」）は、次の理由で見送った。ナレッジベース（`CREATE_KB=1`）のコレクションは `VECTORSEARCH` 型で、ID 指定の書き込み・`_update` は `SEARCH` 型だけ、`TIMESERIES` 型は upsert ができない
  （[Supported operations](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-genref.html)、
  [Choosing a collection type](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-overview.html)。2026-09-16 に確認）ので相乗りはできない見込み。
  **別に立てると最小 OCU（月 ≒ $240）がもう 1 セット出る可能性がある**（コレクショングループに入らない Classic のコレクションは KMS キーが同じなら OCU を共有できるが、型が違っても共有されるかは未確認）。
  異常の置き場は DynamoDB のまま（履歴の正本は S3 Tables）。
- S3 Tables の保守（compaction は S3 Tables が自動で行う。書き込みの粒度・パーティションの切り方は未定）。
- S3 sink（MSK Connect）を既定で作るか。Spark が Kafka を直接読むので役割が重なり、外せば $0.142/MCU 時間（月 ≒ $104）が浮く。いまの既定は作る（`CREATE_S3_SINK=1`）。

---

## フェーズ 3（いまの WORKFLOW）— Temporal でエージェントが調査し、人が承認して直す

**2026-09-17 に着手した**（ユーザー決定「ECSで着手して」「agent gateway の MCP も一緒に」）。`terraform/workflow`（ECS on Fargate のタスク + DynamoDB の修復案テーブル + AgentCore Gateway（MCP）と tools Lambda）、`workflow/`（ワーカー）、`tools/`（Gateway のツール）、`agent/mcp_client.py` / `agent/proposals.py`、Web の「承認」タブ、`WORKFLOW=1 ops/up.sh`、`tests/test_workflow.py`。手順は README の「workflow（WORKFLOW）」。**AWS 上の apply は未確認。**
**2026-09-17 夕方のユーザー決定**「Spark が異常を検知したら EventBridge にイベント発行して、それを検知した agent が Neptune や S3、OpenSearch、Prometheus を見に行って原因分析 → 修復の提案 → 人間の承認 → Temporal で実行」
「Step Functions じゃなくて Temporal（EKS）だった。ただいまの段階では EKS ではなく ECS で OK」で、入口を DynamoDB の polling から **EventBridge → SQS** に変え（`terraform/workflow/events.tf`）、
エージェントに OpenSearch / Prometheus / S3 Tables を見るツール（`agent/evidence.py`）を足した。stream の detector Lambda は消し、検知は Spark（`spark/snmp_sinks.py` の detect）に寄せた。
**2026-09-16 のユーザー決定**「原因調査は AI エージェントがする。人がするのは修復を実行する承認だけ」、
**2026-09-17 のユーザー決定**「フェーズ 3 は Temporal でエージェントが原因調査して人間が承認するまで」で、
旧 5A（調べる）と旧 5B（人が承認して直す）を 1 つのフェーズにまとめた。

### 決まっていること

- **異常の検知 → 原因の調査 → 修復案の提示 → 人の承認 → 修復 → 検証**を、1 本のワークフローとして **Temporal** で回す。
- **入口は EventBridge → SQS**（2026-09-17 ユーザー決定）。Spark の driver が既定のバスに `netops.spark` / `AnomalyOpened` を出し、ルールが SQS `<prefix>-anomalies` に流す。ワーカーの starter が long polling で受けて `investigate-<anomaly_id>` を起こす。キューが無ければ（events.tf を出していなければ）60 秒ごとの DynamoDB polling に戻る。
- **調査の材料は 4 つ。**Neptune（`neighbors` / `blast_radius`）、OpenSearch Serverless の logs（`search_logs`）、Prometheus（`query_metrics`）、S3 Tables の履歴（`query_history`。Athena をまだ出していないので案内だけ）。実体は `agent/evidence.py` で、Runtime と tools Lambda の両方が同じものを呼ぶ。
- 調査の中身はエージェントが行う。**承認までは何も直さない。**
- **承認は人が行う（HITL）。**承認を外すこと（Zero-Touch）は **pending**（2026-09-16 決定）。
- 承認の後の段は 3 つ: AwaitApproval（承認待ち。却下と時間切れで終わる）→ Apply（直す）→ Verify（直ったか確かめる。数回まで）。**ロールバックは作らない。**
- Temporal の置き場は **ECS on Fargate**（2026-09-16 ユーザー決定「一旦 ECS にしようか」、2026-09-17「ただいまの段階では EKS ではなく ECS で OK」）。
  - 最初は **EKS** だった（AgentCore と連携させたいため）。ところが AgentCore は API（`InvokeAgentRuntime`）で呼ぶので、**呼ぶ側が EKS でも ECS でも変わらない。**
  - EKS にすると、クラスタとエンドポイントで**月 ≒ $153〜$173 が上乗せ**になる。ECS ならどちらも 0 なので、ECS に変えた。
  - 「一旦」なので、Kubernetes の形で試す必要が出たら EKS に戻す。そのときの費用は下の「EKS に戻すとき」にある。
- **チャットはワークフローに載せない。**同期のチャットを載せると 1 往復ごとに実行が要る。
- **エージェントのツールは AgentCore Gateway（MCP、IAM 認証）に出す**（2026-09-17 ユーザー要望）。ツール定義は `tools/tools.json`（8 つ）、実体は tools Lambda（`tools/handler.py`。2026-09-17 から VPC の中に置き、Neptune のトポロジ・DynamoDB の異常一覧・OpenSearch のログ・Prometheus のメトリクスに届く）。Runtime は SSM の `gateway-url` があれば `tools/list` と `tools/call` を Gateway に投げ、無ければ（届かなければ）コンテナの中の同名の関数で答える。Gateway は `create_gateway=false` で外せる。
- **Web とワーカーは Temporal でつながない。**修復案テーブル（DynamoDB）の `status` を Web が書き、ワーカーがポーリングで読む。画面側に Temporal の SDK を入れず、Temporal を閉域の外に出さないため。

### 着手できる時期

**AGENT と、PIPELINE の stream と analytics が動いていること**（Spark が異常を DynamoDB と EventBridge に出すこと）が前提（2026-09-17 に検知を Spark に寄せたので、analytics も要る）。
調査の材料が増えるほど質は上がるが、閾値で出た異常だけでもワークフローは回せる。

### 入れるときに効く費用と前提

| 見るもの | いま分かっていること |
|---|---|
| クラスタ | **料金はかからない**（ECS はクラスタ自体に課金しない） |
| タスク | Fargate の東京単価は ARM $0.04045/vCPU 時間 + $0.00442/GB 時間、x86 $0.05056/vCPU 時間 + $0.00553/GB 時間（AWS Price List の公開 JSON、2026-09-16 確認）。**サーバーとワーカーで 1 vCPU / 2 GB を ARM で動かすと ≒ $0.05/h（1 日 4 時間で ≒ $0.20、置きっぱなしで月 ≒ $36）** |
| エンドポイント | **足すものは 0 個。**Fargate のタスクは ECS 用のエンドポイント（`ecs` / `ecs-agent` / `ecs-telemetry`）を要らない（ECS の文書、2026-09-16 確認）。要るのは ECR（イメージ）と CloudWatch Logs（`awslogs`）で、**どちらも main にある**。AgentCore は main の `bedrock-agentcore` で呼ぶ |
| Temporal のデータ | 保存先に PostgreSQL / MySQL / SQLite を使え、**Elasticsearch は必須ではない**（Temporal の文書、2026-09-16 確認）。**PoC の規模なら SQLite で足りるので、DB を別に立てずに始められる。**ただしタスクを止めると SQLite ごと消える（毎日 down する運用なので困りにくい）。実行件数が増えたら OpenSearch か Elasticsearch が推奨されている |

- **VPC は base/core を使い回す。**`terraform/pipeline/stream` / `terraform/pipeline/graph` / `terraform/pipeline/analytics` と同じく `data.terraform_remote_state.main.outputs.vpc_id` で入れる。新しい VPC を作ると ECR や Logs のエンドポイントを一から並べることになる。
- **サーバーとワーカーは、まず 1 つのタスクに同居させる。**同じタスクなら `localhost` で繋がり、ロードバランサもサービス検出も要らない。
  別々のタスクに分けて **Service Connect を使うなら `ecs-agent` エンドポイントが要る**（Envoy の管理がこれを使う。同じ ECS の文書）。
- イメージは VPC 内の ECR から引く（AGENT の Runtime と同じ）。`temporalio/temporal` 1.9.1 は arm64 を持つ（マニフェスト、2026-09-17 確認）ので、`ops/up.sh` が ECR にミラーする。
- 毎日 `ops/down.sh` で消す運用なので、**このタスクも「使う日に作って当日中に消す」側に入れる。**

### EKS に戻すとき

- **クラスタ本体が $0.10/h（月 ≒ $73）**かかる（東京・2026-09-16 確認）。止めない限り毎時かかる。
- **エンドポイントが 4〜5 個足りない。**AWS の文書（閉域クラスタの作り方、2026-09-16 確認）と base/core を突き合わせると、`ec2` / `sts` / `eks` と、`oidc-eks`（IRSA）か `eks-auth`（Pod Identity）のどちらかが無い（ロードバランサを使うなら `elasticloadbalancing` も）。
- **EKS はサブネットを別々の AZ に 2 つ以上要求する**ので、エンドポイントも 2 AZ ぶんで見る。**月 ≒ $80〜$100。**合わせて**月 ≒ $153〜$173 の上乗せ**になる。
- `sts` は `terraform/pipeline/stream` が持っている。**同じ VPC に同じサービスのエンドポイントを 2 つ作らない**（二重に課金される）。
- 閉域を外す（NAT を置く）話にはしない。**閉域という前提そのものを崩す。**

### 2026-09-17 の実装で決めたこと（PMO 判断。変えるなら issue で）

- Temporal のデータはタスク内の SQLite（`temporal server start-dev --db-filename`）。タスクが入れ替わると実行履歴は消える。毎日消す運用なので外に出さない。
- Temporal の Web UI（8233）は Web の EC2 を踏み台にした SSM のポートフォワーディング（`AWS-StartPortForwardingSessionToRemoteHost`）で PC から開く。認証は無い。
- ワークフローは**異常 1 件に 1 実行**（`investigate-<anomaly_id>`。同じ異常が open のうちは起こし直さない）。
- エージェントは Temporal のアクティビティから AgentCore Runtime を `InvokeAgentRuntime` で呼び、JSON（cause / action / command / reason）で答えさせる。
- 調査に読ませるのは DynamoDB の異常（1 件）と、Runtime のツールで引けるトポロジ・異常一覧・ナレッジベース。S3 Tables の `snmp_metrics` はまだ読ませていない。
- 承認は Web の「承認」タブ。承認待ちは 120 分（`approval_timeout_minutes`）、Verify は 30 秒おきに 6 回（`verify_attempts`）。
- 打てるのは lab の EC2 への `sudo lab heal-main` / `sudo lab check` だけ（SSM Run Command の `AWS-RunShellScript`。IAM でその 1 台と文書に絞る）。**実機には何も打たない。**

### 決まっていないこと

- **AWS 上の apply は未確認。**Gateway（MCP）に VPC モードの Runtime と Fargate のタスクから届くか、`start-dev` が Fargate で上がるか、`temporalio` 1.33.0 の SDK と Temporal 1.9.1 の組み合わせ、Nova 2 Lite が求めた JSON で答えるか、組織の SCP / IAM が ECS / Gateway / Lambda を止めていないか（README の「確認できていないこと」）。
- Gateway のツールは静的トポロジしか見ない（Neptune は Runtime の中のツールだけ）。Gateway があるときのチャットのトポロジを Neptune に戻すか。
- 調査に S3 Tables の `snmp_metrics`（履歴）を読ませるか。
- 実機に対して何をどこまで打たせるか。**lab 以外に打つ話は何も決まっていない。**
- **Zero-Touch にする時期（pending）。**

---

## 更新するとき

- 費用の数字は README の「1 時間起動したときの試算」が正本。こちらは要約なので、直すときは両方直す。
- **単価を書くときは確認した日を併記する。**3 か月以上前のものは引き直す。
- 実アカウント ID・ARN・CIDR・ホスト名・顧客名はここにも書かない。
