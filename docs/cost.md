# 費用の試算

← [README](../README.md)

## 1 時間起動したときの試算

**土台 + AGENT（既定）をチャットを使いながら 1 時間で約 $0.36（約 54 円）。何もせず置いておくだけで約 $0.18/h（約 27 円）、1 か月で約 $131（約 19,700 円）。**`CREATE_KB=1` なら置いておくだけで +$0.36/h（1 か月 +$263）で、その大半は OpenSearch Serverless の最小 OCU。

東京リージョン、単価は 2026-09-14 に AWS Price List API で確認した税抜の値。$1 = 150 円で換算した。

**土台（`terraform/base/ecr` + `terraform/base/core`。必ず作る）は約 $0.05/h（約 8 円）。**

| 土台の項目 | 単価 | 1 時間の想定 | 金額 |
|---|---|---|---|
| インターフェイスエンドポイント（EC2 用）ssm / ssmmessages × 1 AZ | $0.014/h/AZ、データ $0.01/GB | 2 AZ 時間 | $0.028 |
| EC2 t4g.small（Gradio に 2 GB） | $0.0216/h | 1 時間 | $0.022 |
| EBS gp3 8 GB | $0.096/GB 月 | 1 時間 | $0.001 |
| S3（Web の部品・手順書）/ S3 ゲートウェイエンドポイント | 数 MB / 無料 | | 約 $0 |
| Session Manager | EC2 への接続は無料 | | $0 |
| ECR | $0.10/GB 月 | イメージ数百 MB | 約 $0 |
| **合計** | | | **約 $0.05（約 8 円）** |

**共用のエンドポイント（`terraform/base/core` の `create_shared_endpoints`。AGENT / lab / analytics のどれかを作るとき）は約 $0.08/h（約 13 円）。**ecr.api / ecr.dkr / logs × 2 AZ = 6 AZ 時間 × $0.014 = $0.084。2026-09-18 に `terraform/agent` から土台へ移したので、下の AGENT の表ではなくここに数える。

**AGENT（`terraform/agent`）は置いておくだけで約 $0.05/h（約 8 円。共用のエンドポイントと合わせて約 $0.13/h）、チャットを 1 分に 1 回使うと約 $0.23/h（約 35 円）。**

| AGENT の項目 | 単価 | 1 時間の想定 | 金額 |
|---|---|---|---|
| インターフェイスエンドポイント（Runtime 用）bedrock-runtime × 2 AZ | $0.014/h/AZ、データ $0.01/GB | 2 AZ 時間。データは数 MB | $0.028 |
| インターフェイスエンドポイント（EC2 用）bedrock-agentcore × 1 AZ | 同上 | 1 AZ 時間 | $0.014 |
| Guardrails（コンテンツフィルタ、プロンプト攻撃を含む） | $0.15/1,000 テキストユニット（1 ユニット = 1,000 文字まで） | 60 往復 × 質問 1 + 回答 1 ユニット | $0.018 |
| AgentCore Runtime | $0.0895/vCPU 時間、$0.00945/GB 時間。CPU は実消費、秒課金 | 1 セッション。CPU 実消費 60 秒、メモリ 0.5 GB × 1 時間 | 約 $0.01 |
| Bedrock Amazon Nova 2 Lite（jp 推論プロファイル） | 入力 $0.396/100 万、出力 $3.311/100 万トークン | 60 往復 × 入力 3,000・出力 400 トークン | $0.15 |
| SSM パラメータ（Standard）/ CloudWatch Logs | 無料 / 取り込み $0.76/GB | 数 MB | 約 $0.005 |
| **合計** | | | **置いておくだけ約 $0.05、使って約 $0.23（約 35 円）** |

**`CREATE_KB=1`（ナレッジベース）はさらに約 $0.36/h（約 54 円）。**使わなくてもかかる OpenSearch Serverless の最小 OCU が大半で、1 か月で約 $263（約 39,500 円）。

| ナレッジベースの項目 | 単価 | 1 時間の想定 | 金額 |
|---|---|---|---|
| インターフェイスエンドポイント（ナレッジベース用）bedrock-agent-runtime × 2 AZ | $0.014/h/AZ | 2 AZ 時間 | $0.028 |
| OpenSearch Serverless（スタンバイなし） | インデックス $0.326/OCU 時間、検索 $0.334/OCU 時間 | 最小のインデックス 0.5 OCU + 検索 0.5 OCU。**使わなくてもかかる** | $0.33 |
| Rerank（Amazon Rerank 1.0） | $0.001/検索ユニット（1 ユニット = 資料 100 件まで。質問を含めて 500 トークンを超える資料は複数件に数える） | 質問 60 回 × 候補 20 件 = 60 ユニット | $0.06 |
| Titan Text Embeddings V2 | $0.000029/1,000 トークン | 質問 60 回と md 3 つの取り込みで数千トークン | 約 $0 |
| Nova 2 Lite の入力の増分 | 上と同じ | 60 往復 × 資料の分 1,500 トークン | $0.04 |
| **合計** | | | **置いておくだけ約 $0.36、使って約 $0.46（約 69 円）** |

PIPELINE（`PIPELINE=1`）は上に含めていない。単価は東京リージョンの税抜で、2026-09-15（analytics は 2026-09-17）に AWS Price List API と料金ページで確認した。

**PIPELINE の 4 ルート（lab + stream + analytics + graph）は約 $1.50/h（約 225 円）、1 か月置くと約 $1,095（約 164,000 円）**なので、使う日に作って当日中に消す。内訳は lab + graph が約 $0.23/h、stream が約 $0.71/h、analytics が約 $0.20/h、`SINK_OPENSEARCH` の OpenSearch Serverless が最大 $0.33/h と Prometheus が $0.03/h。

| lab + graph の項目 | 単価 | 1 時間 |
|---|---|---|
| lab の EC2 t4g.large | $0.0864/h | $0.086 |
| lab の gp3 16 GB | $0.096/GB 月 | $0.002 |
| Neptune db.t4g.medium × 1 | $0.1424/h（Standard。I/O 最適化は $0.192） | $0.142 |
| Neptune ストレージ・I/O | $0.12/GB 月、$0.24/100 万 I/O | 約 $0 |
| 状態を書く Lambda + EventBridge のルール | Lambda は月 100 万回まで無料、EventBridge の自分のアカウントの AWS サービスからのイベントは無料（カスタムイベントは $1.00/100 万件）。エンドポイントは足さない | 約 $0 |
| **合計** | | **約 $0.23（約 35 円）** |

lab は止めれば EBS の月 $1.5 だけ。lab だけを 1 か月起動したままだと約 $65（約 9,700 円）。`SKIP_LAB=1` なら約 $0.14/h、`SKIP_GRAPH=1` なら約 $0.09/h。

**stream は約 $0.71/h（約 107 円）、1 か月置くと約 $518（約 78,000 円）。**Kafka 4.x（KRaft）が受け付ける Standard ブローカーは m5 / m7g だけで（kafka.m5.large を使う）、kafka.t3.small は `CreateCluster` が `Unsupported InstanceType` で拒否する（2026-09-18 実機。3.x 用）。

| stream の項目 | 単価 | 1 時間 |
|---|---|---|
| MSK kafka.m5.large × 2 | $0.271/h/ブローカー | $0.542 |
| MSK ストレージ 10 GB × 2 | $0.114/GB 月 | $0.003 |
| MSK Connect 1 MCU | $0.142/MCU 時間 | $0.142 |
| インターフェイスエンドポイント sts × 1 AZ（MSK Connect 用。2026-09-17 に lambda を外した） | $0.014/h/AZ | $0.014 |
| DynamoDB（オンデマンド）/ SSM / S3 | 数百万リクエストまでほぼ無料枠 | 約 $0 |
| **合計** | | **約 $0.71（約 107 円）** |

MSK Connect を作らなければ（`CREATE_S3_SINK=0`）stream は約 $0.57/h（約 86 円）、1 か月で約 $415（約 62,000 円）。

**analytics は約 $0.20/h（約 30 円）、1 か月置くと約 $146（約 21,900 円）。**ジョブが動いている間だけ EMR Serverless に課金され、アプリケーション（器）と S3 Tables のテーブルは置いておくだけならほぼ 0。

| analytics の項目 | 単価 | 1 時間 |
|---|---|---|
| EMR Serverless（ARM）driver 1 vCPU + executor 1 vCPU | $0.052585/vCPU 時間 | $0.105 |
| EMR Serverless（ARM）メモリ。1 vCPU の上限 8 GB で見る（設定は 2 GB ずつなので実際はこれより下） | $0.005746/GB 時間 | $0.046 |
| インターフェイスエンドポイント s3tables × 2 AZ | $0.014/h/AZ | $0.028 |
| インターフェイスエンドポイント events（EventBridge の PutEvents）× 2 AZ | $0.014/h/AZ | $0.028 |
| S3 Tables のストレージ・リクエスト・compaction | $0.0265/GB 月 + リクエスト | 約 $0 |
| DynamoDB の異常テーブル（オンデマンド）/ EventBridge（カスタムイベント $1/100 万） | 数百万リクエストまでほぼ無料枠 | 約 $0 |
| **合計** | | **約 $0.20（約 30 円）** |

`SINK_OPENSEARCH` / `SINK_PROMETHEUS` は既定で作るが、上には含めていない（2026-09-17。どちらも `0` にすると消える）。`SINK_S3=0` なら上の `s3tables` のエンドポイントの $0.028 も消える。

| 格納先の項目 | 単価 | 1 時間 |
|---|---|---|
| `prometheus`: インターフェイスエンドポイント aps-workspaces × 2 AZ | $0.014/h/AZ | $0.028 |
| `prometheus`: Amazon Managed Service for Prometheus の取り込み | 料金ページの値は確認できていない（10 秒間隔 × 機器 4 台 × 数十 field で月数百万サンプル）。ワークスペース自体は無料 | 未検証 |
| `opensearch`: OpenSearch Serverless の TIMESERIES コレクション | インデックス $0.326/OCU 時間、検索 $0.334/OCU 時間 | `CREATE_KB=1` のコレクション（VECTORSEARCH 型）と OCU を共有するか確認できていない。共有されなければ（既定の `CREATE_KB=0` では必ず）最小 0.5 + 0.5 OCU で **+$0.33** |

| パターン（土台 + AGENT） | 1 時間 | 1 か月（730 時間） |
|---|---|---|
| 上の想定（1 人が 1 分に 1 回話す） | 約 $0.36（約 54 円） | 使い方しだい |
| 置いておくだけ | 約 $0.18（約 27 円） | **約 $131（約 19,700 円）** |
| 置いておくだけ + `CREATE_KB=1` | 約 $0.54（約 81 円） | 約 $394（約 59,100 円） |
| 土台だけ（AGENT を destroy。共用のエンドポイントも作らない） | 約 $0.05（約 8 円） | 約 $37（約 5,500 円） |
| `AGENT=0 PIPELINE=1`（土台 + 共用のエンドポイント + PIPELINE、`SINK_*` 既定） | 約 $1.63（約 245 円） | 約 $1,190（約 178,500 円） |
| 全部（AGENT + PIPELINE + WORKFLOW、`SINK_*` 既定） | 約 $1.74（約 261 円） | 約 $1,270（約 190,500 円） |

注意すること。

- **置いておくだけの費用の大半はエンドポイントの時間課金（AGENT の 9 AZ 時間で $0.126/h）と、`CREATE_KB=1` なら OpenSearch Serverless の最小 OCU（月約 $240）。EC2 を止めてもほとんど減らない。**使わない期間は `ops/down.sh` で消す（AGENT だけ消すなら `terraform -chdir=terraform/agent destroy`。土台は残せる）。
- OCU は負荷に応じて増える。上は最小のまま収まる前提。
- **一番ぶれるのはモデルの利用量。**会話が長いほど入力トークンが積み上がる。エージェントは直近 10 往復と、毎回取り直す資料 5 件（候補 20 件をリランクで絞ったもの）だけを送り、応答は 1,024 トークンで打ち切る（`agent/app.py`）。
- リランクは使った分だけの課金で、置いておくだけならかからない。候補数（変数 `number_of_results`）を 100 件以下に保てば、質問 1 回 = 1 ユニットのまま。
- OpenSearch Serverless とガードレールとリランクの単価は 2026-09-14 に Price List API で確認した（リランクは `APN1-AmazonRerank-v1-searchunits`、モデルは `APN1-Nova2.0Lite-input-tokens` / `APN1-Nova2.0Lite-output-tokens`）。価格表に `jp.` の推論プロファイル専用の行は無く、global でない東京の行が当たる前提で計算した（`global.` の行は入力 $0.36 / 出力 $3.01）。ガードレールの Standard 階層に別の単価があるかは確認できていない。
- t4g.small の単価は t4g.micro（$0.0108/h、検証済み）の 2 倍として置いた（同じ世代の倍々の値。Price List API では未確認）。
- 消費税、データ転送（DX / VPN 側の料金を含む）、Route 53 Resolver、Support プラン、組織で既に払っているエンドポイントは含めていない。

### WORKFLOW を足したとき

**AGENT + PIPELINE の上にさらに約 $0.06/h（約 9 円）。**Temporal のサーバーとワーカーを Fargate の 1 タスクで動かす分と sqs の interface エンドポイント 1 本だけで、Gateway と tools Lambda と DynamoDB と SQS は呼んだ分だけの課金（置いておくだけなら約 $0）。

| 項目 | 単価 | 1 時間の想定 | 金額 |
|---|---|---|---|
| Fargate（ARM64、Linux）1 vCPU / 2 GB × 1 タスク | $0.04045/vCPU 時間、$0.00442/GB 時間（東京の ARM。2026-09-16 に Price List の公開 JSON で確認。`docs/phases.md`） | 1 時間 | $0.049 |
| Fargate の ephemeral storage 20 GB | 20 GB までは無料 | | $0 |
| AgentCore Gateway | 呼んだ分だけ（ツール呼び出し 1,000 回あたりの単価。未検証） | 数十回 | 約 $0 |
| Lambda（tools） | リクエスト $0.20/100 万、実行 $0.0000133/GB 秒（arm64） | 数十回 × 0.5 秒 | 約 $0 |
| DynamoDB（修復案テーブル、オンデマンド） | 書き込み $1.4269/100 万、読み取り $0.285/100 万（東京。未検証） | 数百回 | 約 $0 |
| AgentCore Runtime（調査の呼び出し） | 上の表と同じ | 異常 1 件につき 1 回 | 約 $0 |
| CloudWatch Logs（ワーカーと Temporal） | 取り込み $0.76/GB | 数 MB | 約 $0.005 |
| インターフェイスエンドポイント sqs × 1 AZ（ワーカーが SQS を long polling する経路。`create_sqs_endpoint=false` で外せる） | $0.014/h/AZ | 1 時間 | $0.014 |
| SQS（standard）/ EventBridge のルール | 100 万リクエストまで無料枠 | 数百回 | 約 $0 |
| **合計** | | | **約 $0.06（約 9 円）** |

- Fargate は 1 タスクに 2 つのコンテナ（temporal + worker）を入れ、タスク単位の課金なので、コンテナが増えても vCPU / GB を増やさなければ同じ。`task_cpu` / `task_memory` を上げるとその比で増える。
- ECS のクラスタと Cloud Map は無料。EKS と違いクラスタ時間の課金（$0.10/h）が無いので、ECS にした（`docs/phases.md`）。
- Runtime と Fargate のタスクから Gateway へは `bedrock-agentcore` の interface エンドポイント（`terraform/agent` で作ったもの）を通る前提で、エンドポイントを足していない。

