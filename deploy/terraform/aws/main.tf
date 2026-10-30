# The AWS footprint for one environment: a VPC, an EKS cluster with one managed
# node group, an EFS file system for the artefact volume (ReadWriteMany, so the
# job and every API replica can mount it), an S3 bucket that keeps every
# published predictions file, and the IAM roles the pods use through EKS Pod
# Identity. Validated and linted in CI; not applied, because applying it costs
# money and this repository was built without spending any (see README.md).

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  private_subnets = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 4, k)]
  public_subnets  = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 8, k + 48)]
  bucket_name     = "${var.name}-${var.environment}-artifacts-${data.aws_caller_identity.current.account_id}"
}

# ---------------------------------------------------------------- network

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = "${var.name}-${var.environment}"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = local.private_subnets
  public_subnets  = local.public_subnets

  # One NAT gateway, not one per zone: the only outbound traffic is the weekly
  # export and image pulls, and a zone outage on a Tuesday costs nothing.
  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

# ---------------------------------------------------------------- cluster

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = "${var.name}-${var.environment}"
  kubernetes_version = var.kubernetes_version

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.private_subnets

  # Reachable from the internet for kubectl, with the private endpoint for the
  # nodes. Restrict endpoint_public_access_cidrs to the office once it exists.
  endpoint_public_access = true

  enable_cluster_creator_admin_permissions = true
  enabled_log_types                        = ["api", "audit", "authenticator"]

  addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = {}
    eks-pod-identity-agent = {}
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
    aws-efs-csi-driver = {
      pod_identity_association = [{
        role_arn        = aws_iam_role.efs_csi.arn
        service_account = "efs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.node_instance_types
      min_size       = var.node_min_size
      max_size       = var.node_max_size
      desired_size   = var.node_desired_size

      # The weekly job asks for 2Gi and the API replicas for 256Mi each; two
      # t3.large nodes leave room for the monitoring stack as well.
      labels = {
        workload = "general"
      }
    }
  }
}

# ---------------------------------------------------------------- artefact volume (EFS)

resource "aws_security_group" "efs" {
  name        = "${var.name}-${var.environment}-efs"
  description = "NFS from the cluster nodes to the artefact file system"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "NFS from nodes"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_efs_file_system" "artifacts" {
  creation_token = "${var.name}-${var.environment}-artifacts"
  encrypted      = true

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = {
    Name = "${var.name}-${var.environment}-artifacts"
  }
}

resource "aws_efs_mount_target" "artifacts" {
  for_each = toset(module.vpc.private_subnets)

  file_system_id  = aws_efs_file_system.artifacts.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# ---------------------------------------------------------------- artefact archive (S3)

resource "aws_s3_bucket" "artifacts" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {
      prefix = ""
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ---------------------------------------------------------------- identities

data "aws_iam_policy_document" "pod_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# The scoring job and the API: read and write the artefact bucket, nothing else.
resource "aws_iam_role" "scoring" {
  name               = "${var.name}-${var.environment}-scoring"
  assume_role_policy = data.aws_iam_policy_document.pod_assume.json
}

data "aws_iam_policy_document" "scoring" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role_policy" "scoring" {
  name   = "artifacts-bucket"
  role   = aws_iam_role.scoring.id
  policy = data.aws_iam_policy_document.scoring.json
}

resource "aws_eks_pod_identity_association" "scoring" {
  cluster_name    = module.eks.cluster_name
  namespace       = var.namespace
  service_account = var.service_account
  role_arn        = aws_iam_role.scoring.arn
}

# CSI drivers, with the AWS-managed policies they document.
resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-${var.environment}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_assume.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role" "efs_csi" {
  name               = "${var.name}-${var.environment}-efs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_assume.json
}

resource "aws_iam_role_policy_attachment" "efs_csi" {
  role       = aws_iam_role.efs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
}
