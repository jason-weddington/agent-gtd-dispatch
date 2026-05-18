#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Deploy the latest agent-gtd-dispatch to the dispatch host.
#
# Pulls main, syncs deps, and restarts the service on the dispatch host.
# Called by release.sh after cutting a version tag.
#
# Environment variables:
#   DISPATCH_HOST   SSH target (default: pironman01)
#   SERVICE_USER    Service account owning the working copy (default: dispatch-svc)
#   SERVICE_NAME    Systemd service unit name (default: dispatch-api)
#   REPO_DIR        Working copy path on the host (default: /home/dispatch-svc/agent-gtd-dispatch)

DISPATCH_HOST="${DISPATCH_HOST:-pironman01}"
SERVICE_USER="${SERVICE_USER:-dispatch-svc}"
SERVICE_NAME="${SERVICE_NAME:-dispatch-api}"
REPO_DIR="${REPO_DIR:-/home/${SERVICE_USER}/agent-gtd-dispatch}"

echo "Deploying to ${DISPATCH_HOST} (${SERVICE_USER}:${REPO_DIR})..."

ssh "${DISPATCH_HOST}" bash -s <<EOF
set -euo pipefail

# Pull latest main
sudo -u ${SERVICE_USER} git -C ${REPO_DIR} pull origin main

# Sync dependencies
sudo -u ${SERVICE_USER} bash -c "cd ${REPO_DIR} && /home/${SERVICE_USER}/.local/bin/uv sync"

# Restart the service
sudo systemctl restart ${SERVICE_NAME}

# Quick health probe
sleep 2
if curl -sf --max-time 5 http://localhost:8100/health >/dev/null; then
    echo "[OK]   Service is healthy"
else
    echo "[WARN] Health check failed after restart — check: journalctl -u ${SERVICE_NAME} -n 50" >&2
    exit 1
fi

echo "Deploy complete."
EOF
