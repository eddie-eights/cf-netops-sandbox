# 前提

← [README](../README.md)

## 前提

### AWS 側

- リージョンは `ap-northeast-1`。
- **VPC は `terraform/base/core` が作る**（既存の VPC は使わない。destroy すれば VPC ごと消えるので、消し忘れが残らない）。
  既定は `10.0.0.0/16` に `/24` のプライベートサブネット 2 つ（AZ ID `apne1-az1` と `apne1-az4`）。IGW も NAT も無く、外へはエンドポイントだけ。
  社内のネットワークと CIDR が重なると DX / VPN でつなぐときに困るので、重なるなら変数 `vpc_cidr` を変える（`/16`〜`/24`）。
  AZ ID は AgentCore Runtime が東京で対応する `apne1-az1` / `apne1-az2` / `apne1-az4` から選ぶ（変数 `az_id_a` / `az_id_b`。既定のままでよい）。
- チャットのモデルは Amazon Nova 2 Lite（`jp.amazon.nova-2-lite-v1:0`）、埋め込みは Amazon Titan Text Embeddings V2。どちらも Amazon のモデルなので、AWS Marketplace の購読も初回利用フォームも要らない。組織の SCP や IAM で Bedrock のモデルを絞っているなら、この 2 つとリランクのモデルを許可する。
- リランクは Amazon Rerank 1.0（`amazon.rerank-v1:0`）を使う。Amazon のモデルは AWS Marketplace を通さないので、購読・EULA への同意・Marketplace の支払い方法は要らない。東京リージョンで使える（2026-09-14 に AWS の文書で確認）。
- apply する人の権限に、IAM ロールの作成（名前付き）と `iam:CreateServiceLinkedRole` が含まれる。VPC モードの初回に `AWSServiceRoleForBedrockAgentCoreNetwork` が自動で作られる。
- apply する人の権限に、OpenSearch Serverless（`aoss:*`。インデックスを作るのに `aoss:APIAccessAll` が要る）と、ガードレールの作成が含まれる。
  Standard 階層のガードレールを作るには、ガードレールそのものに加えて `arn:aws:bedrock:<リージョン>:<アカウント>:guardrail-profile/apac.guardrail.v1:0` への `bedrock:CreateGuardrail` が要る。管理者権限なら足りる。
- **OpenSearch のインデックスは、Terraform を打つ PC から直接作る**（opensearch provider）。そのためデータアクセスポリシーに apply する人の ARN が要る。
  既定では Terraform が認証情報から自動で入れる（`data.aws_iam_session_context` の `issuer_arn`。IAM ユーザーならその ARN、ロールを assume しているなら元のロールの ARN）。自動で取れない形のときだけ変数 `kb_admin_principal_arn` に渡す（手順 0-3）。
  **apply と destroy は同じ人（同じロール）で打つ。**別の人が destroy すると、インデックスを消すところで 403 になる。
- **ガードレールの判定は、東京以外の APAC のリージョンで行われることがある**（Standard 階層はクロスリージョン推論が必須）。行き先は ap-northeast-1 / ap-northeast-2 / ap-northeast-3 / ap-south-1 / ap-southeast-1 / ap-southeast-2（2026-09-14 に AWS の文書で確認）。データを国内に留める決まりがある場合は使えない。
- イメージのビルドは、インターネットに出られる端末で行う（Docker と buildx）。
- **Session Manager の設定（アカウント単位）で KMS 暗号化を必須にしている場合**は、`kms` エンドポイントとインスタンスロールへの `kms:Decrypt` が別に要る。この Terraform には入れていない。
- **PIPELINE は、組織の SCP / IAM で止められやすい。**`PIPELINE=1` は t4g.large の `ec2:RunInstances`、Neptune の `rds:CreateDBCluster` / `rds:CreateDBInstance`、MSK の `kafka:CreateCluster` / `kafka:CreateClusterV2`、MSK Connect の `kafkaconnect:CreateCustomPlugin` / `kafkaconnect:CreateConnector`、EMR Serverless の `emr-serverless:CreateApplication` / `emr-serverless:StartJobRun`、S3 Tables の `s3tables:CreateTableBucket` / `s3tables:CreateTable` などが要る。
  インスタンスの種類やサービスを SCP / IAM で絞っていると、apply がエラーに `explicitly denied` / `explicit deny` と出して止まる。管理者に許可を頼むか、`deploy.env` の `SKIP_LAB=1` / `SKIP_STREAM=1` / `SKIP_ANALYTICS=1` / `SKIP_GRAPH=1` で止められた部分を外す（「[うまくいかないとき](troubleshooting.md)」）。AGENT だけなら要らない。

### 利用者の PC 側

- AWS CLI v2 と **Session Manager plugin** が入っている（閉域なら社内の配布経路で入れる）。
- その AWS アカウントの認証情報で CLI が使える。
- **PC から `ssm.ap-northeast-1.amazonaws.com` と `ssmmessages.ap-northeast-1.amazonaws.com` に 443 で届く。**経路は次のどちらか。

| 経路 | やること |
|---|---|
| 社内プロキシなどで AWS のパブリックな API に出られる | 何も足さない。変数 `client_cidr` は空のまま |
| DX / VPN で VPC に入り、この VPC のエンドポイントを使う | 変数 `client_cidr` に社内の CIDR を入れる（ssm / ssmmessages にだけ 443 を許す SG が付く）。さらに、上の 2 つの名前がエンドポイントのプライベート IP に解決されるようにする（社内 DNS から Route 53 Resolver へ転送するか、PC の hosts に書く） |

エンドポイントのプライベート IP は apply 後にこれで分かる。

```bash
aws ec2 describe-network-interfaces --region ap-northeast-1 \
  --filters Name=description,Values='VPC Endpoint Interface vpce-*' Name=tag:Project,Values=netops-nwc-poc \
  --query 'NetworkInterfaces[].[Description,PrivateIpAddress]' --output table
```

ENI にタグが付かず上で何も出ないときは、`aws ec2 describe-vpc-endpoints --filters Name=tag:Project,Values=netops-nwc-poc` で `NetworkInterfaceIds` を見て、その ID で引く。
**Route 53 Resolver のインバウンドエンドポイントは別料金**で、下の試算に含めていない。

### Terraform を打つ PC 側

- Terraform 1.11 以上（`terraform version`）。provider は `terraform init` が `registry.terraform.io` から取るので、そこに 443 で届く（版は各ルートの `.terraform.lock.hcl` で固定してある）。
- **`terraform/base/core` の apply / destroy を打つ PC から `*.ap-northeast-1.aoss.amazonaws.com` に 443 で届く。**インデックスの作成と削除はその PC から OpenSearch Serverless へ直接つなぐ（ネットワークポリシーは公開なので、インターネットか社内プロキシの先で届けばよい）。
- 社内の SSL 検査がある PC は、次の「社内 PC で使うとき」を先に済ませる。

### WSL2 の準備

この一式のコマンドは全部 bash 用なので、Windows でも **WSL2 の中で打てば PowerShell とのクォートの違いを気にしなくてよい**。Windows 側に入れた aws / terraform / docker は WSL からは見えないので、全部 WSL 側に入れる。WSL で足りているか、次を見る。

| 見るもの | 確認 |
|---|---|
| AWS CLI v2 と Session Manager plugin が **WSL 側**に入っている | `aws --version` と `session-manager-plugin` を WSL のシェルで打つ。Windows 側にだけ入れても WSL の `aws ssm start-session` からは見えない（Linux 版の deb / rpm を WSL に入れる） |
| Terraform 1.11 以上が **WSL 側**に入っている | `terraform version`。入っていなければ下の HashiCorp の apt リポジトリから入れる |
| docker で arm64 のビルドができる | `docker buildx ls` の `Platforms` に `linux/arm64` があること。Docker Desktop（WSL2 backend）なら最初からある。**WSL に直接 Docker Engine を入れる場合**は下の 3 点 |
| 改行が LF のまま | `lab/lab.sh` と `web/` の `.py` は EC2 の Linux で動くので、CRLF になっていると `set -euo pipefail\r` で落ちる。配布された zip は **WSL の中で展開**する（`/mnt/c` 配下でなく `~` 配下）。Windows のエディタで開いて保存し直すと CRLF になることがある。`file lab/lab.sh` に `CRLF` が出なければよい |
| （Docker Engine を WSL に直接入れるとき）docker.com の apt リポジトリから入れる | Ubuntu 標準の `docker.io` には buildx が無い。`docker-ce docker-ce-cli containerd.io docker-buildx-plugin` を入れ、`sudo usermod -aG docker $USER` の後にシェルを開き直す |
| （同）dockerd が起動している | `/etc/wsl.conf` に `[boot]` `systemd=true` を書いて `wsl --shutdown` で入り直すと `systemctl enable --now docker` が使える。systemd を使わないなら毎回 `sudo service docker start` |
| （同）arm64 の QEMU を登録する | `docker run --privileged --rm tonistiigi/binfmt --install arm64` を 1 回打つ（WSL を再起動すると消えるので、`docker buildx ls` に `linux/arm64` が無ければ打ち直す）。エージェントのイメージは AgentCore Runtime の要件で arm64 必須なので、これが無いと手順 2 が通らない |
| uv がある | `uv --version`。手順 4 の wheel 取得と、手元のテスト（「[手元で確かめる](development.md)」）に使う。Python 3.13 は `.python-version` を見て uv が自分で取ってくるので、apt の python3 や pip は要らない |

**WSL に Terraform を入れる**（Ubuntu の WSL2。HashiCorp の apt リポジトリ）。社内 PC は先に「社内 PC で使うとき」の 1 で社内 CA を入れておく（入れないと `apt-get update` が証明書で落ちる）。

```bash
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y terraform
terraform version
```

`Terraform v1.11` 以上が出ればよい。

**WSL に Docker Engine を入れて push するまでの一連の流れ**（Ubuntu の WSL2。Docker Desktop は使わない。2026-09-15 時点の docker.com の手順）。

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo usermod -aG docker "$USER"
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

ここで PowerShell から `wsl --shutdown` して WSL を開き直す（docker グループと systemd を効かせるため）。開き直したら次を打つ。

```bash
sudo systemctl enable --now docker
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx ls          # Platforms に linux/arm64 があれば準備完了
```

あとは手順 2（エージェント）と lab-1（lab のイメージ）のコマンドをそのまま打つ。QEMU で翻訳実行するので、エージェントのビルドはネイティブより数倍かかる（目安は数分から十数分）。
WSL を再起動すると QEMU の登録は消えるので、`docker buildx ls` に `linux/arm64` が無くなっていたら `binfmt --install arm64` だけ打ち直す。

### Mac で打つとき

コマンドは WSL と同じ（全部 bash 用）。違うのは道具の入れ方だけ。

| 見るもの | 確認 |
|---|---|
| AWS CLI v2 / Terraform 1.11 以上 / uv / Session Manager plugin | `aws --version`、`terraform version`、`uv --version`、`session-manager-plugin`。Homebrew なら下のコマンドで入る |
| Docker Desktop が起動していて arm64 のビルドができる | `docker buildx ls` の `Platforms` に `linux/arm64` があること。Apple Silicon はネイティブで作るので QEMU の登録は要らない（WSL より速い） |
| `ops/up.sh` / `ops/down.sh` が動く bash | macOS 標準の `/bin/bash` 3.2 のままでよい（3.2 で動く書き方にしてある）。Homebrew の bash を入れる必要はない |
| 認証 | `aws login` で入ってよい。スクリプトはそのまま打てる。Terraform を手で打つときは手順 0-1 の「`aws login` で入っているとき」 |

```bash
brew install awscli uv
brew tap hashicorp/tap && brew install hashicorp/tap/terraform
brew install --cask session-manager-plugin
```

Apple Silicon の Mac（Docker Desktop 29 / buildx 0.33 / Terraform 1.16 / AWS CLI 2.36）で、道具が揃うことと `ops/up.sh` / `ops/down.sh` が bash 3.2 で構文エラーにならないことは 2026-09-16 に確認した。同日に `aws login` のプロファイルで `ops/up.sh` を打ったときは、ECR とイメージまで作ったあと `terraform/base/core` の plan が opensearch provider の `NoCredentialProviders` で落ちた。手順 0 で credential_process に切り替えるようにしたあと、同じプロファイルで `terraform/base/core` の apply まで通した。2026-09-17 に Mac で通しの `ops/up.sh` を打ったところ、手順 4-3 の取り込みで `ST�: unbound variable` になった。macOS の bash 3.2 は UTF-8 ロケール（`LANG=ja_JP.UTF-8` など）だと `$ST（` のように変数名の直後に全角文字が続くとその文字のバイトまで変数名に読む（`LC_ALL=C` だと出ないので `bash -n` では見つからない）。ops のスクリプトでは非 ASCII の直前の変数を `${ST}` の形に揃えた。Intel Mac は確かめていない。

### 社内 PC で使うとき

社内ネットワークは SSL インスペクションで証明書チェーンを社内 CA に差し替えている（プロキシは無い）。WSL の中の道具は Windows の証明書ストアを見ないので、**社内 CA を WSL に入れ、AWS CLI と Terraform にその場所を教える。**1 回だけ行う（6 で毎回の設定をファイルに残す）。

1. Windows で `certmgr.msc` を開き、信頼されたルート証明機関から社内のルート証明書を **Base-64 encoded X.509 (.CER)** でエクスポートする。WSL に写して、証明書ストアに入れる（`<エクスポートしたファイル>` は Windows 側のパス。`/mnt/c/Users/…` の形）。

```bash
sudo cp <エクスポートしたファイル> /usr/local/share/ca-certificates/corp-root.crt
sudo update-ca-certificates
```

   `1 added` と出ればよい。これで `apt-get` / `curl` / `terraform init`（provider の取得）/ `git` が通る。

2. AWS CLI は Python の証明書を使うので、WSL のストアを明示する。

```bash
export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

3. OpenSearch Serverless へインデックスを作る opensearch provider にも同じファイルを渡す。`ops/up.sh` / `ops/down.sh` は `OPENSEARCH_CACERT_FILE`（無ければ `AWS_CA_BUNDLE`）を読んで `-var opensearch_cacert_file=…` を付ける。手で `terraform -chdir=terraform/agent apply` / `destroy` を打つときは `TF_VAR_opensearch_cacert_file` を Terraform が読む（`CREATE_KB=1` のときだけ要る）。

```bash
export OPENSEARCH_CACERT_FILE=/etc/ssl/certs/ca-certificates.crt
export TF_VAR_opensearch_cacert_file=/etc/ssl/certs/ca-certificates.crt
```

4. `terraform init` は 1 のストアをそのまま使うので、足すものは無い。`Failed to query available provider packages` や `x509: certificate signed by unknown authority` が出たら 1 をやり直す。
5. Docker Engine を WSL に入れているなら、dockerd に新しいストアを読ませる。

```bash
sudo systemctl restart docker
```

6. 2 と 3 の `export` はターミナルごとなので、`~/.bashrc` に残す。

```bash
cat >> ~/.bashrc <<'EOF2'
export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
export OPENSEARCH_CACERT_FILE=/etc/ssl/certs/ca-certificates.crt
export TF_VAR_opensearch_cacert_file=/etc/ssl/certs/ca-certificates.crt
EOF2
```

コンテナの中の `pip install`（手順 2）と `apk add`（lab-1）は WSL のストアを見ないので、それぞれの手順に別の書き方がある。

### `create_*_endpoint(s)` 変数

`terraform/base/core` は VPC と、どの機能も使うエンドポイント（`create_ssm_endpoints` / `create_s3_gateway_endpoint` / `create_shared_endpoints`）を作り、`terraform/agent` は bedrock のエンドポイント（`create_runtime_endpoints` / `create_kb_endpoint` / `create_agentcore_endpoint`）を作る。6 つとも**既定の `true` のまま**にする。`create_shared_endpoints` は ecr.api / ecr.dkr / logs × 2 AZ で、Runtime と lab の EC2 がイメージを取り、Spark と MSK Connect と ECS がログを出すのに使う（2026-09-18 に `terraform/agent` から移した。それまでは `AGENT=0 PIPELINE=1` で lab がイメージを取れなかった）。`ops/up.sh` は AGENT も lab も analytics も作らないときだけ `false` で打つ
（`false` は、既存の VPC に載せ替えたときのための名残）。

**VPC の中から S3 に出る経路は S3 ゲートウェイだけ**で、そのポリシーが許す先は 3 つ。`terraform/base/core` のバケット `netops-nwc-poc-kb-…`（EC2 が `web/` を取る、lab が `lab/` を取る、MSK Connect が `stream/` に書く）、
ECR のレイヤー置き場（Runtime のイメージ取得）、AL2023 の dnf リポジトリ（`/usr/bin/python3` は 3.9 のままなので、起動時に `dnf install python3.13` で 3.13 を入れる）。
バケットに対する操作の絞り込みはゲートウェイではなく各ロールの IAM ポリシーで行う（2026-09-15 にゲートウェイ側で絞っていて `s3:ListBucket` が落ち、EC2 が `web/` を取れなかった。同日に直した）。

