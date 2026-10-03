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
| apply が `EntityAlreadyExists` など「もうある」 | state を消した・別の PC で apply した。[architecture/README.md](architecture/README.md) の get-resources で `Project=<prefix>` を探して手で消す |
| `does not have an attribute named "…"` | 前のルート（`base/ecr` → `base/core` → …）をこの PC で apply していない、または先に消した。`ops/up.sh` を打ち直す |
| `Error acquiring the state lock` | 同じルートを別のターミナルで打っている。終わるのを待つ |
| `aws_lambda_invocation.kb_index` が失敗（CREATE_KB=1） | KB のベクトルインデックスを VPC の中の Lambda `<接頭辞>-kb-index` が作る。ログは CloudWatch Logs の `/aws/lambda/<接頭辞>-kb-index`。403 や接続できないのは 4 分半まで打ち直してから落ちる: 権限の反映待ちなら `ops/up.sh` を打ち直す。続くなら `terraform/base/core` の OpenSearch Serverless の VPC エンドポイント（`create_opensearch_endpoint`）が ACTIVE か見る |
| 「terraform/base/core に OpenSearch Serverless の VPC エンドポイントが無い」の precondition で止まる | KB か logs のコレクションを作るのに、base/core に VPC エンドポイントが無い。`ops/up.sh` を通して打つ（`CREATE_KB` か `SINK_OPENSEARCH` を見て base/core に渡す）。ルートを手で apply したなら base/core を `-var create_opensearch_endpoint=true` で打ち直す |
| destroy が `provider["registry.terraform.io/opensearch-project/opensearch"]` で止まる | 2026-09-28 より前に作った agent の state（`opensearch_index.kb` 入り）。コミット f7b1688 の `terraform/agent` で destroy する |
| 手順 0 の後に「2026-09-29 より前の SG（internal）が残っている」で止まる | SG をワークロードごとに分ける前の state。まだ何も作っていない。先に `ops/down.sh` を打つ（Runtime の ENI が残るあいだは VPC・サブネット・`internal` が残るので、時間をおいて打ち直す） |
| `terraform apply` が「state に security_group_ids が無い」（Resource postcondition failed）で止まる | 土台（`terraform/base/core`）が SG をワークロードごとに分ける前の state。`ops/up.sh` を通さずにルートを直接 apply したときに出る。先に `ops/down.sh` を打ってから `ops/up.sh` |
| `ops/lab-debug.sh up` が「前の形（土台の VPC を使う）のまま」で止まる | 2026-10-04 より前に作ったスタック。`ops/lab-debug.sh down` のあと `up`（今はスタックが自分の VPC を持つ） |
| 2026-10-04 より前のデバッグ用の EC2 が残ったまま `ops/down.sh` で土台（base/core）が消えない（`DeleteConflict` / `DependencyViolation`） | 前の形のスタックは土台のサブネット・SG・境界ポリシーを使っていて、今の `ops/down.sh` はそれを消さない。`ops/lab-debug.sh down` のあと `ops/down.sh` を打ち直す（`ops/up.sh` もそのスタック向けの ECR のエンドポイントを外すので、先に消しておく） |
| `ops/up.sh` が「注意: LAB_DEBUG は使わない」と出す | `deploy.env` から `LAB_DEBUG` の行を消す。デバッグ用の EC2 は `ops/lab-debug.sh up` / `down` |
| `ops/lab-debug.sh` が「… が ROLLBACK_COMPLETE」（ROLLBACK_FAILED / DELETE_FAILED）で止まる | 原因は `aws cloudformation describe-stack-events --stack-name <prefix>-lab-debug`。`ops/lab-debug.sh down` のあと `up` |
| ビルドの `pip install` が `CERTIFICATE_VERIFY_FAILED` | 社内 CA の差し替え。`ReadTimeoutError` は QEMU が遅いだけなので打ち直す |

## 閉域（`explicit deny`）

| 症状 | 原因と直し方 |
|---|---|
| ワークロードのログに `<サービス>.ap-northeast-1.amazonaws.com` への接続のタイムアウト（`Connect timeout` / `ConnectTimeoutError`） | そのサービスのインターフェース型エンドポイントが無い（VPC にインターネットへの経路が無いので、どこにも出られない）。手順 0 の一覧にあるか見る。無ければ `ops/up.sh` の `endpoints_for` に足し、`terraform/base/core` の `interface_endpoints` の validation にも足す |
| VPC の中の相手（Neptune / MSK / ECS のタスク / 機器）への接続がタイムアウトする | 土台の通信の表にその流れが無い（SG は表に無い通信を VPC の中でも通さない。[architecture/core.md](architecture/core.md) の「SG」）。`terraform/base/core/security_groups.tf` の `sg_flows` に 1 行足して `ops/up.sh` を打ち直す。拒んだ通信は VPC フローログに出るので、CloudWatch Logs Insights でロググループ `/<prefix>/vpc-flow-logs` に `filter action = "REJECT" \| stats count(*) by srcAddr, dstAddr, dstPort, protocol \| sort count(*) desc` を打つ（1〜2 分遅れて出る。IP は ENI の一覧で SG を引く）。送り元が S3 の公開 IP（プレフィックスリスト `com.amazonaws.ap-northeast-1.s3` の範囲）で宛先が 32768 以上のポートの REJECT が少し出るのは、閉じた接続に遅れて届いたパケットで、表の漏れではない |
| ワークロードのログに `AccessDenied ... with an explicit deny in an identity-based policy` | その呼び出しが VPC のエンドポイントを通らなかった（`<prefix>-network-perimeter` の Deny。PC から打った CLI などで、VPC の外から呼んだとき）。呼んだサービスのインターフェース型エンドポイントが手順 0 の一覧にあるか見る。無ければ `ops/up.sh` の `endpoints_for` に足す。切り分けは `NETWORK_PERIMETER=0 ops/up.sh`（[setup.md](setup.md) の「閉域を一時的に外すとき」） |
| `... in a resource-based policy`（S3 / S3 Tables / SNS / SQS / AgentCore） | リソースポリシーの Deny。apply した人と AWS のサービスは外してあるので、ほかの人か、VPC の外の PC から打った。apply した本人の PC から打つか、VPC の中（Web の EC2 に SSM で入る）から打つ |
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
| `KeyError: 'MODEL_ID'` / 404 / `bedrock:InvokeModel` の `AccessDenied`（主体が Web のロール） | EC2 に `agent/app.py` が置かれている（[architecture/README.md](architecture/README.md) の「どのファイルがどこで動くか」）。`ops/up.sh` を打ち直す。Web のロールに権限を足して直さない |
| 「エージェントの呼び出しに失敗しました」 | journald の `invoke failed:` の行。`AccessDenied` は Runtime の ARN とインスタンスロール、`Could not connect` は bedrock-agentcore のエンドポイント（手順 0 の一覧にあるか、SG `endpoints` が `<prefix>-web` からの 443 を受けているか） |
| 150 秒で失敗する | Runtime が返らなかった。初回は起動が遅いので再送する |
| Runtime のログの `InvokeModel` が `ap-northeast-3` で `AccessDeniedException` | `jp.` のモデルは大阪にも振り分けられる。SCP / Permissions boundary が大阪を止めている |
| 機器の質問に「資料に見当たらない」 | ツールを呼んでいない（Runtime のログに `tools=0`）。機器名をそのまま書いて聞き直す |
| 回答に `参照:` が付かない | `CREATE_KB=1` でない、または取り込みが失敗している。Runtime のログの `retrieve failed` を見る |
| 普通の質問がガードレールの定型文で返る | 誤検知。`terraform/agent/kb.tf` の `aws_bedrock_guardrail.this` のフィルタを弱め、版を作り直す（[development.md](development.md)） |

## パイプラインと WORKFLOW

| 症状 | 原因と直し方 |
|---|---|
| 回線を落としても Grafana のルール `link_down` が Normal のまま | まず `SNMP_POLL=1` か（既定は 0 で、SNMP のポーリングをしないのでルールは発火しない。IF の up / down は `SINK_SPLUNK=1` で trap から見る）。通知まで 2 分ほどかかる（[pipeline.md](pipeline.md) の「アラート」の表）。それでも変わらなければ、Grafana の Explore で `snmp_interface_ifOperStatus` が来ているか見る（来ていなければ Telegraf か Spark。下の行と [pipeline.md](pipeline.md) の「Spark を確かめる」）。Alerting → Alert rules にルールが無いなら `SINK_PROMETHEUS=0` か `GRAFANA=0`（アラートの定義ごと並べない） |
| Splunk のアラートが出ない | `SINK_SPLUNK=1` か（既定は 0）。Splunk の検索で `index=* source="telegraf:snmp_trap"` にイベントが来ているか、保存済みサーチが動いたか（`index=_internal sourcetype=scheduler savedsearch_name=netops_*`）を見る（[pipeline.md](pipeline.md) の「Splunk のアラート」） |
| アラートは出ているのに SNS に届かない（Grafana の Contact points の `nwc-sns` が失敗、Splunk の `sendmodalert` に `ERROR`） | タイムアウトなら `sns` のインターフェース型エンドポイント（手順 0 の一覧）。`AccessDenied` ならタスクロールの `sns:Publish` と、トピックのポリシー（VPC の外からの publish を拒む）。ログは `/ecs/<prefix>-grafana`、Splunk は検索 `index=_internal sourcetype=splunkd sendmodalert netops_sns` |
| トポロジに赤い線が出ない | `/aws/lambda/<prefix>-graph-status` のログを見る。呼ばれていなければ送り手か SNS（上の 3 行）。`UNREGISTERED` の警告は、アラートの機器名・IF 名がトポロジに無い（Splunk なら IP を `DEVICE_MAP` で機器名に直せていない。lab に足した機器なら `ops/sync-graph.sh --replace`）。`読めないメッセージ（捨てる）` は本文の形が違う（[pipeline.md](pipeline.md) の「アラート」） |
| BGP / IS-IS の層や機器の `ALARM` が変わらない | 仕様。出すのは Splunk のアラートで、`SINK_SPLUNK=0`（既定）なら見つかるのは Grafana の `link_down` だけ（それも `SNMP_POLL=1` のときだけ） |
| Grafana のダッシュボード「netops / SNMP metrics」が空、エージェントの `query_metrics` が何も返さない | `SNMP_POLL=0`（既定）。SNMP のポーリングをしないので `metrics` トピックに何も載らない。見るなら `deploy.env` に `SNMP_POLL=1` を書いて `ops/up.sh`（Telegraf の取りにいく側のタスクが入れ替わる） |
| トポロジは赤くなるのに修復案が出ない | SNS → SQS か、ワーカー。`WORKFLOW=1` か、起こす種類か（ワークフローを起こすのは `link_down` だけ）を見る。`terraform -chdir=terraform/workflow output -raw anomaly_dlq_url` のキューに溜まっていれば、ワーカーが 5 回読んで処理できなかった。ワーカーのログは `terraform -chdir=terraform/workflow output -raw worker_logs_command`（[workflow.md](workflow.md) の「うまくいかないとき」） |
| 承認しても approved のまま進まない | ワーカーのイメージが古い。`deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh`（[workflow.md](workflow.md)） |
| 手順 7-2c で「Telegraf のサービスが 10 分たっても安定しない」 | タスクが起きては止まっている。サービスは 2 つ（受ける側 `<prefix>-telegraf-dialout` と取りにいく側 `<prefix>-telegraf-dialin`）。`terraform -chdir=terraform/pipeline/stream output -raw telegraf_dialout_list_tasks_command`（取りにいく側は `telegraf_dialin_list_tasks_command`）に `--desired-status STOPPED` を足して打ち、`aws ecs describe-tasks` の `stoppedReason` を見る。`CannotPullContainerError` / `ResourceInitializationError` は ecr.api / ecr.dkr / logs のエンドポイント（手順 0 の一覧）と S3 の gateway。起きてすぐ終わるならロググループ `/ecs/<prefix>-telegraf` の最初の行（ストリームは受ける側が `dialout/…`、取りにいく側が `dialin/…`。`SNMP_AGENTS が無いか形が違う` なら stream の変数 `snmp_agents` の形（見るのは `SNMP_POLL=1` のときだけ）、`SNMP_POLL は 0 か 1` なら変数 `snmp_poll`。`ops/up.sh` を通して打つ）。NLB のヘルスチェック（`8080/tcp`、Telegraf の `outputs.health`。Telegraf が動いていれば 200）が通らないと入れ替えが続く（NLB の後ろにいるのは受ける側だけ） |
| syslog の項目（ホスト名・本文など）が崩れる、取れない | Telegraf の `syslog_standard` と機器の形式が合っていない。既定は RFC3164（本番の Cisco）、lab の SR Linux は RFC5424。up.sh は `deploy.env` の `SYSLOG_STANDARD`（空なら RFC3164）を stream の `syslog_standard` に渡す。lab のログを見るなら `SYSLOG_STANDARD=RFC5424` にして打ち直す。`lab telegraf run`（デバッグ用の EC2）は常に RFC5424 |
| デバッグ用の EC2 で Telegraf の出力を見たい | `sudo lab telegraf status` / `logs -f` / `test` / `gnmi`（出力は標準出力。MSK へは送らない）。SNMP のポーリングは既定で止めてあるので、`test` やメトリクスの行を見るなら `sudo SNMP_POLL=1 lab telegraf run` で起こし直す |
| `tg gnmi`（`SNMP_POLL=1` なら `tg test`。取りにいく側のタスクで打つ）は通るのに trap / syslog が Kafka に来ない | lab の EC2 の DNAT の宛先が古い NLB の IP か、転送が無い。lab の EC2 で `sudo lab forward-status`、無ければ `sudo lab forward`（SSM `/<prefix>/telegraf-address` を読み直す） |
| Grafana / Splunk のポートフォワードがつながらない | 踏み台は Web の EC2（`Online` か上の「画面に入れない」のコマンドで見る）。タスクが動いているか `aws ecs list-services --cluster <prefix>-analytics` と `describe-services` の `runningCount` を見る。Cloud Map の名前（`grafana.<prefix>.internal` / `splunk.<prefix>.internal`）はタスクが動いていないと引けない。起きないときはロググループ `/ecs/<prefix>-grafana` / `/ecs/<prefix>-splunk` と `stoppedReason`（ECR のエンドポイントと、SSM のパスワードが消えていないか） |
| Grafana に入れない（パスワードが違う） | admin のパスワードは SSM の値（`grafana_password_command`）。タスクが起きたときに読むので、SSM を手で変えたら `aws ecs update-service --force-new-deployment` で作り直す |
| 手順 7-4b で「Splunk が 20 分たっても HEALTHY にならない」 | ロググループ `/ecs/<prefix>-splunk` を見る。初回は設定の展開で 5〜10 分かかる。ライセンスに同意していない旨で止まるならタスク定義の `SPLUNK_START_ARGS` / `SPLUNK_GENERAL_TERMS`。Spark のジョブはそのまま起きるので、Splunk が起きたあとで落ちていれば `ops/up.sh` を打ち直す |

## 消すとき

| 症状 | 原因と直し方 |
|---|---|
| `DependencyViolation`（SG / サブネット） | Runtime の ENI が残っている（最大 8 時間）。時間をおいて `ops/down.sh` を打ち直す |
| `ops/down.sh` の最後の一覧に `<prefix>-lab-debug` の VPC やバケットが出る | デバッグ用の EC2 のスタック。`ops/down.sh` は消さないので `ops/lab-debug.sh down` |
| `ops/lab-debug.sh down` が「… を空にできなかった」/「消えなかった」 | 打ち直す。原因は `aws cloudformation describe-stack-events --region ap-northeast-1 --stack-name <prefix>-lab-debug` |
| `ops/down.sh` の最後に残りが出る | 上と同じなら待つ。それ以外は get-resources で `Project=<prefix>` を探して手で消す |
