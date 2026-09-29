# 前提

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

## AWS 側

- **VPC は `terraform/base/core` が作る。**既定は `10.0.0.0/16` に `/24` のプライベートサブネット 2 つ（`apne1-az1` / `apne1-az4`）。インターネットへの経路は無い（NAT Gateway も IGW もパブリックサブネットも作らない。外から入る経路も無い）。社内のネットワークと重なるなら `deploy.env` の `VPC_CIDR` を変える（`/16`〜`/24`）。
- **SG はワークロードごとに 1 つ（12 個）と、VPC エンドポイント用の `endpoints`。**全部 `terraform/base/core` の `security_groups.tf` が作り、ルールはそこの通信の表から作る。表に無い通信は VPC の中でも通らない（送信も絞る）。表は [architecture.md](architecture.md) の「SG」。VPC の全 ENI の通信は VPC フローログ（ロググループ `/<prefix>/vpc-flow-logs`、保存 7 日）に残る。
- AWS の API（SSM / ECR / CloudWatch Logs / Bedrock / AgentCore / S3 Tables / EventBridge / SQS / Prometheus）へはインターフェース型エンドポイントで届き、VPC の外からの呼び出しは Deny で拒む（[architecture.md](architecture.md) の「閉域」）。ほかに S3 の Gateway 型（無料。ポリシーは付けない）と、KB か logs のコレクションを作るときの OpenSearch Serverless の 1 本（2 AZ で約 $0.03/h）。エンドポイントの無い API へは届かない（NAT Gateway が無いので Deny より先に接続のタイムアウトになる）。
- 組織の SCP で `aws:SourceVpc` の Deny をすでに掛けているなら、この Terraform の Deny と重なっても害は無い。逆に VPC エンドポイントの作成を SCP で止めていると、手順 3 で落ちる。
- 使うモデル: Nova 2 Lite（`jp.amazon.nova-2-lite-v1:0`）、Titan Text Embeddings V2、Rerank（`amazon.rerank-v1:0`）。どれも Amazon のモデルなので Marketplace の購読は要らない。SCP や IAM でモデルを絞っているなら、この 3 つを許可する。
- apply する人に要る権限（管理者権限なら足りる）:
  - IAM ロールの作成と `iam:CreateServiceLinkedRole`
  - `aoss:*`
  - ガードレールの作成。`guardrail-profile/apac.guardrail.v1:0` への `bedrock:CreateGuardrail` も要る
- **OpenSearch Serverless のコレクション（KB と logs）は公開しない。**ネットワークポリシーは `terraform/base/core` の VPC エンドポイント 1 本（2 つのコレクションで共用）だけを通し、KB はそれに加えて Bedrock のサービス（`bedrock.amazonaws.com`）を通す。エンドポイントを通らない接続は公開側からの扱いになるので、VPC の中からでもエンドポイントが無ければ届かない。KB のベクトルインデックスは VPC の中の Lambda が作り、apply する人の PC は OpenSearch につながない（データアクセスポリシーにも人は入らない。apply と destroy を別の人が打ってもよい）。
- ガードレールの判定は、東京以外の APAC のリージョン（大阪、ソウル、ムンバイ、シンガポール、シドニー）で行われることがある。データを国内に留める決まりがあるなら使えない。
- Session Manager の設定で KMS の暗号化を必須にしているなら、インスタンスロールへの `kms:Decrypt` が別に要る（この Terraform には入れていない。`kms` のエンドポイントも NAT Gateway も無いので、`kms` の API へは届かない。そのときは `kms` のエンドポイントを足す）。
- **PIPELINE は組織の SCP / IAM で止められやすい**（EC2 の t4g.xlarge、Neptune、MSK、EMR Serverless、S3 Tables、ECS Fargate と内部 NLB（Telegraf / Grafana / Splunk）、Cloud Map）。apply が `explicitly denied` で止まったら、管理者に許可を頼むか `SKIP_*` で外す。

## 利用者の PC 側

AWS CLI v2 と Session Manager plugin を入れる。PC から `ssm.ap-northeast-1.amazonaws.com` と `ssmmessages.ap-northeast-1.amazonaws.com` に 443 で届く必要がある（社内プロキシ経由でよい）。EC2 側は VPC の ssm / ssmmessages のエンドポイントで同じ 2 つに届く。
エンドポイントの SG はワークロードの SG からしか受けないので、DX / VPN の先の PC から VPC のエンドポイントを使う形（2026-09-26 まであった `CLIENT_CIDR`）には対応していない。PC は公開の SSM の API に出られればよい（ポートフォワーディングは PoC だけの入口として割り切る）。

## Terraform を打つ PC 側

- AWS CLI v2、Terraform 1.11 以上、Docker buildx（arm64）、Session Manager plugin、uv。
- `registry.terraform.io` に 443 で届くこと（OpenSearch Serverless には PC からつながない）。
- イメージをビルドするので、インターネットに出られること。`SINK_SPLUNK=1`（ECS に立てる既定の形）では Docker Hub から `splunk/splunk` の amd64 のイメージ（約 2〜3 GB）を引いて ECR に写す。
- x86_64 の PC では、agent / worker / Grafana のビルドに QEMU（binfmt）が要る（下の WSL2 の `binfmt` の行。Telegraf は COPY だけなので要らない）。

### Mac

```bash
brew install awscli uv
brew tap hashicorp/tap && brew install hashicorp/tap/terraform
brew install --cask session-manager-plugin
```

Docker Desktop も入れて起動しておく。bash は macOS 標準の 3.2 のままでよい。

### WSL2（Ubuntu）

道具は全部 **WSL 側**に入れる（Windows 側に入れたものは WSL から見えない）。zip は WSL のホームに展開し、改行を LF のまま使う（`file lab/lab.sh` に `CRLF` が出なければよい）。社内 PC は先に下の「社内 PC の CA」の 1 を済ませる。

Terraform:

```bash
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y terraform
```

Docker Engine（Docker Desktop を使うなら要らない）:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo usermod -aG docker "$USER"
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

PowerShell で `wsl --shutdown` して WSL を開き直し、次を打つ。

```bash
sudo systemctl enable --now docker
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx ls
```

`docker buildx ls` に `linux/arm64` があればよい。WSL を再起動すると消えるので、無くなったら `binfmt` の行だけ打ち直す。

AWS CLI v2 と Session Manager plugin は Linux 版を、uv は公式の手順で入れる。Python 3.13 は uv が `.python-version` を見て自分で取る。

## 社内 PC の CA

SSL 検査で証明書が社内 CA に差し替わる PC では、社内 CA を WSL に入れ、AWS CLI と Terraform に場所を教える。1 回だけ。

1. Windows の `certmgr.msc` で、信頼されたルート証明機関から社内のルート証明書を **Base-64 encoded X.509 (.CER)** で書き出し、WSL のストアに入れる。`1 added` と出ればよい。

```bash
sudo cp <エクスポートしたファイル> /usr/local/share/ca-certificates/corp-root.crt && sudo update-ca-certificates
```

2. Docker Engine を WSL に入れているなら `sudo systemctl restart docker`。
3. AWS CLI と Terraform にファイルの場所を渡す設定を `~/.bashrc` に書き、ターミナルを開き直す。

```bash
echo 'export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt' >> ~/.bashrc
```

`terraform init` で `x509: certificate signed by unknown authority` が出たら 1 をやり直す。

## 閉域を一時的に外すとき

`AccessDenied`（`with an explicit deny in an identity-based policy` / `resource-based policy`）が出て、VPC の外からの呼び出しを疑うときは、`NETWORK_PERIMETER=0 ops/up.sh` で Deny だけを外して打ち直す（エンドポイントは残る）。通るようになったら、どの呼び出しがエンドポイントを通っていないかを CloudTrail の `vpcEndpointId` の無いイベントで探し、直したら `1` に戻す。

2026-09-26 に NAT Gateway に替える前の配置（エンドポイント 12 本と、ルートごとに SG とルールを持つ形。最後の commit は `7c42b0f`）は、エンドポイントを戻したいまは使わない。SG は 2026-09-26〜29 は全部で共有する `internal` 1 つで、2026-09-29 から土台の通信の表に替えた。
