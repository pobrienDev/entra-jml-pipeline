#!/usr/bin/env bash
# Builds the zip that gets deployed to the Function app.
#
# The app's root is src/. Two things from outside it are added: the
# department map (so the deployed code reads the same file the repo and
# Terraform do) and requirements.txt (so Azure installs the pinned
# dependencies during deployment).
#
# Usage: scripts/build_package.sh [output.zip]     default: dist/function-app.zip

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/dist/function-app.zip}"
case "$out" in /*) ;; *) out="$PWD/$out" ;; esac

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp -R "$root/src/." "$stage/"
mkdir -p "$stage/config"
cp "$root/config/department_groups.json" "$stage/config/"
cp "$root/requirements.txt" "$stage/"
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$stage" -name '.DS_Store' -delete

mkdir -p "$(dirname "$out")"
rm -f "$out"
(cd "$stage" && zip -qr "$out" .)
echo "$out"
