data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  availability_zone     = coalesce(var.availability_zone, data.aws_availability_zones.available.names[0])
  web_private_ip        = cidrhost(var.public_subnet_cidr, 10)
  processing_private_ip = cidrhost(var.public_subnet_cidr, 20)
  common_tags = {
    Application = var.name
  }
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = local.availability_zone
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "web" {
  name        = "${var.name}-web"
  description = "Public HTTPS and private worker protocol"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Public Nginx HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.public_https_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-web" }
}

resource "aws_security_group" "processing" {
  name        = "${var.name}-processing"
  description = "No inbound access; administration is through SSM"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-processing" }
}

resource "aws_vpc_security_group_ingress_rule" "worker_to_web" {
  security_group_id            = aws_security_group.web.id
  referenced_security_group_id = aws_security_group.processing.id
  description                  = "Worker protocol from the processing host only"
  from_port                    = 8765
  to_port                      = 8765
  ip_protocol                  = "tcp"
}

resource "aws_eip" "web" {
  domain = "vpc"
  tags   = { Name = "${var.name}-web" }
}

