"""Tests for the S3-staged Lambda deploy script.

The script exists because a direct 47 MB update-function-code upload repeatedly stalled.
The properties worth pinning: it never deploys to an account Terraform does not manage,
it always cleans up the staged zip, and it only reports success when Lambda is running
exactly the local archive.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, call

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy_lambda.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_lambda", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before executing, as a normal import would: dataclasses resolve their
    # annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deploy = _load()

_OUTPUTS = {
    "artifacts_bucket_name": "artifacts-bucket",
    "aws_account_id": "111122223333",
    "aws_region": "ap-southeast-1",
    "lambda_function_name": "BotFn",
    "chart_lambda_function_name": "ChartFn",
}


def _terraform_json(outputs: dict[str, str]) -> str:
    return json.dumps({name: {"value": value} for name, value in outputs.items()})


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "function.zip"
    path.write_bytes(b"pretend zip contents")
    return path


def _lambda_client(code_sha: str, status: str = "Successful") -> MagicMock:
    client = MagicMock()
    client.get_function_configuration.return_value = {
        "CodeSha256": code_sha,
        "LastUpdateStatus": status,
        "LastModified": "2026-10-09T08:00:00.000+0000",
    }
    return client


# ── resolve_profile ──────────────────────────────────────────────────────────


def test_cli_profile_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    assert deploy.resolve_profile("from-cli") == "from-cli"


def test_env_profile_beats_tfvars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tfvars = tmp_path / "local.auto.tfvars"
    tfvars.write_text('aws_profile = "from-tfvars"\n', encoding="utf-8")
    monkeypatch.setattr(deploy, "LOCAL_TFVARS", tfvars)
    monkeypatch.setenv("AWS_PROFILE", "from-env")

    assert deploy.resolve_profile(None) == "from-env"


def test_tfvars_profile_is_read_as_terraform_writes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tfvars = tmp_path / "local.auto.tfvars"
    tfvars.write_text(
        '# comment\naws_account_id = "111122223333"\n  aws_profile    = "work-tf"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "LOCAL_TFVARS", tfvars)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    assert deploy.resolve_profile(None) == "work-tf"


def test_no_profile_anywhere_uses_the_default_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy, "LOCAL_TFVARS", tmp_path / "absent.tfvars")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    assert deploy.resolve_profile(None) is None


# ── read_terraform_outputs ───────────────────────────────────────────────────


def test_outputs_are_read_from_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(return_value=MagicMock(stdout=_terraform_json(_OUTPUTS)))
    monkeypatch.setattr(deploy.subprocess, "run", run)

    outputs = deploy.read_terraform_outputs()

    assert outputs.bucket == "artifacts-bucket"
    assert outputs.account_id == "111122223333"
    assert outputs.function_names == {"bot": "BotFn", "charts": "ChartFn"}


def test_missing_outputs_point_at_terraform_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = {k: v for k, v in _OUTPUTS.items() if k != "artifacts_bucket_name"}
    run = MagicMock(return_value=MagicMock(stdout=_terraform_json(partial)))
    monkeypatch.setattr(deploy.subprocess, "run", run)

    with pytest.raises(deploy.DeployError, match="terraform apply"):
        deploy.read_terraform_outputs()


def test_failed_terraform_output_points_at_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = subprocess.CalledProcessError(1, "terraform", stderr="no state")
    monkeypatch.setattr(deploy.subprocess, "run", MagicMock(side_effect=failure))

    with pytest.raises(deploy.DeployError, match="terraform init"):
        deploy.read_terraform_outputs()


# ── deploy_function ──────────────────────────────────────────────────────────


def test_code_sha256_matches_lambdas_encoding(archive: Path) -> None:
    expected = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest())
    assert deploy.code_sha256(archive) == expected.decode()


def test_deploy_uploads_updates_waits_verifies_and_deletes(archive: Path) -> None:
    s3 = MagicMock()
    lambda_client = _lambda_client(deploy.code_sha256(archive))

    modified = deploy.deploy_function(s3, lambda_client, "bkt", "BotFn", archive)

    key = s3.upload_file.call_args.args[2]
    assert s3.upload_file.call_args.args[:2] == (str(archive), "bkt")
    lambda_client.update_function_code.assert_called_once_with(
        FunctionName="BotFn", S3Bucket="bkt", S3Key=key
    )
    lambda_client.get_waiter.assert_called_once_with("function_updated_v2")
    assert s3.delete_object.call_args == call(Bucket="bkt", Key=key)
    assert modified == "2026-10-09T08:00:00.000+0000"


def test_staged_zip_is_deleted_even_when_the_update_fails(archive: Path) -> None:
    s3 = MagicMock()
    lambda_client = _lambda_client(deploy.code_sha256(archive))
    lambda_client.update_function_code.side_effect = RuntimeError("update rejected")

    with pytest.raises(RuntimeError, match="update rejected"):
        deploy.deploy_function(s3, lambda_client, "bkt", "BotFn", archive)

    s3.delete_object.assert_called_once()


def test_a_mismatched_checksum_is_not_reported_as_success(archive: Path) -> None:
    # Lambda running anything other than this build — a stale upload, the wrong key —
    # must fail the deploy rather than print "verified".
    lambda_client = _lambda_client("c29tZXRoaW5nIGVsc2U=")

    with pytest.raises(deploy.DeployError, match="did not deploy this build"):
        deploy.deploy_function(MagicMock(), lambda_client, "bkt", "BotFn", archive)


def test_a_failed_update_status_is_not_reported_as_success(archive: Path) -> None:
    lambda_client = _lambda_client(deploy.code_sha256(archive), status="Failed")

    with pytest.raises(deploy.DeployError, match="Failed"):
        deploy.deploy_function(MagicMock(), lambda_client, "bkt", "BotFn", archive)


# ── main ─────────────────────────────────────────────────────────────────────


def _patch_main(
    monkeypatch: pytest.MonkeyPatch, archive: Path, account: str
) -> dict[str, Any]:
    monkeypatch.setattr(
        deploy,
        "read_terraform_outputs",
        lambda: deploy.TerraformOutputs(
            bucket="bkt",
            account_id="111122223333",
            region="ap-southeast-1",
            function_names={"bot": "BotFn", "charts": "ChartFn"},
        ),
    )
    monkeypatch.setitem(deploy.TARGETS, "bot", ("lambda_function_name", archive))
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": account}
    monkeypatch.setattr(deploy.boto3, "Session", MagicMock(return_value=session))
    deployed: dict[str, Any] = {}

    def _record(*args: Any) -> str:
        deployed["args"] = args
        return "2026-10-09T08:00:00.000+0000"

    monkeypatch.setattr(deploy, "deploy_function", _record)
    return deployed


def test_main_refuses_credentials_for_another_account(
    monkeypatch: pytest.MonkeyPatch, archive: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The machine's default profile is a different account; a forgotten --profile must
    # stop here rather than deploy, or attempt to, somewhere else.
    deployed = _patch_main(monkeypatch, archive, account="999999999999")

    assert deploy.main(["bot", "--profile", "work"]) == 1
    assert "Refusing to deploy" in capsys.readouterr().err
    assert deployed == {}


def test_main_deploys_when_the_account_matches(
    monkeypatch: pytest.MonkeyPatch, archive: Path
) -> None:
    deployed = _patch_main(monkeypatch, archive, account="111122223333")

    assert deploy.main(["bot", "--profile", "personal-tf"]) == 0
    assert deployed["args"][2:] == ("bkt", "BotFn", archive)


def test_main_with_no_targets_deploys_the_bot(
    monkeypatch: pytest.MonkeyPatch, archive: Path
) -> None:
    # The bare command is the everyday one; argparse rejected its own list default
    # before this was applied after parsing instead.
    deployed = _patch_main(monkeypatch, archive, account="111122223333")

    assert deploy.main([]) == 0
    assert deployed["args"][2:] == ("bkt", "BotFn", archive)


def test_main_stops_when_an_archive_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deployed = _patch_main(monkeypatch, tmp_path / "absent.zip", account="111122223333")

    assert deploy.main(["bot"]) == 1
    assert "build_lambda.py" in capsys.readouterr().err
    assert deployed == {}
