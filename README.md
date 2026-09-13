# fukuda-nwc-poc — NetOps フェーズ 1（閉域ネットワーク版 / CloudFormation）

ブラウザのチャット画面から AgentCore Runtime 上のエージェントと話し、エージェントが Bedrock（Claude Haiku 4.5）で答える。
これを、**インターネットに出口の無い VPC** で動かすための CloudFormation 一式。

ブラウザからは **SSM Session Manager のポートフォワーディング**で入る。NAT Gateway・EIP・パブリック IP・ロードバランサ・証明書は使わない。
EC2 のセキュリティグループに**受信ルールは 1 つも無い**。

## 構成

図解（構成図・通信の順番・作るリソース・費用）は [`docs/20260914-fukuda-nwc-poc-architecture.html`](docs/20260914-fukuda-nwc-poc-architecture.html)。GitHub ではソースが表示されるので、clone してブラウザで開く（`docs/design-system/` を同じ場所に置いたまま）。

```
利用者の PC
  │ aws ssm start-session（AWS-StartPortForwardingSession）
  │   ブラウザ → http://localhost:8080/ → Session Manager plugin
  │   ─ TLS（WebSocket）─▶ ssmmessages エンドポイント
  ▼
EC2（AL2023 arm64、プライベートサブネット、受信ルールなし）
  │ SSM Agent → 127.0.0.1:8080 のチャット Web（Python 3.13 標準ライブラリ）
  │ aws bedrock-agentcore invoke-agent-runtime（インスタンスロールで署名）
  ▼ bedrock-agentcore エンドポイント
AgentCore Runtime（VPC モード）
  ▼ bedrock-runtime / logs / ecr エンドポイント、S3 ゲートウェイ
Bedrock Converse（jp 推論プロファイル）
```

| ファイル | 中身 |
|---|---|
| `ecr.yaml` | エージェントイメージの ECR リポジトリ。先にデプロイする |
| `main.yaml` | VPC エンドポイント / AgentCore Runtime / EC2（チャット Web は UserData に埋め込み）/ IAM |
| `agent/` | Runtime に載せるコンテナ（Python 3.13、`bedrock-agentcore` SDK、arm64） |

## なぜこの形にしたか

| 要件 | どう満たすか |
|---|---|
| インターネットから届かない | EC2 にパブリック IP も受信ルールも無い。SSM Agent が内側から ssmmessages へつなぎに行く |
| 通信が暗号化される | PC から AWS までは Session Manager の TLS。ブラウザは自分の PC の `localhost` を見るだけ |
| 人単位で絞れて、記録が残る | 入れるかどうかは IAM の `ssm:StartSession` で決まる。誰がいつ入ったかは CloudTrail に残る |
| 証明書が要らない | `localhost` はブラウザが安全なコンテキストとして扱う。hosts の書き換えも要らない |
| ブラウザに AWS の認証情報を置かない | Runtime を呼ぶのは EC2 のインスタンスロール |

却下した案は次の通り。

| 案 | 却下した理由 |
|---|---|
| EC2 にプライベート IP で直接 HTTP | 平文で、ネットワークに届く人は誰でも開ける。HTTPS にするには証明書が要る |
| Private API Gateway + S3 | 名前解決（hosts かエンドポイントの DNS 名）が要り、利用者の認証も別に作る必要がある |
| ブラウザから AgentCore を直接呼ぶ | 認証情報をブラウザに置くか、インターネット上の IdP が要る |
| NLB / ALB | 時間課金が増える。証明書の問題は残る |

## 名前とタグ

- リソース名は `fukuda-nwc-poc-<何>`（パラメータ `NamePrefix`）。Runtime 名だけはハイフンが使えないので `fukuda_nwc_poc_agent`。
- タグを付けられるリソースには全部 `Project=fukuda-nwc-poc`・`owner=fukuda`（パラメータ `Owner`）・`Name` を付ける。EBS ボリュームにはインスタンスのタグが伝わる。
- スタックにも `--tags Project=fukuda-nwc-poc owner=fukuda` を付ける（手順 1・3）。スタック自体でも探せる。
- 作ったものの一覧はこれで出る。

```bash
aws resourcegroupstaggingapi get-resources --region ap-northeast-1 \
  --tag-filters Key=Project,Values=fukuda-nwc-poc \
  --query 'ResourceTagMappingList[].ResourceARN' --output table
```

**タグが付かないもの**: IAM インスタンスプロファイル（CloudFormation が非対応）、EC2 の ENI、AgentCore が作る Runtime のロググループと ENI。
ロググループは手順 4 で手で付ける。

## ログ

| 何のログ | どこ | 保持 |
|---|---|---|
| 誰がいつセッションを開いたか | CloudTrail の `StartSession` / `TerminateSession` | 組織の CloudTrail の設定 |
| エージェントの実行ログ | CloudWatch Logs `/aws/bedrock-agentcore/runtimes/<AgentRuntimeId>-DEFAULT` | 手順 4 で 7 日に設定。付けないと無期限 |
| チャット Web の呼び出し失敗 | EC2 の journald（`journalctl -u fukuda-nwc-poc-web`） | インスタンスの中だけ。終了すると消える |

- **ポートフォワーディングのセッションは、Session Manager のセッションログ（S3 / CloudWatch Logs）の対象外。**AWS のドキュメントに明記されている。記録されるのは接続したという事実（CloudTrail）だけ。
- 会話の中身はどこにも保存しない。残したいなら Bedrock のモデル呼び出しログ（アカウント単位の設定）を使う。
- Cost Explorer で `Project` 別に費用を見るには、Billing のコスト配分タグで `Project` を有効にする（組織の管理アカウントで行う設定）。

## 前提

### AWS 側

- リージョンは `ap-northeast-1`。
- **Runtime のサブネットは AgentCore が対応する AZ に置く。**東京は AZ ID `apne1-az1` / `apne1-az2` / `apne1-az4`。
  AZ 名と AZ ID の対応はアカウントごとに違うので、次で確かめる。

  ```bash
  aws ec2 describe-subnets --region ap-northeast-1 --query 'Subnets[].[SubnetId,AvailabilityZoneId,CidrBlock]' --output table
  ```

- VPC の `enableDnsSupport` と `enableDnsHostnames` が有効。
- Bedrock のモデルアクセスが有効（Anthropic のモデルは初回利用フォームの提出が要る）。
- デプロイする人の権限に、IAM ロールの作成（名前付き）と `iam:CreateServiceLinkedRole` が含まれる。VPC モードの初回に `AWSServiceRoleForBedrockAgentCoreNetwork` が自動で作られる。
- イメージのビルドは、インターネットに出られる端末で行う（Docker と buildx）。
- **Session Manager の設定（アカウント単位）で KMS 暗号化を必須にしている場合**は、`kms` エンドポイントとインスタンスロールへの `kms:Decrypt` が別に要る。このテンプレートには入れていない。

### 利用者の PC 側

- AWS CLI v2 と **Session Manager plugin** が入っている（閉域なら社内の配布経路で入れる）。
- その AWS アカウントの認証情報で CLI が使える。
- **PC から `ssm.ap-northeast-1.amazonaws.com` と `ssmmessages.ap-northeast-1.amazonaws.com` に 443 で届く。**経路は次のどちらか。

| 経路 | やること |
|---|---|
| 社内プロキシなどで AWS のパブリックな API に出られる | 何も足さない。`ClientCidr` は空のまま |
| DX / VPN で VPC に入り、このスタックのエンドポイントを使う | `ClientCidr` に社内の CIDR を入れる（ssm / ssmmessages にだけ 443 を許す SG が付く）。さらに、上の 2 つの名前がエンドポイントのプライベート IP に解決されるようにする（社内 DNS から Route 53 Resolver へ転送するか、PC の hosts に書く） |

エンドポイントのプライベート IP はデプロイ後にこれで分かる。

```bash
aws ec2 describe-network-interfaces --region ap-northeast-1 \
  --filters Name=description,Values='VPC Endpoint Interface vpce-*' Name=tag:Project,Values=fukuda-nwc-poc \
  --query 'NetworkInterfaces[].[Description,PrivateIpAddress]' --output table
```

ENI にタグが付かず上で何も出ないときは、`aws ec2 describe-vpc-endpoints --filters Name=tag:Project,Values=fukuda-nwc-poc` で `NetworkInterfaceIds` を見て、その ID で引く。
**Route 53 Resolver のインバウンドエンドポイントは別料金**で、下の試算に含めていない。

### 既存の VPC エンドポイントがある場合

社内の VPC には、エンドポイントが既にあることが多い。**同じサービスのプライベート DNS 付きエンドポイントは 1 VPC に 1 つしか作れない**ので、次のパラメータで作らないようにする。

| パラメータ | 既にあるもの | 設定 |
|---|---|---|
| `CreateRuntimeEndpoints` | ecr.api / ecr.dkr / logs / bedrock-runtime | `false`。足りないものは別途用意する |
| `CreateSsmEndpoints` | ssm / ssmmessages | `false` |
| `CreateAgentCoreEndpoint` | bedrock-agentcore | `false` |
| `CreateS3GatewayEndpoint` | 対象ルートテーブルの S3 ゲートウェイエンドポイント | `false` |

既存のエンドポイントを使うときは、その SG に次を足す。**既存のエンドポイントポリシーで拒否されていると、Runtime の作成やチャットが失敗する。**

- ecr / logs / bedrock-runtime の SG に、出力 `RuntimeSecurityGroupId` からの 443
- ssm / ssmmessages / bedrock-agentcore の SG に、出力 `InstanceSecurityGroupId` からの 443

**S3 ゲートウェイエンドポイントのポリシーは、同じルートテーブルを使う全ワークロードに効く。**
このテンプレートは ECR のレイヤー置き場（`prod-ap-northeast-1-starport-layer-bucket`）と AL2023 の dnf リポジトリ（`al2023-repos-ap-northeast-1-de612dc2`）の読み取りだけを許すので、
他のシステムと共有のルートテーブルに付けると、そのシステムの S3 アクセスが止まる。専用サブネットか、既存のゲートウェイを使う。

**EC2 も S3 ゲートウェイを通る。**AL2023 の `/usr/bin/python3` は 3.9 のままなので、起動時に `dnf install python3.13` で 3.13 を入れる。dnf のリポジトリは S3 にあるため、`RouteTableIds` には `InstanceSubnetId` のルートテーブルも入れる（Runtime と同じなら 1 つでよい）。既存のゲートウェイを使う（`CreateS3GatewayEndpoint=false`）ときは、そのポリシーが `arn:aws:s3:::al2023-repos-ap-northeast-1-de612dc2/*` の `s3:GetObject` を許しているか確かめる。

## 手順

以下はすべて**人が実行する**。AWS にリソースが作られ、課金が始まる。例の値（アカウント ID、VPC / サブネット ID、CIDR）はすべて架空。

### 1. ECR リポジトリを作る

```bash
aws cloudformation deploy \
  --region ap-northeast-1 \
  --stack-name fukuda-nwc-poc-ecr \
  --template-file ecr.yaml \
  --tags Project=fukuda-nwc-poc owner=fukuda
```

```bash
aws cloudformation describe-stacks --region ap-northeast-1 --stack-name fukuda-nwc-poc-ecr \
  --query 'Stacks[0].Outputs[?OutputKey==`RepositoryUri`].OutputValue' --output text
```

### 2. イメージをビルドして push する

インターネットに出られる端末で行う。**必ず arm64 でビルドする。**タグは上書きできない設定なので、更新のたびに変える。

```bash
REPO=123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/fukuda-nwc-poc-agent
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64 -t "$REPO:v1" --push agent/
```

### 3. 本体をデプロイする

名前付きの IAM ロールを作るので `CAPABILITY_NAMED_IAM` が要る。

```bash
aws cloudformation deploy \
  --region ap-northeast-1 \
  --stack-name fukuda-nwc-poc \
  --template-file main.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags Project=fukuda-nwc-poc owner=fukuda \
  --parameter-overrides \
    VpcId=vpc-0123456789abcdef0 \
    RuntimeSubnetIds=subnet-0aaaaaaaaaaaaaaaa,subnet-0bbbbbbbbbbbbbbbb \
    InstanceSubnetId=subnet-0aaaaaaaaaaaaaaaa \
    RouteTableIds=rtb-0cccccccccccccccc \
    Owner=fukuda \
    AgentImageUri=123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/fukuda-nwc-poc-agent:v1
```

DX / VPN 経由でこのスタックの ssm エンドポイントを使うなら `ClientCidr=192.0.2.0/24` を足す。Runtime の作成に数分かかる。

```bash
aws cloudformation describe-stacks --region ap-northeast-1 --stack-name fukuda-nwc-poc \
  --query 'Stacks[0].Outputs' --output table
```

### 4. Runtime のロググループに保持期間とタグを付ける

Runtime のロググループは AgentCore が作るので、スタックの管理外になる。既定は無期限保持。
名前は出力 `RuntimeLogGroupName`。**まだ無ければ、手順 5 で 1 回チャットした後に行う。**

```bash
LOG_GROUP=/aws/bedrock-agentcore/runtimes/fukuda_nwc_poc_agent-AbCdEf1234-DEFAULT
aws logs put-retention-policy --region ap-northeast-1 --log-group-name "$LOG_GROUP" --retention-in-days 7
aws logs tag-resource --region ap-northeast-1 \
  --resource-arn "arn:aws:logs:ap-northeast-1:123456789012:log-group:$LOG_GROUP" \
  --tags Project=fukuda-nwc-poc,owner=fukuda
```

### 5. 利用者に権限を渡す

利用者の IAM ロール（または Identity Center の許可セット）に次を付ける。`Project` タグの付いたインスタンスへのポートフォワーディングだけを許す。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PortForwardToChatWeb",
      "Effect": "Allow",
      "Action": "ssm:StartSession",
      "Resource": "arn:aws:ec2:ap-northeast-1:123456789012:instance/*",
      "Condition": {
        "StringEquals": { "ssm:resourceTag/Project": "fukuda-nwc-poc" },
        "BoolIfExists": { "ssm:SessionDocumentAccessCheck": "true" }
      }
    },
    {
      "Sid": "PortForwardDocumentOnly",
      "Effect": "Allow",
      "Action": "ssm:StartSession",
      "Resource": "arn:aws:ssm:ap-northeast-1::document/AWS-StartPortForwardingSession"
    },
    {
      "Sid": "OwnSessions",
      "Effect": "Allow",
      "Action": ["ssm:TerminateSession", "ssm:ResumeSession"],
      "Resource": "arn:aws:ssm:*:*:session/${aws:username}-*"
    }
  ]
}
```

- `ssm:SessionDocumentAccessCheck` を付けると、ドキュメントを省略したシェルセッションが通らなくなる。**画面を開くだけの人にシェルを渡さない**ため。
- 3 つ目は IAM ユーザーの例。ロールで入る場合、セッション ID の先頭はロールのセッション名になるので、Resource をそれに合わせる。
- トラブル時に中を見る管理者には、別途 `SSM-SessionManagerRunShell`（既定のシェル）での `ssm:StartSession` を渡す。

### 6. 画面を開く

インスタンスが Session Manager に登録されるまで数分かかる。`Online` になるのを待つ。

```bash
aws ssm describe-instance-information --region ap-northeast-1 \
  --filters Key=tag:Project,Values=fukuda-nwc-poc \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,AgentVersion]' --output table
```

利用者の PC でポートフォワーディングを始める（コマンドは出力 `StartSessionCommand` にもある）。**開いている間はこのターミナルを閉じない。**

```bash
aws ssm start-session --region ap-northeast-1 \
  --target i-0123456789abcdef0 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
```

Windows ではクォートの扱いが違うので、パラメータをファイルにして渡すのが確実。`pf.json` に `{"portNumber":["8080"],"localPortNumber":["8080"]}` と書いて `--parameters file://pf.json` とする。

`Waiting for connections...` と出たら、ブラウザで **http://localhost:8080/** を開く。質問を送って返事が来れば一周完了。
PC の 8080 が使用中なら `localPortNumber` を変え、URL のポートも合わせる。

## うまくいかないとき

| 症状 | 見るところ |
|---|---|
| `SessionManagerPlugin is not found` | PC に Session Manager plugin が入っていない |
| `start-session` がタイムアウトする / 名前が解決できない | PC から ssm / ssmmessages に届いていない（前提の「利用者の PC 側」） |
| `TargetNotConnected` | インスタンスが登録されていない。ssm / ssmmessages エンドポイントとその SG、インスタンスロール、手順 6 の `PingStatus`。起動直後は数分待つ。SSM Agent が 3.3.40.0 より古いと `ec2messages` エンドポイントも要る |
| `AccessDeniedException`（start-session） | 手順 5 の権限。インスタンスに `Project` タグがあるか |
| ブラウザが「接続できない」 | Web が落ちている。管理者がシェルで入り `sudo systemctl status fukuda-nwc-poc-web` と `sudo journalctl -u fukuda-nwc-poc-web -n 100`。起動時の失敗は `/var/log/cloud-init-output.log`。`python3.13` のインストールで止まっていたら S3 ゲートウェイ（前提の「既存の VPC エンドポイントがある場合」） |
| `403` の JSON | `localhost` 以外の名前で開いている。`http://localhost:<ポート>/` で開く |
| 送信すると `502` | journald の `invoke failed:` の行。`AccessDenied` は Runtime の ARN とインスタンスロール、`Could not connect to the endpoint URL` は bedrock-agentcore エンドポイントと SG、`invalid choice` は AMI の CLI が古い。その先は Runtime のログ |
| 送信すると `504` | Runtime が 120 秒で返らなかった。初回のセッション起動が遅い場合は再送する |
| しばらく放置すると切れる | Session Manager のアイドルタイムアウト（既定 20 分）。`start-session` をやり直し、画面を再読み込みする（会話は新しくなる） |
| Runtime の作成が失敗する | サブネットの AZ ID、エンドポイントの SG とポリシー、`iam:CreateServiceLinkedRole` |

## 変更するとき

- **画面や Web サーバを直すときは `main.yaml` の UserData を編集して再デプロイする。**CloudFormation はインスタンスを停止・起動し、起動のたびにファイルを書き直す。1〜2 分切れる。
- **エージェントを更新するときは、新しいタグで push して `AgentImageUri` だけ変えて再デプロイする。**
- `ImageId` は再デプロイのたびに最新の AL2023 を引く。新しい AMI が出ていると**インスタンスが作り直され、インスタンス ID が変わる**（Web は状態を持たないので中身は失われない）。`StartSessionCommand` の出力を見直す。
- UserData の本文は `Fn::Sub` を通るので、ドル記号と波かっこの組み合わせを HTML / Python / シェルに書かない（書くなら `${!...}` にする）。
- UserData の上限は 16 KB。いまは約 10 KB。

## 片付け

```bash
aws cloudformation delete-stack --region ap-northeast-1 --stack-name fukuda-nwc-poc
aws cloudformation wait stack-delete-complete --region ap-northeast-1 --stack-name fukuda-nwc-poc
aws cloudformation delete-stack --region ap-northeast-1 --stack-name fukuda-nwc-poc-ecr
aws logs delete-log-group --region ap-northeast-1 --log-group-name "$LOG_GROUP"
```

- **Runtime の ENI は削除後も最大 8 時間残る。**その間は Runtime の SG が消せず、スタック削除が `DELETE_FAILED` になることがある。時間をおいて削除し直す。
- ECR スタックはイメージごと消える（`EmptyOnDelete`）。残すなら消さなくてよい（保管料は月数円）。
- 消し残しは「名前とタグ」の `get-resources` で確かめる。

## 1 時間起動したときの試算

**チャットを使いながら 1 時間で約 $0.51（約 77 円）。何もせず置いておくだけで約 $0.17/h（約 25 円）。**

東京リージョン、単価は 2026-09-14 に AWS Price List API で確認した税抜の値。$1 = 150 円で換算した。

| 項目 | 単価 | 1 時間の想定 | 金額 |
|---|---|---|---|
| インターフェイスエンドポイント（Runtime 用）ecr.api / ecr.dkr / logs / bedrock-runtime × 2 AZ | $0.014/h/AZ、データ $0.01/GB | 8 AZ 時間。データは数 MB | $0.112 |
| インターフェイスエンドポイント（EC2 用）ssm / ssmmessages / bedrock-agentcore × 1 AZ | 同上 | 3 AZ 時間 | $0.042 |
| S3 ゲートウェイエンドポイント | 無料 | | $0 |
| EC2 t4g.micro | $0.0108/h | 1 時間 | $0.011 |
| EBS gp3 8 GB | $0.096/GB 月 | 1 時間 | $0.001 |
| Session Manager | EC2 への接続は無料 | | $0 |
| AgentCore Runtime | $0.0895/vCPU 時間、$0.00945/GB 時間。CPU は実消費、秒課金 | 1 セッション。CPU 実消費 60 秒、メモリ 0.5 GB × 1 時間 | 約 $0.01 |
| Bedrock Claude Haiku 4.5（jp 推論プロファイル） | 入力 $1.10/100 万、出力 $5.50/100 万トークン | 60 往復 × 入力 3,000・出力 400 トークン | $0.33 |
| CloudWatch Logs | 取り込み $0.76/GB | 数 MB | 約 $0.005 |
| **合計** | | | **約 $0.51（約 77 円）** |

| パターン | 1 時間 | 1 か月（730 時間） |
|---|---|---|
| 上の想定（1 人が 1 分に 1 回話す） | 約 $0.51（約 77 円） | 使い方しだい |
| 置いておくだけ | 約 $0.17（約 25 円） | **約 $121（約 18,200 円）** |
| EC2 だけ止めて置いておく | 約 $0.155（約 23 円） | 約 $113（約 17,000 円） |
| エンドポイントを全部既存で流用 | 約 $0.36（約 54 円） | 置いておくだけなら約 $9 |

注意すること。

- **費用の大半はエンドポイントの時間課金。EC2 を止めてもほとんど減らない。**使わない期間は `fukuda-nwc-poc` スタックを消す。
- **一番ぶれるのはモデルの利用量。**会話が長いほど入力トークンが積み上がる。エージェントは直近 10 往復だけを送り、応答は 1,024 トークンで打ち切る（`agent/app.py`）。
- 消費税、データ転送（DX / VPN 側の料金を含む）、Route 53 Resolver、Support プラン、組織で既に払っているエンドポイントは含めていない。

## 入っていないもの

- 会話の永続化。履歴は Runtime のセッション（microVM）の中にだけあり、アイドル 5 分か 1 時間、または画面の再読み込みで消える。
- チャット Web のログの CloudWatch Logs 転送（CloudWatch エージェント）。必要なら AL2023 の `amazon-cloudwatch-agent` を入れる。logs エンドポイントは Runtime 用に作ったものが VPC 全体で使える。
- 複数人の同時利用を想定した作り。t4g.micro で数人程度まで。1 回の送信ごとに AWS CLI を起動する（1〜2 秒余計にかかる）。

## 確認したこと・確認できていないこと

確認したこと（2026-09-14）。

- `cfn-lint` で `ecr.yaml` / `main.yaml` にエラー・警告なし。
- UserData を展開したシェルが `bash -n` を通る。Web サーバは Python 3.13 で、偽の `aws` コマンドを使って、画面の配信・チャット・Host の拒否・入力検証・失敗時の 502 を確かめた。
- 東京で ssm / ssmmessages / bedrock-agentcore のエンドポイントサービスが 1a / 1c / 1d にあり、プライベート DNS に対応している。
- AL2023（2023.12.20260817）の AWS CLI は 2.33.15 で、`bedrock-agentcore invoke-agent-runtime` を持つ。
- AL2023 のパッケージ一覧に `python3.13` がある。`/usr/bin/python3` は 3.9 のまま（https://docs.aws.amazon.com/linux/al2023/ug/python.html ）。
- `bedrock-agentcore` 1.23.0 は Python 3.10 以上で、3.13 を対応版に挙げている（PyPI の `requires_python` と classifiers）。

確認できていないこと。

- **実環境へのデプロイ。**上はすべて手元の静的検査と模擬テストで、AWS 上では動かしていない。
- 閉域の EC2 から、S3 ゲートウェイ経由で AL2023 のリポジトリに届き `dnf install python3.13` が通るか（バケット名は AWS の文書の例から取った）。
- VPC モードの Runtime が、イメージの取得に VPC 内の ECR / S3 エンドポイントを使うのか、サービス側で取得するのか。安全側に倒してエンドポイントを作る設定を既定にした。
- SSM Agent のポートフォワーディングが `localhost` を IPv6（`::1`）で先に試すか。Web は `127.0.0.1` だけで待つ。つながらない場合は journald とセッションのエラーを見る。
- Session Manager plugin が社内プロキシの環境変数に従うか。
- Runtime のロググループが作られるタイミング（Runtime の作成時か、最初の呼び出し時か）。
- `InvokeAgentRuntime` が CloudTrail の管理イベントとして既定で記録されるか。
