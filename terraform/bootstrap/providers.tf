terraform {
  # use_lockfile, which the main config's backend relies on, needs 1.10 or later.
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # This config's state lives in the bucket it creates. The first apply necessarily ran on
  # local state, since the bucket did not exist yet; `terraform init -migrate-state` then
  # moved it here. Rebuilding the bucket from nothing would repeat that: comment out this
  # block, apply, restore it, migrate.
  #
  # Backend blocks cannot read variables, so the bucket and region are repeated from
  # terraform.tfvars. The AWS profile is not committed: pass it at init with
  # `-backend-config=backend.local.hcl`.
  backend "s3" {
    bucket       = "expenses-bot-tfstate-ojg0cd"
    key          = "bootstrap/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  # Same guard as the main config: refuse to plan against any account but the one in the
  # gitignored local.auto.tfvars. See ../providers.tf.
  profile             = var.aws_profile
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project = "ExpensesCalculatorAgenticBot"
    }
  }
}
