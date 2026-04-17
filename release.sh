#!/usr/bin/env bash
# Cut a release and publish it.
#
# During development: commit freely on main, push to origin, deploy liberally.
# When you hit a meaningful boundary — a coherent batch of work worth marking
# as a checkpoint — run this script. It will:
#   1. Bump the version via python-semantic-release (if there are feat/fix
#      commits since the last tag; no-op otherwise)
#   2. Push main + tags to BOTH origin and github
#   3. Deploy to the running service

set -euo pipefail

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "error: must be on main branch." >&2
  exit 1
fi

uv run semantic-release version --no-push --no-vcs-release
git push origin main --tags
git push github main --tags
./deploy.sh
