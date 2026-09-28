# ---------------------------------------------------------------- VPC
# この root module が VPC ごと作り、destroy すると VPC ごと消える（消し忘れを残さない）。
# ワークロードは private subnet a / b に置き、AWS の API へは endpoints.tf のエンドポイントだけを通る。既定ではインターネットへの経路が無い
# （IGW も NAT Gateway も作らない）。AWS の外（Splunk の HEC）へ出るときだけ var.create_nat_gateway で 1 AZ の NAT Gateway を作る
# （ops/up.sh が SINK_SPLUNK=1 のときに作らせる）。どちらでも受信の経路は無い: IGW に向くのは public subnet だけで、そこに置くのは NAT Gateway だけ。
# 2026-09-26〜28 は NAT Gateway が常にあった（AWS の API もそこから出ていた）。2026-09-28 にエンドポイントと aws:SourceVpc の Deny に戻し、
# 同日のユーザー決定で NAT も既定で作らないことにした
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_subnet" "a" {
  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_a
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-private-a" }
}

resource "aws_subnet" "b" {
  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_b
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-private-b" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-private" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "b" {
  subnet_id      = aws_subnet.b.id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------- internet egress (NAT Gateway, 1 AZ, var.create_nat_gateway)
# public subnet は NAT Gateway の置き場だけ（subnet a と同じ AZ）。map_public_ip_on_launch は切ったままで、ここにインスタンスは置かない。
# NAT は 1 AZ に 1 つ（PoC なので AZ 障害時の冗長より 0.062 USD/h の節約を取る。subnet b からも同じ NAT を通る）
resource "aws_subnet" "public" {
  count = var.create_nat_gateway ? 1 : 0

  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_a
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 2)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-public-a" }
}

resource "aws_internet_gateway" "this" {
  count = var.create_nat_gateway ? 1 : 0

  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-igw" }
}

resource "aws_route_table" "public" {
  count = var.create_nat_gateway ? 1 : 0

  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-public" }
}

resource "aws_route" "public_default" {
  count = var.create_nat_gateway ? 1 : 0

  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "public" {
  count = var.create_nat_gateway ? 1 : 0

  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_eip" "nat" {
  count = var.create_nat_gateway ? 1 : 0

  domain = "vpc"

  tags = { Name = "${local.name_prefix}-nat" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = var.create_nat_gateway ? 1 : 0

  subnet_id     = aws_subnet.public[0].id
  allocation_id = aws_eip.nat[0].id

  tags = { Name = "${local.name_prefix}-nat" }

  depends_on = [aws_internet_gateway.this]
}

# NAT があるとき、private subnet の外向きは NAT へ（S3 は endpoints.tf の gateway エンドポイントが経路で先に取り、ほかの AWS の API は
# インターフェース型エンドポイントの private DNS で VPC の中のアドレスに向く。NAT を通るのは AWS の外だけ）。NAT が無ければ既定ルートも無い
resource "aws_route" "private_default" {
  count = var.create_nat_gateway ? 1 : 0

  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}
