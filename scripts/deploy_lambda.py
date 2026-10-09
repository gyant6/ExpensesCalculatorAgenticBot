"""Deploy the built Lambda archives through the S3 artifacts bucket.

Run from anywhere, after `scripts/build_lambda.py`:

    uv run python scripts/deploy_lambda.py              # the bot only
    uv run python scripts/deploy_lambda.py bot charts   # both functions

For each function: upload its zip to the artifacts bucket, point the function at it, wait
until Lambda reports the update finished, check the code Lambda now runs is byte-for-byte
the local zip, then delete the zip so the bucket is empty between deploys. A direct
47 MB `update-function-code` upload sends the archive in one request and repeatedly
stalled and failed whole; an S3 upload goes in parts that retry individually, and the
function update itself is then a small request that Lambda serves from S3 inside AWS.

Nothing machine- or account-specific lives here. Function names, the bucket, the region
and the target account come from `terraform output`, and the AWS profile is taken, first
match wins, from --profile, the AWS_PROFILE variable, `aws_profile` in the gitignored
terraform/local.auto.tfvars (the file Terraform reads on the same machine), or else the
default credential chain, as on a CI runner. The script refuses to deploy when the
credentials belong to any account other than the one Terraform manages.

Exit codes:
    0: every requested function was updated and verified.
    1: a precondition failed (missing archive, wrong account, Terraform outputs
       unavailable) or an update did not verify. Nothing further is deployed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient
    from mypy_boto3_s3 import S3Client

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
TERRAFORM_DIR: Final = REPO_ROOT / "terraform"
LOCAL_TFVARS: Final = TERRAFORM_DIR / "local.auto.tfvars"

# The function's Terraform output name and its archive, per deploy target.
TARGETS: Final = {
    "bot": ("lambda_function_name", REPO_ROOT / "function.zip"),
    "charts": ("chart_lambda_function_name", REPO_ROOT / "chart_function.zip"),
}
# The bot changes with almost every release; the chart function rarely does.
_DEFAULT_TARGET: Final = "bot"

# 8 MB parts, each retried on its own: a dropped connection costs one part, not the
# whole upload.
_TRANSFER: Final = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=4,
)
_CLIENT_CONFIG: Final = Config(
    retries={"max_attempts": 10, "mode": "standard"},
    connect_timeout=30,
    read_timeout=120,
)
_PROFILE_LINE: Final = re.compile(r'^\s*aws_profile\s*=\s*"([^"]+)"', re.MULTILINE)


class DeployError(RuntimeError):
    """A precondition or verification failed; the message says which and why."""


@dataclass(frozen=True)
class TerraformOutputs:
    """The values this script needs from `terraform output`."""

    bucket: str
    account_id: str
    region: str
    function_names: dict[str, str]


def resolve_profile(cli_profile: str | None) -> str | None:
    """Pick the AWS profile to deploy with.

    Args:
        cli_profile: The --profile argument, if given.

    Returns:
        --profile, else AWS_PROFILE, else `aws_profile` from terraform/local.auto.tfvars,
        else None to use the default credential chain.
    """
    if cli_profile:
        return cli_profile
    if os.environ.get("AWS_PROFILE"):
        return os.environ["AWS_PROFILE"]
    if LOCAL_TFVARS.is_file():
        match = _PROFILE_LINE.search(LOCAL_TFVARS.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return None


def read_terraform_outputs() -> TerraformOutputs:
    """Read function names, bucket, account and region from `terraform output`.

    Returns:
        The values, as Terraform last applied them.

    Raises:
        DeployError: If Terraform is not installed or initialised, or an output this
            script needs is missing — usually because the artifacts bucket has not been
            applied yet.
    """
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise DeployError("terraform is not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise DeployError(
            f"`terraform output` failed; run `terraform init` in terraform/ first.\n"
            f"{exc.stderr.strip()}"
        ) from exc

    outputs = {
        name: entry["value"] for name, entry in json.loads(result.stdout).items()
    }
    needed = ["artifacts_bucket_name", "aws_account_id", "aws_region"] + [
        output for output, _ in TARGETS.values()
    ]
    missing = [name for name in needed if name not in outputs]
    if missing:
        raise DeployError(
            f"Terraform outputs missing: {missing}. Run `terraform apply` in terraform/ "
            "to create the artifacts bucket and its outputs."
        )
    return TerraformOutputs(
        bucket=outputs["artifacts_bucket_name"],
        account_id=outputs["aws_account_id"],
        region=outputs["aws_region"],
        function_names={
            target: outputs[output] for target, (output, _) in TARGETS.items()
        },
    )


def code_sha256(path: Path) -> str:
    """The archive's SHA-256, base64-encoded — the form Lambda reports as CodeSha256."""
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.b64encode(digest).decode("ascii")


def deploy_function(
    s3: S3Client,
    lambda_client: LambdaClient,
    bucket: str,
    function_name: str,
    archive: Path,
) -> str:
    """Stage one archive in S3, update the function from it, verify, and clean up.

    The staged zip is deleted whether or not the update succeeds; the bucket's one-day
    expiry rule removes it if this process dies before reaching that point.

    Args:
        s3: S3 client for the artifacts bucket.
        lambda_client: Lambda client for the function's region.
        bucket: The artifacts bucket.
        function_name: The Lambda function to update.
        archive: The local zip to deploy.

    Returns:
        The function's LastModified timestamp after the update.

    Raises:
        DeployError: If the code Lambda reports does not match the local archive.
        botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError: If an AWS
            call fails after its retries, or the update does not finish.
    """
    expected = code_sha256(archive)
    key = f"{function_name}/{expected.replace('/', '_').rstrip('=')}.zip"

    print(f"  uploading {archive.name} ({archive.stat().st_size / 1e6:.1f} MB)...")
    s3.upload_file(str(archive), bucket, key, Config=_TRANSFER)
    try:
        print("  updating the function from S3...")
        lambda_client.update_function_code(
            FunctionName=function_name, S3Bucket=bucket, S3Key=key
        )
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
        print("  staged zip deleted")

    config = lambda_client.get_function_configuration(FunctionName=function_name)
    if config["CodeSha256"] != expected:
        raise DeployError(
            f"{function_name} reports code {config['CodeSha256']}, but the local archive "
            f"is {expected}. The update did not deploy this build."
        )
    if config["LastUpdateStatus"] != "Successful":
        raise DeployError(
            f"{function_name} update status is {config['LastUpdateStatus']}: "
            f"{config.get('LastUpdateStatusReason', 'no reason given')}"
        )
    return config["LastModified"]


def main(argv: list[str] | None = None) -> int:
    """Deploy the requested functions; see the module docstring for behaviour."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # No default here: with nargs="*", argparse checks a list default against `choices`
    # as a whole and rejects it, so the default is applied after parsing instead.
    parser.add_argument(
        "targets",
        nargs="*",
        choices=sorted(TARGETS),
        help=f"Functions to deploy (default: {_DEFAULT_TARGET}).",
    )
    parser.add_argument("--profile", help="AWS profile; see the module docstring.")
    args = parser.parse_args(argv)
    targets: list[str] = args.targets or [_DEFAULT_TARGET]

    try:
        outputs = read_terraform_outputs()
        archives = {target: TARGETS[target][1] for target in targets}
        absent = [str(path) for path in archives.values() if not path.is_file()]
        if absent:
            raise DeployError(
                f"Archive not found: {absent}. Run `uv run python "
                "scripts/build_lambda.py` first."
            )

        profile = resolve_profile(args.profile)
        session = boto3.Session(profile_name=profile, region_name=outputs.region)
        account = session.client("sts").get_caller_identity()["Account"]
        if account != outputs.account_id:
            raise DeployError(
                f"Credentials (profile {profile or 'default chain'}) are for account "
                f"{account}, but Terraform manages {outputs.account_id}. Refusing to "
                "deploy."
            )

        s3 = session.client("s3", config=_CLIENT_CONFIG)
        lambda_client = session.client("lambda", config=_CLIENT_CONFIG)
        for target, archive in archives.items():
            function_name = outputs.function_names[target]
            print(f"=== {target}: {function_name} ===")
            modified = deploy_function(
                s3, lambda_client, outputs.bucket, function_name, archive
            )
            print(f"  deployed and verified; LastModified {modified}")
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
