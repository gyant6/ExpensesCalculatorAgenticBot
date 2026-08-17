"""Build `function.zip`, the deployment artefact for the Lambda webhook handler.

Run from anywhere: `uv run python scripts/build_lambda.py`

Lambda has no package manager at runtime. It unpacks the zip to `/var/task` and puts
that directory on `sys.path`, so every third-party dependency has to be present in the
archive as files. This script assembles that archive:

    uv export      pinned production dependencies, straight from uv.lock
    uv pip install those dependencies into package/, built for the Lambda runtime
    zip            package/ at the archive root, then src/ alongside it

The layout is what makes imports resolve once deployed: dependencies at the root so
`import telegram` finds `/var/task/telegram/`, and the application under `src/` so the
configured handler `src.bot.main.lambda_handler` resolves.

Dependencies are resolved for the Lambda runtime rather than the machine running this
script, so a build on Windows or macOS produces the same Linux artefact as a build on
the CI runner. Wheels for the target platform cannot be compiled locally, so
`--only-binary` makes a package that publishes no matching wheel fail loudly here
instead of at cold start in production.

Exit codes:
    0: the archive was written and is within Lambda's direct-upload limit.
    1: the archive was written but exceeds a Lambda size limit, or a build step failed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "package"
ZIP_FILE = ROOT / "function.zip"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
SOURCE_DIR = ROOT / "src"

# Must match `runtime` and `architectures` on the aws_lambda_function resource in
# terraform/main.tf. A mismatch surfaces as an ImportError at cold start, because the
# compiled extensions in packages like pydantic-core and numpy are built per Python
# version and per architecture.
LAMBDA_PYTHON_VERSION = "3.13"
LAMBDA_PLATFORM = "x86_64-unknown-linux-gnu"

# Uploading a zip directly — whether by `terraform apply` or `aws lambda
# update-function-code --zip-file` — is capped at 50 MB. Larger artefacts have to be
# staged in S3 and referenced by bucket and key. The unzipped cap of 250 MB covers the
# archive plus anything Lambda layers add.
DIRECT_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
UNZIPPED_LIMIT_BYTES = 250 * 1024 * 1024

# Byte-compiled caches belong to the interpreter that produced them, so shipping this
# machine's would be useless at best. Lambda regenerates what it needs.
EXCLUDED_DIR_NAMES = frozenset({"__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})

# Lambda runs as a non-root user and refuses to import files it cannot read. Python's
# zipfile records the host filesystem's mode, which on Windows carries no useful POSIX
# permissions, so every entry is stamped explicitly instead.
FILE_MODE = 0o644


def _run(*args: str) -> None:
    """Run a command from the project root, raising if it fails.

    Args:
        *args: The command and its arguments, passed without a shell.

    Raises:
        subprocess.CalledProcessError: If the command exits non-zero.
        FileNotFoundError: If the executable is not on PATH.
    """
    print(f"  $ {' '.join(args)}")
    subprocess.run(args, check=True, cwd=ROOT)


def _iter_files(source_root: Path) -> list[Path]:
    """List every file under a directory that belongs in the archive.

    Args:
        source_root: Directory to walk.

    Returns:
        Paths of the files to archive, sorted so the archive is byte-stable between
        builds of identical inputs.
    """
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and path.suffix not in EXCLUDED_SUFFIXES
        and EXCLUDED_DIR_NAMES.isdisjoint(path.relative_to(source_root).parts)
    )


def _add_tree(archive: zipfile.ZipFile, source_root: Path, arc_root: str = "") -> int:
    """Copy a directory tree into an open archive.

    Entries are written with a fixed timestamp and an explicit POSIX mode rather than
    the ones on disk, so the same inputs produce the same archive on any host.

    Args:
        archive: The open archive to write into.
        source_root: Directory whose contents are copied.
        arc_root: Path prefix inside the archive. Empty places contents at the root.

    Returns:
        Total uncompressed size in bytes of the entries written.

    Raises:
        OSError: If a file cannot be read.
    """
    total_bytes = 0
    for path in _iter_files(source_root):
        relative = path.relative_to(source_root).as_posix()
        arcname = f"{arc_root}/{relative}" if arc_root else relative
        # Fixed date rather than the file's mtime, so rebuilding unchanged inputs does
        # not produce a different archive. 1980-01-01 is the earliest a zip can encode.
        info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = FILE_MODE << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        data = path.read_bytes()
        archive.writestr(info, data)
        total_bytes += len(data)
    return total_bytes


def _format_size(num_bytes: int) -> str:
    """Render a byte count as megabytes to one decimal place."""
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def main() -> int:
    """Build the deployment archive and report its size against Lambda's limits.

    Returns:
        0 if the archive fits within Lambda's direct-upload limit, 1 if it exceeds
        either the direct-upload or the unzipped limit.

    Raises:
        subprocess.CalledProcessError: If dependency export or installation fails.
        FileNotFoundError: If `uv` is not on PATH.
    """
    print("Cleaning previous build...")
    shutil.rmtree(PACKAGE_DIR, ignore_errors=True)
    ZIP_FILE.unlink(missing_ok=True)
    REQUIREMENTS_FILE.unlink(missing_ok=True)

    try:
        print("Exporting production dependencies from uv.lock...")
        _run(
            "uv",
            "export",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "-o",
            str(REQUIREMENTS_FILE),
        )

        print(f"Installing for Python {LAMBDA_PYTHON_VERSION} on {LAMBDA_PLATFORM}...")
        _run(
            "uv",
            "pip",
            "install",
            "--requirements",
            str(REQUIREMENTS_FILE),
            "--target",
            str(PACKAGE_DIR),
            "--python-version",
            LAMBDA_PYTHON_VERSION,
            "--python-platform",
            LAMBDA_PLATFORM,
            "--only-binary",
            ":all:",
        )

        print("Writing archive...")
        with zipfile.ZipFile(ZIP_FILE, "w", zipfile.ZIP_DEFLATED) as archive:
            dependency_bytes = _add_tree(archive, PACKAGE_DIR)
            source_bytes = _add_tree(archive, SOURCE_DIR, arc_root=SOURCE_DIR.name)
    finally:
        shutil.rmtree(PACKAGE_DIR, ignore_errors=True)
        REQUIREMENTS_FILE.unlink(missing_ok=True)

    zipped_bytes = ZIP_FILE.stat().st_size
    unzipped_bytes = dependency_bytes + source_bytes

    print(f"\nWrote {ZIP_FILE.name}")
    print(f"  dependencies : {_format_size(dependency_bytes)} unzipped")
    print(f"  application  : {_format_size(source_bytes)} unzipped")
    print(f"  archive      : {_format_size(zipped_bytes)} zipped")

    exit_code = 0
    if zipped_bytes > DIRECT_UPLOAD_LIMIT_BYTES:
        print(
            f"\nERROR: {_format_size(zipped_bytes)} exceeds Lambda's "
            f"{_format_size(DIRECT_UPLOAD_LIMIT_BYTES)} direct-upload limit. "
            "Stage the archive in S3 and deploy it with --s3-bucket/--s3-key."
        )
        exit_code = 1
    if unzipped_bytes > UNZIPPED_LIMIT_BYTES:
        print(
            f"\nERROR: {_format_size(unzipped_bytes)} unzipped exceeds Lambda's "
            f"{_format_size(UNZIPPED_LIMIT_BYTES)} limit. Drop dependencies or move "
            "them into a layer."
        )
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
