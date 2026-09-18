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
#   AGENT_USER      Agent subprocess user whose Claude Code, lefthook and dev toolchain
#                   (Rust tools + gitleaks, Step 4.9) are refreshed (default: dispatch)
#   SERVICE_NAME    Systemd service unit name (default: dispatch-api)
#   DISPATCH_INDEX  Homelab wheel index URL (default: https://pypi.lab.jasonweddington.com/simple/)
#   RUST_DEFAULT_TOOLCHAIN  Rust toolchain pointed at by 'rustup default' when the agent
#                   user has rustup but no usable default (default: stable)
#   PERSONAL_KB_HOOK_SRC  Source for the personal-kb-hook uv tool refresh (setup-
#                   dispatch-host.sh Step 4.10). Deliberately unpinned by default —
#                   see the PINNING ASYMMETRY note in Step 4.10 of setup-dispatch-host.sh.
#                   (default: git+ssh://git@ubuntu-vm01/home/git/repos/personal_kb#subdirectory=packages/personal-kb-hook)
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
RUST_DEFAULT_TOOLCHAIN="${RUST_DEFAULT_TOOLCHAIN:-stable}"
PERSONAL_KB_HOOK_SRC="${PERSONAL_KB_HOOK_SRC:-git+ssh://git@ubuntu-vm01/home/git/repos/personal_kb#subdirectory=packages/personal-kb-hook}"

# --- Dev toolchain data (single source of truth: templates/dev-toolchain.sh) ---
# deploy.sh runs LOCALLY from a repo checkout, so the tool list is sourced here and
# the resulting lists are interpolated into the unquoted ssh heredoc below (same
# mechanism as ${AGENT_USER}/${SERVICE_USER}). The list is deliberately NOT copied
# into this file — adding a tool stays a one-line change in templates/dev-toolchain.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLCHAIN_CONF="${SCRIPT_DIR}/templates/dev-toolchain.sh"
if [ -f "${TOOLCHAIN_CONF}" ]; then
    # shellcheck source=templates/dev-toolchain.sh
    . "${TOOLCHAIN_CONF}"
    # Entries contain a '|' separator, and these lists are interpolated into the
    # heredoc as literal SCRIPT TEXT (not as a runtime expansion), so a bare '|'
    # would be parsed as a pipe on the remote side. Single-quote each entry.
    DEV_TOOLCHAIN_PKG_LIST=""
    for _e in "${DEV_TOOLCHAIN_CARGO_PKGS[@]}"; do
        DEV_TOOLCHAIN_PKG_LIST="${DEV_TOOLCHAIN_PKG_LIST}'${_e}' "
    done
    GITLEAKS_ARCH_MAP_LIST=""
    for _e in "${GITLEAKS_ARCH_MAP[@]}"; do
        GITLEAKS_ARCH_MAP_LIST="${GITLEAKS_ARCH_MAP_LIST}'${_e}' "
    done
    # Substitute {version} locally; {arch} is resolved on each host from `uname -m`.
    GITLEAKS_URL_VERSIONED="${GITLEAKS_URL_TEMPLATE//\{version\}/${GITLEAKS_VERSION}}"
else
    echo "[WARN] ${TOOLCHAIN_CONF} not found — dev toolchain refresh will be skipped on every host" >&2
    DEV_TOOLCHAIN_PKG_LIST=""
    GITLEAKS_ARCH_MAP_LIST=""
    GITLEAKS_URL_VERSIONED=""
    GITLEAKS_VERSION=""
fi

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
#
# --refresh is load-bearing, NOT belt-and-braces: --force reinstalls the tool but
# resolves against uv's CACHED copy of the simple index, so a wheel published
# moments earlier by release.sh is invisible and the host silently keeps the
# version it already had. Observed 2026-09-18: 1.25.1 was on the index and all
# three hosts stayed on 1.25.0 while this script printed "All hosts deployed".
sudo -u ${SERVICE_USER} -H /home/${SERVICE_USER}/.local/bin/uv tool install --force --refresh agent-gtd-dispatch --index ${DISPATCH_INDEX}

# Gate: uv tool list must show agent-gtd-dispatch after the install.
if ! sudo -u ${SERVICE_USER} -H /home/${SERVICE_USER}/.local/bin/uv tool list | grep -q '^agent-gtd-dispatch'; then
    echo "[ERR]  uv tool list does not show agent-gtd-dispatch after install" >&2
    exit 1
fi

# Gate: when the caller states an expected version, the INSTALLED version must
# match it. "The service is healthy" says nothing about which code is running —
# a deploy that installs nothing is otherwise indistinguishable from success.
if [ -n "${EXPECT_VERSION:-}" ]; then
    _installed=\$(sudo -u ${SERVICE_USER} -H /home/${SERVICE_USER}/.local/bin/uv tool list \\
        | sed -n 's/^agent-gtd-dispatch v\\([0-9][0-9.]*\\).*/\\1/p' | head -n1)
    if [ "\$_installed" != "${EXPECT_VERSION}" ]; then
        echo "[ERR]  expected agent-gtd-dispatch ${EXPECT_VERSION} but \$_installed is installed" >&2
        exit 1
    fi
    echo "[OK]   agent-gtd-dispatch \$_installed (matches expected)"
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

# Refresh the agent user's dev toolchain (setup-dispatch-host.sh Step 4.9): the
# Rust tools and gitleaks that dispatched repos' hooks and gate commands call.
# The package list and the gitleaks pin come from templates/dev-toolchain.sh,
# sourced locally above and interpolated here — never hand-copied.
# Non-fatal throughout: a failed refresh leaves the agent on its current binaries
# and must not abort the deploy (only the health check below may exit non-zero).
_cargo="/home/${AGENT_USER}/.cargo/bin/cargo"
_rustup="/home/${AGENT_USER}/.cargo/bin/rustup"

# Self-heal: a host can have rustup installed with toolchains present but no
# default set (e.g. r7-research, 2026-09-17) — every cargo invocation then
# fails. If cargo is unusable but rustup exists, point the default at
# RUST_DEFAULT_TOOLCHAIN before deciding whether to skip the refresh below.
if ! sudo -u ${AGENT_USER} -H "\$_cargo" --version >/dev/null 2>&1 && [ -x "\$_rustup" ]; then
    if sudo -u ${AGENT_USER} -H "\$_rustup" default ${RUST_DEFAULT_TOOLCHAIN} >/dev/null 2>&1; then
        echo "[OK]   rust default toolchain HEALED for ${AGENT_USER} (was unusable) — set to ${RUST_DEFAULT_TOOLCHAIN}"
    fi
fi

if ! sudo -u ${AGENT_USER} -H "\$_cargo" --version >/dev/null 2>&1; then
    echo "[WARN] no usable rust default toolchain for ${AGENT_USER} — dev toolchain refresh skipped; run 'sudo ./setup-dispatch-host.sh' (Step 4.9) on this host" >&2
else
    _tc_failed=""
    for _entry in ${DEV_TOOLCHAIN_PKG_LIST}; do
        _crate="\${_entry%%|*}"
        _bin="\${_entry#*|}"
        if _TC_OUT=\$(sudo -u ${AGENT_USER} -H "\$_cargo" binstall -y "\$_crate" 2>&1); then
            echo "[OK]   \$_crate (\$_bin) refreshed for ${AGENT_USER}"
        else
            echo "[WARN] cargo binstall \$_crate failed for ${AGENT_USER} — agent keeps its current binary. Last output:" >&2
            printf '%s\n' "\$_TC_OUT" | tail -n 5 | sed 's/^/[WARN]   /' >&2
            _tc_failed="\${_tc_failed} \$_crate"
        fi
    done
    if [ -n "\$_tc_failed" ]; then
        echo "[WARN] Dev toolchain incomplete for ${AGENT_USER}:\${_tc_failed} — run 'sudo ./setup-dispatch-host.sh' to retry" >&2
    fi
fi

# gitleaks: re-install only when the installed binary differs from the pinned version.
_gl_dest="/home/${AGENT_USER}/.local/bin/gitleaks"
_gl_machine=\$(uname -m)
_gl_arch=""
for _map in ${GITLEAKS_ARCH_MAP_LIST}; do
    if [ "\${_map%%|*}" = "\$_gl_machine" ]; then
        _gl_arch="\${_map#*|}"
    fi
done
_gl_have=\$(sudo -u ${AGENT_USER} -H "\$_gl_dest" version 2>/dev/null | tr -d '[:space:]' | sed 's/^v//' || true)
if [ -z "${GITLEAKS_VERSION}" ]; then
    echo "[WARN] no gitleaks pin available (templates/dev-toolchain.sh missing) — skipping gitleaks refresh" >&2
elif [ -z "\$_gl_arch" ]; then
    echo "[WARN] unsupported architecture '\$_gl_machine' for gitleaks — agent keeps its current binary" >&2
elif [ "\$_gl_have" = "${GITLEAKS_VERSION}" ]; then
    echo "[OK]   gitleaks (${AGENT_USER}): \$_gl_have"
else
    _gl_url=\$(printf '%s' '${GITLEAKS_URL_VERSIONED}' | sed "s/{arch}/\$_gl_arch/")
    _gl_tmp=\$(mktemp -d)
    if curl -fsSL "\$_gl_url" -o "\$_gl_tmp/gitleaks.tar.gz" \\
        && tar -xzf "\$_gl_tmp/gitleaks.tar.gz" -C "\$_gl_tmp" gitleaks \\
        && sudo install -m 0755 -o ${AGENT_USER} -g "\$(id -gn ${AGENT_USER})" "\$_gl_tmp/gitleaks" "\$_gl_dest"; then
        echo "[OK]   gitleaks (${AGENT_USER}): \$(sudo -u ${AGENT_USER} -H "\$_gl_dest" version 2>/dev/null || echo unknown)"
    else
        echo "[WARN] gitleaks ${GITLEAKS_VERSION} install failed (\$_gl_url) — agent keeps its current binary" >&2
    fi
    rm -rf "\$_gl_tmp"
fi

# Refresh personal-kb-hook for the agent user (setup-dispatch-host.sh Step 4.10).
# --force is both the install and the upgrade path, so this always reinstalls from
# whatever PERSONAL_KB_HOOK_SRC currently resolves to — deliberately UNPINNED (see
# the PINNING ASYMMETRY note in Step 4.10 of setup-dispatch-host.sh: unlike the
# personal_kb MCP server, this hook is additive and degrades silently rather than
# breaking a run, so tracking the branch head is the lower-risk choice). Non-fatal:
# a failed refresh leaves the agent on its current binary (if any) and must not
# abort the deploy. Does NOT touch settings.json (wiring is setup's job, not
# deploy's) and does NOT restart the service.
if HOOK_OUT=\$(sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/uv tool install --force --from '${PERSONAL_KB_HOOK_SRC}' personal-kb-hook 2>&1); then
    HOOK_VER=\$(sudo -u ${AGENT_USER} -H /home/${AGENT_USER}/.local/bin/personal-kb-hook --version 2>/dev/null || echo present)
    echo "[OK]   personal-kb-hook (${AGENT_USER}): \${HOOK_VER}"
else
    echo "[WARN] personal-kb-hook install failed for ${AGENT_USER} — agent keeps its current binary (if any). Last output:" >&2
    printf '%s\n' "\$HOOK_OUT" | tail -n 5 | sed 's/^/[WARN]   /' >&2
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
