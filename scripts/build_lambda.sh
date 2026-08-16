#!/usr/bin/env bash
# Build function.zip for Lambda deployment.
# Run from the project root: bash scripts/build_lambda.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGE_DIR="$ROOT/package"
ZIP_FILE="$ROOT/function.zip"

echo "Cleaning previous build..."
rm -rf "$PACKAGE_DIR" "$ZIP_FILE"

echo "Exporting dependencies..."
uv export --no-dev --no-hashes -o "$ROOT/requirements.txt"

echo "Installing dependencies into package/..."
pip install -r "$ROOT/requirements.txt" --target "$PACKAGE_DIR" --upgrade --quiet

echo "Zipping dependencies..."
cd "$PACKAGE_DIR"
zip -r "$ZIP_FILE" . --quiet

echo "Adding source code..."
cd "$ROOT"
zip -r "$ZIP_FILE" src/ --quiet

echo "Cleaning up..."
rm -rf "$PACKAGE_DIR" "$ROOT/requirements.txt"

echo "Done: $ZIP_FILE ($(du -sh "$ZIP_FILE" | cut -f1))"
