#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — Deploy the latest agent-gtd-dispatch wheel to one or more hosts.
#
# Runs `uv tool install --force agent-gtd-dispatch --index <DISPATCH_INDEX>` as the
# service user on each host, then restarts the systemd unit and probes /health.
# Hosts consume the wheel published to the private index named by
# DISPATCH_INDEX; no working copy on the host, no source-tree state.
# It also refreshes /etc/sudoers.d/dispatch-svc via scripts/sudoers-refresh.sh
# before the wheel install.
# It also refreshes the agent user's agent-gtd/tightbeam_kb registrations via scripts/kb-mcp-refresh.sh and probes agent-gtd with templates/mcp-probe.py (non-fatal).
# It also retires the agent user's legacy personal-kb-hook settings.json blocks via
# templates/personal-kb-hook-prune.py (non-fatal), so hosts provisioned before the
# hook was dropped cannot keep loading it.
#
# The private index is passed via `--index` (NOT `--index-url`) so it is
# ADDED to the default PyPI list; uv resolves fastapi/uvicorn/anthropic/etc.
# from public PyPI while pulling agent-gtd-dispatch and agent-gtd-dispatch-protocol
# from the private index. Both wheels must already be on the index (the
# maintainer-local release.local.sh hook publishes them before deploying).
#
# Environment variables:
#   DISPATCH_HOSTS  Space-separated SSH targets (default: derived from the GTD host
#                   registry, see scripts/fleet-hosts.sh)
#   DISPATCH_HOST   Single SSH target — if set, overrides DISPATCH_HOSTS (back-compat)
#   SERVICE_USER    Service account owning the tool install (default: dispatch-svc)
#   AGENT_USER      Agent subprocess user whose Claude Code, lefthook, dev toolchain
#                   (Rust tools + gitleaks, Step 4.9) and Playwright browsers
#                   (Step 4.11) are refreshed (default: dispatch)
#   SERVICE_NAME    Systemd service unit name (default: dispatch-api)
#   DISPATCH_INDEX  REQUIRED. Private wheel index URL (PEP 503 simple/ root)
#   RUST_DEFAULT_TOOLCHAIN  Rust toolchain pointed at by 'rustup default' when the agent
#                   user has rustup but no usable default (default: stable)
#
# Exit code: 0 if every host succeeded. Non-zero on first failure (other hosts skipped).
# Non-Linux hosts (uname -s preflight, scripts/fleet-hosts.sh) are never deployed:
# each gets a WARN and the run exits 1 after the Linux hosts are done.

. "$(dirname "$0")/scripts/fleet-hosts.sh"
resolve_fleet_hosts || exit 1
SERVICE_USER="${SERVICE_USER:-dispatch-svc}"
AGENT_USER="${AGENT_USER:-dispatch}"
SERVICE_NAME="${SERVICE_NAME:-dispatch-api}"
DISPATCH_INDEX="${DISPATCH_INDEX:?DISPATCH_INDEX must be set to the private wheel index URL (PEP 503 simple/ root)}"
RUST_DEFAULT_TOOLCHAIN="${RUST_DEFAULT_TOOLCHAIN:-stable}"

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
    APT_PKG_LIST="${DEV_TOOLCHAIN_APT_PKGS[*]}"
    NPM_GLOBAL_LIST=""
    for _e in "${DEV_TOOLCHAIN_NPM_GLOBAL_PKGS[@]}"; do
        NPM_GLOBAL_LIST="${NPM_GLOBAL_LIST}'${_e}' "
    done
    # Substitute {version} locally; {arch} is resolved on each host from `uname -m`.
    GITLEAKS_URL_VERSIONED="${GITLEAKS_URL_TEMPLATE//\{version\}/${GITLEAKS_VERSION}}"
    # macOS cross-check toolchain: zig pin, pinned crates, rust targets (same pattern).
    ZIG_ARCH_MAP_LIST=""
    for _e in "${ZIG_ARCH_MAP[@]}"; do
        ZIG_ARCH_MAP_LIST="${ZIG_ARCH_MAP_LIST}'${_e}' "
    done
    ZIG_URL_VERSIONED="${ZIG_URL_TEMPLATE//\{version\}/${ZIG_VERSION}}"
    ZIG_INSTALL_DIR="${ZIG_INSTALL_DIR_TEMPLATE//\{version\}/${ZIG_VERSION}}"
    CARGO_PINNED_LIST=""
    for _e in "${DEV_TOOLCHAIN_CARGO_PINNED_PKGS[@]}"; do
        CARGO_PINNED_LIST="${CARGO_PINNED_LIST}'${_e}' "
    done
    RUST_TARGET_LIST=""
    for _e in "${DEV_TOOLCHAIN_RUST_TARGETS[@]}"; do
        RUST_TARGET_LIST="${RUST_TARGET_LIST}'${_e}' "
    done
    # Playwright pins, same single-quoted-entry interpolation pattern as above.
    PLAYWRIGHT_VERSION_LIST=""
    for _e in "${PLAYWRIGHT_VERSIONS[@]}"; do
        PLAYWRIGHT_VERSION_LIST="${PLAYWRIGHT_VERSION_LIST}'${_e}' "
    done
else
    echo "[WARN] ${TOOLCHAIN_CONF} not found — dev toolchain refresh will be skipped on every host" >&2
    DEV_TOOLCHAIN_PKG_LIST=""
    APT_PKG_LIST=""
    NPM_GLOBAL_LIST=""
    GITLEAKS_ARCH_MAP_LIST=""
    GITLEAKS_URL_VERSIONED=""
    GITLEAKS_VERSION=""
    ZIG_VERSION=""
    ZIG_ARCH_MAP_LIST=""
    ZIG_URL_VERSIONED=""
    ZIG_INSTALL_DIR=""
    ZIG_BIN_LINK=""
    CARGO_PINNED_LIST=""
    RUST_TARGET_LIST=""
    PLAYWRIGHT_VERSION_LIST=""
    PLAYWRIGHT_BROWSERS=""
fi

deploy_one() {
    local host="$1"
    echo
    echo "########## ${host} ##########"

    # Rescue sweep: ship the script and its units ahead of the wheel. They are
    # plain files rather than package data because the sweep must keep working
    # even when the service is broken — it exists precisely for the runs a broken
    # service abandons, so coupling its delivery to the service's own health
    # would remove it exactly when it is needed.
    scp -q "${SCRIPT_DIR}/scripts/rescue-sweep.sh" \
        "${SCRIPT_DIR}/templates/dispatch-rescue-sweep.service.tmpl" \
        "${SCRIPT_DIR}/templates/dispatch-rescue-sweep.timer.tmpl" \
        "${host}:/tmp/" || {
        echo "[WARN] could not stage rescue-sweep files on ${host} — skipping sweep refresh" >&2
    }

    local _stage
    _stage=$(ssh "${host}" mktemp -d) || { echo "[ERR]  could not stage sudoers refresh on ${host}" >&2; return 1; }
    [[ "$_stage" == /tmp/* ]] || { echo "[ERR]  could not stage sudoers refresh on ${host}" >&2; return 1; }
    scp -q "${SCRIPT_DIR}/scripts/sudoers-refresh.sh" "${SCRIPT_DIR}/scripts/kb-mcp-refresh.sh" "${SCRIPT_DIR}/templates/mcp-probe.py" "${SCRIPT_DIR}/templates/sudoers-dispatch-svc.tmpl" "${SCRIPT_DIR}/templates/personal-kb-hook-prune.py" "${SCRIPT_DIR}/templates/agent-gtd-kill-guard/pkill" "${SCRIPT_DIR}/templates/agent-gtd-kill-guard/killall" "${host}:${_stage}/" || { echo "[ERR]  could not stage sudoers refresh on ${host}" >&2; return 1; }

    ssh "${host}" bash -s <<EOF
set -euo pipefail
if ! sudo bash "${_stage}/sudoers-refresh.sh" --template "${_stage}/sudoers-dispatch-svc.tmpl" --service-user ${SERVICE_USER} --agent-user ${AGENT_USER} 2>&1 | sed "s/^/[${host}] /"; then rm -rf "${_stage}"; echo "[ERR]  sudoers refresh failed on ${host} — host left untouched" >&2; exit 1; fi
if sudo test -f /home/${SERVICE_USER}/.env; then if ! sudo bash "${_stage}/kb-mcp-refresh.sh" --env-file /home/${SERVICE_USER}/.env --agent-user ${AGENT_USER} --probe "${_stage}/mcp-probe.py" 2>&1 | sed "s/^/[${host}] /"; then echo "[WARN] MCP refresh failed on ${host} — agent-gtd/KB registrations may be stale" >&2; fi; else echo "[SKIP] MCP refresh: /home/${SERVICE_USER}/.env not present on ${host}"; fi
# _stage stays alive until the personal-kb-hook retirement below has run — it holds
# templates/personal-kb-hook-prune.py (staged above) and is removed with the rest of
# _stage afterwards.

# Refuse stray user-level steering BEFORE installing anything: headless runs load
# ~/.claude/CLAUDE.md and ~/.claude/rules/*.md even with --system-prompt, so a
# copied-in personal CLAUDE.md silently changes every run on this host. Dispatch
# steering lives in the dispatch prompt. ~/.claude/settings.json is legitimate and is
# deliberately not checked.
_stray=""
for _f in /home/${AGENT_USER}/.claude/CLAUDE.md /home/${AGENT_USER}/.claude/CLAUDE.local.md; do
    if sudo test -e "\$_f"; then _stray="\${_stray} \$_f"; fi
done
if sudo test -d /home/${AGENT_USER}/.claude/rules && [ -n "\$(sudo ls -A /home/${AGENT_USER}/.claude/rules 2>/dev/null)" ]; then _stray="\${_stray} /home/${AGENT_USER}/.claude/rules/"; fi
if [ -n "\$_stray" ]; then
    rm -rf "${_stage}"
    echo "[ERR]  stray user-level steering for ${AGENT_USER} on ${host}:\$_stray — dispatched runs would load it; remove it (dispatch steering lives in the dispatch prompt)" >&2
    exit 1
fi

# Install/refresh the wheel from the private index.
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
    if [ "\$_installed" != "${EXPECT_VERSION:-}" ]; then
        echo "[ERR]  expected agent-gtd-dispatch ${EXPECT_VERSION:-} but \$_installed is installed" >&2
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

# System build packages and global npm tools (setup-dispatch-host.sh Step 4.9,
# sub-action 0), from templates/dev-toolchain.sh. Only missing packages are installed,
# so a fully provisioned host runs no apt-get. Non-fatal.
_apt_missing=""
for _pkg in ${APT_PKG_LIST}; do
    dpkg-query -W -f='\${Status}' "\$_pkg" 2>/dev/null | grep -q "install ok installed" || _apt_missing="\${_apt_missing} \$_pkg"
done
if [ -z "\$_apt_missing" ]; then
    echo "[OK]   system build packages present"
elif ! command -v apt-get >/dev/null 2>&1; then
    echo "[WARN] apt-get not found — missing build packages:\$_apt_missing" >&2
elif sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 \
    && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \$_apt_missing >/dev/null 2>&1; then
    echo "[OK]   installed system build packages:\$_apt_missing"
else
    echo "[WARN] apt-get install failed for:\$_apt_missing" >&2
fi
for _entry in ${NPM_GLOBAL_LIST}; do
    _npkg="\${_entry%%|*}"
    _nbin="\${_entry#*|}"
    if sudo -u ${AGENT_USER} -H bash -lc "command -v \$_nbin" >/dev/null 2>&1; then
        echo "[OK]   \$_nbin present for ${AGENT_USER}"
    elif command -v npm >/dev/null 2>&1 && sudo npm install -g "\$_npkg" >/dev/null 2>&1; then
        echo "[OK]   installed \$_npkg globally"
    else
        echo "[WARN] could not install \$_npkg globally (npm missing or install failed)" >&2
    fi
done

# Refresh the agent user's dev toolchain (setup-dispatch-host.sh Step 4.9): the
# Rust tools and gitleaks that dispatched repos' hooks and gate commands call.
# The package list and the gitleaks pin come from templates/dev-toolchain.sh,
# sourced locally above and interpolated here — never hand-copied.
# Non-fatal throughout: a failed refresh leaves the agent on its current binaries
# and must not abort the deploy (only the health check below may exit non-zero).
_cargo="/home/${AGENT_USER}/.cargo/bin/cargo"
_rustup="/home/${AGENT_USER}/.cargo/bin/rustup"

# Self-heal: a host can have rustup installed with toolchains present but no
# default set (seen on an x86_64 host) — every cargo invocation then
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
    # Pinned crates ("<crate>|<binary>|<version>"): install only when the binary is
    # absent or does not report the pinned version as a whole token. Absolute path:
    # sudo -u -H has no login PATH.
    for _entry in ${CARGO_PINNED_LIST}; do
        _crate="\${_entry%%|*}"
        _prest="\${_entry#*|}"
        _bin="\${_prest%%|*}"
        _ver="\${_prest#*|}"
        if sudo -u ${AGENT_USER} -H "/home/${AGENT_USER}/.cargo/bin/\$_bin" --version 2>/dev/null | grep -q -F -w "\$_ver"; then
            echo "[OK]   \$_bin \$_ver present for ${AGENT_USER}"
        elif _TC_OUT=\$(sudo -u ${AGENT_USER} -H "\$_cargo" binstall -y --force "\$_crate@=\$_ver" 2>&1); then
            echo "[OK]   \$_crate \$_ver installed for ${AGENT_USER}"
        else
            echo "[WARN] cargo binstall \$_crate@=\$_ver failed for ${AGENT_USER} — continuing. Last output:" >&2
            printf '%s\n' "\$_TC_OUT" | tail -n 5 | sed 's/^/[WARN]   /' >&2
            _tc_failed="\${_tc_failed} \$_crate"
        fi
    done
    # Rust targets on EVERY installed toolchain.
    for _tc in \$(sudo -u ${AGENT_USER} -H "\$_rustup" toolchain list 2>/dev/null | awk '{print \$1}' | grep -v -x -e no -e '' || true); do
        for _t in ${RUST_TARGET_LIST}; do
            if sudo -u ${AGENT_USER} -H "\$_rustup" target list --toolchain "\$_tc" --installed 2>/dev/null | grep -qx "\$_t"; then
                echo "[OK]   \$_t (\$_tc) present"
            elif sudo -u ${AGENT_USER} -H "\$_rustup" target add "\$_t" --toolchain "\$_tc" >/dev/null 2>&1; then
                echo "[OK]   \$_t (\$_tc) added"
            else
                echo "[WARN] rustup target add \$_t --toolchain \$_tc failed for ${AGENT_USER} — continuing" >&2
                _tc_failed="\${_tc_failed} \$_t@\$_tc"
            fi
        done
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

# zig: re-install only when the installed version differs from the pinned version.
_zig_machine=\$(uname -m)
_zig_arch=""
_zig_sha=""
for _e in ${ZIG_ARCH_MAP_LIST}; do
    _m="\${_e%%|*}"; _rest="\${_e#*|}"; _zarch="\${_rest%%|*}"; _zsha="\${_rest#*|}"
    if [ "\$_m" = "\$_zig_machine" ]; then
        _zig_arch="\$_zarch"
        _zig_sha="\$_zsha"
    fi
done
_zig_have=\$(sudo -u ${AGENT_USER} -H bash -lc 'zig version' 2>/dev/null | tr -d '[:space:]' || true)
if [ -z "${ZIG_VERSION}" ]; then
    echo "[WARN] no zig pin available (templates/dev-toolchain.sh missing) — skipping zig refresh" >&2
elif [ -z "\$_zig_arch" ]; then
    echo "[WARN] zig: architecture \$_zig_machine not in ZIG_ARCH_MAP — skipped" >&2
elif [ "\$_zig_have" = "${ZIG_VERSION}" ]; then
    echo "[OK]   zig: found=\${_zig_have:-none} pinned=${ZIG_VERSION} arch=\$_zig_machine action=skip"
    echo "[OK]   zig (${AGENT_USER}): \$_zig_have"
else
    echo "[OK]   zig: found=\${_zig_have:-none} pinned=${ZIG_VERSION} arch=\$_zig_machine action=install"
    _zig_url=\$(printf '%s' '${ZIG_URL_VERSIONED}' | sed "s/{arch}/\$_zig_arch/")
    _zig_tmp=\$(mktemp -d)
    _zig_reason=""
    if ! curl -fsSL "\$_zig_url" -o "\$_zig_tmp/zig.tar.xz"; then
        _zig_reason="download of \$_zig_url failed"
    else
        _zig_actual=\$(sha256sum "\$_zig_tmp/zig.tar.xz" | awk '{print \$1}')
        if [ "\$_zig_actual" != "\$_zig_sha" ]; then
            _zig_reason="sha256 mismatch: expected \$_zig_sha got \$_zig_actual"
        elif _zig_stage=\$(sudo mktemp -d "/opt/.zig-${ZIG_VERSION}.XXXXXX") \
            && sudo tar -xJf "\$_zig_tmp/zig.tar.xz" -C "\$_zig_stage" --strip-components=1 \
            && sudo rm -rf "${ZIG_INSTALL_DIR}" \
            && sudo mv "\$_zig_stage" "${ZIG_INSTALL_DIR}" \
            && sudo chmod 0755 "${ZIG_INSTALL_DIR}" \
            && sudo ln -sfn "${ZIG_INSTALL_DIR}/zig" "${ZIG_BIN_LINK}"; then
            :
        else
            _zig_reason="extract/install step failed"
            [ -n "\${_zig_stage:-}" ] && sudo rm -rf "\$_zig_stage"
        fi
    fi
    rm -rf "\$_zig_tmp"
    if [ -n "\$_zig_reason" ]; then
        echo "[WARN] zig ${ZIG_VERSION} install failed (\$_zig_reason) — agent keeps its current binary" >&2
    else
        echo "[OK]   zig (${AGENT_USER}): \$(sudo -u ${AGENT_USER} -H bash -lc 'zig version' 2>/dev/null || echo unknown)"
    fi
fi

# State line: one summary of the cross-check toolchain, same shape as setup Step 4.9.
_st_zig=\$(sudo -u ${AGENT_USER} -H bash -lc 'zig version' 2>/dev/null | tr -d '[:space:]' || true)
_st_pin_name=""
_st_pin_ver=""
for _entry in ${CARGO_PINNED_LIST}; do
    _st_pin_name="\${_entry#*|}"; _st_pin_name="\${_st_pin_name%%|*}"
    break
done
if [ -n "\$_st_pin_name" ]; then
    _st_pin_ver=\$(sudo -u ${AGENT_USER} -H "/home/${AGENT_USER}/.cargo/bin/\$_st_pin_name" --version 2>/dev/null | awk '{print \$NF}' || true)
fi
_st_missing=""
for _t in ${RUST_TARGET_LIST}; do
    _t_ok=0
    for _tc in \$([ -x "\$_rustup" ] && sudo -u ${AGENT_USER} -H "\$_rustup" toolchain list 2>/dev/null | awk '{print \$1}' | grep -v -x -e no -e '' || true); do
        if sudo -u ${AGENT_USER} -H "\$_rustup" target list --toolchain "\$_tc" --installed 2>/dev/null | grep -qx "\$_t"; then
            _t_ok=1
        else
            _t_ok=0
            break
        fi
    done
    if [ "\$_t_ok" = 0 ]; then _st_missing="\${_st_missing:+\$_st_missing,}\$_t"; fi
done
echo "[STATE] cross-toolchain zig=\${_st_zig:-MISSING} \${_st_pin_name:-pinned-crate}=\${_st_pin_ver:-MISSING} rust_targets_missing=\${_st_missing:-none}"

# Refresh the agent user's Playwright browsers (setup-dispatch-host.sh Step 4.11).
# Dispatched repos run Playwright e2e suites (camera-profiles apps/desktop,
# @playwright/test 1.63.0) and the agent user cannot install anything, so the
# browsers must already be in its DEFAULT cache (~/.cache/ms-playwright — never
# a private browsers-path env var, which only the repo that set it can see). The
# version list and browser names come from
# templates/dev-toolchain.sh, sourced locally above and interpolated here — never
# hand-copied. "install-deps" (apt system libraries) needs root, so it runs as root
# here exactly as Step 4.11 does at provision time — both are idempotent, so
# re-running is safe. PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT lifts Playwright's
# 30s download-connection cap, which in-run installs routinely exceed. Non-fatal
# throughout (the whole block is wrapped in if/else — it never trips set -e): a
# failed refresh leaves the agent on its current browsers and the deploy continues.
_pw_npx=\$(sudo -u ${AGENT_USER} -H bash -lc 'command -v npx' 2>/dev/null || true)
if [ -z "${PLAYWRIGHT_VERSION_LIST}" ]; then
    echo "[WARN] no Playwright version pin available (templates/dev-toolchain.sh missing) — skipping Playwright browser refresh" >&2
elif [ -z "\$_pw_npx" ]; then
    echo "[WARN] npx not found for ${AGENT_USER} (login shell) — Playwright browser refresh skipped; run 'sudo ./setup-dispatch-host.sh' (Step 4.11) on this host" >&2
else
    for _v in ${PLAYWRIGHT_VERSION_LIST}; do
        # npm reads a .npmrc in the current directory as project config. The ssh
        # session's cwd is the deploying user's home, whose .npmrc (e.g. a prefix=
        # line pointing at a missing dir) then fails root's npx with ENOENT (exit
        # 254). Run from / rather than /tmp: this runs as root and /tmp is
        # world-writable, so anyone could plant a .npmrc or node_modules there and
        # redirect root's npx.
        _pw_deps_log=\$(mktemp)
        if ( cd / && sudo env PATH="\$(dirname "\$_pw_npx"):/usr/bin:/bin" "\$_pw_npx" --yes "playwright@\$_v" install-deps ${PLAYWRIGHT_BROWSERS:-} ) >"\$_pw_deps_log" 2>&1; then
            echo "[OK]   playwright@\$_v install-deps (${PLAYWRIGHT_BROWSERS:-})"
        else
            _rc=\$?
            echo "[WARN] playwright@\$_v install-deps failed (exit \$_rc) for ${AGENT_USER} — system libraries for the browsers may be incomplete. Last output:" >&2
            tail -n 5 "\$_pw_deps_log" >&2 || true
        fi
        rm -f "\$_pw_deps_log"
        # cd /tmp: the ssh session's cwd is the deploying user's home, which the
        # agent user usually cannot read, and npm then dies with EACCES spawning sh.
        _pw_log=\$(mktemp)
        if sudo -u ${AGENT_USER} -H bash -lc "cd /tmp && PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=600000 '\$_pw_npx' --yes playwright@\$_v install ${PLAYWRIGHT_BROWSERS:-}" >"\$_pw_log" 2>&1; then
            echo "[OK]   Playwright \$_v (${PLAYWRIGHT_BROWSERS:-}) for ${AGENT_USER}"
        else
            echo "[WARN] playwright@\$_v install failed for ${AGENT_USER} — agent keeps its current browsers (if any). Last output:" >&2
            tail -n 5 "\$_pw_log" >&2 || true
        fi
        rm -f "\$_pw_log"
    done
fi

# personal-kb-hook is retired: its settings.json wiring must not survive on any
# host. Prune matcher groups whose command references personal-kb-hook, preserving
# every other setting, and drop event keys the prune empties. Idempotent, so
# re-running it on every deploy is safe. Non-fatal throughout: the heredoc runs
# under `set -euo pipefail`, so the elif/else form keeps a failed prune from
# aborting the deploy — it prints a WARN and settings.json is left unchanged.
_kb_settings="/home/${AGENT_USER}/.claude/settings.json"
if ! sudo test -f "\$_kb_settings"; then echo "[SKIP] personal-kb-hook retirement: \$_kb_settings not present on this host"; elif _prune_out="\$(sudo python3 "${_stage}/personal-kb-hook-prune.py" "\$_kb_settings")"; then sudo chown ${AGENT_USER}:${AGENT_USER} "\$_kb_settings"; echo "[OK]   personal-kb-hook retirement: \${_prune_out}"; else echo "[WARN] personal-kb-hook retirement failed: \$_kb_settings left unchanged" >&2; fi

# Kill guard (setup-dispatch-host.sh Step 4.12): refresh the pkill/killall shims
# so a host on the routine channel is not left unguarded between provisionings.
# deploy.sh has no AGENT_GROUP, so the agent user's primary group is derived on
# the host. Non-fatal throughout: a failed refresh prints a WARN naming the
# setup step to re-run and the deploy continues.
_kg_dir="/home/${AGENT_USER}/.local/share/agent-gtd-dispatch/kill-guard"
_kg_grp="\$(id -gn ${AGENT_USER} 2>/dev/null || echo ${AGENT_USER})"
if sudo install -d -m 0755 -o ${AGENT_USER} -g "\$_kg_grp" "\$_kg_dir"; then
    for _kg in pkill killall; do
        if sudo cmp -s "${_stage}/\$_kg" "\$_kg_dir/\$_kg" 2>/dev/null; then
            echo "[SKIP] kill guard: \$_kg_dir/\$_kg already up to date"
        elif sudo install -m 0755 -o ${AGENT_USER} -g "\$_kg_grp" "${_stage}/\$_kg" "\$_kg_dir/\$_kg"; then
            echo "[OK]   kill guard: installed \$_kg_dir/\$_kg"
        else
            echo "[WARN] kill guard: could not install \$_kg_dir/\$_kg — run 'sudo ./setup-dispatch-host.sh' (Step 4.12)" >&2
        fi
    done
else
    echo "[WARN] kill guard: could not create \$_kg_dir — run 'sudo ./setup-dispatch-host.sh' (Step 4.12)" >&2
fi
rm -rf "${_stage}"

# Rescue sweep timer — install/refresh, then restart. Non-fatal: a host that
# cannot take the sweep is still a working dispatch host, and failing the deploy
# over it would leave the fleet on an older wheel for a watchdog's sake.
if [ -f /tmp/rescue-sweep.sh ]; then
    sudo install -m 0755 /tmp/rescue-sweep.sh /usr/local/bin/dispatch-rescue-sweep
    for _k in service timer; do
        sed -e "s|{{AGENT_USER}}|${AGENT_USER}|g" \
            -e "s|{{AGENT_GROUP}}|${AGENT_USER}|g" \
            -e "s|{{WORKSPACE_ROOT}}|/home/${AGENT_USER}/workspace|g" \
            -e "s|{{SWEEP_BIN}}|/usr/local/bin/dispatch-rescue-sweep|g" \
            "/tmp/dispatch-rescue-sweep.\${_k}.tmpl" \
            | sudo tee "/etc/systemd/system/dispatch-rescue-sweep.\${_k}" >/dev/null
    done
    sudo systemctl daemon-reload
    if sudo systemctl enable --now dispatch-rescue-sweep.timer >/dev/null 2>&1; then
        echo "[OK]   rescue sweep timer active (every 4 min)"
    else
        echo "[WARN] rescue sweep timer failed to enable on this host" >&2
    fi
    rm -f /tmp/rescue-sweep.sh /tmp/dispatch-rescue-sweep.*.tmpl
else
    echo "[WARN] rescue-sweep files not staged — timer not refreshed" >&2
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

# Preflight over ALL hosts before any per-host work: only Linux hosts are
# deployed; a non-Linux host is skipped with a WARN and fails the run at the end.
partition_linux_hosts
partition_ok=1
report_host_partition || partition_ok=0

for host in ${LINUX_HOSTS}; do
    deploy_one "${host}"
done

if [ "${partition_ok}" -ne 1 ]; then
    echo
    echo "[ERR]  Linux hosts deployed; some hosts were skipped or could not be probed (see above)." >&2
    exit 1
fi

echo
echo "All hosts deployed."
