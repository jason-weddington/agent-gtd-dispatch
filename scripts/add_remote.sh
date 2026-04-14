#!/usr/bin/env bash
set -euo pipefail

# add_remote.sh — Create a bare repo on the git server and add it as a remote.
#
# Usage: add_remote.sh [remote-name]

GIT_HOST="${GIT_HOST:-ubuntu-vm01}"
GIT_USER="${GIT_USER:-git}"

usage() {
    cat <<EOF
Usage: add_remote.sh [remote-name]

Creates a bare repo on the git server and adds it as a remote.
Run from within a git repository.

Arguments:
  remote-name   Name for the remote (default: origin)

Environment:
  GIT_HOST  Override server hostname (default: ubuntu-vm01)
  GIT_USER  Override server user (default: git)
EOF
    exit 0
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

# Must be in a git repo
if ! git rev-parse --git-dir &>/dev/null; then
    echo "Error: Not a git repository." >&2
    exit 1
fi

REPO_NAME=$(basename "$PWD")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
REMOTE_NAME="${1:-origin}"

# Check if remote already exists
if git remote get-url "$REMOTE_NAME" &>/dev/null; then
    echo "Error: Remote '$REMOTE_NAME' already exists." >&2
    echo "  URL: $(git remote get-url "$REMOTE_NAME")" >&2
    exit 1
fi

# Create the bare repo on the server via git-shell-command
echo "Creating bare repository on ${GIT_HOST}..."
ssh "${GIT_USER}@${GIT_HOST}" create "$REPO_NAME"

# Add the remote
REMOTE_URL="${GIT_USER}@${GIT_HOST}:repos/${REPO_NAME}"
echo "Adding remote '$REMOTE_NAME': $REMOTE_URL"
git remote add "$REMOTE_NAME" "$REMOTE_URL"

# Push and set upstream
echo "Pushing branch '$BRANCH' to $REMOTE_NAME..."
git push --set-upstream "$REMOTE_NAME" "$BRANCH"

echo "Done."
