#!/usr/bin/env bash
set -euo pipefail

# setup-dispatch-user.sh — Create a dedicated dispatch user for running headless Claude agents.
# Run on the dispatch worker machine (e.g., pironman01). Requires sudo.
#
# Usage: sudo bash setup-dispatch-user.sh

DISPATCH_USER="dispatch"
DISPATCH_HOME="/home/$DISPATCH_USER"
WORKSPACE_DIR="$DISPATCH_HOME/workspace"
SSH_DIR="$DISPATCH_HOME/.ssh"
SERVICE_NAME="dispatch-api"
API_PORT=8100

# --- Colors ---
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
die()   { printf "${RED}[ERROR]${RESET} %s\n" "$1" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo bash setup-dispatch-user.sh

Creates a 'dispatch' system user for running headless Claude Code agents.

The script:
  - Creates the dispatch user (no sudo, no password, bash shell)
  - Generates an SSH keypair for git access
  - Creates workspace directory
  - Installs a systemd service for the dispatch API

After running:
  1. Add the printed public key to git@ubuntu-vm01's authorized_keys
  2. Copy the dispatch API code to /home/dispatch/agent-gtd-dispatch/
  3. Create /home/dispatch/.env with required environment variables
  4. Enable and start the service: systemctl enable --now dispatch-api
EOF
    exit 0
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage
[[ $EUID -ne 0 ]] && die "This script must be run as root (use sudo)."

# --- Create dispatch user (idempotent) ---
if ! id -u "$DISPATCH_USER" &>/dev/null; then
    adduser --system --shell /bin/bash --group --home "$DISPATCH_HOME" \
        --gecos "Dispatch worker for Agent GTD" "$DISPATCH_USER"
    info "Created '$DISPATCH_USER' user"
else
    info "'$DISPATCH_USER' user already exists"
fi

# Ensure the user has NO sudo access
if groups "$DISPATCH_USER" 2>/dev/null | grep -q sudo; then
    die "'$DISPATCH_USER' is in the sudo group — this is not allowed. Remove it first."
fi

# --- Create directories ---
mkdir -p "$WORKSPACE_DIR" "$SSH_DIR"
chmod 700 "$SSH_DIR"
info "Directories ready"

# --- Generate SSH keypair (idempotent) ---
KEY_PATH="$SSH_DIR/id_ed25519"
if [[ -f "$KEY_PATH" ]]; then
    warn "SSH key already exists at $KEY_PATH"
else
    ssh-keygen -t ed25519 -C "${DISPATCH_USER}@$(hostname)" -f "$KEY_PATH" -N ""
    info "Generated SSH keypair"
fi

# --- Fix ownership ---
chown -R "$DISPATCH_USER:$DISPATCH_USER" "$DISPATCH_HOME"

# --- Install systemd service ---
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Agent GTD Dispatch API
After=network.target

[Service]
Type=simple
User=$DISPATCH_USER
Group=$DISPATCH_USER
WorkingDirectory=$DISPATCH_HOME/agent-gtd-dispatch
EnvironmentFile=$DISPATCH_HOME/.env
ExecStart=/usr/local/bin/uv run uvicorn agent_gtd_dispatch.main:app --host 0.0.0.0 --port $API_PORT
Restart=on-failure
RestartSec=5

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$WORKSPACE_DIR $DISPATCH_HOME/agent-gtd-dispatch
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
info "Installed systemd service: $SERVICE_NAME"

# --- Summary ---
echo ""
echo "========================================"
echo "  Dispatch user setup complete"
echo "========================================"
echo ""
echo "  User:       $DISPATCH_USER"
echo "  Home:       $DISPATCH_HOME"
echo "  Workspace:  $WORKSPACE_DIR"
echo "  API port:   $API_PORT"
echo ""
echo "SSH public key (add to git@ubuntu-vm01 authorized_keys):"
echo ""
cat "${KEY_PATH}.pub"
echo ""
echo "Next steps:"
echo "  1. Add the public key above to git@ubuntu-vm01:"
echo "     sudo bash setup-git-server.sh ${KEY_PATH}.pub"
echo "  2. Clone the dispatch repo:"
echo "     sudo -u $DISPATCH_USER git clone git@ubuntu-vm01:repos/agent-gtd-dispatch $DISPATCH_HOME/agent-gtd-dispatch"
echo "  3. Create $DISPATCH_HOME/.env (see .env.example)"
echo "  4. Install Claude Code for the dispatch user"
echo "  5. Start the service: systemctl enable --now $SERVICE_NAME"
echo ""
