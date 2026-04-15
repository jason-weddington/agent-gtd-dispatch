#!/usr/bin/env bash
# Enforce conventional commits only on main branch.
# Used as a commit-msg hook via pre-commit.
set -euo pipefail

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
    exit 0
fi

msg=$(head -1 "$1")
if ! echo "$msg" | grep -qE "^(feat|fix|chore|docs|refactor|test|style|perf|ci|build|revert)(\(.+\))?\!?: .+"; then
    echo "Bad commit message on main: $msg"
    echo "Must follow Conventional Commits: type(scope): description"
    exit 1
fi
