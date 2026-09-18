# ---------------------------------------------------------------- VPC
# この root module が VPC ごと作り、destroy すると VPC ごと消える（消し忘れを残さない）。IGW も NAT も無い閉域で、外へはエンドポイントだけ
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
