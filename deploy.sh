#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Deploy the latest agent-gtd-dispatch to one or more hosts.
#
# Pulls main, syncs deps, and restarts the service on each host in sequence.
# Called by release.sh after cutting a version tag.
#
# Environment variables:
#   DISPATCH_HOSTS  Space-separated SSH targets (default: "pironman01 r7-research ubuntu-pi-01")
#   DISPATCH_HOST   Single SSH target — if set, overrides DISPATCH_HOSTS (back-compat)
#   SERVICE_USER    Service account owning the working copy (default: dispatch-svc)
#   SERVICE_NAME    Systemd service unit name (default: dispatch-api)
#   REPO_DIR        Working copy path on the host (default: /home/dispatch-svc/agent-gtd-dispatch)
#
# Exit code: 0 if every host succeeded. Non-zero on first failure (other hosts skipped).

if [ -n "${DISPATCH_HOST:-}" ]; then
    HOSTS="${DISPATCH_HOST}"
else
    HOSTS="${DISPATCH_HOSTS:-pironman01 r7-research ubuntu-pi-01}"
fi
SERVICE_USER="${SERVICE_USER:-dispatch-svc}"
SERVICE_NAME="${SERVICE_NAME:-dispatch-api}"
REPO_DIR="${REPO_DIR:-/home/${SERVICE_USER}/agent-gtd-dispatch}"

deploy_one() {
    local host="$1"
    echo
    echo "########## ${host} ##########"

    ssh "${host}" bash -s <<EOF
set -euo pipefail

# Pull latest main
sudo -u ${SERVICE_USER} git -C ${REPO_DIR} pull origin main

# Sync dependencies
sudo -u ${SERVICE_USER} bash -c "cd ${REPO_DIR} && /home/${SERVICE_USER}/.local/bin/uv sync"

# Restart the service
sudo systemctl restart ${SERVICE_NAME}

# Quick health probe — retry loop (up to 30s for slower hosts)
_ok=0
for _i in \$(seq 1 30); do
    if curl -sf --max-time 1 http://localhost:8100/health >/dev/null 2>&1; then
        _ok=1
        break
    fi
    if [ \$_i -lt 30 ]; then
        sleep 1
    fi
done

if [ \$_ok -eq 1 ]; then
    echo "[OK]   Service is healthy"
else
    echo "[WARN] Health check failed after restart — check: journalctl -u ${SERVICE_NAME} -n 50" >&2
    exit 1
fi

echo "Deploy complete."
EOF
}

for host in ${HOSTS}; do
    deploy_one "${host}"
done

echo
echo "All hosts deployed."
