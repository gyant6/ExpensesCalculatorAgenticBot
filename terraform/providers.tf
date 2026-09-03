terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
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
