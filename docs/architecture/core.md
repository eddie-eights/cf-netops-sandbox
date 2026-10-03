# 構成: 土台（core）

← [構成](README.md)

`terraform/base/core` と `terraform/base/ecr`。どの機能を作るときも必ず作る。

- VPC（プライベートサブネットだけ。NAT Gateway も IGW も無い）、VPC エンドポイント、閉域の Deny、SG、フローログ、バケット、ロール、ECR。
- Web の EC2: Gradio の画面（チャット / トポロジ / 承認）。パブリック IP も受信ルールも無く、SSM Agent が内側から ssmmessages へつなぐ。PC からは SSM のポートフォワーディングで `localhost:8080` に開く。Web は EC2 のまま。Grafana と Splunk の踏み台も兼ねる（ECS にするとタスクの IP が変わり、踏み台にしにくい）。
- アラートの SNS トピック `<prefix>-alerts`（`alerts.tf`）: Grafana と Splunk が publish し、graph の Lambda（[pipeline.md](pipeline.md)）と workflow の SQS（[workflow.md](workflow.md)）へ配る。

## 閉域

AWS の API へは全部 VPC エンドポイントから行き、この VPC を通らない呼び出しを拒む。VPC にインターネットへの経路は無い（NAT Gateway も IGW も作らない。Splunk も analytics の ECS に立て、AWS の外へは送らない）。

| 層 | 何をする | どこ |
|---|---|---|
| 経路 | インターフェース型エンドポイント（private DNS）。`ops/up.sh` が機能から選ぶ: 土台 ssm / ssmmessages、AGENT は bedrock-runtime / bedrock-agentcore / ecr.api / ecr.dkr / logs（KB で bedrock-agent-runtime）、lab は ecr、stream は ecr.api / ecr.dkr / logs（Telegraf の ECS）、analytics は s3tables / logs（Prometheus で aps-workspaces、Grafana か ECS の Splunk で ecr.api / ecr.dkr、Grafana のアラートか ECS の Splunk で sns）、WORKFLOW は sqs / s3tables / bedrock-agentcore(.gateway) など。S3 は gateway 型（無料）、OpenSearch Serverless は専用の 1 本 | `terraform/base/core/endpoints.tf` |
| エンドポイントポリシー | このアカウントのプリンシパルだけ（盗んだ他のアカウントの鍵で VPC から持ち出す経路を塞ぐ）。S3 の gateway は付けない（dnf と ECR のレイヤーが止まる） | 同上 |
| IAM の Deny | ワークロードのロール全部（Web、Runtime、lab、EMR、ECS（Temporal / Telegraf / Grafana / Splunk）、tools Lambda）に `<prefix>-network-perimeter` を付ける。s3 / s3tables / sqs / sns / ssm / bedrock / aps / AgentCore の呼び出しで `aws:SourceVpc` がこの VPC でなければ拒む。デバッグ用の EC2（CloudFormation の `<prefix>-lab-debug`）のロールは同じ Action と条件の Deny を自分のスタックの VPC に向けてインラインで持つ | `terraform/base/core/perimeter.tf`、各ルートの attachment、`cloudformation/lab-debug.yaml` |
| リソースポリシーの Deny | バケット、S3 Tables のテーブルバケット、SNS のトピック（`sns:Publish`）、SQS（本体と DLQ）、AgentCore の Runtime と Gateway。同じ条件で、どのプリンシパルからでも VPC の外なら拒む | `bucket.tf`、`alerts.tf`、`pipeline/analytics/tables.tf`、`workflow/events.tf`、`workflow/gateway.tf`、`agent/runtime.tf` |

- **拒まないもの**: apply した人（`terraform` を打つ PC は VPC の外なので。PoC の割り切り）、AWS のサービス自身（`aws:PrincipalIsAWSService`）とサービスが代わりに呼ぶもの（`aws:ViaAWSService`。SNS → SQS / Lambda、Bedrock → S3 など）、KB のロール `<prefix>-kb`（取り込みは Bedrock のサービス側で動く）。
- S3 Tables の Iceberg REST は、S3 Tables が裏で呼ぶ API に元の VPC が付かないので `aws:CalledViaLast = s3tables.amazonaws.com` を外してある。
- Neptune と MSK の IAM 認証にはこの条件キーが無いので Deny に入れない（どちらも VPC の中にしか口が無い）。Prometheus のワークスペースはリソースポリシーの Deny を確かめていないので IAM の側だけ。
- apply する人が替わったら、その人が `ops/up.sh` を打ち直す（外すプリンシパルが入れ替わる）。前の人の設定のままバケットに入れないときは [troubleshooting.md](../troubleshooting.md) の「閉域」。
- 本番では、apply も VPC の中（CI のランナーなど）から打ち、外す人を無くす。AWS の外（Splunk Cloud など）へ送る必要が出て NAT Gateway を足すなら、出る先を Network Firewall のドメインの許可リストで絞る（この PoC には無い）。

## SG

ワークロードごとに SG を 1 つと、VPC エンドポイント用の `endpoints`。SG もルールも `terraform/base/core/security_groups.tf` にまとめ、ルールは通信の表（`local.sg_flows`）から作る。表に無い通信は受信も送信も通らない。ほかのルートは土台の output `security_group_ids` から自分の SG を読んで付けるだけで、ルールは作らない（SG とルールに時間課金は無いので、機能を作らないときもそろえて作る）。

| 送る側 | 受ける側 | ポート | 何のため |
|---|---|---|---|
| web / lab / telegraf / spark / grafana / splunk / lambda / workflow / runtime | endpoints / S3（プレフィックスリスト） | 443/tcp | AWS の API（インターフェース型）と S3（gateway 型。ECR のレイヤーと dnf も） |
| web / runtime / spark / lambda / workflow | neptune | 8182/tcp | Gremlin（Neptune は IAM 認証） |
| web | grafana / splunk / workflow | 3000 / 8000 / 8233（tcp） | SSM のポートフォワーディング（Grafana / Splunk Web / Temporal UI） |
| telegraf / spark | msk | 9098/tcp | Kafka（IAM 認証） |
| msk | msk | 9092〜9098/tcp | ブローカー同士 |
| spark | spark | 全部の tcp | 1 つのジョブのドライバとエグゼキュータ |
| spark | splunk | 8088/tcp | HEC（`SINK_SPLUNK=1`） |
| telegraf_nlb | telegraf | 1162/udp、5140/udp、57000/tcp、8080/tcp | trap・syslog・MDT の転送と、NLB のヘルスチェック |
| lab の管理ネットワーク（203.0.113.0/24） | telegraf_nlb | 162/udp、5140/udp | 機器の trap と syslog（lab の EC2 が DNAT するので送り元は機器の IP のまま） |
| `MDT_SOURCE_CIDRS` の CIDR（既定は空で行が無い） | telegraf_nlb | 57000/tcp | 本番の Cisco の MDT の dial-out（[collection.md](../collection.md)）。`0.0.0.0/0` は変数の検査で拒む |
| telegraf | lab の管理ネットワーク | 161/udp、57400/tcp | SNMP のポーリング（`SNMP_POLL=1` のときだけ使う。SG は既定でも開けておく）と gNMI（VPC のルートで lab の EC2 へ） |

- lab の EC2 が転送する流れは、SG が見る IP が lab の EC2 ではなく機器の管理 IP になる。そこで、相手の ENI の IP が見える側だけを SG の参照で書き（lab の送信は telegraf_nlb へ、lab の受信は telegraf から）、反対側は管理ネットワークの CIDR で書く。
- 開けていないもの: Temporal の gRPC 7233（ワーカーは同じタスクの `localhost`。Temporal も `127.0.0.1` だけで待つ）と Splunk の管理 API 8089。インターネットからの受信は、SG の前に経路が無い。
- DNS（VPC の +2）・IMDS・ECS のタスクメタデータ・Time Sync は SG の対象外なので、表に無くても届く。
- `endpoints` は表の 443 だけを受け、外へは出さない。
- VPC の全 ENI の通信は VPC フローログ（`terraform/base/core/flow_logs.tf`）でロググループ `/<prefix>/vpc-flow-logs` に残る（保存 7 日。集約 60 秒）。表の漏れで拒んだ通信は `action = REJECT` で出る（問い合わせは [troubleshooting.md](../troubleshooting.md)）。
- SG の説明（description）を変えると作り直しになり、ENI が付いていると消えない。変えるときは先に `ops/down.sh` を打つ。2026-09-29 より前の state（全部で共有する `internal` 1 つ）が残っていると、`ops/up.sh` は手順 0 の後で止まる。ほかのルートは土台の `security_group_ids` を読むが、無ければ apply の前に止まり（`terraform_remote_state` の postcondition）、destroy は古い state のままでも通る（SG の ID は `try` で読む）。
