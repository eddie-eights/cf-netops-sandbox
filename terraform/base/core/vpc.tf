# ---------------------------------------------------------------- VPC
# この root module が VPC ごと作り、destroy すると VPC ごと消える（消し忘れを残さない）。
# ワークロードは private subnet a / b に置き、外へは 1 AZ の NAT Gateway から出る（受信の経路は無い: IGW に向くのは public subnet だけで、
# そこに置くのは NAT Gateway だけ。private subnet のアドレスにはインターネットから届かない）。
# 2026-09-26 まではインターフェース型エンドポイント（PrivateLink）だけの閉域だった。戻すときは 7c42b0f の terraform/ を見る（docs/setup.md）
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

# ---------------------------------------------------------------- internet egress (NAT Gateway, 1 AZ)
# public subnet は NAT Gateway の置き場だけ（subnet a と同じ AZ）。map_public_ip_on_launch は切ったままで、ここにインスタンスは置かない。
# NAT は 1 AZ に 1 つ（PoC なので AZ 障害時の冗長より 0.062 USD/h の節約を取る。subnet b からも同じ NAT を通る）
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_a
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 2)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-public-a" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-public" }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = { Name = "${local.name_prefix}-nat" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  subnet_id     = aws_subnet.public.id
  allocation_id = aws_eip.nat.id

  tags = { Name = "${local.name_prefix}-nat" }

  depends_on = [aws_internet_gateway.this]
}

# private subnet の外向きは全部 NAT へ（S3 だけは endpoints.tf の gateway エンドポイントが先に取る）
resource "aws_route" "private_default" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this.id
}
