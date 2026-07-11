#!/usr/bin/env bash
set -euo pipefail

# talos-update.sh — Pull the published talos binary from pi-04 to the dispatch fleet.
#
# Resolves the target version from <TALOS_ARTIFACT_BASE>/latest (or --version <TOKEN>),
# checks the currently installed version on each host, and downloads + installs the
# per-arch binary only when the host is stale.
#
# NO SERVICE RESTART: talos is invoked as a fresh subprocess on every dispatch run,
# so the next run picks up the new binary automatically.  Do NOT restart dispatch-api here.
#
# Environment variables:
#   DISPATCH_HOSTS        Space-separated SSH targets (default: "pironman01 r7-research")
#   DISPATCH_HOST         Single SSH target — if set, overrides DISPATCH_HOSTS (back-compat)
#   TALOS_ARTIFACT_BASE   Base URL of the talos artifact tree served by pi-04's Caddy.
#                         PROVISIONAL default: https://pypi.lab.jasonweddington.com/talos
#                         The exact URL is being finalised (pypi.lab vs talos.lab subdomain);
#                         override via this env var until the canonical URL is pinned.
#   AGENT_USER            OS user that owns the talos binary (default: dispatch)
#   AGENT_GROUP           OS group for the installed binary (default: same as AGENT_USER)
#
# Exit code: 0 if every host succeeded.  Non-zero on first failure (remaining hosts skipped).

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# PROVISIONAL: override TALOS_ARTIFACT_BASE once the pi-04 Caddy URL is finalised.
TALOS_ARTIFACT_BASE="${TALOS_ARTIFACT_BASE:-https://pypi.lab.jasonweddington.com/talos}"

AGENT_USER="${AGENT_USER:-dispatch}"
AGENT_GROUP="${AGENT_GROUP:-${AGENT_USER}}"

if [ -n "${DISPATCH_HOST:-}" ]; then
    HOSTS="${DISPATCH_HOST}"
else
    HOSTS="${DISPATCH_HOSTS:-pironman01 r7-research}"
fi

TARGET_TOKEN=""  # resolved after argument parsing (below)

# ---------------------------------------------------------------------------
# Colors (disabled when not writing to a terminal)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    RESET='\033[0m'
else
    GREEN='' YELLOW='' RED='' CYAN='' RESET=''
fi

info()  { printf "${GREEN}[OK]${RESET}   %s\n" "$*"; }
skip()  { printf "${CYAN}[SKIP]${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${RESET} %s\n" "$*"; }
die()   { printf "${RED}[ERROR]${RESET} %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE'
Usage: ./talos-update.sh [--version <TOKEN>]

Pull the published talos binary from pi-04 to the dispatch fleet.
Checks the installed version on each host; downloads only when stale.

Options:
  --version <TOKEN>  Pin a specific version token (e.g. 0.1.0-ga1b2c3d).
                     Default: resolve from <TALOS_ARTIFACT_BASE>/latest.
  -h, --help         Show this help text.

Environment variables:
  DISPATCH_HOSTS        Space-separated SSH targets       (default: "pironman01 r7-research")
  DISPATCH_HOST         Single SSH target (overrides DISPATCH_HOSTS)
  TALOS_ARTIFACT_BASE   Artifact base URL served by pi-04 (provisional default: pypi.lab URL)
  AGENT_USER            OS user that owns the talos binary (default: dispatch)
  AGENT_GROUP           OS group for the installed binary  (default: same as AGENT_USER)

Pi-04 artifact layout:
  <BASE>/latest            → one-line file: the current version token
  <BASE>/<TOKEN>/<arch>/talos  → the binary  (arch ∈ {x86_64, aarch64})

Note: setup-dispatch-host.sh --with-talos remains the from-scratch bootstrap/fallback
(performs an on-host cargo build).  talos-update.sh is the fast bump channel only.
USAGE
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            [[ $# -ge 2 ]] || die "--version requires an argument"
            TARGET_TOKEN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            die "Unknown argument: $1  (run with --help for usage)"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve target version (fetch <BASE>/latest unless --version was given)
# ---------------------------------------------------------------------------
if [ -z "$TARGET_TOKEN" ]; then
    printf "Resolving latest version from %s/latest ...\n" "${TALOS_ARTIFACT_BASE}"
    TARGET_TOKEN="$(curl -fsSL "${TALOS_ARTIFACT_BASE}/latest" | tr -d '[:space:]')"
    [ -n "$TARGET_TOKEN" ] \
        || die "Empty response from ${TALOS_ARTIFACT_BASE}/latest — cannot determine target version"
    info "Latest version: ${TARGET_TOKEN}"
else
    info "Pinned version: ${TARGET_TOKEN}"
fi

# ---------------------------------------------------------------------------
# Per-host update function
# ---------------------------------------------------------------------------
update_one() {
    local host="$1"
    printf "\n########## %s ##########\n" "${host}"

    # Variables expanded *locally* before sending to the remote shell:
    #   TARGET_TOKEN, TALOS_ARTIFACT_BASE, AGENT_USER, AGENT_GROUP
    # Everything else (remote variables, command substitutions) must be \$-escaped.
    # shellcheck disable=SC2087  # intentional: local vars expand, remote vars are \$-escaped
    ssh "${host}" bash -s <<EOF
set -euo pipefail

# ---------------------------------------------------------------------------
# Remote color helpers (stdout-aware)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    _G='\033[0;32m' _Y='\033[0;33m' _R='\033[0;31m' _C='\033[0;36m' _Z='\033[0m'
else
    _G='' _Y='' _R='' _C='' _Z=''
fi
_info() { printf "\${_G}[OK]\${_Z}   %s\n" "\$*"; }
_skip() { printf "\${_C}[SKIP]\${_Z} %s\n" "\$*"; }
_die()  { printf "\${_R}[ERROR]\${_Z} %s\n" "\$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Values injected by the local script
# ---------------------------------------------------------------------------
TARGET_TOKEN="${TARGET_TOKEN}"
TALOS_ARTIFACT_BASE="${TALOS_ARTIFACT_BASE}"
AGENT_USER="${AGENT_USER}"
AGENT_GROUP="${AGENT_GROUP}"

# ---------------------------------------------------------------------------
# Map uname -m → arch token (x86_64 or aarch64; anything else is fatal)
# ---------------------------------------------------------------------------
_machine="\$(uname -m)"
case "\${_machine}" in
    x86_64)   ARCH="x86_64"  ;;
    aarch64)  ARCH="aarch64" ;;
    *) _die "Unsupported architecture '\${_machine}' on \$(hostname -s) — only x86_64 and aarch64 are supported" ;;
esac

# ---------------------------------------------------------------------------
# Read currently installed version token
# Treat a missing binary or parse failure as 'none' (not an error).
# ---------------------------------------------------------------------------
_talos_bin="/home/\${AGENT_USER}/.local/bin/talos"
CURRENT_TOKEN="none"
if sudo -u "\${AGENT_USER}" "\${_talos_bin}" --version >/dev/null 2>&1; then
    _ver="\$(sudo -u "\${AGENT_USER}" "\${_talos_bin}" --version 2>/dev/null | awk '{print \$2}')"
    [ -n "\${_ver}" ] && CURRENT_TOKEN="\${_ver}"
fi

# ---------------------------------------------------------------------------
# Skip if already at the target version (idempotent)
# ---------------------------------------------------------------------------
if [ "\${CURRENT_TOKEN}" = "\${TARGET_TOKEN}" ]; then
    _skip "\$(hostname -s) [talos]: already at \${TARGET_TOKEN} — nothing to do"
    exit 0
fi

printf "  host:    %s\n"  "\$(hostname -s)"
printf "  arch:    %s\n"  "\${ARCH}"
printf "  current: %s\n"  "\${CURRENT_TOKEN}"
printf "  target:  %s\n"  "\${TARGET_TOKEN}"

# ---------------------------------------------------------------------------
# Download to a tempfile; install only on success (never a half-file over live binary)
# ---------------------------------------------------------------------------
_artifact_url="\${TALOS_ARTIFACT_BASE}/\${TARGET_TOKEN}/\${ARCH}/talos"
_tmpfile="\$(mktemp /tmp/talos-update.XXXXXXXXXX)"
_cleanup() { rm -f "\${_tmpfile}"; }
trap _cleanup EXIT

printf "  Downloading %s ...\n" "\${_artifact_url}"
# -fsSL: fail on HTTP errors (4xx/5xx), follow redirects, silent progress bar, show errors
curl -fsSL -o "\${_tmpfile}" "\${_artifact_url}"

# Create the destination directory if needed (should already exist on a provisioned host)
_local_bin_dir="/home/\${AGENT_USER}/.local/bin"
sudo mkdir -p "\${_local_bin_dir}"

# Atomic install: copies tempfile to dest with correct mode+ownership in one step.
# Never overwrites the live binary with a partial download.
sudo install -m 0755 -o "\${AGENT_USER}" -g "\${AGENT_GROUP}" "\${_tmpfile}" "\${_talos_bin}"

# ---------------------------------------------------------------------------
# Verify the freshly-installed binary reports the expected version
# ---------------------------------------------------------------------------
_installed_ver="\$(sudo -u "\${AGENT_USER}" "\${_talos_bin}" --version 2>/dev/null | awk '{print \$2}')"
if [ "\${_installed_ver}" != "\${TARGET_TOKEN}" ]; then
    _die "Version mismatch after install on \$(hostname -s): expected '\${TARGET_TOKEN}', got '\${_installed_ver}' — manual intervention required"
fi

_info "\$(hostname -s) [talos]: \${CURRENT_TOKEN} → \${TARGET_TOKEN}"

# NO service restart: talos is a fresh subprocess per dispatch run; the next run
# picks up the new binary automatically.  dispatch-api must NOT be restarted here.
EOF
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
for host in ${HOSTS}; do
    update_one "${host}"
done

printf "\n"
info "All hosts updated to talos ${TARGET_TOKEN}."
