# 前提

← [README](../README.md)

`<prefix>` は `deploy.env` の `OWNER` から作る接頭辞 `<owner>-nwc-poc`。

## AWS 側

- **VPC は `terraform/base/core` が作る。**既定は `10.0.0.0/16` に `/24` のプライベートサブネット 2 つ（`apne1-az1` / `apne1-az4`）。IGW も NAT も無い。社内のネットワークと重なるなら `deploy.env` の `VPC_CIDR` を変える（`/16`〜`/24`）。
- 使うモデル: Nova 2 Lite（`jp.amazon.nova-2-lite-v1:0`）、Titan Text Embeddings V2、Rerank（`amazon.rerank-v1:0`）。どれも Amazon のモデルなので Marketplace の購読は要らない。SCP や IAM でモデルを絞っているなら、この 3 つを許可する。
- apply する人に要る権限（管理者権限なら足りる）:
  - IAM ロールの作成と `iam:CreateServiceLinkedRole`
  - `aoss:*`
  - ガードレールの作成。`guardrail-profile/apac.guardrail.v1:0` への `bedrock:CreateGuardrail` も要る
- **apply と destroy は同じ人（同じロール）で打つ。**OpenSearch のデータアクセスポリシーには apply した人の ARN が入るので、別の人が destroy すると 403 になる。自動で ARN が取れないときは `ADMIN_ARN` に書く。
- ガードレールの判定は、東京以外の APAC のリージョン（大阪、ソウル、ムンバイ、シンガポール、シドニー）で行われることがある。データを国内に留める決まりがあるなら使えない。
- Session Manager の設定で KMS の暗号化を必須にしているなら、`kms` のエンドポイントとインスタンスロールへの `kms:Decrypt` が別に要る（この Terraform には入れていない）。
- **PIPELINE は組織の SCP / IAM で止められやすい**（EC2 の t4g.large、Neptune、MSK、EMR Serverless、S3 Tables）。apply が `explicitly denied` で止まったら、管理者に許可を頼むか `SKIP_*` で外す。

## 利用者の PC 側

AWS CLI v2 と Session Manager plugin を入れる。PC から `ssm.ap-northeast-1.amazonaws.com` と `ssmmessages.ap-northeast-1.amazonaws.com` に 443 で届く必要がある。

| 経路 | やること |
|---|---|
| 社内プロキシなどで AWS のパブリックな API に出られる | 何もしない（`CLIENT_CIDR` は空） |
| DX / VPN で VPC に入る | `CLIENT_CIDR` に社内の CIDR を書く。上の 2 つの名前が VPC のエンドポイントの IP に解決されるよう、社内 DNS か hosts に書く |

エンドポイントの IP は apply のあとにこれで引く。

```bash
aws ec2 describe-network-interfaces --region ap-northeast-1 \
  --filters Name=description,Values='VPC Endpoint Interface vpce-*' Name=tag:Project,Values=<prefix> \
  --query 'NetworkInterfaces[].[Description,PrivateIpAddress]' --output table
```

## Terraform を打つ PC 側

- AWS CLI v2、Terraform 1.11 以上、Docker buildx（arm64）、Session Manager plugin、uv。
- `registry.terraform.io` と `*.ap-northeast-1.aoss.amazonaws.com` に 443 で届くこと（OpenSearch のインデックスはこの PC から作る）。
- イメージをビルドするので、インターネットに出られること。

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
3. AWS CLI と OpenSearch の provider に同じファイルを渡す設定を `~/.bashrc` に書き、ターミナルを開き直す。

```bash
cat >> ~/.bashrc <<'EOF2'
export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
export OPENSEARCH_CACERT_FILE=/etc/ssl/certs/ca-certificates.crt
export TF_VAR_opensearch_cacert_file=/etc/ssl/certs/ca-certificates.crt
EOF2
```

`terraform init` で `x509: certificate signed by unknown authority` が出たら 1 をやり直す。
