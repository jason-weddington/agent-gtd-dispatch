#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Deploy the latest agent-gtd-dispatch wheel to one or more hosts.
#
# Runs `uv tool install --force agent-gtd-dispatch --index <homelab>` as the
# service user on each host, then restarts the systemd unit and probes /health.
# Hosts consume the wheel published to the homelab index (pi-04 pypi.lab) by
# release.sh; no working copy on the host, no source-tree state.
#
# The homelab index is passed via `--index` (NOT `--index-url`) so it is
# ADDED to the default PyPI list; uv resolves fastapi/uvicorn/anthropic/etc.
# from public PyPI while pulling agent-gtd-dispatch and agent-gtd-dispatch-protocol
# from the homelab. Both wheels must already be on the index; release.sh publishes
# them before this script runs.
#
# Environment variables:
#   DISPATCH_HOSTS  Space-separated SSH targets (default: "pironman01 r7-research r7-server")
#   DISPATCH_HOST   Single SSH target — if set, overrides DISPATCH_HOSTS (back-compat)
#   SERVICE_USER    Service account owning the tool install (default: dispatch-svc)
#   AGENT_USER      Agent subprocess user whose Claude Code and lefthook are refreshed (default: dispatch)
#   SERVICE_NAME    Systemd service unit name (default: dispatch-api)
#   DISPATCH_INDEX  Homelab wheel index URL (default: https://pypi.lab.jasonweddington.com/simple/)
#
# Exit code: 0 if every host succeeded. Non-zero on first failure (other hosts skipped).

if [ -n "${DISPATCH_HOST:-}" ]; then
    HOSTS="${DISPATCH_HOST}"
else
    HOSTS="${DISPATCH_HOSTS:-pironman01 r7-research r7-server}"
fi
SERVICE_USER="${SERVICE_USER:-dispatch-svc}"
AGENT_USER="${AGENT_USER:-dispatch}"
SERVICE_NAME="${SERVICE_NAME:-dispatch-api}"
DISPATCH_INDEX="${DISPATCH_INDEX:-https://pypi.lab.jasonweddington.com/simple/}"

deploy_one() {
    local host="$1"
    echo
    echo "########## ${host} ##########"

    ssh "${host}" bash -s <<EOF
set -euo pipefail

# Install/refresh the wheel from the homelab index.
# -H sets HOME=/home/${SERVICE_USER} so uv installs the tool under the service
# user's ~/.local (not root's HOME); without it the entry point lands in the
# wrong place and the systemd ExecStart path is missing.
sudo -u ${SERVICE_USER} -H /home/${SERVICE_USER}/.local/bin/uv tool install --force agent-gtd-dispatch --index ${DISPATCH_INDEX}

# Gate: uv tool list must show agent-gtd-dispatch after the install.
if ! sudo -u ${SERVICE_USER} -H /home/${SERVICE_USER}/.local/bin/uv tool list | grep -q '^agent-gtd-dispatch'; then
    echo "[ERR]  uv tool list does not show agent-gtd-dispatch after install" >&2
    exit 1
fi

# Refresh the agent user's Claude Code. Headless 'claude -p' runs never
# self-update, so without this the fleet silently drifts (hosts were found
# ~125 releases behind). Non-fatal: a failed update keeps the old binary.
if ! sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/claude update >/dev/null 2>&1; then
    echo "[WARN] claude update failed for ${AGENT_USER} — agent keeps its current version" >&2
fi
echo "[OK]   Claude Code (${AGENT_USER}): \$(sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/claude --version)"

# Install/refresh lefthook for the agent user. Repos that use lefthook.yml
# (e.g. harness-design) cannot activate their hooks without it. Non-fatal:
# install --upgrade installs when absent and upgrades when present; a failure
# keeps whatever lefthook (if any) the agent already has.
if ! LH_OUT=\$(sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/uv tool install --upgrade lefthook 2>&1); then
    echo "[WARN] lefthook install/upgrade failed for ${AGENT_USER} — agent keeps its current lefthook (if any). Last output:" >&2
    printf '%s\n' "\$LH_OUT" | tail -n 5 | sed 's/^/[WARN]   /' >&2
fi

if LH_VER=\$(sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/lefthook version 2>/dev/null); then
    echo "[OK]   lefthook (${AGENT_USER}): \${LH_VER}"
else
    echo "[WARN] lefthook not runnable for ${AGENT_USER} — lefthook repos cannot activate hooks on this host" >&2
fi

# Restart the service so systemd runs the freshly-installed entry point.
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
