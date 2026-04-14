#!/usr/bin/env bash
set -euo pipefail

# setup-git-server.sh — Create a dedicated git user with git-shell for hosting bare repos.
# Reusable: run on any machine where you want to host bare repos.
#
# Usage: sudo bash setup-git-server.sh <key1.pub> [key2.pub ...]

REPOS_DIR="/home/git/repos"
SSH_DIR="/home/git/.ssh"
SHELL_CMDS_DIR="/home/git/git-shell-commands"
AUTHORIZED_KEYS="$SSH_DIR/authorized_keys"

# --- Colors (when stdout is a terminal) ---
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    RESET='\033[0m'
else
    GREEN='' YELLOW='' RED='' RESET=''
fi

info()  { printf "${GREEN}[OK]${RESET} %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${RESET} %s\n" "$1"; }
error() { printf "${RED}[ERROR]${RESET} %s\n" "$1" >&2; }
die()   { error "$1"; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo bash setup-git-server.sh <key1.pub> [key2.pub ...]

Creates a dedicated 'git' system user with git-shell for hosting bare repos.

Arguments:
  key1.pub ...   One or more SSH public key files to authorize.

The script is idempotent — safe to re-run. It will:
  - Create the 'git' user (or verify its shell if it exists)
  - Create /home/git/repos/ for bare repositories
  - Install authorized_keys with restricted SSH options
  - Install git-shell-commands: create, list, no-interactive-login
EOF
    exit 0
}

# --- Argument parsing ---
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage
[[ $# -lt 1 ]] && die "At least one public key file is required.\n\nUsage: sudo bash setup-git-server.sh <key1.pub> [key2.pub ...]"

# --- Prerequisite checks ---
[[ $EUID -ne 0 ]] && die "This script must be run as root (use sudo)."

GIT_SHELL=$(command -v git-shell 2>/dev/null || true)
[[ -z "$GIT_SHELL" ]] && die "git-shell not found. Install git-core: apt install git-core"

# Ensure git-shell is in /etc/shells (required for user creation with this shell)
if ! grep -qxF "$GIT_SHELL" /etc/shells 2>/dev/null; then
    echo "$GIT_SHELL" >> /etc/shells
    info "Added $GIT_SHELL to /etc/shells"
fi

# Validate public key files before doing anything
for keyfile in "$@"; do
    [[ ! -f "$keyfile" ]] && die "Key file not found: $keyfile"
    if ! head -1 "$keyfile" | grep -qE '^(ssh-|ecdsa-|sk-)'; then
        die "File does not look like a public key: $keyfile"
    fi
done

# --- Create git user (idempotent) ---
if ! id -u git &>/dev/null; then
    adduser --system --shell "$GIT_SHELL" --group --home /home/git --gecos "Git repository hosting" git
    info "Created 'git' user with git-shell"
else
    current_shell=$(getent passwd git | cut -d: -f7)
    if [[ "$current_shell" != "$GIT_SHELL" ]]; then
        chsh -s "$GIT_SHELL" git
        info "Updated 'git' user shell to $GIT_SHELL"
    else
        info "'git' user already exists with correct shell"
    fi
fi

# --- Create directory structure ---
mkdir -p "$REPOS_DIR" "$SSH_DIR" "$SHELL_CMDS_DIR"
chmod 700 "$SSH_DIR"
info "Directory structure ready"

# --- Install authorized_keys ---
touch "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"

KEY_OPTIONS="no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"

for keyfile in "$@"; do
    key_content=$(cat "$keyfile")
    # Extract just the key blob (type + base64) for dedup, ignoring comments
    key_blob=$(awk '{print $1, $2}' <<< "$key_content")

    if grep -qF "$key_blob" "$AUTHORIZED_KEYS" 2>/dev/null; then
        warn "Key already authorized (skipped): $keyfile"
    else
        echo "$KEY_OPTIONS $key_content" >> "$AUTHORIZED_KEYS"
        info "Authorized key: $keyfile"
    fi
done

# --- Install git-shell-commands ---

# no-interactive-login: blocks interactive SSH sessions
cat > "$SHELL_CMDS_DIR/no-interactive-login" <<'SCRIPT'
#!/bin/sh
printf '%s\n' "Hi $USER! You've successfully authenticated, but interactive shell access is not provided."
exit 128
SCRIPT

# create: initialize a new bare repo
cat > "$SHELL_CMDS_DIR/create" <<'SCRIPT'
#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
    echo "Usage: create <repo-name>"
    exit 1
fi

REPO="$1"

# Sanitize: only allow alphanumeric, hyphens, underscores, dots
if ! printf '%s' "$REPO" | grep -qE '^[a-zA-Z0-9._-]+$'; then
    echo "Error: Invalid repository name '$REPO'"
    echo "  Allowed characters: letters, digits, hyphens, underscores, dots"
    exit 1
fi

REPO_PATH="$HOME/repos/${REPO}"

if [ -d "$REPO_PATH" ]; then
    echo "Repository '$REPO' already exists at repos/$REPO"
    exit 0
fi

git init --bare --initial-branch=main "$REPO_PATH"
echo "Created bare repository: repos/$REPO"
SCRIPT

# list: show available repos
cat > "$SHELL_CMDS_DIR/list" <<'SCRIPT'
#!/bin/sh
echo "Available repositories:"
ls -1 "$HOME/repos/" 2>/dev/null || echo "  (none)"
SCRIPT

chmod +x "$SHELL_CMDS_DIR"/*
info "Installed git-shell-commands: create, list, no-interactive-login"

# --- Fix ownership ---
chown -R git:git /home/git
info "Ownership set to git:git"

# --- Summary ---
echo ""
echo "========================================"
echo "  Git server setup complete"
echo "========================================"
echo ""
echo "  User:    git"
echo "  Shell:   $GIT_SHELL"
echo "  Repos:   $REPOS_DIR"
echo "  Keys:    $(grep -c '' "$AUTHORIZED_KEYS") authorized"
echo ""
echo "Test from a client machine:"
echo "  ssh git@$(hostname) list"
echo "  ssh git@$(hostname) create test-repo"
echo ""
