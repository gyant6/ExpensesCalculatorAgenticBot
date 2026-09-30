terraform {
  # use_lockfile below needs 1.10 or later.
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # State lives in the bucket created by terraform/bootstrap/, so it is shared by every
  # machine and CI rather than held on whichever laptop last ran apply. use_lockfile
  # writes a lock object beside the state, stopping two applies from running at once; it
  # replaces the deprecated DynamoDB lock table.
  #
  # Backend blocks cannot read variables, so these values are literal. The AWS profile is
  # not committed: pass it at init with `-backend-config=backend.local.hcl`.
  backend "s3" {
    bucket       = "expenses-bot-tfstate-ojg0cd"
    key          = "bot/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  # Named profile to authenticate with, when the machine has more than one set of
  # credentials. Null falls through to the standard credential chain, which is what CI
  # uses — there are no named profiles on a runner. Set it in local.auto.tfvars.
  profile = var.aws_profile

  # The guard. Terraform refuses to plan when the credentials in use belong to any other
  # account, so a forgotten profile cannot provision this bot — and the SSM parameters
  # holding its token — somewhere unintended. Every ARN below is still built from
  # data.aws_caller_identity: this value is only ever compared, never interpolated, so it
  # cannot silently produce policies scoped to an account we are not in.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project = "ExpensesCalculatorAgenticBot"
    }
  }
}
