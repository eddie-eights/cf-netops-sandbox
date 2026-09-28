# うまくいかないとき

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。多くは `ops/up.sh` を打ち直せば直る（できているものは飛ばす）。

## `ops/up.sh` / Terraform

| 症状 | 原因と直し方 |
|---|---|
| 手順 0 で `キー「…」は使えない` / `2 回ある` / `は 1 か 0` | `deploy.env` の書き間違い。まだ何も作っていない。`deploy.env.example` と見比べて直す |
| `terraform init` が `x509: certificate signed by unknown authority` | 社内 CA が入っていない（[setup.md](setup.md) の「社内 PC の CA」） |
| apply が `AccessDenied` / `InvalidClientTokenId`（読み取りは通る） | `sts get-session-token` の一時セッションで打っている。長期キーか SSO のプロファイルで打ち直す |
| apply が `explicitly denied` で止まる | 組織の SCP / IAM が止めている。管理者に頼むか、`SKIP_*` で外す（lab は `SKIP_LAB=1` と `SKIP_STREAM=1`、MSK は `SKIP_STREAM=1`、EMR / S3 Tables は `SKIP_ANALYTICS=1`、Neptune は `SKIP_GRAPH=1`）。打ち直さないなら `ops/down.sh` |
| apply が `EntityAlreadyExists` など「もうある」 | state を消した・別の PC で apply した。[architecture.md](architecture.md) の get-resources で `Project=<prefix>` を探して手で消す |
| `does not have an attribute named "…"` | 前のルート（`base/ecr` → `base/core` → …）をこの PC で apply していない、または先に消した。`ops/up.sh` を打ち直す |
| `Error acquiring the state lock` | 同じルートを別のターミナルで打っている。終わるのを待つ |
| `aws_lambda_invocation.kb_index` が失敗（CREATE_KB=1） | KB のベクトルインデックスを VPC の中の Lambda `<接頭辞>-kb-index` が作る。ログは CloudWatch Logs の `/aws/lambda/<接頭辞>-kb-index`。403 や接続できないのは 4 分半まで打ち直してから落ちる: 権限の反映待ちなら `ops/up.sh` を打ち直す。続くなら `terraform/base/core` の OpenSearch Serverless の VPC エンドポイント（`create_opensearch_endpoint`）が ACTIVE か見る |
| `opensearch_vpc_endpoint_id が空` の precondition で止まる | KB か logs のコレクションを作るのに、base/core に VPC エンドポイントが無い。`ops/up.sh` を通して打つ（`CREATE_KB` か `SINK_OPENSEARCH` を見て base/core に渡す）。ルートを手で apply したなら base/core を `-var create_opensearch_endpoint=true` で打ち直す |
| destroy が `provider["registry.terraform.io/opensearch-project/opensearch"]` で止まる | 2026-09-28 より前に作った agent の state（`opensearch_index.kb` 入り）。コミット f7b1688 の `terraform/agent` で destroy する |
| ビルドの `pip install` が `CERTIFICATE_VERIFY_FAILED` | 社内 CA の差し替え。`ReadTimeoutError` は QEMU が遅いだけなので打ち直す |

## 閉域（`explicit deny`）

| 症状 | 原因と直し方 |
|---|---|
| ワークロードのログに `<サービス>.ap-northeast-1.amazonaws.com` への接続のタイムアウト（`Connect timeout` / `ConnectTimeoutError`） | そのサービスのインターフェース型エンドポイントが無い（VPC にインターネットへの経路が無いので、どこにも出られない）。手順 0 の一覧にあるか見る。無ければ `ops/up.sh` の `endpoints_for` に足し、`terraform/base/core` の `interface_endpoints` の validation にも足す |
| ワークロードのログに `AccessDenied ... with an explicit deny in an identity-based policy` | その呼び出しが VPC のエンドポイントを通らず NAT Gateway（`SINK_SPLUNK=1` のときだけある）から出た（`<prefix>-network-perimeter` の Deny）。呼んだサービスのインターフェース型エンドポイントが手順 0 の一覧にあるか見る。無ければ `ops/up.sh` の `endpoints_for` に足す。切り分けは `NETWORK_PERIMETER=0 ops/up.sh`（[setup.md](setup.md) の「閉域を一時的に外すとき」） |
| `... in a resource-based policy`（S3 / S3 Tables / SQS / AgentCore） | リソースポリシーの Deny。apply した人と AWS のサービスは外してあるので、ほかの人か、VPC の外の PC から打った。apply した本人の PC から打つか、VPC の中（Web の EC2 に SSM で入る）から打つ |
| apply する人が替わり、バケットや S3 Tables に `AccessDenied` で apply できない | 外すプリンシパルが前の人のまま。前の人が `ops/up.sh` を打ち直すか、管理者（ルートか、ポリシーを消せる人）が `aws s3api delete-bucket-policy --bucket <バケット>` と `aws s3tables delete-table-bucket-policy --table-bucket-arn <ARN>` でポリシーを消してから、新しい人が `ops/up.sh` を打つ。ポリシーの取得・変更・削除は Deny から外してあるので、同じアカウントで権限のある人なら VPC の外からでも消せる |
| KB の取り込み（`StartIngestionJob`）が S3 を読めない | KB のロール `<prefix>-kb` はバケットの Deny から外してある。名前を変えたなら `terraform/base/core/perimeter.tf` の `perimeter_exempt_principals` も変える |

## 画面に入れない

| 症状 | 原因と直し方 |
|---|---|
| `SessionManagerPlugin is not found` | PC に Session Manager plugin が無い |
| `start-session` がタイムアウトする / 名前が解決できない | PC から ssm / ssmmessages に 443 で届いていない（[setup.md](setup.md) の「利用者の PC 側」） |
| `TargetNotConnected` | インスタンスが SSM に登録されていない。起動直後なら数分待つ。下のコマンドで `Online` か見る |
| `AccessDeniedException` | 利用者の IAM ポリシー（[deploy.md](deploy.md) の「利用者に画面を渡す」）と、インスタンスの `Project` タグ |
| しばらく放置すると切れる | アイドル 20 分で切れる。`start-session` をやり直して再読み込みする |

```bash
aws ssm describe-instance-information --region ap-northeast-1 --filters Key=tag:Project,Values=<prefix> --query 'InstanceInformationList[].[InstanceId,PingStatus,AgentVersion]' --output table
```

## チャットの答えがおかしい

Web のログは Web の EC2 で `sudo journalctl -u <prefix>-web -n 100`、起動時の失敗は `/var/log/cloud-init-output.log`。

| 症状 | 原因と直し方 |
|---|---|
| ブラウザが「接続できない」 | Web が落ちている。上のログを見る。`web/ is not in s3://` なら Web の部品が S3 に無いので `ops/up.sh` を打ち直す |
| `ModuleNotFoundError: No module named 'toolkit'` など | 同じ。`ops/up.sh` を打ち直す |
| `KeyError: 'MODEL_ID'` / 404 / `bedrock:InvokeModel` の `AccessDenied`（主体が Web のロール） | EC2 に `agent/app.py` が置かれている（[architecture.md](architecture.md)）。`ops/up.sh` を打ち直す。Web のロールに権限を足して直さない |
| 「エージェントの呼び出しに失敗しました」 | journald の `invoke failed:` の行。`AccessDenied` は Runtime の ARN とインスタンスロール、`Could not connect` は bedrock-agentcore のエンドポイント（手順 0 の一覧にあるか、SG `endpoints` が VPC の中からの 443 を受けているか） |
| 150 秒で失敗する | Runtime が返らなかった。初回は起動が遅いので再送する |
| Runtime のログの `InvokeModel` が `ap-northeast-3` で `AccessDeniedException` | `jp.` のモデルは大阪にも振り分けられる。SCP / Permissions boundary が大阪を止めている |
| 機器の質問に「資料に見当たらない」 | ツールを呼んでいない（Runtime のログに `tools=0`）。機器名をそのまま書いて聞き直す |
| 回答に `参照:` が付かない | `CREATE_KB=1` でない、または取り込みが失敗している。Runtime のログの `retrieve failed` を見る |
| 普通の質問がガードレールの定型文で返る | 誤検知。`terraform/agent/kb.tf` の `aws_bedrock_guardrail.this` のフィルタを弱め、版を作り直す（[development.md](development.md)） |

## パイプラインと WORKFLOW

| 症状 | 原因と直し方 |
|---|---|
| 異常一覧には出るのに、トポロジも調査ワークフローも動かない（`ops/up.sh` を打ち直した直後） | 古い Spark のジョブが動いたまま。手順 7-5 は `SpecHash` タグの無い（この仕組みより前の）ジョブも古いとみなして止めるので、`ops/up.sh` を打ち直す。止まらなければ `cancel-job-run`（[pipeline.md](pipeline.md) の「変えたとき」） |
| 同じ link down が 2 件になり、片方の対象が `?` | 同じ（古いスクリプト）。残った `<機器>#link_down#?` は自動で resolved にならないので、Neptune の `anomaly` と `proposal` の頂点を消す（`g.V('<機器>#link_down#?').drop()`。修復案は id が `<機器>#link_down#?#` で始まるもの） |
| トポロジに赤い線が出ない | `/aws/lambda/<prefix>-graph-status` のログを見る |
| 承認しても approved のまま進まない | ワーカーのイメージが古い。`deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh`（[workflow.md](workflow.md)） |

## 消すとき

| 症状 | 原因と直し方 |
|---|---|
| `DependencyViolation`（SG / サブネット） | Runtime の ENI が残っている（最大 8 時間）。時間をおいて `ops/down.sh` を打ち直す |
| `ops/down.sh` の最後に残りが出る | 上と同じなら待つ。それ以外は get-resources で `Project=<prefix>` を探して手で消す |
