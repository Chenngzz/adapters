#!/usr/bin/env bash
# Oracle: apply the upstream reference fix (git diff base_commit..reference_commit)
# to the agent workspace. Same apply rule as upstream reset_workspace(): plain
# `git apply`, retried without image files if that fails.
set -euo pipefail
cd /workspace/product
git config --global --add safe.directory /workspace/product
patch=/solution/reference.patch
if ! git apply "$patch"; then
  echo "git apply failed; retry excluding images" >&2
  git checkout -q -- .
  git apply \
    --exclude='*.png' --exclude='*.PNG' --exclude='*.jpg' --exclude='*.jpeg' \
    --exclude='*.JPG' --exclude='*.JPEG' --exclude='*.gif' --exclude='*.GIF' \
    --exclude='*.webp' --exclude='*.WEBP' --exclude='*.ico' --exclude='*.ICO' \
    --exclude='*.bmp' --exclude='*.BMP' "$patch"
fi
git status --short
