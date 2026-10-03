# 勉強会メモ

← [README](../README.md)

勉強会で話す順に並べてある。1〜6 はデータの置き場、7〜10 はコンテナイメージ、11〜14 は Neptune の基礎、15 は MSK とクライアントのつなぎ。

## データの置き場

この PoC で「何を・どこに・なぜ」置いているかのまとめ。2026-09-24 に DynamoDB をやめ、「いま」は Neptune、履歴と証跡は S3 Tables に寄せた。2026-10-02 に検知を Spark から Grafana と Splunk へ移し、異常の頂点と `anomaly_events` をやめた（5 に経緯）。**障害の履歴の置き場はまだ決めていない。**

### 1. 置き場は 2 つ（+ 見るための写し 2 つ）

| データ | 置き場 | 書く | 読む |
|---|---|---|---|
| 生データの履歴（metrics / gnmi / traps / logs の全部） | S3 Tables（Iceberg）`snmp_metrics` | Spark の `iceberg` | まだ読む側が無い（エージェントの `query_history` は Athena 未配備のため案内だけ返す） |
| 異常の「いま」 | 置かない。機器・回線・層の `status`（下の 2 行）と、Grafana / Splunk のアラートの状態で見る | — | — |
| 障害の履歴（開いた・閉じた） | **未定**。いまは Grafana / Splunk のアラートの履歴で見る（どちらもタスクと一緒に消える） | — | — |
| 修復案の「いま」（pending → approved …） | Neptune の頂点 `proposal`（id は `<anomaly_id>#<first_seen>`） | worker、Web の承認タブ | worker、Web の承認タブ、エージェントの `list_proposals` |
| 修復案の証跡（作成・承認・却下・適用・確認） | S3 Tables `proposal_events` | worker（PyIceberg） | まだ読む側が無い（証跡） |
| トポロジと、機器・回線の状態（物理層） | Neptune の頂点 `device` / `interface`、辺 `link` | 投入スクリプト、Lambda `graph-status`（アラートの `link_down` / `trap`） | エージェントの `neighbors` / `blast_radius` / `topology_graph`、Web の「トポロジ」タブ |
| IP 層・EVPN/BGP 層と、その状態 | Neptune の頂点 `ip_interface` / `isis_adjacency` / `bgp_session` / `evpn_instance` / `ethernet_segment`（[Neptune の層](#neptune-の層)） | 投入スクリプト、Lambda `graph-status`（アラートの `bgp_down` / `isis_down`） | エージェントの `layers`、Web の「トポロジ」タブの層の表 |

ほかに、検索用のログ（OpenSearch `snmp-logs`）とグラフ用のメトリクス（Prometheus）がある。この 2 つは見るための写しで、正本ではない。`SINK_SPLUNK=1` なら全トピックを Splunk（HTTP Event Collector）にも送る。これも写しで、Splunk は analytics の ECS に立てる（index はタスクと一緒に消える。docs/pipeline.md）。

検知はこの写しの上で動く。Grafana のアラートルールは Prometheus を、Splunk の保存済みサーチは Splunk の index を見て、発火と解消を SNS のトピック `<prefix>-alerts` に出す（[pipeline.md](pipeline.md) の「アラート」）。

```mermaid
flowchart LR
  MSK["MSK<br/>metrics / gnmi / traps / logs"] --> SPARK["Spark<br/>EMR Serverless"]
  SPARK -->|"全部 append"| ICE["S3 Tables<br/>snmp_metrics"]
  SPARK --> PROM["Prometheus"] --> GRAF["Grafana<br/>アラートルール"]
  SPARK -.->|"SINK_SPLUNK=1"| SPL["Splunk<br/>保存済みサーチ"]
  GRAF -->|"firing / resolved"| SNS["SNS<br/>prefix-alerts"]
  SPL -.-> SNS
  SNS --> GL["Lambda graph-status"] -->|"status"| NEP["Neptune<br/>トポロジ + proposal"]
  SNS --> SQS["SQS"] --> WK["Temporal worker"]
  WK <-->|"proposal"| NEP
  WK -->|"1 段ごとに 1 行"| PEV["S3 Tables<br/>proposal_events"]
  WEB["Web"] <-->|"トポロジ / 承認"| NEP
```

### 2. 1 回の障害で何が書かれるか

`sudo lab fail-main` でアクセス側 Leaf の fabric（`dc1-leaf-01 ethernet-1/1`）を落としたときの流れ（既定の構成。送り手は Grafana）。

1. **ポーリング（10 秒ごと）:** Telegraf が `ifOperStatus=down` を拾い、MSK の `metrics` に出す。
2. **Spark:** `iceberg` が行をそのまま `snmp_metrics` に追記し、`prometheus` が同じ値を Prometheus に書く。どちらも up か down かを判断しない。
3. **Grafana:** ルール `link_down` が 30 秒ごとに `ifOperStatus` を見て、down の IF を `firing` として SNS のトピック `<prefix>-alerts` に出す。異常の id は `dc1-leaf-01#link_down#ethernet-1/1`。ここでは頂点も行も書かない。
4. **Lambda `graph-status`:** SNS から受け取り、Neptune の IF の頂点の `status` を `DOWN` にする。`SINK_SPLUNK=1` なら、同じ回線の IS-IS の隣接が Splunk の `isis_down`（gNMI）で届き、頂点 `dc1-leaf-01#isis#ethernet-1/1.0` も `DOWN` になる。
5. **worker:** SQS から受け取り、異常ごとに Temporal のワークフロー `investigate-<anomaly_id>` を起こす。
6. **修復案:** worker が Neptune に `proposal` の頂点を `pending` で置き（id は `<anomaly_id>#<first_seen>`。`first_seen` はアラートの `starts_at`）、`proposal_events` に `created` を足す。
   Web で承認すると頂点が `approved` になり、worker がそれを拾って `approved` の行を足す。`heal-main` を打つと `applied`、解消の通知が届くと `verified` の行が続く。
7. **回復:** Grafana が `resolved` を出す。Lambda が IF の `status` を `UP` に戻し、worker は走っているワークフローにシグナル `resolved` を送る。

障害が開いた・閉じたこと自体は、どこにも行として残らない（置き場は未定）。残るのは修復の流れで、`proposal_events` の `proposal_id`（`<anomaly_id>#<first_seen>`）で 1 回の発生をまとめて追える。

### 3. なぜこの分け方か

| 置き場 | 向いていること | この PoC で使っている機能 |
|---|---|---|
| Neptune | つながりをたどる。頂点 1 つの「いま」を書き換える | 隣接と影響範囲（`blast_radius`）の探索。`has('status','pending').property(single, …)` を 1 本の Gremlin にした条件付き更新（人が決めた status を上書きしない） |
| S3 Tables（Iceberg） | 大量の追記と、後からの集計。安い | Spark の append、worker の PyIceberg の append |

- **「いま」と証跡を分ける:** 頂点は書き換わるので、それだけでは「いつ誰が承認したか」が後から追えない。変わるたびに S3 Tables に 1 行足し、上書きしない。
- **Web と Iceberg:** Web は Iceberg を読まない。Athena もまだ配備していないので、S3 Tables は書くだけの状態。

### 4. 気を付けること

- **Neptune が止まっても検知は止まらない:** 見つけるのは Grafana と Splunk で、Neptune を読まない。止まるのは `status` の更新（Lambda）、修復案の読み書き（worker と Web の承認タブ）、トポロジのツール。そのあいだのアラートは SQS に残り、worker が 5 回受け取っても処理できなければ DLQ へ行く。
- **承認・却下を書けるのはコードの上だけ:** Neptune の IAM は頂点ごとに絞れず、Runtime と Web のロールはどちらも書ける。チャットから決めさせないのは、`decide` をツールに出していないから（HITL の線はコードで引いている）。
- **証跡は二重に入ることがある:** Spark の読み直しやアクティビティの再試行で同じ行がもう一度入る。集計するときは `event_id` で重複を落とす。
- **worker が止まっているあいだの承認:** Web で承認した事実は頂点にあるが、`proposal_events` の `approved` の行は worker が拾ったときに書く。worker が起きないまま時間が過ぎると、証跡に承認が残らない。Temporal の履歴はタスクと一緒に消える。
- **`ops/down.sh` は証跡も消す:** テーブルバケットごと消えるので、`proposal_events` も残らない。残したいときは消す前に書き出す。
- **SINK_S3=0 でもテーブルバケットはできる:** 証跡の置き場なのでいつも作る（生データの `snmp_metrics` だけが SINK_S3 に従う）。

### 5. 経緯: DynamoDB をやめた（2026-09-24）

それまでは異常の「いま」を DynamoDB `<prefix>-anomalies`、修復案を `<prefix>-proposals` に置いていた。次の穴があった。

- 「いつ異常が開いて、いつ閉じたか」がどこにも無かった。anomalies は最新の 1 行しか持たず、開き直すと前の発生が消えた。
- 修復案の承認・却下が DynamoDB の 1 行にしか無く、上書きで過去が追えなかった。
- 置き場が 3 つ（S3 Tables、DynamoDB、Neptune）あり、同じ異常が DynamoDB と Neptune の両方にあった。

そこで次のように寄せた。

1. **異常の「いま」:** Neptune の頂点 `anomaly`。`detect` が Neptune に書く。
2. **障害の履歴:** `detect` が S3 Tables の `anomaly_events` に追記する。
3. **修復案:** Neptune の頂点 `proposal`。条件付き書き込みは Gremlin の `fold().coalesce(unfold(), addV(...))` と `has('status','pending')` で置き換えた。
4. **修復案の証跡:** worker が S3 Tables の `proposal_events` に 1 段ごとに追記する。

**2026-10-02: 異常の頂点と `anomaly_events` をやめた。** 検知とアラートの相関を Grafana と Splunk に寄せ、Spark の `detect`（上の 1 と 2、EventBridge への発火）を消した。Neptune にはネットワークトポロジの情報だけを置く方針で、機器・回線・層の動的な `status` はトポロジに含める。修復案（上の 3 と 4）はこの方針から外れるが、障害の履歴と一緒に置き場を決めるまでそのまま動かしている。

| | 以前（DynamoDB あり） | 2026-09-24 から | 2026-10-02 から |
|---|---|---|---|
| 置き場の数 | 3 つ（+ 写し 2 つ） | 2 つ（+ 写し 2 つ） | 同じ |
| 異常の「いま」 | DynamoDB | Neptune の頂点 `anomaly` | 置かない（`status` とアラートの状態で見る） |
| 障害の履歴 | 無い | `anomaly_events` | 未定（Grafana / Splunk のアラートの履歴） |
| 修復案の証跡 | 無い（1 行を上書き） | `proposal_events` | 同じ |
| 費用 | DynamoDB は放置中ほぼ $0 | ほとんど変わらない（S3 Tables の小さな追記だけ） | 同じ |
| Neptune が止まったとき | トポロジだけ見えない | 検知の書き込みも異常一覧も止まる | `status` の更新と修復案が止まる（検知は止まらない） |

### 6. コードの入口

| 見たいもの | ファイル |
|---|---|
| 検知（アラートのルール、保存済みサーチ、SNS への publish） | [grafana/provisioning/alerting/netops.yaml](../grafana/provisioning/alerting/netops.yaml)、[splunk/netops_alerts/](../splunk/netops_alerts/) |
| アラートのトピックと、本文の読み方 | [terraform/base/core/alerts.tf](../terraform/base/core/alerts.tf)、[workflow/rules.py](../workflow/rules.py) の `alerts_from_message` |
| 証跡のテーブル（`proposal_events`） | [terraform/pipeline/analytics/tables.tf](../terraform/pipeline/analytics/tables.tf) |
| 修復案の頂点と証跡（Terraform 側の説明と IAM） | [terraform/workflow/proposals.tf](../terraform/workflow/proposals.tf)、[terraform/workflow/iam.tf](../terraform/workflow/iam.tf) |
| worker の読み書き（Gremlin と PyIceberg） | [workflow/awsio.py](../workflow/awsio.py) |
| 証跡の 1 行の形 | [workflow/rules.py](../workflow/rules.py) の `proposal_event` |
| Web とエージェントの読み書き | [agent/graph.py](../agent/graph.py) の `list_records` / `get_record` / `update_record` |
| Neptune の機器・回線・層の status | [graph/status_handler.py](../graph/status_handler.py) |

## コンテナイメージ

ECR に置くイメージが「どこで・何をして」いるかのまとめ。2026-09-25 時点のコードで確かめた内容に、2026-09-28 に足した `telegraf` / `grafana` / `splunk` を加えた。

### 7. イメージと動く場所

| イメージ（`<prefix>-…`） | 元 | 動く場所 | 役目 |
|---|---|---|---|
| `agent` | [agent/](../agent/)（自前ビルド） | AgentCore Runtime | チャットの本体。Bedrock のモデルを呼び、Neptune のトポロジ、OpenSearch / Prometheus の証拠を集めて答え、承認待ちの修復案を作る |
| `lab-srlinux` | `ghcr.io/nokia/srlinux`（ミラー。約 1 GB） | lab の EC2（containerlab） | スイッチ（Nokia SR Linux、`ixr-d2l`）。`lab/splab.clab.yml.in` の 6 台（Leaf-SW 2 / Spine 2 / Leaf 2）がこれで立ち、`lab/srlinux/<機器>.cli` で IS-IS・iBGP EVPN・VXLAN・LAG・SNMP の trap・syslog が入る。SNMP エージェントと gNMI は機器に内蔵（containerlab が v2c の `public` と `57400/tcp` を入れる）。監視される「機器」そのもので、**trap の宛先（`system snmp trap-group`）を書いた機器が監視対象**（いまは 6 台全部） |
| `lab-multitool` | `ghcr.io/srl-labs/network-multitool`（ミラー） | lab の EC2（containerlab） | ping / traceroute / tcpdump 入りの VM 役（`wan-upstream-01` / `dc1-host-01`）。Leaf の組へ bond0（LACP）で 2 本つなぎ、疎通確認と障害の再現に使う |
| `temporal` | `temporalio/temporal`（ミラー） | ECS Fargate（WORKFLOW=1） | Temporal のサーバー。`server start-dev` で 1 コンテナで動く。Fargate はプライベート網から Docker Hub を引けないので ECR にミラーする |
| `worker` | [workflow/](../workflow/)（自前ビルド） | ECS Fargate（WORKFLOW=1） | Temporal のワーカー。SQS のアラートを拾い、Runtime に修復案を作らせ、Neptune と S3 Tables に記録し、承認後に SSM で lab の機器へ流して検証する。同じタスクの `temporal` に `localhost:7233` でつなぐ |
| `telegraf` | [telegraf/](../telegraf/)（公式の `telegraf:1.40.0` に設定のテンプレートと `tg` を足す） | ECS Fargate（stream。内部 NLB の後ろ） | 機器の SNMP のポーリング・gNMI の購読・trap・syslog を受けて MSK に書く。2026-09-28 まで lab とは別の EC2 で systemd の下に rpm で動いていた |
| `grafana` | [grafana/](../grafana/)（公式の Grafana OSS にデータソースの plugin と provisioning を焼き込む） | ECS Fargate（analytics。`GRAFANA=1`） | Prometheus（AMP）と OpenSearch Serverless を SigV4 で読んで見せる。アラートルール（`link_down`）を評価して SNS へ出す |
| `splunk` | [splunk/](../splunk/)（公式の `splunk/splunk:10.4.3` に検知のアプリ `netops_alerts` と入口のスクリプトを足す。amd64 だけ、約 2〜3 GB） | ECS Fargate x86（analytics。`SINK_SPLUNK=1` のとき） | Splunk Enterprise（試用ライセンス）。Spark が HEC に全トピックを送り、保存済みサーチが trap と gNMI から異常を見つけて SNS へ出す |

分けて見ると、監視される側が `lab-srlinux` / `lab-multitool`、集める側が `telegraf`、考える側が `agent`、実行する側が `temporal` / `worker`、見る側と見つける側が `grafana` / `splunk`。

### 8. arm64 に揃える（Splunk だけ x86）

- **AgentCore Runtime は linux/arm64 のイメージしか動かせない。** x86_64 でビルドしたイメージは起動しない。`agent/Dockerfile` の冒頭にも書いてある。
- ほかも arm64 に揃えてある: ECS Fargate は `cpu_architecture = "ARM64"`（[terraform/workflow/ecs.tf](../terraform/workflow/ecs.tf)、stream の Telegraf、analytics の Grafana）、lab / Web の EC2 は `t4g`（Graviton）だけを受け付ける。
- 例外は `splunk`。公式イメージが amd64 しか無いので、そのタスクだけ `X86_64` にし、`docker buildx build --platform linux/amd64` で作る（公式イメージに COPY するだけなので、arm64 の PC でもエミュレーション無しで作れる）。
- だから `splunk` のほかは、PC 側の `docker buildx build` は必ず `--platform linux/arm64`、`docker pull` も `--platform linux/arm64`。Mac（Apple Silicon）はそのまま、WSL2 は `binfmt` を入れる（[setup.md](setup.md)）。
- ミラーの push で「only the available single-platform image was pushed」と出るのは、arm64 だけ push したという意味で問題ない。

### 9. タグ

- ECR のリポジトリは `IMMUTABLE`（[terraform/base/ecr/main.tf](../terraform/base/ecr/main.tf)）。同じタグへの上書きはできないので、コードを変えたらタグを進める。
- 自前ビルドの `agent` / `worker` は `IMAGE_TAG`（既定 `v1`）。ミラーは上流の版そのまま（`ops/up.sh` の `SRLINUX_TAG` / `MULTITOOL_TAG` / `TEMPORAL_TAG`）。
- `telegraf` / `grafana` / `splunk` は `<版>-<ディレクトリの中身の sha256 の先頭 12 文字>`（`ops/up.sh` の `dir_tag`）。中身を変えれば自動でタグが変わるので、`IMAGE_TAG` を上げなくてよい。
- `ops/up.sh` は ECR にそのタグが無いときだけビルドして push する（手順 2）。

### 10. コードの入口

| 見たいもの | ファイル |
|---|---|
| ビルドと push、タグの定数 | [ops/up.sh](../ops/up.sh) の手順 2 |
| リポジトリの定義 | [terraform/base/ecr/main.tf](../terraform/base/ecr/main.tf) |
| lab のどの機器がどのイメージか | [lab/splab.clab.yml.in](../lab/splab.clab.yml.in)（[lab/gen_lab.py](../lab/gen_lab.py) が作る） |
| Runtime がどのイメージを指すか | [terraform/agent/variables.tf](../terraform/agent/variables.tf) の `agent_image_tag` |
| Fargate のタスク定義（temporal と worker の 2 コンテナ） | [terraform/workflow/ecs.tf](../terraform/workflow/ecs.tf) |
| Telegraf / Grafana / Splunk のタスク定義 | [terraform/pipeline/stream/telegraf.tf](../terraform/pipeline/stream/telegraf.tf)、[terraform/pipeline/analytics/grafana.tf](../terraform/pipeline/analytics/grafana.tf)、[terraform/pipeline/analytics/splunk.tf](../terraform/pipeline/analytics/splunk.tf) |

## Neptune の層

設計の「物理層・IP 層・EVPN/BGP 層のそれぞれの接続情報と、各層を紐づける ID」を Neptune でどう持つか。2026-09-26 に lab を Spine-Leaf（EVPN-VXLAN）にしたときに入れた。元データは `lab/lab_topology.py` が `lab/srlinux/*.cli` から作る（`agent/data/layers.json` はその写し）。

| 層 | 頂点（label） | id | 下の層を指す property | 同じ層の辺 |
|---|---|---|---|---|
| 物理 | `device` / `interface` | `dc1-leaf-01` / `dc1-leaf-01#ethernet-1/1` | — | `link`（機器 ⇄ 機器。`a_if` / `b_if`、kind は fabric / lag / l2 / mgmt） |
| IP | `ip_interface` | `dc1-leaf-01#ethernet-1/1.0` | `interface_id` → `interface`（辺 `over`。ループバック `system0.0` は物理層に無いので空） | — |
| IP | `isis_adjacency` | `dc1-leaf-01#isis#ethernet-1/1.0` | `ip_interface_id` / `interface_id`（辺 `over`） | `peer`（両端の隣接） |
| EVPN・BGP | `bgp_session` | `dc1-leaf-01#bgp#10.255.0.1` | `ip_interface_id` → ループバック `system0.0`（辺 `over`） | `peer`（Leaf ⇄ Spine の RR） |
| EVPN・BGP | `evpn_instance` | `dc1-leaf-01#evi#100` | `ip_interface_id` → VTEP のループバック（辺 `over`）、`interfaces`（`lag1.0`。辺 `attach` → `ip_interface`） | `tunnel`（同じ EVI の VTEP 同士） |
| EVPN・BGP | `ethernet_segment` | `dc1-leaf-01#es#ES-2` | `interface_id` → `lag1`（辺 `over`） | `segment`（同じ ESI の 2 台） |

- 頂点はどれも `layer`（`ip` / `evpn`）と `device_id` を持ち、動的な `status`（`UP` / `DOWN`。無ければ UP）は Lambda `graph-status` が Splunk のアラート（gNMI の `bgp_down` / `isis_down`）から書く。`SINK_SPLUNK=0`（既定）ではこのアラートが出ないので変わらない。エージェントの `layers` ツールと Web の層の表は、id と `interface_id` / `ip_interface_id` で下の層へ追える。
- 未登録の扱いは物理層と同じ。トポロジに無い BGP のセッションが落ちたら `registered=false` の頂点を作って残し、`ops/sync-graph.sh --replace` で置き換わる。
- 実機に替えても形は変わらない。SR-MPLS にするときは `bgp_session` の `afi` と `evpn_instance` の `vtep`（VXLAN）を SR のラベルに読み替えるだけで、id と辺はそのまま。

## Neptune の基礎

Neptune そのものの仕組みと、この PoC での使い方の関係。2026-09-25 に AWS のドキュメント（[クラスターとインスタンス](https://docs.aws.amazon.com/neptune/latest/userguide/feature-overview-db-clusters.html)、[ストレージ](https://docs.aws.amazon.com/neptune/latest/userguide/feature-overview-storage.html)、[耐障害性](https://docs.aws.amazon.com/neptune/latest/userguide/backup-restore-overview-fault-tolerance.html)、[上限](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html)）とこのリポジトリのコードで確かめた内容。料金は目安で、料金ページでは確かめていない。

### 11. Neptune とは

AWS が運用を持つグラフデータベース。データを頂点と辺で持ち、「A とつながっているものを、さらにその先まで」たどる問い合わせが得意。表の JOIN を何段も重ねずに済む。

| | Neptune Database（この PoC が使う） | Neptune Analytics（使っていない） |
|---|---|---|
| 向き | 少しずつ読み書きする普段の処理 | グラフ全体の分析 |
| 仕組み | Aurora と同じ系統のストレージに置く | メモリに載せて計算する |
| 得意なこと | 頂点 1 つの更新、近くのつながりをたどる | PageRank、コミュニティ検出、最短経路、ベクトル検索 |
| VPC | 要る | 要らない |

- **問い合わせの言語は 3 つ:** Gremlin（手順としてたどり方を書く。この PoC はこれ）、openCypher（`MATCH (a)-[:LINK]->(b)` のように形を描く）、SPARQL（RDF 用）。Gremlin と openCypher は同じプロパティグラフに使える。
- **向いていない:** 表の集計（SQL が無い）、単純なキーと値、時系列、全文検索。
- **費用:** 無料枠は無く、動いているあいだずっと時間で課金される。Serverless でもゼロまでは縮まない。この PoC の `db.t4g.medium` 1 台で約 $0.11/h（[variables.tf](../terraform/pipeline/graph/variables.tf) の説明にある値で、料金 API では未確認）。

### 12. 実体は Aurora か、AZ 冗長か

**ストレージは Aurora と同じ系統、エンジンは Neptune 独自のグラフエンジン。** MySQL や PostgreSQL ではないので SQL は使えない。クラスター・プライマリ・読み取りレプリカ・エンドポイントの考え方は Aurora とほぼ同じ。

| 部分 | 冗長化 |
|---|---|
| ストレージ（クラスターボリューム） | 何もしなくても 3 つの AZ に複製される。AZ が 1 つ落ちてもデータは失われない |
| インスタンス | 書き込みを受けるプライマリは 1 つだけ。読み取りレプリカを最大 15 台足せる。別の AZ にレプリカがあれば、プライマリが落ちたときに昇格して、通常 120 秒以内（60 秒以内のことも多い）に戻る。レプリカが無いと、プライマリを作り直すまで使えない |

この PoC は [neptune.tf](../terraform/pipeline/graph/neptune.tf) で `db.t4g.medium` を 1 台だけ作り、レプリカは無い。**データは AZ 障害でも残るが、サービスはインスタンスの AZ が落ちると作り直すまで止まる**（4 のとおり、止まると `status` の更新と修復案の読み書きも止まる）。その日に消す使い捨てなので費用を優先している。止めたくないなら別の AZ にレプリカを 1 台足す（インスタンス代はほぼ 2 倍）。

### 13. グラフはいくつ作れるか

- **Neptune Database は 1 クラスター = 1 グラフ。** 1 つのクラスターの中に名前付きの別のグラフを並べる機能は無い（RDF の名前付きグラフは別）。分けたいときは、同じグラフの中でラベルで分けるか、クラスターを分ける（クラスターごとにインスタンス代がかかる）。
- **この PoC はラベルで分けている:** `device` / `interface` と上の層の `ip_interface` / `bgp_session` など、`proposal` は同じグラフの中にある。ラベルはいくつ増やしてもよく、同じグラフにあるから辺でつなげる。
- **頂点と辺の数に上限は無く、上限はストレージの大きさ:** 1 クラスター最大 128 TiB。増えて効いてくるのは、全件を引く問い合わせ（`g.V().hasLabel('proposal')` など）の遅さと、ストレージ・I/O の料金。
- Neptune Analytics は「グラフ 1 つ = リソース 1 つ」で、グラフごとに課金される。アカウントあたりの数の上限は Service Quotas で確かめる。

### 14. トポロジをグラフにする意味

**いまの形:** Neptune に置くのはトポロジ（物理層・IP 層・EVPN/BGP 層）と、その頂点の `status`（UP / DOWN / ALARM）。隣接と影響範囲（`neighbors` / `blast_radius`）、層をまたいだ追跡（`layers`）は辺をたどって答える。障害そのものは頂点にしていない。2026-10-02 までは別の頂点 `anomaly` にしていたが、次の 2 点でグラフの強みを使っていなかった（5）。

1. **頂点は発生 1 回ごとではなく「機器 + 種類 + IF」ごとに 1 つだった。** 同じ回線が 2 回落ちると同じ頂点を開き直し、`first_seen` を上書きしていた。
2. **機器の頂点と辺でつながっていなかった。** どの機器の障害かは id の文字列でしか分からなかった。

修復案の `proposal` はいまも頂点だが、同じく辺が無い。

```mermaid
flowchart LR
  subgraph now["いま"]
    D1["device dc1-leaf-01<br/>status=ALARM"] --- I1["interface dc1-leaf-01#ethernet-1/1<br/>status=DOWN"]
    P1["proposal …#first_seen<br/>（辺なし）"]
  end
  subgraph idea["障害を頂点にして辺を張るなら（案）"]
    I2["interface dc1-leaf-01#ethernet-1/1"] -- "occurred_on" --- X2["incident（発生 1 回ごと）"]
    X2 -- "handled_by" --> P2["proposal"]
  end
```

**障害を頂点にして辺でつなぐと楽に答えられる問い:**

- **根本原因の絞り込み:** 同じ時刻に Leaf 3 台で回線断が出たとき、共通の上流（同じ Spine、同じ回線）を探す。表なら段数分の JOIN、グラフなら「共通の隣」を探すだけ。
- **影響範囲とのひも付け:** 障害の頂点から下流へたどって、止まる拠点を出す（いまの `blast_radius` を障害から起こせる）。
- **再発のパターン:** 「この Spine につながる回線で過去 30 日に何回落ちたか、毎回同じ修復案で直ったか」。
- **エージェントへの材料集め:** 障害から、つながる機器・同時刻の別の障害・過去に効いた修復案をたどって渡す（GraphRAG と同じ考え方）。

**グラフにしても得をしない問い:** 月の件数、機器ごとのランキング、時系列（表と Athena が向く）。障害 1 件の長いログ（S3 に置き、頂点には場所だけ持たせる）。似た障害のベクトル検索は Neptune Database ではできない（Neptune Analytics か Knowledge Bases が要る）。

**この PoC では:** 8 台のラボで単発の回線断が中心なので、効いているのは影響範囲だけ。複数機器の同時障害（Spine 障害で配下の Leaf がまとめて落ちる）を扱うか、エージェントに原因の推定までさせるなら、辺を張る価値が出る。障害の履歴の置き場を決めるときは、この案（Neptune に発生ごとの頂点）と、表（S3 Tables）や Splunk に置く案を比べる。Neptune にはトポロジだけを置く方針（5）とぶつかるので、辺を張るなら方針から見直すことになる。

## MSK とクライアントのつなぎ

Telegraf・Spark が「どのブローカーにつなぐか」をどう知るか。2026-09-25 に手動構築（手順 5）で確かめた内容を、2026-09-28 に Telegraf を ECS に移したのに合わせて直した。

### 15. ブローカーの渡し方と msk-bootstrap

**ブートストラップサーバーとは。** Kafka のクライアントは、クラスターにつなぐときに「最初に話しかけるブローカーのアドレス一覧」が要る。これがブートストラップサーバーで、`b-1.<クラスター>.kafka.ap-northeast-1.amazonaws.com:9098,b-2.<クラスター>...:9098` のような文字列。クライアントはここに一度つなぐと、クラスター全体の構成（どのブローカーがどのパーティションを持つか）を教えてもらい、以降はそれに従って直接つなぐ。だから全ブローカーを列挙する必要は無いが、MSK はふつう全部を返す。ポート 9098 は SASL/IAM 用（9092 が平文、9094 が TLS、9096 が SASL/SCRAM）。この PoC は IAM だけを有効にしているので 9098 しか使わない。

**Telegraf は環境変数でもらう。** この文字列はクラスターを作り終えるまで決まらない（クラスター名から推測できない乱数が入る）。2026-09-28 までは Telegraf の EC2 が [terraform/pipeline/lab](../terraform/pipeline/lab) で MSK より先に作られたので、起動時に SSM の `/<prefix>/msk-bootstrap` を読んでいた。いまの Telegraf は MSK と同じ [terraform/pipeline/stream](../terraform/pipeline/stream) の ECS のタスクなので、[telegraf.tf](../terraform/pipeline/stream/telegraf.tf) がタスク定義の環境変数 `KAFKA_BROKERS` に `aws_msk_cluster.stream.bootstrap_brokers_sasl_iam` をそのまま入れる。SSM は読まない。

**`msk-bootstrap` は残す。** [terraform/pipeline/stream/msk.tf](../terraform/pipeline/stream/msk.tf) は今もクラスターを作った直後に `/<prefix>/msk-bootstrap`（String）へ書く。手動構築と確かめるときに使う（手動構築では `aws kafka get-bootstrap-brokers` の `BootstrapBrokerStringSaslIam` を `aws ssm put-parameter` で入れる。手順 5-5）。Runtime と Web のロールに付く `<prefix>-stream-parameters-read` は `parameter/<prefix>/*` の読み取りだけで、Kafka の権限は無い（この 2 つは MSK に書かない）。

**Telegraf 側の流れ**（[telegraf/telegraf.sh](../telegraf/telegraf.sh) の `render`。コンテナの入口 `tg run` が最初に呼ぶ）。

1. タスクの環境変数 `KAFKA_BROKERS` / `SNMP_AGENTS` / `GNMI_TARGETS` / `AWS_REGION` の形を確かめる。崩れていればそこで終わり、ECS がタスクを立て直す（ログに理由が出る）。
2. `telegraf.conf.in` の `__KAFKA_BROKERS__` などを埋めて `/tmp/telegraf.conf` を作る。`[[outputs.kafka]]` が metrics / gnmi / traps / logs の分あり、どれも同じブローカーに `sasl_mechanism = "AWS-MSK-IAM"` でつなぐ。
3. 認証はタスクロール `<prefix>-telegraf-task`。Telegraf の MSK IAM 認証は profile の指定が要る（[telegraf.conf.in](../telegraf/telegraf.conf.in) の注記）ので、鍵の無い `[default]`（region だけ）を `/tmp/aws_config` に置き、SDK が ECS の入れる `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` のロールに落ちるようにしてある。

**IAM は 1 段。** タスクロールのポリシー `<prefix>-telegraf-task`（[telegraf.tf](../terraform/pipeline/stream/telegraf.tf)）は次の 2 文。前の `<prefix>-stream-produce` にあった `Bootstrap`（`ssm:GetParameter`）は要らなくなったので消した。

| Sid | 許可 | 使うとき |
|---|---|---|
| `Kafka` | `kafka-cluster:Connect` / `DescribeCluster` / `WriteData` / `WriteDataIdempotently` / `DescribeTopic` / `CreateTopic` を「クラスターの ARN」と「`topic/<クラスター>/*`」に | トピックに書く（`auto.create.topics.enable=true` なので初回の書き込みでトピックができる。そのために `CreateTopic` が要る） |
| `EcsExec` | `ssmmessages` の 4 つ | ECS Exec で入って `tg test` / `tg gnmi` を打つ |

実行ロール `<prefix>-telegraf-exec` は `AmazonECSTaskExecutionRolePolicy`（ECR とログ）だけ。どちらのロールにも閉域の Deny（`<prefix>-network-perimeter`）を付ける。

**クライアントごとの渡し方。** どちらも SSM を読まない。

| クライアント | ブローカーの知り方 | 理由 |
|---|---|---|
| Telegraf（ECS） | タスク定義の環境変数 `KAFKA_BROKERS`（同じ root の MSK の属性） | MSK と同じ root で作られ、タスクは起動のたびに環境変数をもらえるから |
| Spark（EMR Serverless） | [terraform/pipeline/analytics](../terraform/pipeline/analytics) が stream の state の `bootstrap_brokers` を読み、ジョブの引数 `--bootstrap` で渡す（[spark/snmp_sinks.py](../spark/snmp_sinks.py)） | ジョブは起動のたびに引数をもらえるので、パラメータストアを引く必要が無い |

**確かめ方。** ロググループ `/ecs/<prefix>-telegraf` に「`/tmp/telegraf.conf を作った（brokers: …）`」が出ていれば `render` は通っている。ECS Exec で入って（[pipeline.md](pipeline.md) の「Telegraf に入る」）`tg test` を打つと、ポーリングだけを 1 回まわして標準出力に出す（MSK には送らない）ので、機器との疎通と MSK との疎通を切り分けられる。MSK 側は、Kafka の `WriteData` が拒まれればログに出る。
