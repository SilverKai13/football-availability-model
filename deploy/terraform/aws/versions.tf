terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State belongs in a bucket with locking, not on a laptop. Uncomment and fill
  # in before the first apply; `terraform init -migrate-state` moves it.
  # backend "s3" {
  #   bucket       = "my-terraform-state"
  #   key          = "availabilityiq/prod.tfstate"
  #   region       = "eu-west-2"
  #   use_lockfile = true
  #   encrypt      = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        Project     = "availabilityiq"
        Environment = var.environment
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}
