# うまくいかないとき

← [README](../README.md)


| 症状 | 見るところ |
|---|---|
| コマンドが `--instance-ids` / `--target` / `--knowledge-base-id` の値が不正だと言う（`InvalidInstanceID.Malformed`、`Invalid target`、`ValidationException`）、または `s3:///web/` のようにバケット名が欠ける | 環境変数が空。`echo "$KB_BUCKET"` などで確かめる。別のターミナルには入っていないので、手順 0 と、その枠の 1 行目（出力から取る行）を打ち直す |
| `terraform -chdir=… output -raw …` の行の `echo` が空、または `No outputs found` / `Output "…" not found` | そのルートをこの PC で apply していない、展開したフォルダの直下で打っていない、または state を消した。`ls terraform/*/terraform.tfstate` でどのルートに state があるか見る |
| `terraform init` が `x509: certificate signed by unknown authority` / `Failed to query available provider packages` | WSL に社内 CA が無い（「[社内 PC で使うとき](setup.md)」の 1）。社内 PC でなければ `registry.terraform.io` に届くか |
| `terraform apply` が認証エラー（`AccessDenied` / `InvalidClientTokenId` / `not authorized to perform: iam:CreateRole`）で落ちる。読み取りは通る | IAM ユーザーの一時セッション（`sts get-session-token`）で打っている。手順 0-1 のとおり、一時セッションを挟まず長期キーか SSO のプロファイルで打ち直す |
| apply が `EntityAlreadyExists` / `AlreadyExistsException` / `BucketAlreadyOwnedByYou` / `RepositoryAlreadyExistsException` など「もうある」で落ちる | 同じ名前のリソースが Terraform の外にある。state を消した・別の PC で apply した（「[デプロイの詳しい説明](deploy.md)」の state の注意）か、同じ名前で手で作ったものが残っている。`deploy.env` の `NAME_PREFIX` を変えると名前ごと分けられる |
| `terraform/base/core` の apply が `does not have an attribute named "agent_repository_url"` | 手順 1 の `terraform/base/ecr` をこの PC で apply していない |
| `terraform/pipeline/lab` / `stream` / `graph` が `does not have an attribute named "vpc_id"`（`kb_bucket_name` なども同じ） | `terraform/base/core` をこの PC で apply していない、または先に destroy した。main を apply してから打ち直す（destroy のときは main を戻してから順番どおりに消す） |
| `terraform/pipeline/stream` の plan が `lab_security_group_id / lab_role_name が読めない` / `Confluent S3 sink の zip が無い` | s-2 の表 |
| `terraform/base/core` の plan / apply / destroy が `NoCredentialProviders: no valid providers in chain`（`with provider["registry.terraform.io/opensearch-project/opensearch"]`） | `aws login` で入ったプロファイルを opensearch provider が読めない。`ops/up.sh` / `ops/down.sh` は手順 0 で「AWS CLI 経由（credential_process）で渡す」と出して自分で回避する。手で打つときは 0-1 の「`aws login` で入っているとき」 |
| `Error acquiring the state lock` | 同じルートの terraform が別のターミナルで動いている（`ops/up.sh` を 2 つ打った など）。終わるのを待ってから打ち直す |
| `SessionManagerPlugin is not found` | PC に Session Manager plugin が入っていない |
| `start-session` がタイムアウトする / 名前が解決できない | PC から ssm / ssmmessages に届いていない（前提の「[利用者の PC 側](setup.md)」） |
| `TargetNotConnected` | インスタンスが登録されていない。ssm / ssmmessages エンドポイントとその SG、インスタンスロール、[手順の詳しい説明](deploy-manual.md) の手順 7 の `PingStatus`。起動直後は数分待つ。SSM Agent が 3.3.40.0 より古いと `ec2messages` エンドポイントも要る |
| `AccessDeniedException`（start-session） | 手順 6 の権限。インスタンスに `Project` タグがあるか |
| ブラウザが「接続できない」 | Web が落ちている。管理者がシェルで入り `sudo systemctl status netops-poc-web` と `sudo journalctl -u netops-poc-web -n 100`。起動時の失敗は `/var/log/cloud-init-output.log`（Web が 20 秒で立たなければ journald もここに写る）。環境変数は `/etc/netops-poc-web.env`（並びは `.env.example`）。`python3.13` のインストールや `aws s3 sync` が `AccessDenied` で止まっていたら S3 ゲートウェイのポリシー（前提の「[`create_*_endpoint(s)` 変数](setup.md)」） |
| ブラウザが「接続できない」が、journald に `web/ is not in s3://` | 手順 4 の Web の部品を置いていない。置いてインスタンスを再起動する |
| journald に `ModuleNotFoundError: No module named 'toolkit'`（`topology` / `anomalies` / `graph` / `proposals` も同じ） | 手順 4 の `agent/` の 5 モジュールを `web/` に置いていない。`for f in …` の行を打ってから再起動する |
| journald に `KeyError: 'MODEL_ID'`（`KNOWLEDGE_BASE_ID` も同じ）、または cloud-init のログに `is not web/app.py` | S3 の `web/app.py` が `agent/app.py` になっている（「[どのファイルがどこで動くか](architecture.md)」）。手順 4 の `web/app.py` を置く行を打ち直して再起動する。EC2 の環境変数に `MODEL_ID` を足すのは直し方が違う |
| ブラウザで `{"detail":"Not Found"}` / 404 | 同じ原因。EC2 で動いているのが `BedrockAgentCoreApp`（`/invocations` しか無い） |
| journald の `AccessDenied` が `bedrock:Retrieve` / `bedrock:InvokeModel` で、主体が `netops-poc-web` のロール | 同じ原因。Web のロールにこの権限は無く、足さない。Retrieve と InvokeModel は Runtime のロール（`runtime.tf` の `aws_iam_role.runtime`）が持つ |
| Runtime のログの `InvokeModel` が `AccessDeniedException` で、リソースが `ap-northeast-3` の `foundation-model` | `jp.amazon.nova-2-lite-v1:0` は東京と大阪に振り分ける。`aws_iam_role.runtime` のポリシーの `BedrockInvoke` はリージョンを `*` にしてあるので、出るなら Terraform の外のポリシー（SCP / Permissions boundary）が大阪を止めている。振り分け先は `aws bedrock get-inference-profile --region ap-northeast-1 --inference-profile-identifier jp.amazon.nova-2-lite-v1:0` で見える |
| journald に `environment variable RUNTIME_ARN / AWS_REGION is not set` | Web の環境変数が渡っていない。user_data が `/etc/netops-poc-web.env` に書く（並びは `.env.example` と同じ）ので、`sudo cat /etc/netops-poc-web.env` を `.env.example` と見比べ、無ければ `/var/log/cloud-init-output.log` で `cat > /etc/…` より前（`aws s3 sync` や `pip install`）で止まっていないか見る。user_data は起動 20 秒後に Web が動いていなければ journald をこのログに写すので、cloud-init のログ 1 本で分かる |
| 手順 2 のビルドで `pip install` が `Retrying (Retry(total=4 …))` を繰り返して落ちる | 行末が `CERTIFICATE_VERIFY_FAILED` なら社内 CA の差し替え（手順 2 の「社内ネットワークで打つとき」）。`agent/Dockerfile` の `--trusted-host` が残っているか見る。`ReadTimeoutError` は QEMU が遅いだけなので打ち直す |
| `docker login` / `docker push` / `aws` が `x509: certificate signed by unknown authority` や `SSL validation failed` | WSL 側に社内 CA が無いか、`AWS_CA_BUNDLE` が入っていない（「[社内 PC で使うとき](setup.md)」の 1・2・5） |
| `pip install` で `No matching distribution` | wheel が arm64 / cp313 でない。手順 4 の `pip download` の `--platform` と `--abi` を確かめ、`wheels/` を置き直して再起動する |
| 「エージェントの呼び出しに失敗しました」 | journald の `invoke failed:` の行。`AccessDenied` は Runtime の ARN とインスタンスロール、`Could not connect to the endpoint URL` は bedrock-agentcore エンドポイントと SG。その先は Runtime のログ |
| 送信して 150 秒で失敗する | Runtime が返らなかった（ツールの往復を含む）。初回のセッション起動が遅い場合は再送する |
| 機器の質問に「資料に見当たらない」と返る | モデルがツールを呼んでいない。Runtime のログの `tools=0`。機器名をそのまま書いて聞き直す（例 `hq-ce-01 の接続先は`）。`agent/data/` に無い機器は `known` の一覧を添えてエラーになる |
| しばらく放置すると切れる | Session Manager のアイドルタイムアウト（既定 20 分）。`start-session` をやり直し、画面を再読み込みする（会話は新しくなる） |
| Runtime の作成が失敗する | サブネットの AZ ID、エンドポイントの SG とポリシー、`iam:CreateServiceLinkedRole` |
| `opensearch_index.kb` の作成が `x509` / `certificate signed by unknown authority` で失敗する | opensearch provider が社内 CA を知らない。「[社内 PC で使うとき](setup.md)」の 3（`TF_VAR_opensearch_cacert_file` か `-var opensearch_cacert_file=…`） |
| `opensearch_index.kb` の作成が 403 で失敗する | データアクセスポリシーの反映待ち（Terraform は 60 秒待ってから作るが、足りないことがある）なら、時間をおいて同じ apply を打ち直す。続くなら apply した人の ARN が自動で取れていない。手順 0-3 で `ADMIN_ARN` を入れて `-var kb_admin_principal_arn="$ADMIN_ARN"` を付ける。destroy で出るなら apply した人と別の人で打っている |
| apply がコレクションやインデックスのところで `dial tcp` / `i/o timeout` | PC から `*.ap-northeast-1.aoss.amazonaws.com:443` に届いていない（前提の「[Terraform を打つ PC 側](setup.md)」） |
| ガードレールの作成が失敗する | 東京以外のリージョンで apply した（変数 `guardrail_profile_id` はリージョンで決まる）、apply する人に guardrail-profile への権限が無い |
| 回答に `参照:` が付かない / 「資料に見当たらない」ばかり | 手順 4 の取り込みをしていない、`docs/` の下に置いていない、取り込みジョブが失敗している |
| Runtime のログに `retrieve failed` | bedrock-agent-runtime エンドポイントと SG、Runtime のロールの `bedrock:Retrieve` |
| `retrieve failed` のエラーがリランクの `AccessDeniedException` | ナレッジベースのロールに `bedrock:Rerank` / リランクモデルへの `bedrock:InvokeModel` が無いか、組織の SCP などでリランクモデルの呼び出しが止められている。急ぐなら `-var rerank_model_id=` （空）を付けて apply すると、リランクなしで動く |
| 普通の質問がガードレールの定型文で返る | 誤検知。Runtime のログの `stop=guardrail_intervened` で確かめ、`terraform/agent/kb.tf` の `aws_bedrock_guardrail.this` の該当フィルタの強さを下げて、ガードレールの版を作り直す（「[変更するとき](development.md)」） |
| destroy が SG やサブネットで `DependencyViolation` になって止まる | Runtime の ENI が残っている（削除後も最大 8 時間）。時間をおいて同じ destroy（か `ops/down.sh`）を打ち直す。`ops/down.sh` は ENI が残っていれば VPC・サブネット・Runtime の SG を残して他を消すので、ここでは止まらない。手で足した ENI / SG が残っていないかも見る |
| `PIPELINE=1` の apply が `explicitly denied` / `explicit deny` で止まる（`ec2:RunInstances`、`rds:CreateDBCluster`、`kafka:CreateCluster`、`emr-serverless:CreateApplication`、`s3tables:CreateTableBucket` など） | 組織の SCP / IAM が、t4g.large・Neptune・MSK・EMR Serverless・S3 Tables などの作成を止めている（「[前提](setup.md)」の「AWS 側」）。管理者に許可を頼むか、`deploy.env` で止められた部分を外す（lab なら `SKIP_LAB=1` と `SKIP_STREAM=1`、MSK なら `SKIP_STREAM=1`、EMR Serverless / S3 Tables なら `SKIP_ANALYTICS=1`、Neptune なら `SKIP_GRAPH=1`）。途中まで作られたものは課金が続くので、打ち直さないなら `ops/down.sh` を打つ |
| `ops/up.sh` が手順 0 で `… 行目のキー「…」は使えない` / `が 2 回ある` / `は 1 か 0` と出して止まる | `deploy.env` の書き間違い。まだ何も作っていない。出た行番号の行を `deploy.env.example` と見比べて直す（「[`deploy.env` に書けるもの](deploy.md)」） |
| 承認タブで承認しても approved のまま applied / verified に進まず、ワーカーのログにも何も出ない | temporalio の `workflow.wait_condition(timeout=…)` は時間切れで `asyncio.TimeoutError` を投げ、握らないとワークフロー自体が失敗する（タスク失敗でないので `Failed activation` も出ない。2026-09-18 実機）。`workflow/worker.py` は握るように直してある。ワーカーが古ければ `deploy.env` の `IMAGE_TAG` を上げて `ops/up.sh`（同じタグなら ECR にあるものをそのまま使い、ビルドしない）。Temporal の UI で `Failed` のワークフローに `TimeoutError` が付いていたらこれ |
| チャットと異常一覧は異常を出すのに、トポロジに赤い線が出ない | `/aws/lambda/<接頭辞>-graph-status` の `AccessDeniedException` … `neptune-db:DeleteDataViaQuery`。Gremlin の `property('status', …)` は既存値の削除を伴うので Delete も要る（`terraform/pipeline/graph/sync.tf`。2026-09-18 実機）。直したら `ops/up.sh`（graph の apply で IAM が変わる）。対象が `?` の異常は機器の頂点にも辺にも当たらないので写らない |
| 同じ link down が poll と trap の 2 件になり、trap 側の対象が `?` | Telegraf 1.40 の `snmp_trap` は MIB が無いと varbind を `iso.3.6.1.2.1.2.2.1.2.38` の名前で書く（先頭が `iso.`）。`spark/snmp_sinks.py` は `.1.` に読み替える（2026-09-18 実機）。Spark のスクリプトは `ops/up.sh` が S3 に置くが**動いているジョブは起こし直さない**ので、`aws emr-serverless cancel-job-run` してから `ops/up.sh`。残った `<機器>#link_down#?` の異常は自動では resolved にならないので `aws dynamodb delete-item`（anomalies と proposals の両方）で消す |
| 一度 resolved になった異常がもう一度開いても、調査ワークフローが起きない | `first_seen` が前の値のまま残り、ワーカーが「前と同じ異常」と見ていた。Spark は開き直すとき `first_seen` を今にして `resolved_at` を消す（2026-09-18 に直した。上の Spark の起こし直しと同じ手順） |
| 異常一覧には出るのに、調査ワークフローもトポロジの色も動かない（`ops/up.sh` を打ち直した直後） | EventBridge の Source は `<接頭辞>.spark`（1 つの AWS アカウントを何人かで使っても混ざらないように、`NAME_PREFIX` に連動させてある）。**動いている Spark のジョブは `ops/up.sh` では起こし直らない**ので、古いスクリプトが `netops.spark` を出したままだと新しいルールに当たらない。`aws emr-serverless cancel-job-run` してから `ops/up.sh`（上の 2 行と同じ手順）。出ている Source はドライバーのログ（`格納先: …; 検知: … → EventBridge default（Source …）`）で確かめられる |

