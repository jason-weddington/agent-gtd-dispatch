#!/usr/bin/env bash
set -euo pipefail

# setup-dispatch-host.sh — Idempotent installer for the agent-gtd-dispatch service.
#
# Bootstraps a fresh dispatch host OR migrates an existing host to the
# two-user-split architecture (dispatch-svc runs the API; dispatch runs agents).
# Re-running on an already-configured host is a no-op.
#
# Usage:
#   sudo ./setup-dispatch-host.sh [OPTIONS]
#
# Options:
#   --agent-user USER      Unprivileged agent subprocess user (default: dispatch)
#   --service-user USER    Service account user (default: dispatch-svc)
#   --env-file PATH        Path to a pre-filled .env file to install
#   --dry-run              Print 'Would: <action>' for every step; no mutations
#   --smoke                After install, verify the API is reachable (GET /health and GET /info return HTTP 200)
#   --with-talos           Build and install the talos engine binary for AGENT_USER (opt-in; default off)
#   --with-postgres        Install local Postgres + pgvector, create an AGENT_USER-named
#                          CREATEDB role (peer auth over the Unix socket), install pgvector
#                          into template1, write KB_TEST_DATABASE_URL + KB_REQUIRE_POSTGRES_TESTS
#                          to the service env (opt-in; default off)
#   --restart-if-stale     Step 6.5: when the running service environment is stale
#                          relative to the installed env file AND zero runs are
#                          active, restart the service automatically (opt-in;
#                          default off — otherwise Step 6.5 only warns)
#   -h, --help             Show this help text

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="agent-gtd-dispatch"
# Defaults clone from public GitHub (anonymous https). Point at a fork or a
# self-hosted origin via DISPATCH_REPO_URL / AGENT_GTD_REPO_URL. NOTE: GitHub is
# release-cadence; a host that must run tip-of-main should override to the origin
# that carries it.
GIT_REMOTE_URL="${DISPATCH_REPO_URL:-https://github.com/jason-weddington/${REPO_NAME}}"
AGENT_GTD_REMOTE_URL="${AGENT_GTD_REPO_URL:-https://github.com/jason-weddington/agent-gtd}"
HARNESS_DESIGN_REPO_URL="${HARNESS_DESIGN_REPO_URL:-git@ubuntu-vm01:repos/harness-design}"

# Derive the git host(s) to seed into known_hosts from the configured remotes,
# so this works against any git server (homelab, GitHub, enterprise) — not just
# the homelab default. Handles scp-style (git@host:path) and ssh:// URLs.
git_host_from_url() {
    local url="$1"
    # http(s):// URLs authenticate via TLS — no SSH host key needed; return empty
    case "$url" in
        http://*|https://*) return 0 ;;
    esac
    url="${url#ssh://}"   # drop ssh:// scheme if present
    url="${url#*@}"       # drop user@ if present
    printf '%s' "${url%%[:/]*}"  # take up to the first ':' or '/'
}
GIT_HOSTS="$(printf '%s\n%s\n' \
    "$(git_host_from_url "$GIT_REMOTE_URL")" \
    "$(git_host_from_url "$AGENT_GTD_REMOTE_URL")" | sort -u | sed '/^[[:space:]]*$/d' | tr '\n' ' ')"
SERVICE_NAME="dispatch-api"
API_PORT=8100
SUDOERS_FILE="/etc/sudoers.d/dispatch-svc"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
TMPL_DIR="${SCRIPT_DIR}/templates"
# Budget per MCP server for the Step 4.6 initialize + tools/list probe. 600s is sized
# for a cold ~/.cache/uv building personal_kb from git on the aarch64 Pi 5. Set to 0 to
# skip the probe pass entirely.
MCP_PROBE_TIMEOUT="${MCP_PROBE_TIMEOUT:-600}"

# --- Defaults (overridden by CLI flags) ---
AGENT_USER="dispatch"
SERVICE_USER="dispatch-svc"
ENV_FILE_SRC=""
DRY_RUN=false
SMOKE=false
WITH_TALOS=false
WITH_POSTGRES=false
# Toolchain 'rustup default' is pointed at when a host has rustup but no usable default.
RUST_DEFAULT_TOOLCHAIN="${RUST_DEFAULT_TOOLCHAIN:-stable}"
# Step 6.5 (service environment freshness): restart the service automatically
# when stale AND zero runs are active. ENV_MUTATED_VARS accumulates the names
# of every var this run wrote into $SERVICE_ENV (via _note_env_mutation),
# used as the mtime-fallback stale-name list when /proc is unreadable.
RESTART_IF_STALE=false
ENV_MUTATED_VARS=()
ENV_FRESHNESS_STATE="unchecked"

# --- Colors ---
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
would() { printf "${YELLOW}[DRY]${RESET}  Would: %s\n" "$*"; }

usage() {
    cat <<'EOF'
Usage: sudo ./setup-dispatch-host.sh [OPTIONS]

Idempotent installer for the agent-gtd-dispatch service.
Bootstraps a fresh host OR migrates an existing host to the two-user split.

Options:
  --agent-user USER      Unprivileged agent subprocess user  (default: dispatch)
  --service-user USER    Service account user                (default: dispatch-svc)
  --env-file PATH        Pre-filled .env file to install
  --dry-run              Print 'Would: <action>' for every step; make no changes
  --smoke                After install, verify the API is reachable (GET /health and GET /info return HTTP 200)
  --with-talos           Build and install the talos engine binary for AGENT_USER (opt-in; default off)
  --with-postgres        Install local Postgres + pgvector, create a '${AGENT_USER}' role with CREATEDB
                         (peer-auth over the Unix socket, no password), install the vector
                         extension into 'template1' so every database the test suite creates
                         inherits it, and write KB_TEST_DATABASE_URL=postgresql:///postgres +
                         KB_REQUIRE_POSTGRES_TESTS=1 to the service env file (opt-in; default off)
  --restart-if-stale     Step 6.5: when the running service environment is stale
                         relative to the installed env file AND zero runs are
                         active, restart the service automatically (opt-in;
                         default off — otherwise Step 6.5 only warns)
  -h, --help             Show this help text

Environment variables:
  DISPATCH_REPO_URL        Git remote for agent-gtd-dispatch repo
  AGENT_GTD_REPO_URL       Git remote for agent_gtd repo
  HARNESS_DESIGN_REPO_URL  Git remote for harness-design repo (used with --with-talos;
                           default: git@ubuntu-vm01:repos/harness-design)
  DISPATCH_SINGLE_USER   Set to '1' for single-user mode (no sudoers, no user split;
                         see docs/install.md ## Single-user mode for details)

Examples:
  # Fresh install (interactive .env generation from template):
  sudo ./setup-dispatch-host.sh

  # Migrate pironman01 (provide existing .env with DISPATCH_AGENT_SUBPROCESS_USER added):
  sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env

  # Preview all changes without applying them:
  sudo ./setup-dispatch-host.sh --dry-run

  # Full install + smoke test:
  sudo ./setup-dispatch-host.sh --env-file /tmp/dispatch.env --smoke

  # Install Postgres + pgvector (dispatch host only; dry-run preview):
  sudo ./setup-dispatch-host.sh --with-postgres --dry-run

  # Install Postgres + pgvector and smoke-test the vector extension:
  sudo ./setup-dispatch-host.sh --with-postgres --smoke
EOF
    exit 0
}

# ===========================================================================
# Helper functions (defined before use)
# ===========================================================================

_render_sudoers() {
    local tmpl="${TMPL_DIR}/sudoers-dispatch-svc.tmpl"
    [[ -f "$tmpl" ]] || die "Template not found: ${tmpl}"
    sed \
        -e "s|{{SERVICE_USER}}|${SERVICE_USER}|g" \
        -e "s|{{AGENT_USER}}|${AGENT_USER}|g" \
        "$tmpl"
}

_install_sudoers() {
    local tmpfile
    tmpfile="$(mktemp /tmp/dispatch-sudoers.XXXXXX)"
    _render_sudoers > "$tmpfile"
    if ! visudo -c -f "$tmpfile"; then
        rm -f "$tmpfile"
        die "visudo validation failed — sudoers fragment NOT installed"
    fi
    install -m 0440 -o root -g root "$tmpfile" "$SUDOERS_FILE"
    rm -f "$tmpfile"
    info "Installed sudoers fragment: ${SUDOERS_FILE}"
}

_render_unit() {
    local tmpl="${TMPL_DIR}/dispatch-api.service.tmpl"
    [[ -f "$tmpl" ]] || die "Template not found: ${tmpl}"
    sed \
        -e "s|{{SERVICE_USER}}|${SERVICE_USER}|g" \
        -e "s|{{SERVICE_GROUP}}|${SERVICE_GROUP}|g" \
        -e "s|{{AGENT_USER}}|${AGENT_USER}|g" \
        -e "s|{{WORKING_DIR}}|${SERVICE_HOME}|g" \
        -e "s|{{ENV_FILE}}|${SERVICE_ENV}|g" \
        -e "s|{{SERVICE_HOME}}|${SERVICE_HOME}|g" \
        -e "s|{{UV_BIN}}|${UV_BIN}|g" \
        "$tmpl"
}

_install_unit() {
    _render_unit > "$SYSTEMD_UNIT"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    info "Installed and started systemd unit: ${SERVICE_NAME}"
}

_health_check() {
    local url="http://localhost:${API_PORT}/health"
    local attempts=0 max=10 delay=3
    while (( attempts < max )); do
        if curl -sf --max-time 5 "$url" &>/dev/null; then
            info "Health check passed: ${url}"
            return 0
        fi
        # Not `(( attempts++ ))`: post-increment from 0 evaluates to 0 (exit 1), and
        # `set -e` then kills the installer silently on the first failed probe.
        attempts=$((attempts + 1))
        warn "Health check attempt ${attempts}/${max} failed — retrying in ${delay}s"
        sleep "$delay"
    done
    die "Health check failed after $((max * delay))s — service may not have started"
}

_smoke_test() {
    local api_url="http://localhost:${API_PORT}"

    # Assertion (a): GET /health → HTTP 200 + 'status' key
    info "Smoke test: GET /health ..."
    local http_code health_body
    http_code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${api_url}/health" 2>/dev/null)" \
        || die "Smoke test: GET /health request failed (curl error)"
    [[ "$http_code" == "200" ]] \
        || die "Smoke test: GET /health returned HTTP ${http_code} (expected 200)"
    health_body="$(curl -sf --max-time 10 "${api_url}/health" 2>/dev/null)"
    echo "$health_body" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'status' in d" 2>/dev/null \
        || die "Smoke test: GET /health response missing expected key 'status': ${health_body}"
    info "Smoke assertion (a) passed: GET /health → HTTP 200, 'status' key present"

    # Assertion (b): GET /info → HTTP 200 + 'version' key
    info "Smoke test: GET /info ..."
    local info_body
    http_code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${api_url}/info" 2>/dev/null)" \
        || die "Smoke test: GET /info request failed (curl error)"
    [[ "$http_code" == "200" ]] \
        || die "Smoke test: GET /info returned HTTP ${http_code} (expected 200)"
    info_body="$(curl -sf --max-time 10 "${api_url}/info" 2>/dev/null)"
    echo "$info_body" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'version' in d" 2>/dev/null \
        || die "Smoke test: GET /info response missing expected key 'version': ${info_body}"
    info "Smoke assertion (b) passed: GET /info → HTTP 200, 'version' key present"

    info "Smoke test complete"
}

_read_env_var() {  # $1=var name in $SERVICE_ENV; strips surrounding single/double quotes
    local v
    v="$(sed -n "s/^$1=//p" "$SERVICE_ENV" | tail -n1)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

# Step 6.5 bookkeeping: record that this run wrote $1 (a var NAME, or the
# literal '<entire file>' when the whole file was freshly installed) into
# $SERVICE_ENV. Called from every non-dry-run write site. Consumed by the
# mtime-fallback path of Step 6.5 when /proc/<pid>/environ is unreadable.
_note_env_mutation() {
    ENV_MUTATED_VARS+=("$@")
}

# ===========================================================================
# Argument parsing
# ===========================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-user)   AGENT_USER="$2";    shift 2 ;;
        --service-user) SERVICE_USER="$2";  shift 2 ;;
        --env-file)     ENV_FILE_SRC="$2";  shift 2 ;;
        --dry-run)        DRY_RUN=true;        shift   ;;
        --smoke)          SMOKE=true;          shift   ;;
        --with-talos)     WITH_TALOS=true;     shift   ;;
        --with-postgres)  WITH_POSTGRES=true;  shift   ;;
        --restart-if-stale) RESTART_IF_STALE=true; shift ;;
        -h|--help)        usage ;;
        *) die "Unknown option: $1  (run with --help for usage)" ;;
    esac
done

# Extend GIT_HOSTS to include the harness-design origin when --with-talos is set,
# so the harness host is keyscanned into known_hosts on a fresh host.
if $WITH_TALOS; then
    _harness_host="$(git_host_from_url "$HARNESS_DESIGN_REPO_URL")"
    if [[ -n "$_harness_host" ]]; then
        GIT_HOSTS="$(printf '%s\n%s\n' "$GIT_HOSTS" "$_harness_host" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
    fi
fi

[[ $EUID -ne 0 ]] && die "This script must be run as root (use sudo)."

# ===========================================================================
# Single-user mode detection (runs after argument parsing, before derived paths)
# The mode is captured ONCE here; no later step re-reads $DISPATCH_SINGLE_USER.
# ===========================================================================
if [[ "${DISPATCH_SINGLE_USER:-}" == "1" ]]; then
    SINGLE_USER=true
elif [[ -n "${DISPATCH_SINGLE_USER:-}" ]]; then
    die "DISPATCH_SINGLE_USER must be '1' if set; got '${DISPATCH_SINGLE_USER}'. Unset it for two-user mode or set it to '1' for single-user mode."
else
    SINGLE_USER=false
fi

if $SINGLE_USER; then
    if [[ -z "${SUDO_USER:-}" ]] || [[ "${SUDO_USER}" == "root" ]]; then
        die "DISPATCH_SINGLE_USER=1 requires invocation via sudo from a non-root login user; got SUDO_USER=${SUDO_USER:-<unset>}. Re-run as: sudo DISPATCH_SINGLE_USER=1 ./setup-dispatch-host.sh"
    fi
    TARGET_USER="$SUDO_USER"
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    if [[ -z "$TARGET_HOME" ]]; then
        die "Could not resolve home directory for user '${TARGET_USER}' via getent passwd"
    fi
    if [[ ! -d "$TARGET_HOME" ]]; then
        die "Home directory for '${TARGET_USER}' does not exist on disk: ${TARGET_HOME}"
    fi
    # In single-user mode both agent and service run as the same user
    AGENT_USER="$TARGET_USER"
    SERVICE_USER="$TARGET_USER"
    AGENT_HOME="$TARGET_HOME"
    SERVICE_HOME="$TARGET_HOME"
fi

# Derived paths (set after argument parsing and single-user detection)
if ! $SINGLE_USER; then
    AGENT_HOME="/home/${AGENT_USER}"
    SERVICE_HOME="/home/${SERVICE_USER}"
fi
SERVICE_REPO="${SERVICE_HOME}/${REPO_NAME}"
if $SINGLE_USER; then
    SERVICE_ENV="${SERVICE_HOME}/.config/agent-gtd-dispatch/env"
else
    SERVICE_ENV="${SERVICE_HOME}/.env"
fi
AGENT_WORKSPACE="${AGENT_HOME}/workspace"
CLAUDE_SRC="${AGENT_HOME}/.local/bin/claude"

# --- Primary group derivation ---
# On Debian/Ubuntu each user gets a matching private group (USER:USER convention).
# On RHEL/AL2023 the primary group may differ (e.g. 'amazon', 'ec2-user').
# Derive the real primary group once so all chown/install -g calls are portable.
# In single-user mode TARGET_USER already exists, so id -gn is reliable.
# In two-user mode adduser --group (Step 1) always creates a matching private group,
# so AGENT_USER == AGENT_GROUP and SERVICE_USER == SERVICE_GROUP remain correct.
if $SINGLE_USER; then
    TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"
    AGENT_GROUP="$TARGET_GROUP"
    SERVICE_GROUP="$TARGET_GROUP"
else
    AGENT_GROUP="$AGENT_USER"
    SERVICE_GROUP="$SERVICE_USER"
fi

# ===========================================================================
# Banner
# ===========================================================================
echo ""
printf "${GREEN}========================================${RESET}\n"
printf "${GREEN}  Dispatch host installer${RESET}\n"
printf "${GREEN}========================================${RESET}\n"
echo ""
echo "  Agent user:   ${AGENT_USER}  (${AGENT_HOME})"
echo "  Service user: ${SERVICE_USER}  (${SERVICE_HOME})"
echo "  Wheel index:  ${DISPATCH_WHEEL_INDEX:-https://pypi.lab.jasonweddington.com/simple/}"
echo "  Service unit: ${SYSTEMD_UNIT}"
$DRY_RUN && echo "  Mode:         DRY RUN — no mutations"
if $SINGLE_USER; then
    echo "  Mode:         SINGLE-USER (user=${TARGET_USER})"
else
    echo "  Mode:         TWO-USER SPLIT"
fi
echo ""

# ===========================================================================
# Mode mismatch guard (read-only check; runs before any mutations, incl. --dry-run)
# ===========================================================================
_mismatch_errors=()
if $SINGLE_USER; then
    # AC-3: die if two-user-split artifacts are present on this host
    if id -u dispatch-svc &>/dev/null; then
        _mismatch_errors+=("system user 'dispatch-svc' exists")
    fi
    if [[ -f /etc/sudoers.d/dispatch-svc ]]; then
        _mismatch_errors+=("/etc/sudoers.d/dispatch-svc exists")
    fi
    if [[ -f "$SYSTEMD_UNIT" ]]; then
        _unit_user="$(grep -E '^User=' "$SYSTEMD_UNIT" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' ')"
        if [[ -n "$_unit_user" ]] && [[ "$_unit_user" != "$TARGET_USER" ]]; then
            _mismatch_errors+=("${SYSTEMD_UNIT} has User=${_unit_user} (expected ${TARGET_USER})")
        fi
    fi
    if [[ ${#_mismatch_errors[@]} -gt 0 ]]; then
        printf "${RED}[ERROR]${RESET} mode mismatch: this host appears to be configured for the two-user split;\n" >&2
        printf "${RED}[ERROR]${RESET} refusing to create a mixed state. To switch modes, manually rollback per docs/install.md.\n" >&2
        printf "${RED}[ERROR]${RESET} Conflicting artifacts found:\n" >&2
        for _e in "${_mismatch_errors[@]}"; do
            printf "${RED}[ERROR]${RESET}   - %s\n" "$_e" >&2
        done
        exit 1
    fi
else
    # AC-4: die if existing unit has User= that doesn't match the configured $SERVICE_USER
    if [[ -f "$SYSTEMD_UNIT" ]]; then
        _unit_user="$(grep -E '^User=' "$SYSTEMD_UNIT" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' ')"
        if [[ -n "$_unit_user" ]] && [[ "$_unit_user" != "$SERVICE_USER" ]]; then
            die "mode mismatch: this host appears to be configured for single-user mode (${SYSTEMD_UNIT} has User=${_unit_user}, expected ${SERVICE_USER}); refusing to create a mixed state. To switch modes, manually rollback per docs/install.md."
        fi
    fi
fi

# ===========================================================================
# Step 1: User creation
# ===========================================================================
echo "--- Step 1: Users ---"

_create_user() {
    local user="$1" home="$2" gecos="$3"
    if id -u "$user" &>/dev/null; then
        skip "'${user}' user already exists — already configured"
    elif $DRY_RUN; then
        would "create user '${user}' with home ${home}"
    else
        adduser --system --shell /bin/bash --group --home "$home" \
            --gecos "$gecos" "$user"
        info "Created user '${user}'"
    fi
}

if $SINGLE_USER; then
    skip "single-user mode — skipping user creation (running as ${TARGET_USER})"
else
    _create_user "$SERVICE_USER" "$SERVICE_HOME" "Dispatch service account (agent-gtd-dispatch API)"
    _create_user "$AGENT_USER"   "$AGENT_HOME"   "Dispatch agent subprocess user"
fi

# Guard: neither user may be in the sudo group
for u in "$AGENT_USER" "$SERVICE_USER"; do
    if id -u "$u" &>/dev/null && groups "$u" 2>/dev/null | grep -q sudo; then
        die "'${u}' is in the sudo group — this is not allowed. Remove it first."
    fi
done

# Create workspace directory for agent user
# In single-user mode: only touch $AGENT_WORKSPACE — never $AGENT_HOME (operator's
# own home). A group-writable setgid (2775) operator $HOME triggers sshd StrictModes
# pubkey rejection → SSH lockout risk on a dev box.
if $DRY_RUN; then
    would "create ${AGENT_WORKSPACE} owned by ${AGENT_USER} (mode 2775)"
else
    mkdir -p "$AGENT_WORKSPACE"
    if $SINGLE_USER; then
        chown "${AGENT_USER}:${AGENT_GROUP}" "$AGENT_WORKSPACE"
        chmod 2775 "$AGENT_WORKSPACE"
    else
        # Skip recursive chown when all files already have the correct owner/group
        if find "$AGENT_HOME" \( ! -user "$AGENT_USER" -o ! -group "$AGENT_GROUP" \) -print -quit 2>/dev/null | grep -q .; then
            chown -R "${AGENT_USER}:${AGENT_GROUP}" "$AGENT_HOME"
            chmod 2775 "$AGENT_HOME" "$AGENT_WORKSPACE"
            info "Set ownership on ${AGENT_HOME}"
        else
            chmod 2775 "$AGENT_HOME" "$AGENT_WORKSPACE"
            skip "Ownership already correct for ${AGENT_HOME} — already configured"
        fi
    fi
    info "Agent workspace ready: ${AGENT_WORKSPACE}"
fi

# --- SSH key provisioning for AGENT_USER (needed for git auth on fresh box) ---
# In single-user mode the agent IS the operator, who already has a working ~/.ssh and
# git auth (the premise of run-as-self: they reach internal repos as themselves). Never
# chown/chmod the operator's ~/.ssh (A2 group mismatch + sshd StrictModes lockout risk)
# or generate a key in their home — mirrors the $HOME scoping added in 7f806c9.
if $SINGLE_USER; then
    skip "single-user mode — using the operator's existing ~/.ssh and git auth as-is (no chown/keygen on your home). If a git host the agent must clone from isn't yet in your known_hosts, seed it: ssh-keyscan <host> >> ~/.ssh/known_hosts"
elif $DRY_RUN; then
    would "create ${AGENT_HOME}/.ssh/ (mode 700, owner ${AGENT_USER}) if absent"
    would "ssh-keyscan ${GIT_HOSTS}>> ${AGENT_HOME}/.ssh/known_hosts"
    would "generate ed25519 keypair for ${AGENT_USER} if no id_* key exists"
else
    mkdir -p "${AGENT_HOME}/.ssh"
    chmod 700 "${AGENT_HOME}/.ssh"
    chown "${AGENT_USER}:${AGENT_GROUP}" "${AGENT_HOME}/.ssh"
    for gh in $GIT_HOSTS; do
        [[ -z "$gh" ]] && continue
        if ssh-keygen -F "$gh" -f "${AGENT_HOME}/.ssh/known_hosts" >/dev/null 2>&1; then
            skip "Host key for ${gh} already in ${AGENT_HOME}/.ssh/known_hosts — already configured"
        else
            ssh-keyscan "$gh" >> "${AGENT_HOME}/.ssh/known_hosts" 2>/dev/null \
                && info "Populated ${AGENT_HOME}/.ssh/known_hosts via ssh-keyscan ${gh}" \
                || warn "ssh-keyscan ${gh} failed — known_hosts may be incomplete"
        fi
    done
    if ! ls "${AGENT_HOME}/.ssh"/id_* &>/dev/null; then
        runuser -u "$AGENT_USER" -- ssh-keygen -t ed25519 -N "" \
            -f "${AGENT_HOME}/.ssh/id_ed25519" \
            -C "${AGENT_USER}@$(hostname -s)"
        chown "${AGENT_USER}:${AGENT_GROUP}" \
            "${AGENT_HOME}/.ssh/id_ed25519" \
            "${AGENT_HOME}/.ssh/id_ed25519.pub"
        info "Generated SSH keypair for ${AGENT_USER}: ${AGENT_HOME}/.ssh/id_ed25519"
        echo ""
        printf "${YELLOW}========================================${RESET}\n"
        printf "${YELLOW}  ACTION REQUIRED: Add SSH public key  ${RESET}\n"
        printf "${YELLOW}========================================${RESET}\n"
        echo ""
        echo "  A new ed25519 keypair was generated for the '${AGENT_USER}' agent user."
        echo "  Put the public key wherever you host your repos —"
        echo "  e.g. authorized_keys on a local git server, or GitHub Settings → SSH keys."
        echo ""
        echo "  Public key:"
        echo ""
        cat "${AGENT_HOME}/.ssh/id_ed25519.pub"
        echo ""
        echo "  Then re-run this installer with the same arguments:"
        echo "    sudo ./setup-dispatch-host.sh [your original options]"
        echo ""
        die "SSH public key not yet authorized — add it to your git host and re-run"
    fi
    skip "SSH key already present for ${AGENT_USER} at ${AGENT_HOME}/.ssh/ — already configured"
fi

# --- SSH setup for SERVICE_USER (needed for git clone in step 2) ---
if $SINGLE_USER; then
    skip "single-user mode — skipping dispatch-svc SSH key copy (same user)"
elif $DRY_RUN; then
    would "create ${SERVICE_HOME}/.ssh/ (mode 700)"
    would "ssh-keyscan ${GIT_HOSTS}>> ${SERVICE_HOME}/.ssh/known_hosts"
    would "copy ${AGENT_HOME}/.ssh/id_* keys to ${SERVICE_HOME}/.ssh/ if present"
    would "chown -R ${SERVICE_USER}:${SERVICE_GROUP} ${SERVICE_HOME}/.ssh/"
else
    mkdir -p "${SERVICE_HOME}/.ssh"
    chmod 700 "${SERVICE_HOME}/.ssh"
    for gh in $GIT_HOSTS; do
        [[ -z "$gh" ]] && continue
        if ssh-keygen -F "$gh" -f "${SERVICE_HOME}/.ssh/known_hosts" >/dev/null 2>&1; then
            skip "Host key for ${gh} already in ${SERVICE_HOME}/.ssh/known_hosts — already configured"
        else
            ssh-keyscan "$gh" >> "${SERVICE_HOME}/.ssh/known_hosts" 2>/dev/null \
                && info "Populated ${SERVICE_HOME}/.ssh/known_hosts via ssh-keyscan ${gh}" \
                || warn "ssh-keyscan ${gh} failed — known_hosts may be incomplete"
        fi
    done
    # Copy SSH key files from agent user if present (enables git auth for SERVICE_USER)
    # Skip a key's cp/chmod when the destination is byte-identical (idempotent on re-run)
    key_found=false
    any_key_copied=false
    for key in "${AGENT_HOME}/.ssh"/id_*; do
        [[ -f "$key" ]] || continue
        key_found=true
        dest_key="${SERVICE_HOME}/.ssh/$(basename "$key")"
        if cmp -s "$key" "$dest_key" 2>/dev/null; then
            skip "SSH key already present at ${dest_key} — already configured"
        else
            cp "$key" "$dest_key"
            chmod 600 "$dest_key"
            any_key_copied=true
        fi
    done
    if $any_key_copied; then
        info "Copied SSH key(s) from ${AGENT_HOME}/.ssh/ to ${SERVICE_HOME}/.ssh/"
    elif ! $key_found; then
        warn "No id_* keys found in ${AGENT_HOME}/.ssh/ — git clone may fail without auth"
    fi
    # Only chown .ssh when at least one key was freshly copied (avoids unnecessary chown on re-run)
    if $any_key_copied; then
        chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${SERVICE_HOME}/.ssh"
    fi
    info "SSH directory seeded for ${SERVICE_USER}"
fi

# --- Group membership (dispatch-svc needs read access to dispatch group resources) ---
if $SINGLE_USER; then
    skip "single-user mode — skipping group membership setup (same user)"
elif id -u "$SERVICE_USER" &>/dev/null && id -u "$AGENT_USER" &>/dev/null; then
    if getent group "$AGENT_USER" | grep -qw "$SERVICE_USER"; then
        skip "${SERVICE_USER} already in group ${AGENT_USER} — already configured"
    elif $DRY_RUN; then
        would "usermod -aG ${AGENT_USER} ${SERVICE_USER} (for dispatch.db access)"
        would "chmod 2775 ${AGENT_HOME} ${AGENT_WORKSPACE}"
    else
        usermod -aG "$AGENT_USER" "$SERVICE_USER"
        chmod 2775 "$AGENT_HOME" "$AGENT_WORKSPACE"
        info "Added ${SERVICE_USER} to group ${AGENT_USER}; set 2775 on ${AGENT_HOME} and ${AGENT_WORKSPACE}"
    fi
fi

# Fix dispatch.db permissions if it already exists on this host
DB_PATH="${AGENT_WORKSPACE}/dispatch.db"
if [[ -f "$DB_PATH" ]]; then
    if $DRY_RUN; then
        would "chmod g+rw ${DB_PATH} (for ${SERVICE_USER} read/write access via group)"
    elif [[ "$(stat -c '%a' "$DB_PATH")" =~ ^.[2367] ]]; then
        skip "dispatch.db group write bit already set — already configured"
    else
        chmod g+rw "$DB_PATH"
        info "Fixed dispatch.db group permissions: ${DB_PATH}"
    fi
fi

# ===========================================================================
# Step 2: Agent target repo
# ===========================================================================
echo ""
echo "--- Step 2: Agent target repo ---"

# The dispatch service itself is installed from the homelab wheel index in
# Step 4 (uv tool install), after uv is provisioned. No dispatch source tree
# lives on the host.  What remains here is the agent's target repo (agent_gtd)
# — the workspace the spawned agent operates on, not the dispatch service tree.

_clone_repo() {
    local remote="$1" dest="$2" owner="$3"
    if [[ -d "${dest}/.git" ]]; then
        skip "Repo already exists at ${dest} — already configured"
    elif $DRY_RUN; then
        would "clone ${remote} → ${dest}"
    else
        runuser -u "$owner" -- git clone "$remote" "$dest"
        info "Cloned ${remote} → ${dest}"
    fi
}

_clone_repo "$AGENT_GTD_REMOTE_URL" "${SERVICE_HOME}/agent_gtd"       "$SERVICE_USER"

# ===========================================================================
# Step 3: .env file
# ===========================================================================
echo ""
echo "--- Step 3: Environment file ---"

# AC-2: In single-user mode, ensure the XDG-style parent directory exists before
# writing SERVICE_ENV. Two-user mode's $SERVICE_HOME already exists (user was just
# created above), so no extra mkdir is needed for the two-user path.
if $SINGLE_USER && [[ ! -f "$SERVICE_ENV" ]]; then
    _senv_dir="$(dirname "$SERVICE_ENV")"
    if [[ ! -d "$_senv_dir" ]]; then
        if $DRY_RUN; then
            would "create ${_senv_dir}/ (mode 0700, owner ${SERVICE_USER})"
        else
            install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "$_senv_dir"
            info "Created env dir: ${_senv_dir}"
        fi
    fi
fi

# AC-3: In single-user mode, warn if a legacy ~/.env with DISPATCH_API_KEY exists —
# the new XDG path is used from now on; the old file is unrelated (do not auto-migrate).
if $SINGLE_USER && [[ ! -f "$SERVICE_ENV" ]] && [[ -f "${SERVICE_HOME}/.env" ]]; then
    if grep -q '^DISPATCH_API_KEY=' "${SERVICE_HOME}/.env" 2>/dev/null; then
        warn "Found legacy ${SERVICE_HOME}/.env with DISPATCH_API_KEY — single-user mode now uses ${SERVICE_ENV}. Either move it manually (mv ${SERVICE_HOME}/.env ${SERVICE_ENV}) and re-run, or ignore this warning if the existing ~/.env is unrelated."
    fi
fi

if [[ -f "$SERVICE_ENV" ]]; then
    skip "${SERVICE_ENV} already exists — already configured"
elif $DRY_RUN; then
    if [[ -n "$ENV_FILE_SRC" ]]; then
        would "install ${ENV_FILE_SRC} → ${SERVICE_ENV} (mode 0600, owner ${SERVICE_USER})"
    else
        would "generate ${SERVICE_ENV} from ${TMPL_DIR}/dispatch-env.tmpl (mode 0600, owner ${SERVICE_USER})"
    fi
else
    if [[ -n "$ENV_FILE_SRC" ]]; then
        [[ -f "$ENV_FILE_SRC" ]] || die "Env file not found: ${ENV_FILE_SRC}"
        cp "$ENV_FILE_SRC" "$SERVICE_ENV"
        info "Installed env file from ${ENV_FILE_SRC}"
    else
        [[ -f "${TMPL_DIR}/dispatch-env.tmpl" ]] || die "Template not found: ${TMPL_DIR}/dispatch-env.tmpl"
        cp "${TMPL_DIR}/dispatch-env.tmpl" "$SERVICE_ENV"
        warn "Generated .env from template — fill in real values at ${SERVICE_ENV}"
    fi
    chmod 0600 "$SERVICE_ENV"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "$SERVICE_ENV"
    info "Env file installed: ${SERVICE_ENV} (mode 0600)"
    _note_env_mutation '<entire file>'
fi

# In single-user mode, DISPATCH_AGENT_SUBPROCESS_USER must NOT be set —
# the runtime _sudo_wrap already no-ops when it is empty (dispatch.py:34).
if $SINGLE_USER && [[ -f "$SERVICE_ENV" ]]; then
    if grep -q '^DISPATCH_AGENT_SUBPROCESS_USER=' "$SERVICE_ENV" 2>/dev/null; then
        if $DRY_RUN; then
            would "strip DISPATCH_AGENT_SUBPROCESS_USER from ${SERVICE_ENV} (not valid in single-user mode)"
        else
            warn "DISPATCH_AGENT_SUBPROCESS_USER found in ${SERVICE_ENV} — stripping (not applicable in single-user mode; runtime uses direct invocation)"
            sed -i '/^DISPATCH_AGENT_SUBPROCESS_USER=/d' "$SERVICE_ENV"
            info "Stripped DISPATCH_AGENT_SUBPROCESS_USER from ${SERVICE_ENV}"
            _note_env_mutation DISPATCH_AGENT_SUBPROCESS_USER
        fi
    fi
fi

# ===========================================================================
# Step 3.5: DISPATCH_API_KEY (service env)
# ===========================================================================
echo ""
echo "--- Step 3.5: DISPATCH_API_KEY (service env) ---"

if [[ ! -f "$SERVICE_ENV" ]]; then
    if $DRY_RUN; then
        would "mint DISPATCH_API_KEY (python3 secrets.token_urlsafe(32)) and append/replace line in ${SERVICE_ENV} preserving content/owner/0600"
    else
        die "DISPATCH_API_KEY mint: ${SERVICE_ENV} does not exist — Step 3 should have created it"
    fi
else

_api_key_existing="$(_read_env_var DISPATCH_API_KEY)"

if [[ -n "$_api_key_existing" && "$_api_key_existing" != "changeme" ]]; then
    skip "DISPATCH_API_KEY already set in ${SERVICE_ENV} — preserving existing value (re-run after clearing the line to rotate)"
elif $DRY_RUN; then
    would "mint DISPATCH_API_KEY (python3 secrets.token_urlsafe(32)) and append/replace line in ${SERVICE_ENV} preserving content/owner/0600"
else
    _minted_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    _tmpfile="$(mktemp /tmp/dispatch-env.XXXXXX)"
    _MINT_ENV_FILE="$SERVICE_ENV" _MINT_NEW_KEY="$_minted_key" \
    python3 - <<'PYEOF' > "$_tmpfile"
import re, os, sys
env_file = os.environ['_MINT_ENV_FILE']
new_key  = os.environ['_MINT_NEW_KEY']
with open(env_file, 'r') as f:
    content = f.read()
lines = content.splitlines(keepends=True)
replaced = False
out = []
for line in lines:
    if re.match(r'^DISPATCH_API_KEY=(changeme)?\s*$', line.rstrip('\r\n')):
        out.append('DISPATCH_API_KEY=' + new_key + '\n')
        replaced = True
    else:
        out.append(line)
if not replaced:
    if out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.append('DISPATCH_API_KEY=' + new_key + '\n')
sys.stdout.write(''.join(out))
PYEOF
    install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "$_tmpfile" "$SERVICE_ENV"
    rm -f "$_tmpfile"
    echo ""
    printf "${RED}========================================${RESET}\n"
    printf "${RED}  ACTION REQUIRED: Register API key    ${RESET}\n"
    printf "${RED}========================================${RESET}\n"
    echo ""
    echo "  A new DISPATCH_API_KEY was minted and written to:"
    echo "    ${SERVICE_ENV}"
    echo ""
    echo "  Minted key value:"
    echo ""
    echo "    ${_minted_key}"
    echo ""
    echo "  Register this key BEFORE the next restart, in:"
    echo "    Agent GTD Settings → Dispatch hosts → this host's API Key"
    echo "  Dispatches will return 401 until this is done."
    echo ""
    echo "  The key takes effect on the next restart of:"
    echo "    systemctl restart ${SERVICE_NAME}"
    echo "  Step 6 only restarts the service when the rendered unit file differs"
    echo "  from what's installed — it will NOT restart just because this key"
    echo "  changed. Step 6.5 (below) reports whether the running process still"
    echo "  has the old key, and can restart it for you with --restart-if-stale"
    echo "  once zero runs are active."
    echo ""
    info "Minted and installed DISPATCH_API_KEY in ${SERVICE_ENV}"
    _note_env_mutation DISPATCH_API_KEY
fi
fi  # end: if [[ ! -f "$SERVICE_ENV" ]]; else

# ===========================================================================
# Step 3.6: OLLAMA_CLOUD_API_KEY probe (presence ≠ validity)
# ===========================================================================
# Verifies that the key is actually accepted by ollama.com, not merely present.
# This caught a real incident where a truncated key (len 51 vs expected 57)
# silently caused every GLM-family run to 401 at agent runtime (kb-03080).
# Does NOT exit non-zero on failure — setup continues so the rest of the
# service is installed; operator must fix the key and restart.
# ===========================================================================
echo ""
echo "--- Step 3.6: OLLAMA_CLOUD_API_KEY probe ---"

if [[ ! -f "$SERVICE_ENV" ]]; then
    skip "OLLAMA_CLOUD_API_KEY probe skipped (env file does not exist)"
else
    _ollama_cloud_key="$(_read_env_var OLLAMA_CLOUD_API_KEY)"
    if [[ -z "$_ollama_cloud_key" ]]; then
        skip "OLLAMA_CLOUD_API_KEY not set in ${SERVICE_ENV} — skipping probe (GLM engines will be disabled)"
    elif $DRY_RUN; then
        would "probe OLLAMA_CLOUD_API_KEY (len ${#_ollama_cloud_key}) via POST https://ollama.com/api/me (5s timeout) — warn on 401/403, do not exit non-zero"
    else
        _probe_code="$(curl -s -o /dev/null -w "%{http_code}" \
            --max-time 5 \
            -X POST \
            -H "Authorization: Bearer ${_ollama_cloud_key}" \
            -H "Content-Type: application/json" \
            -d '{}' \
            "https://ollama.com/api/me" 2>/dev/null)" || _probe_code="000"
        if [[ "$_probe_code" == "200" ]]; then
            info "OLLAMA_CLOUD_API_KEY probe passed (HTTP 200) — key is valid"
        elif [[ "$_probe_code" == "401" || "$_probe_code" == "403" ]]; then
            echo ""
            printf "${YELLOW}========================================${RESET}\n"
            printf "${YELLOW}  WARNING: OLLAMA_CLOUD_API_KEY INVALID ${RESET}\n"
            printf "${YELLOW}========================================${RESET}\n"
            echo ""
            warn "OLLAMA_CLOUD_API_KEY in ${SERVICE_ENV} was REJECTED by ollama.com (HTTP ${_probe_code})."
            warn "Key length: ${#_ollama_cloud_key} chars (a valid key is typically 57 chars)."
            warn "The GLM-family engines (talos-glm, claude-code-glm, talos-glm-flash) will be"
            warn "UNAVAILABLE until this is fixed. Update OLLAMA_CLOUD_API_KEY in ${SERVICE_ENV}"
            warn "with a valid key from https://ollama.com, then: systemctl restart ${SERVICE_NAME}"
            echo ""
        else
            warn "OLLAMA_CLOUD_API_KEY probe returned HTTP ${_probe_code} (network error or unexpected status) — key validity unknown; GLM engines will attempt to connect at runtime"
        fi
    fi
fi

# ===========================================================================
# Step 4: Install dependencies (uv + agent-gtd-dispatch wheel)
# ===========================================================================
echo ""
echo "--- Step 4: Dependencies ---"

# Homelab wheel index the service is installed from (pi-04 pypi.lab).
DISPATCH_WHEEL_INDEX="${DISPATCH_WHEEL_INDEX:-https://pypi.lab.jasonweddington.com/simple/}"

_ensure_uv() {
    local user="$1" user_home="$2"
    local uv_bin="${user_home}/.local/bin/uv"
    if runuser -u "$user" -- bash -c "[[ -x '${uv_bin}' ]] || command -v uv &>/dev/null"; then
        skip "uv already installed for ${user} — already configured"
    elif $DRY_RUN; then
        would "install uv for ${user} via official installer (curl astral.sh/uv/install.sh)"
    else
        runuser -u "$user" -- bash -c 'curl -fsSL https://astral.sh/uv/install.sh | sh'
        info "Installed uv for ${user}"
    fi
}

_ensure_uv "$SERVICE_USER" "$SERVICE_HOME"
if ! $SINGLE_USER; then
    _ensure_uv "$AGENT_USER" "$AGENT_HOME"
fi

# AC-4: Resolve the real uv binary path for SERVICE_USER (may be brew/apt uv at
# /usr/local/bin/uv rather than the user-local ~/.local/bin/uv). This path is
# substituted into the systemd unit's ExecStart= via the {{UV_BIN}} placeholder.
# Fall back to the user-local path when uv is not yet installed (e.g. --dry-run).
UV_BIN="$(runuser -l "$SERVICE_USER" -c 'command -v uv' 2>/dev/null || true)"
if [[ -z "$UV_BIN" ]]; then
    UV_BIN="${SERVICE_HOME}/.local/bin/uv"
fi

# Install the agent-gtd-dispatch wheel as a uv-managed tool for SERVICE_USER.
# The install is idempotent (repeated runs bring the tool to the current index
# version) and puts the entry point at $SERVICE_HOME/.local/bin/agent-gtd-dispatch,
# which the systemd unit's ExecStart references.
SERVICE_UV="${UV_BIN}"
if $DRY_RUN; then
    would "run 'uv tool install agent-gtd-dispatch --index ${DISPATCH_WHEEL_INDEX}' as ${SERVICE_USER}"
else
    runuser -u "$SERVICE_USER" -- bash -c \
        "'${SERVICE_UV}' tool install agent-gtd-dispatch --index '${DISPATCH_WHEEL_INDEX}'"
    info "Installed agent-gtd-dispatch wheel for ${SERVICE_USER} from ${DISPATCH_WHEEL_INDEX}"
fi

# ===========================================================================
# Step 4.5: Claude Code install for AGENT_USER (idempotent; must precede 5a)
# ===========================================================================
echo ""
echo "--- Step 4.5: Claude Code (agent user) ---"

if [[ -f "$CLAUDE_SRC" ]]; then
    skip "Claude Code already installed for ${AGENT_USER} at ${CLAUDE_SRC} — already configured"
elif $DRY_RUN; then
    would "install Claude Code for ${AGENT_USER} via official installer (curl https://claude.ai/install.sh | bash)"
else
    runuser -u "$AGENT_USER" -- bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
    if [[ -f "$CLAUDE_SRC" ]]; then
        info "Installed Claude Code for ${AGENT_USER}: ${CLAUDE_SRC}"
    else
        warn "Claude Code installer ran but ${CLAUDE_SRC} not found — verify installation"
    fi
fi

# ===========================================================================
# Shared Rust bootstrap helpers (used by Step 4.5b-B/C AND Step 4.9)
# ===========================================================================
# Two steps need rustup + cargo-binstall for AGENT_USER:
#   * Step 4.5b (talos engine, --with-talos only) — builds talos from source.
#   * Step 4.9  (dev toolchain, ALWAYS) — installs the tools that dispatched
#     repos' hooks and gate commands call.
# The install commands live here ONCE so the two call sites can never drift
# (item 75b88467 / AC-2). Each helper carries its own `[[ -x ... ]]` skip guard
# and its own $DRY_RUN branch, so it is idempotent and safe to call from either
# site, in either order, with or without --with-talos.
# ===========================================================================

# Install rustup for AGENT_USER if absent, then guarantee a usable default toolchain.
ensure_rustup() {
    if [[ -x "${AGENT_HOME}/.cargo/bin/rustup" ]]; then
        skip "rustup already installed for ${AGENT_USER} — already configured"
    elif $DRY_RUN; then
        would "install rustup for ${AGENT_USER} via official installer (curl https://sh.rustup.rs | sh -s -- -y, login shell)"
    else
        runuser -l "${AGENT_USER}" -c \
            "curl --proto '=https' --tlsv1.2 -fsSf https://sh.rustup.rs | sh -s -- -y"
        if [[ -x "${AGENT_HOME}/.cargo/bin/rustup" ]]; then
            info "Installed rustup for ${AGENT_USER}"
        else
            die "rustup installer ran but ${AGENT_HOME}/.cargo/bin/rustup not found"
        fi
    fi
    ensure_rust_default_toolchain
}

# Guarantee AGENT_USER has a usable `rustup default` toolchain. A host can have
# rustup installed with toolchains present but no default set (e.g. r7-research,
# 2026-09-17) — every `cargo ...` invocation then fails with "rustup could not
# choose a version of cargo to run". Probes cargo directly (the binary that
# actually failed), not `rustup show active-toolchain`'s exit code, since that
# varies across rustup releases (this fleet runs rustup 1.29.0).
ensure_rust_default_toolchain() {
    if [[ ! -x "${AGENT_HOME}/.cargo/bin/rustup" ]]; then
        if $DRY_RUN; then would "point 'rustup default' at ${RUST_DEFAULT_TOOLCHAIN} for ${AGENT_USER} after installing rustup"; fi
        return 0
    fi

    if _cargo_ver="$(runuser -l "${AGENT_USER}" -c "'${AGENT_HOME}/.cargo/bin/cargo' --version" 2>/dev/null)"; then
        skip "rust default toolchain already usable for ${AGENT_USER} (${_cargo_ver}) — already configured"
        return 0
    fi

    if $DRY_RUN; then
        would "runuser -l ${AGENT_USER} -- rustup default ${RUST_DEFAULT_TOOLCHAIN}"
        return 0
    fi

    _rustup_pre="$(runuser -l "${AGENT_USER}" -c "'${AGENT_HOME}/.cargo/bin/rustup' show active-toolchain" 2>&1 | tr '\n' ' ' || true)"
    warn "No usable rust default toolchain for ${AGENT_USER} (rustup reported: ${_rustup_pre}) — setting default to ${RUST_DEFAULT_TOOLCHAIN}"
    runuser -l "${AGENT_USER}" -c "'${AGENT_HOME}/.cargo/bin/rustup' default ${RUST_DEFAULT_TOOLCHAIN}" \
        || die "rustup default ${RUST_DEFAULT_TOOLCHAIN} failed for ${AGENT_USER}"

    if _cargo_ver="$(runuser -l "${AGENT_USER}" -c "'${AGENT_HOME}/.cargo/bin/cargo' --version" 2>/dev/null)"; then
        info "Set rustup default to ${RUST_DEFAULT_TOOLCHAIN} for ${AGENT_USER}; cargo now reports: ${_cargo_ver}"
    else
        die "rustup default ${RUST_DEFAULT_TOOLCHAIN} succeeded for ${AGENT_USER} but cargo --version still fails"
    fi
}

# Install cargo-binstall for AGENT_USER if absent. Callers must have run
# ensure_rustup first (cargo-binstall lands in ~/.cargo/bin).
ensure_cargo_binstall() {
    if [[ -x "${AGENT_HOME}/.cargo/bin/cargo-binstall" ]]; then
        skip "cargo-binstall already installed for ${AGENT_USER} — already configured"
    elif $DRY_RUN; then
        would "install cargo-binstall for ${AGENT_USER} via install-from-binstall-release.sh (login shell)"
    else
        runuser -l "${AGENT_USER}" -c \
            "curl -L --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/cargo-bins/cargo-binstall/main/install-from-binstall-release.sh | bash"
        if [[ -x "${AGENT_HOME}/.cargo/bin/cargo-binstall" ]]; then
            info "Installed cargo-binstall for ${AGENT_USER}"
        else
            die "cargo-binstall installer ran but ${AGENT_HOME}/.cargo/bin/cargo-binstall not found"
        fi
    fi
}

# ===========================================================================
# Step 4.5b: talos engine provisioning (--with-talos only)
# ===========================================================================
# The talos-* engine family (talos-haiku/sonnet/opus/qwen/glm/glm-flash) invokes the
# `talos` binary as a subprocess. Pass --with-talos to build and install it.
# When absent, this step prints a single [SKIP] line and mutates nothing.
# ===========================================================================
echo ""
echo "--- Step 4.5b: talos engine (agent user) ---"

if ! $WITH_TALOS; then
    skip "talos provisioning skipped — pass --with-talos to build+install the talos engine on this host"
else
    # Ensure AGENT_HOME/.local/bin exists (should already exist; defensive)
    _talos_local_bin="${AGENT_HOME}/.local/bin"
    _talos_dest="${AGENT_HOME}/.local/bin/talos"
    _talos_cargo="${AGENT_HOME}/.cargo/bin/cargo"
    _harness_dest="${AGENT_HOME}/harness-design"

    # Sub-step A: build-essential
    echo ""
    echo "  [4.5b-A] build-essential"
    if dpkg-query -W -f='${Status}' build-essential 2>/dev/null | grep -q 'install ok installed'; then
        skip "build-essential already installed — already configured"
    elif $DRY_RUN; then
        would "apt-get update && apt-get install -y build-essential"
    else
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential
        info "Installed build-essential"
    fi

    # Sub-step B: rustup for AGENT_USER
    # Bootstrap logic lives in ensure_rustup() (shared with Step 4.9 — AC-2);
    # the helper carries the skip guard and the $DRY_RUN branch.
    echo ""
    echo "  [4.5b-B] rustup (${AGENT_USER})"
    ensure_rustup

    # Sub-step C: cargo-binstall + cargo-nextest
    # cargo-binstall bootstrap lives in ensure_cargo_binstall() (shared with Step 4.9).
    # cargo-nextest itself is also in the Step 4.9 toolchain list; installing it here
    # too keeps --with-talos self-contained and is a no-op when 4.9 already did it.
    echo ""
    echo "  [4.5b-C] cargo-nextest (${AGENT_USER})"
    ensure_cargo_binstall
    if [[ -x "${AGENT_HOME}/.cargo/bin/cargo-nextest" ]]; then
        skip "cargo-nextest already installed for ${AGENT_USER} — already configured"
    elif $DRY_RUN; then
        would "runuser -l ${AGENT_USER} -- ${_talos_cargo} binstall -y cargo-nextest"
    else
        # Install cargo-nextest via cargo-binstall (absolute cargo path)
        runuser -l "${AGENT_USER}" -c \
            "${_talos_cargo} binstall -y cargo-nextest"
        if [[ -x "${AGENT_HOME}/.cargo/bin/cargo-nextest" ]]; then
            info "Installed cargo-nextest for ${AGENT_USER}"
        else
            die "cargo-nextest install ran but ${AGENT_HOME}/.cargo/bin/cargo-nextest not found"
        fi
    fi

    # Sub-step D: clone/pull harness-design as AGENT_USER
    echo ""
    echo "  [4.5b-D] harness-design clone/pull (${AGENT_USER})"
    # Creds pre-check: fail fast if the agent user can't reach the harness origin
    if $DRY_RUN; then
        would "git ls-remote ${HARNESS_DESIGN_REPO_URL} HEAD (as ${AGENT_USER}) — fail-fast creds check"
        would "clone ${HARNESS_DESIGN_REPO_URL} → ${_harness_dest} (or git pull if already present)"
    else
        if ! runuser -l "${AGENT_USER}" -c \
                "git ls-remote '${HARNESS_DESIGN_REPO_URL}' HEAD" >/dev/null 2>&1; then
            die "Cannot reach harness-design origin as ${AGENT_USER}. Authorize /home/${AGENT_USER}/.ssh/id_ed25519.pub on the git host (${HARNESS_DESIGN_REPO_URL}) and re-run with --with-talos."
        fi
        if [[ -d "${_harness_dest}/.git" ]]; then
            runuser -l "${AGENT_USER}" -c \
                "git -C '${_harness_dest}' pull --ff-only"
            info "Updated harness-design at ${_harness_dest}"
        else
            runuser -l "${AGENT_USER}" -c \
                "git clone '${HARNESS_DESIGN_REPO_URL}' '${_harness_dest}'"
            info "Cloned harness-design → ${_harness_dest}"
        fi
    fi

    # Sub-step E: cargo build --release -p talos (always runs; incremental makes re-run near-instant)
    echo ""
    echo "  [4.5b-E] cargo build --release -p talos"
    if $DRY_RUN; then
        would "runuser -l ${AGENT_USER} -- ${_talos_cargo} build --release -p talos (in ${_harness_dest})"
    else
        runuser -l "${AGENT_USER}" -c \
            "cd '${_harness_dest}' && '${_talos_cargo}' build --release -p talos"
        [[ -f "${_harness_dest}/target/release/talos" ]] \
            || die "cargo build completed but ${_harness_dest}/target/release/talos not found"
        info "Built talos binary at ${_harness_dest}/target/release/talos"
    fi

    # Sub-step F: install binary (cmp-guarded copy, not symlink)
    echo ""
    echo "  [4.5b-F] install talos binary → ${_talos_dest}"
    if $DRY_RUN; then
        would "install -m 0755 -o ${AGENT_USER} -g ${AGENT_GROUP} ${_harness_dest}/target/release/talos ${_talos_dest}"
    elif cmp -s "${_harness_dest}/target/release/talos" "${_talos_dest}" 2>/dev/null; then
        skip "talos binary already installed and byte-identical — already configured"
    else
        mkdir -p "${_talos_local_bin}"
        install -m 0755 -o "${AGENT_USER}" -g "${AGENT_GROUP}" \
            "${_harness_dest}/target/release/talos" "${_talos_dest}"
        info "Installed talos binary: ${_talos_dest}"
    fi

    # Sub-step G: write TALOS_BIN into SERVICE_ENV (Step-3.5-style python line-rewrite)
    echo ""
    echo "  [4.5b-G] TALOS_BIN in ${SERVICE_ENV}"
    _talos_bin_value="/home/${AGENT_USER}/.local/bin/talos"
    if [[ ! -f "$SERVICE_ENV" ]]; then
        if $DRY_RUN; then
            would "append TALOS_BIN=${_talos_bin_value} to ${SERVICE_ENV}"
        else
            die "TALOS_BIN write: ${SERVICE_ENV} does not exist — Step 3 should have created it"
        fi
    else
        _talos_existing="$(_read_env_var TALOS_BIN)"
        if [[ "$_talos_existing" == "$_talos_bin_value" ]]; then
            skip "TALOS_BIN already set to ${_talos_bin_value} in ${SERVICE_ENV} — already configured"
        elif $DRY_RUN; then
            would "set TALOS_BIN=${_talos_bin_value} in ${SERVICE_ENV} (line-rewrite, preserving all other keys)"
        else
            _talos_tmpfile="$(mktemp /tmp/dispatch-env.XXXXXX)"
            _TALOS_ENV_FILE="$SERVICE_ENV" _TALOS_BIN_VALUE="$_talos_bin_value" \
            python3 - <<'TALOS_PYEOF' > "$_talos_tmpfile"
import re, os, sys
env_file  = os.environ['_TALOS_ENV_FILE']
new_value = os.environ['_TALOS_BIN_VALUE']
with open(env_file, 'r') as f:
    content = f.read()
lines = content.splitlines(keepends=True)
replaced = False
out = []
for line in lines:
    if re.match(r'^TALOS_BIN=', line):
        if line.rstrip('\r\n') == 'TALOS_BIN=' + new_value:
            out.append(line)
        else:
            out.append('TALOS_BIN=' + new_value + '\n')
        replaced = True
    else:
        out.append(line)
if not replaced:
    if out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.append('TALOS_BIN=' + new_value + '\n')
sys.stdout.write(''.join(out))
TALOS_PYEOF
            install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
                "$_talos_tmpfile" "$SERVICE_ENV"
            rm -f "$_talos_tmpfile"
            info "Set TALOS_BIN=${_talos_bin_value} in ${SERVICE_ENV}"
            _note_env_mutation TALOS_BIN
        fi
    fi
fi  # end: if ! $WITH_TALOS

# ===========================================================================
# Step 4.5c: Postgres + pgvector (--with-postgres only)
# ===========================================================================
# Install a local PostgreSQL server with the pgvector extension so headless
# test runs can exercise the PG code paths without reaching out to a desktop
# (or, worse, a shared box holding live data — see kb-03276/kb-03277: pointing
# test suites at a shared Postgres is an ambient-DSN footgun this deliberately
# avoids by keeping the test Postgres local to each dispatch host).
#
# AUTH MODEL (pinned, do not re-derive): Postgres is reachable ONLY over the
# local Unix socket with peer auth. The PG role is named after AGENT_USER (the
# OS user the agent subprocess runs as) — the OS-user/PG-role name match is
# what makes peer auth work with no password and no pg_hba.conf changes. Do
# NOT create a separate 'kbtest' role, do NOT use trust auth, do NOT edit
# pg_hba.conf, and do NOT change listen_addresses or open a TCP listener:
# trust-on-loopback would let any local account on the host connect as a
# CREATEDB role. Postgres's packaged defaults are left alone; the only
# server-side objects created are the role and the template1 extension.
#
# No dedicated test database is created here: personal_kb's test suite
# (kb-core conftest.py::pg_temp_db) connects to the maintenance 'postgres'
# database, runs CREATE DATABASE kb_test_<uuid8> itself, and drops it WITH
# (FORCE) on teardown — so the DSN handed to dispatched runs just points at
# 'postgres'. The vector extension is installed into 'template1' (as
# superuser, once, at provision time) so every database the suite creates
# inherits it automatically — the unprivileged test role must never need to
# run CREATE EXTENSION itself, since pgvector is not a trusted extension.
#
# Re-running on a host that already has Postgres+pgvector is a no-op (every
# sub-step is idempotent).
# ===========================================================================
echo ""
echo "--- Step 4.5c: Postgres + pgvector ---"

if ! $WITH_POSTGRES; then
    skip "Postgres provisioning skipped — pass --with-postgres to install Postgres+pgvector on this host"
else

    # Detect package manager (mirrors the distro-detection approach used elsewhere in the script)
    if command -v apt-get &>/dev/null; then
        _PG_PKG_MGR="apt"
    elif command -v dnf &>/dev/null; then
        _PG_PKG_MGR="dnf"
    elif command -v yum &>/dev/null; then
        _PG_PKG_MGR="yum"
    else
        die "--with-postgres: no supported package manager found (expected apt-get, dnf, or yum)"
    fi

    _PG_ROLE="${AGENT_USER}"          # role name = OS agent user → peer auth via Unix socket
    _PG_MAINT_DB="postgres"           # maintenance DB; the test suite creates/drops its own DBs
    _PG_DSN="postgresql:///${_PG_MAINT_DB}"  # no host/user = Unix socket + OS user as role

    # Helper: idempotent write of a single KEY=VALUE line into SERVICE_ENV,
    # preserving every other key. Mirrors the Step 4.5b-G (TALOS_BIN) pattern.
    _set_service_env_var() {  # $1=VAR_NAME $2=VALUE
        local _name="$1" _value="$2" _existing _tmp
        _existing="$(_read_env_var "$_name")"
        if [[ "$_existing" == "$_value" ]]; then
            skip "${_name} already set in ${SERVICE_ENV} — already configured"
            return
        fi
        if $DRY_RUN; then
            would "set ${_name}=${_value} in ${SERVICE_ENV} (line-rewrite, preserving all other keys)"
            return
        fi
        _tmp="$(mktemp /tmp/dispatch-env.XXXXXX)"
        _SEV_FILE="$SERVICE_ENV" _SEV_NAME="$_name" _SEV_VALUE="$_value" \
        python3 - <<'SEV_PYEOF' > "$_tmp"
import re, os, sys
env_file  = os.environ['_SEV_FILE']
name      = os.environ['_SEV_NAME']
new_value = os.environ['_SEV_VALUE']
with open(env_file, 'r') as f:
    content = f.read()
lines = content.splitlines(keepends=True)
pattern = re.compile('^' + re.escape(name) + '=')
replaced = False
out = []
for line in lines:
    if pattern.match(line):
        out.append(name + '=' + new_value + '\n')
        replaced = True
    else:
        out.append(line)
if not replaced:
    if out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.append(name + '=' + new_value + '\n')
sys.stdout.write(''.join(out))
SEV_PYEOF
        install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "$_tmp" "$SERVICE_ENV"
        rm -f "$_tmp"
        info "Set ${_name}=${_value} in ${SERVICE_ENV}"
        _note_env_mutation "$_name"
    }

    # Helper: build pgvector from source (fallback when no distro package is available)
    _build_pgvector_from_source() {
        local _pg_maj _src
        _pg_maj="$(pg_config --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)"
        [[ -z "$_pg_maj" ]] && die "pg_config not found — cannot determine PG version to build pgvector"
        case "$_PG_PKG_MGR" in
            apt)
                DEBIAN_FRONTEND=noninteractive apt-get install -y \
                    git build-essential "postgresql-server-dev-${_pg_maj}"
                ;;
            dnf|yum)
                "$_PG_PKG_MGR" install -y git gcc make redhat-rpm-config postgresql-devel
                ;;
        esac
        _src="/tmp/pgvector-src-$$"
        git clone --depth 1 https://github.com/pgvector/pgvector.git "$_src"
        (cd "$_src" && make && make install) \
            || die "pgvector build from source failed"
        rm -rf "$_src"
        info "Built and installed pgvector from source"
    }

    # Sub-step A: Install PostgreSQL server
    echo ""
    echo "  [4.5c-A] Install PostgreSQL server"
    case "$_PG_PKG_MGR" in
        apt)
            if dpkg-query -W -f='${Status}' postgresql 2>/dev/null | grep -q 'install ok installed'; then
                skip "postgresql already installed — already configured"
            elif $DRY_RUN; then
                would "apt-get update && apt-get install -y postgresql postgresql-contrib"
            else
                DEBIAN_FRONTEND=noninteractive apt-get update -qq
                DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql postgresql-contrib
                info "Installed postgresql"
            fi
            ;;
        dnf|yum)
            if rpm -q postgresql-server &>/dev/null; then
                skip "postgresql-server already installed — already configured"
            elif $DRY_RUN; then
                would "${_PG_PKG_MGR} install -y postgresql-server postgresql-contrib"
                would "postgresql-setup --initdb (if cluster not yet initialized)"
            else
                "$_PG_PKG_MGR" install -y postgresql-server postgresql-contrib
                info "Installed postgresql-server"
                if [[ ! -f "/var/lib/pgsql/data/PG_VERSION" ]]; then
                    postgresql-setup --initdb
                    info "Initialized PostgreSQL cluster"
                else
                    skip "PostgreSQL cluster already initialized — already configured"
                fi
            fi
            ;;
    esac

    # Sub-step B: Enable + start PostgreSQL service
    echo ""
    echo "  [4.5c-B] Enable + start PostgreSQL service"
    _PG_SERVICE="postgresql"
    if $DRY_RUN; then
        would "systemctl enable ${_PG_SERVICE} && systemctl start ${_PG_SERVICE}"
    else
        if systemctl is-enabled --quiet "$_PG_SERVICE" 2>/dev/null; then
            skip "${_PG_SERVICE} already enabled — already configured"
        else
            systemctl enable "$_PG_SERVICE"
            info "Enabled ${_PG_SERVICE} service"
        fi
        if systemctl is-active --quiet "$_PG_SERVICE" 2>/dev/null; then
            skip "${_PG_SERVICE} already running — already configured"
        else
            systemctl start "$_PG_SERVICE"
            info "Started ${_PG_SERVICE} service"
        fi
    fi

    # Sub-step C: Install pgvector extension
    echo ""
    echo "  [4.5c-C] Install pgvector"
    if $DRY_RUN; then
        would "install pgvector via ${_PG_PKG_MGR} package (postgresql-XX-pgvector / pgvector_XX); fall back to source build if unavailable"
    else
        # Idempotent check: ask pg_config whether the vector.so is already present
        _pg_libdir="$(pg_config --pkglibdir 2>/dev/null || true)"
        if [[ -n "$_pg_libdir" ]] && [[ -f "${_pg_libdir}/vector.so" ]]; then
            skip "pgvector already installed (${_pg_libdir}/vector.so) — already configured"
        else
            case "$_PG_PKG_MGR" in
                apt)
                    _pg_maj_c="$(pg_config --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)"
                    _pgvec_pkg="postgresql-${_pg_maj_c}-pgvector"
                    # Try versioned package first; fall back to unversioned meta-package; then source
                    if [[ -n "$_pg_maj_c" ]] && apt-cache show "$_pgvec_pkg" &>/dev/null 2>&1; then
                        DEBIAN_FRONTEND=noninteractive apt-get install -y "$_pgvec_pkg"
                        info "Installed pgvector via package ${_pgvec_pkg}"
                    elif apt-cache show postgresql-pgvector &>/dev/null 2>&1; then
                        DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-pgvector
                        info "Installed pgvector via package postgresql-pgvector"
                    else
                        warn "No pgvector apt package found — building from source"
                        _build_pgvector_from_source
                    fi
                    ;;
                dnf|yum)
                    _pg_maj_c="$(pg_config --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || echo "16")"
                    _pgvec_pkg="pgvector_${_pg_maj_c}"
                    if "$_PG_PKG_MGR" install -y "$_pgvec_pkg" 2>/dev/null; then
                        info "Installed pgvector via package ${_pgvec_pkg}"
                    else
                        warn "Package ${_pgvec_pkg} not available — building pgvector from source"
                        _build_pgvector_from_source
                    fi
                    ;;
            esac
        fi
    fi

    # Sub-step D: Create role (no superuser, no password) via peer auth. The
    # role name matches AGENT_USER exactly — that identity match is what lets
    # the OS user authenticate over the Unix socket with zero Postgres-side
    # credential configuration.
    echo ""
    echo "  [4.5c-D] Create role '${_PG_ROLE}'"
    if $DRY_RUN; then
        would "sudo -u postgres psql: CREATE ROLE ${_PG_ROLE} WITH LOGIN CREATEDB (role-exists guard, no superuser, no password)"
    else
        if sudo -u postgres psql -tAc \
                "SELECT 1 FROM pg_roles WHERE rolname='${_PG_ROLE}';" 2>/dev/null | grep -q 1; then
            skip "PG role '${_PG_ROLE}' already exists — already configured"
        else
            sudo -u postgres psql -c "CREATE ROLE ${_PG_ROLE} WITH LOGIN CREATEDB;"
            info "Created PG role '${_PG_ROLE}' (LOGIN CREATEDB, no superuser, no password)"
        fi
    fi

    # Sub-step E: vector extension in 'template1'
    # template1 is the template CREATE DATABASE clones by default, so every
    # database the test suite creates — including the throwaway kb_test_<uuid8>
    # databases pg_temp_db creates and drops itself — inherits the extension;
    # the unprivileged role's own `CREATE EXTENSION IF NOT EXISTS vector`
    # becomes a no-op instead of failing on missing superuser.
    echo ""
    echo "  [4.5c-E] vector extension in 'template1'"
    if $DRY_RUN; then
        would "sudo -u postgres psql -d template1: CREATE EXTENSION IF NOT EXISTS vector"
    else
        sudo -u postgres psql -d template1 \
            -c "CREATE EXTENSION IF NOT EXISTS vector;" \
            && info "Ensured extension 'vector' is present in 'template1' (inherited by every new database)" \
            || die "Failed to CREATE EXTENSION vector in 'template1' — is pgvector installed?"
    fi

    # Sub-step F: write KB_TEST_DATABASE_URL + KB_REQUIRE_POSTGRES_TESTS to SERVICE_ENV
    # KB_REQUIRE_POSTGRES_TESTS=1 is the flag consuming repos' conftest will read to
    # turn a missing-DSN skip into a hard failure (that conftest change is out of
    # scope here — see the GTD item — but the flag is safe to set unconditionally
    # now: it has no effect until a conftest honors it).
    echo ""
    echo "  [4.5c-F] KB_TEST_DATABASE_URL + KB_REQUIRE_POSTGRES_TESTS in ${SERVICE_ENV}"
    if [[ ! -f "$SERVICE_ENV" ]]; then
        if $DRY_RUN; then
            would "append KB_TEST_DATABASE_URL=${_PG_DSN} and KB_REQUIRE_POSTGRES_TESTS=1 to ${SERVICE_ENV}"
        else
            die "KB_TEST_DATABASE_URL write: ${SERVICE_ENV} does not exist — Step 3 should have created it"
        fi
    else
        _set_service_env_var KB_TEST_DATABASE_URL "$_PG_DSN"
        _set_service_env_var KB_REQUIRE_POSTGRES_TESTS "1"
    fi

    # Sub-step G: Postgres smoke check (when --smoke + --with-postgres)
    # The real functional check, run AS the AGENT_USER OS user (peer auth
    # requires it — root has no matching PG role): create a throwaway
    # database, confirm vector is present WITHOUT running CREATE EXTENSION
    # (proves template1 inheritance, not a per-database install), create a
    # table with a vector(1024) column, round-trip one row, then drop the
    # database.
    echo ""
    echo "  [4.5c-G] Postgres smoke check (peer auth, template1 inheritance, vector round-trip)"
    if $DRY_RUN; then
        would "as ${AGENT_USER} over the socket: CREATE DATABASE kb_smoke_<rand>; confirm vector present without CREATE EXTENSION; CREATE TABLE with vector(1024) col; INSERT + SELECT one row; DROP DATABASE"
    elif $SMOKE; then
        _smoke_db="kb_smoke_$(tr -dc 'a-z0-9' </dev/urandom 2>/dev/null | head -c8)"
        [[ -z "$_smoke_db" || "$_smoke_db" == "kb_smoke_" ]] && _smoke_db="kb_smoke_$$"
        _smoke_db_dsn="postgresql:///${_smoke_db}"
        _smoke_vec_vals="$(printf '0.1,%.0s' $(seq 1 1024))"
        _smoke_vec_literal="[${_smoke_vec_vals%,}]"

        runuser -u "${AGENT_USER}" -- psql "$_PG_DSN" -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${_smoke_db};" \
            || die "Postgres smoke check FAILED: could not CREATE DATABASE ${_smoke_db} as ${AGENT_USER} (peer auth) over the socket"
        info "Smoke: created throwaway database '${_smoke_db}' as ${AGENT_USER} (peer auth, no CREATE EXTENSION needed yet)"

        if runuser -u "${AGENT_USER}" -- psql "$_smoke_db_dsn" -v ON_ERROR_STOP=1 \
                -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';" \
                -c "CREATE TABLE smoke_vec (id serial PRIMARY KEY, embedding vector(1024));" \
                -c "INSERT INTO smoke_vec (embedding) VALUES ('${_smoke_vec_literal}');" \
                -tAc "SELECT count(*) FROM smoke_vec;" > "/tmp/pg-smoke-out.$$" 2>&1; then
            _smoke_vec_ver="$(sed -n '1p' "/tmp/pg-smoke-out.$$" | tr -d '[:space:]')"
            _smoke_rowcount="$(tail -n1 "/tmp/pg-smoke-out.$$" | tr -d '[:space:]')"
            rm -f "/tmp/pg-smoke-out.$$"
            runuser -u "${AGENT_USER}" -- psql "$_PG_DSN" -v ON_ERROR_STOP=1 -c "DROP DATABASE ${_smoke_db};" \
                || warn "Smoke: could not drop throwaway database '${_smoke_db}' — clean up manually"
            if [[ -n "$_smoke_vec_ver" && "$_smoke_rowcount" == "1" ]]; then
                info "Postgres smoke check passed: '${_smoke_db}' inherited vector v${_smoke_vec_ver} from template1 (no CREATE EXTENSION run), and round-tripped a vector(1024) row as ${AGENT_USER} over the socket"
            else
                die "Postgres smoke check FAILED: expected a non-empty vector version and 1 row in smoke_vec, got version='${_smoke_vec_ver}' rows='${_smoke_rowcount}'"
            fi
        else
            _smoke_err="$(cat "/tmp/pg-smoke-out.$$" 2>/dev/null)"
            rm -f "/tmp/pg-smoke-out.$$"
            runuser -u "${AGENT_USER}" -- psql "$_PG_DSN" -c "DROP DATABASE IF EXISTS ${_smoke_db};" &>/dev/null || true
            die "Postgres smoke check FAILED: vector extension not inherited / table/insert failed in '${_smoke_db}' as ${AGENT_USER} — check the template1 extension install: ${_smoke_err}"
        fi
    else
        skip "Postgres smoke check skipped (pass --smoke to run)"
    fi

fi  # end: if ! $WITH_POSTGRES

# ===========================================================================
# Step 4.6: MCP servers (agent user)
# ===========================================================================
echo ""
echo "--- Step 4.6: MCP servers (agent user) ---"

if ! $DRY_RUN && [[ ! -f "$CLAUDE_SRC" ]]; then
    warn "Claude Code not found at ${CLAUDE_SRC} — skipping MCP server registration (install claude as ${AGENT_USER} first)"
elif $DRY_RUN; then
    would "read AGENT_GTD_URL, AGENT_GTD_API_KEY, AGENT_GTD_MCP_SRC, PERSONAL_KB_URL, PERSONAL_KB_API_KEY, TEAM_KB_URL, TEAM_KB_API_KEY, PERSONAL_KB_MCP_SRC from ${SERVICE_ENV}"
    would "GET <PERSONAL_KB_URL>/api/health and <TEAM_KB_URL>/api/health (reachability warn-check)"
    would "probe each registered MCP server as ${AGENT_USER} with an MCP initialize + tools/list handshake (timeout ${MCP_PROBE_TIMEOUT}s per server)"
else
    MCP_CONF="${TMPL_DIR}/mcp-servers.sh"
    if [[ ! -f "$MCP_CONF" ]]; then
        die "MCP server config not found at ${MCP_CONF} — cannot register MCP servers"
    fi
    # Pull secrets and config from the installed service .env and export so
    # mcp-servers.sh can inject them per-server.  Variables exported here:
    #
    #   AGENT_GTD_URL         → GTD app base URL for the agent-gtd MCP server.
    #   AGENT_GTD_API_KEY     → API key for the agent-gtd MCP server.
    #     LOAD-BEARING: agent-gtd MCP must connect for Step 4 verification to pass.
    #     If either var is unset, the agent can't comment back and dispatch will appear
    #     to succeed while the step-4 verification stays stuck.
    #
    #   AGENT_GTD_MCP_SRC     → optional override for the agent-gtd package source
    #     (e.g. git+ssh://git@<host>/path/agent_gtd for a homelab/private mirror).
    #     Defaults to public GitHub when unset — no entry needed for standard installs.
    #
    #   PERSONAL_KB_MCP_SRC   → optional override for the personal_kb package source,
    #     used by BOTH KB servers. Set it to a PINNED ref
    #     (…/personal_kb'[postgres]'@<sha>) on production hosts: the default tracks the
    #     branch head, and an upstream rewrite is exactly what silently broke KB access
    #     for every dispatched agent (this item). Defaults to the unpinned homelab source.
    #
    #   PERSONAL_KB_URL / PERSONAL_KB_API_KEY → hosted personal-KB service URL + API
    #     key, injected into the personal-kb MCP server's own env block. personal-kb is
    #     skipped when either is unset.
    #   TEAM_KB_URL / TEAM_KB_API_KEY → hosted team-KB service URL + API key, injected
    #     into the team-kb MCP server's own env block (bound there to the same
    #     PERSONAL_KB_URL / PERSONAL_KB_API_KEY variable NAMES — see
    #     templates/mcp-servers.sh; personal_kb's config module reads those names
    #     regardless of which service it is pointed at). team-kb is skipped when
    #     either TEAM_KB_URL or TEAM_KB_API_KEY is unset.
    if [[ -f "$SERVICE_ENV" ]]; then
        AGENT_GTD_URL="$(_read_env_var AGENT_GTD_URL)";                 export AGENT_GTD_URL
        AGENT_GTD_API_KEY="$(_read_env_var AGENT_GTD_API_KEY)";         export AGENT_GTD_API_KEY
        AGENT_GTD_MCP_SRC="$(_read_env_var AGENT_GTD_MCP_SRC)";         export AGENT_GTD_MCP_SRC
        PERSONAL_KB_URL="$(_read_env_var PERSONAL_KB_URL)";             export PERSONAL_KB_URL
        PERSONAL_KB_API_KEY="$(_read_env_var PERSONAL_KB_API_KEY)";     export PERSONAL_KB_API_KEY
        TEAM_KB_URL="$(_read_env_var TEAM_KB_URL)";                     export TEAM_KB_URL
        TEAM_KB_API_KEY="$(_read_env_var TEAM_KB_API_KEY)";             export TEAM_KB_API_KEY
        PERSONAL_KB_MCP_SRC="$(_read_env_var PERSONAL_KB_MCP_SRC)";     export PERSONAL_KB_MCP_SRC
    fi
    # agent-gtd warnings are elevated (LOAD-BEARING for step-4 verification)
    [[ -z "${AGENT_GTD_URL:-}" ]]     && warn "AGENT_GTD_URL not set in ${SERVICE_ENV} — agent-gtd MCP will launch without a URL; Step 4 verification will fail"
    [[ -z "${AGENT_GTD_API_KEY:-}" ]] && warn "AGENT_GTD_API_KEY not set in ${SERVICE_ENV} — agent-gtd MCP will launch without credentials; Step 4 verification will fail"
    [[ -z "${PERSONAL_KB_URL:-}" ]] && warn "PERSONAL_KB_URL not set in ${SERVICE_ENV} — personal-kb MCP server will be skipped"
    [[ -n "${PERSONAL_KB_URL:-}" && -z "${PERSONAL_KB_API_KEY:-}" ]] && warn "PERSONAL_KB_API_KEY not set in ${SERVICE_ENV} — personal-kb MCP server will be skipped (a URL without a key cannot authenticate)"
    [[ -z "${TEAM_KB_URL:-}" ]] && warn "TEAM_KB_URL not set in ${SERVICE_ENV} — team-kb MCP server will be skipped"
    [[ -n "${TEAM_KB_URL:-}" && -z "${TEAM_KB_API_KEY:-}" ]] && warn "TEAM_KB_API_KEY not set in ${SERVICE_ENV} — team-kb MCP server will be skipped (a URL without a key cannot authenticate)"

    # Reachability warn-check: both KB services are LAN-only (no public DNS/ingress),
    # so a failure here is informational, never fatal — registration continues either way.
    if [[ -n "${PERSONAL_KB_URL:-}" ]]; then
        _url="${PERSONAL_KB_URL%/}"
        rc=0; out="$(curl -sS --max-time 10 -w '\n%{http_code}' "${_url}/api/health" 2>&1)" || rc=$?
        _http_code="$(printf '%s' "$out" | tail -n1)"
        _body="$(printf '%s' "$out" | sed '$d')"
        if [[ "$rc" -eq 0 && "$_http_code" == "200" ]] && printf '%s' "$_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            info "KB health check passed: ${_url}/api/health"
        else
            warn "KB health check failed: ${_url}/api/health http=${_http_code} — both KB services are LAN-only (no public DNS/ingress); registration continues"
        fi
    fi
    if [[ -n "${TEAM_KB_URL:-}" ]]; then
        _url="${TEAM_KB_URL%/}"
        rc=0; out="$(curl -sS --max-time 10 -w '\n%{http_code}' "${_url}/api/health" 2>&1)" || rc=$?
        _http_code="$(printf '%s' "$out" | tail -n1)"
        _body="$(printf '%s' "$out" | sed '$d')"
        if [[ "$rc" -eq 0 && "$_http_code" == "200" ]] && printf '%s' "$_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            info "KB health check passed: ${_url}/api/health"
        else
            warn "KB health check failed: ${_url}/api/health http=${_http_code} — both KB services are LAN-only (no public DNS/ingress); registration continues"
        fi
    fi

    # shellcheck source=templates/mcp-servers.sh
    source "$MCP_CONF"

    # Unconditional de-registration pass over the fixed set of known server names —
    # this is what makes a server that's newly skipped (e.g. a KB var removed from the
    # .env) actually disappear, instead of surviving in its stale shape from a
    # previous run. The per-entry remove-then-add loop below is no longer needed.
    for _known_name in agent-gtd aws-documentation-mcp-server personal-kb team-kb; do
        runuser -l "$AGENT_USER" -c \
            "cd '${AGENT_HOME}' && ${CLAUDE_SRC} mcp remove ${_known_name} --scope user 2>/dev/null || true"
    done

    for entry in "${MCP_SERVERS[@]}"; do
        mcp_name="${entry%%|*}"
        mcp_args="${entry#*|}"
        rc=0
        # word-split mcp_args intentionally — they are space-separated CLI flags
        # shellcheck disable=SC2086
        out="$(runuser -l "$AGENT_USER" -c \
            "cd '${AGENT_HOME}' && ${CLAUDE_SRC} mcp add ${mcp_name} ${mcp_args}" 2>&1)" || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            warn "claude mcp add ${mcp_name} failed (rc=${rc}): ${out}"
            continue
        fi
        case "$mcp_name" in
            agent-gtd)
                info "Registered MCP server '${mcp_name}' for ${AGENT_USER} src=${_agent_gtd_mcp_src}"
                ;;
            personal-kb)
                _api_key_state="ABSENT"; [[ -n "${PERSONAL_KB_API_KEY:-}" ]] && _api_key_state="present"
                info "Registered MCP server '${mcp_name}' for ${AGENT_USER} — mode=hosted url=${PERSONAL_KB_URL:-} api_key=${_api_key_state} src=${_personal_kb_mcp_src}"
                ;;
            team-kb)
                _api_key_state="ABSENT"; [[ -n "${TEAM_KB_API_KEY:-}" ]] && _api_key_state="present"
                info "Registered MCP server '${mcp_name}' for ${AGENT_USER} — mode=hosted url=${TEAM_KB_URL:-} api_key=${_api_key_state} src=${_personal_kb_mcp_src}"
                ;;
            *)
                info "Registered MCP server '${mcp_name}' for ${AGENT_USER}"
                ;;
        esac
    done

    # agent-gtd is LOAD-BEARING: assert the REGISTERED (not just intended) env carries
    # a non-empty AGENT_GTD_URL, or the server silently falls back to its local SQLite
    # backend and Step 4 verification would pass against the wrong database.
    rc=0
    agent_gtd_url_registered="$(runuser -l "$AGENT_USER" -c \
        "python3 -c \"import json; d=json.load(open('${AGENT_HOME}/.claude.json')); print(d.get('mcpServers', {}).get('agent-gtd', {}).get('env', {}).get('AGENT_GTD_URL', ''))\"" \
        2>/dev/null)" || rc=$?
    if [[ "$rc" -ne 0 || -z "$agent_gtd_url_registered" ]]; then
        die "agent-gtd MCP registered without AGENT_GTD_URL — the server would fall back to its local SQLite backend and Step 4 verification would silently pass against the wrong database"
    fi

    # MCP probes: a real initialize + tools/list handshake per registered server, as
    # the agent user. This replaces the old `claude` list-servers smoke test, which
    # reports every server as failed when run from a shell whose cwd/PATH differ from
    # the agent's — a registration-name check, not a health check (kb-03289).
    if [[ "${MCP_PROBE_TIMEOUT}" == "0" ]]; then
        skip "MCP probes skipped (MCP_PROBE_TIMEOUT=0)"
    else
        _mcp_probe_tmp="$(mktemp)"
        trap 'rm -f "${_mcp_probe_tmp}"' EXIT
        cp "${TMPL_DIR}/mcp-probe.py" "${_mcp_probe_tmp}"
        chmod 0755 "${_mcp_probe_tmp}"

        info "MCP probes: up to ${MCP_PROBE_TIMEOUT}s per server × ${#MCP_SERVERS[@]} servers — a cold uv cache can make the first run take several minutes per server"

        _mcp_probe() {  # $1=name $2=expect_tool(or "") $3=extra env prefix (or "")
            local name="$1" expect_tool="$2" env_prefix="$3" expect_flag=""
            [[ -n "$expect_tool" ]] && expect_flag="--expect-tool '${expect_tool}'"
            local rc=0
            local out
            out="$(runuser -l "$AGENT_USER" -c \
                "cd '${AGENT_HOME}' && ${env_prefix} python3 '${_mcp_probe_tmp}' --claude-json '${AGENT_HOME}/.claude.json' --name '${name}' --timeout ${MCP_PROBE_TIMEOUT} ${expect_flag}" \
                2>&1)" || rc=$?
            _MCP_PROBE_RC=$rc
            _MCP_PROBE_OUT="$out"
        }

        for entry in "${MCP_SERVERS[@]}"; do
            mcp_name="${entry%%|*}"
            case "$mcp_name" in
                agent-gtd)   expect_tool="add_item" ;;
                personal-kb) expect_tool="kb_search" ;;
                team-kb)     expect_tool="team_kb_search" ;;
                *)           expect_tool="" ;;
            esac

            if [[ "$mcp_name" == "agent-gtd" ]]; then
                # AGENT_GTD_API_KEY is deliberately NOT baked into the registration
                # (see mcp-servers.sh), so it must be supplied as an extra env var at
                # probe time — mirroring the run-time injection in
                # engines.py::build_env(), where the dispatch worker sets this same
                # var per-run. A probe against only the registered env would test a
                # configuration that never runs in production.
                _mcp_probe "$mcp_name" "$expect_tool" "AGENT_GTD_API_KEY='${AGENT_GTD_API_KEY:-}'"
                if [[ "$_MCP_PROBE_RC" -ne 0 ]]; then
                    if [[ "$_MCP_PROBE_RC" -eq 1 ]]; then
                        die "agent-gtd MCP probe failed: ${_MCP_PROBE_OUT}"
                    fi
                    # rc 2 (launch/resolution) or 3 (timeout): retry once — cold uv
                    # cache building agent-gtd-mcp from source is the common cause.
                    _mcp_probe "$mcp_name" "$expect_tool" "AGENT_GTD_API_KEY='${AGENT_GTD_API_KEY:-}'"
                    if [[ "$_MCP_PROBE_RC" -eq 1 ]]; then
                        die "agent-gtd MCP probe failed: ${_MCP_PROBE_OUT}"
                    elif [[ "$_MCP_PROBE_RC" -ne 0 ]]; then
                        warn "agent-gtd MCP probe could not complete (${_MCP_PROBE_OUT}) — cold uv cache or git/PyPI reachability; registration is in place, re-run the installer to confirm"
                    fi
                fi
            else
                _mcp_probe "$mcp_name" "$expect_tool" ""
                if [[ "$_MCP_PROBE_RC" -ne 0 ]]; then
                    warn "${_MCP_PROBE_OUT}"
                fi
            fi

            if [[ "$_MCP_PROBE_RC" -eq 0 ]]; then
                info "${_MCP_PROBE_OUT}"
            fi
            _elapsed="$(printf '%s' "$_MCP_PROBE_OUT" | grep -o 'elapsed_s=[0-9]*' | head -n1 | cut -d= -f2)"
            if [[ -n "${_elapsed:-}" && "$_elapsed" -gt 300 ]]; then
                warn "MCP probe for ${mcp_name} took ${_elapsed}s of a ${MCP_PROBE_TIMEOUT}s budget — raise MCP_PROBE_TIMEOUT before it breaches"
            fi
        done

        rm -f "${_mcp_probe_tmp}"
        trap - EXIT
    fi
fi

# ===========================================================================
# Step 4.7: Pre-commit template directory (agent user)
# ===========================================================================
echo ""
echo "--- Step 4.7: Pre-commit template (agent user) ---"

PRECOMMIT_BIN="${AGENT_HOME}/.local/bin/pre-commit"
GIT_TEMPLATE_DIR="${AGENT_HOME}/.git-template"

# Sub-action A: install pre-commit as a uv tool for AGENT_USER
if [[ -f "$PRECOMMIT_BIN" ]] && runuser -l "$AGENT_USER" -c 'pre-commit --version' &>/dev/null; then
    skip "pre-commit already installed for ${AGENT_USER} — already configured"
elif $DRY_RUN; then
    would "install pre-commit as a uv tool for ${AGENT_USER}"
else
    runuser -l "$AGENT_USER" -c 'uv tool install pre-commit'
    info "Installed pre-commit for ${AGENT_USER}"
fi

# Sub-action B: set init.templateDir in AGENT_USER's global git config
current_templatedir="$(runuser -l "$AGENT_USER" -c 'git config --global --get init.templateDir' 2>/dev/null || true)"
if [[ "$current_templatedir" == "$GIT_TEMPLATE_DIR" ]]; then
    skip "init.templateDir already set to ${GIT_TEMPLATE_DIR} for ${AGENT_USER} — already configured"
elif $DRY_RUN; then
    would "set init.templateDir = ${GIT_TEMPLATE_DIR} in ${AGENT_USER} global git config"
else
    runuser -l "$AGENT_USER" -c "git config --global init.templateDir '${GIT_TEMPLATE_DIR}'"
    info "Set init.templateDir = ${GIT_TEMPLATE_DIR} for ${AGENT_USER}"
fi

# Sub-action C: render hook shims into the template directory (idempotent re-render; always run)
if $DRY_RUN; then
    would "pre-commit init-templatedir -t pre-commit -t commit-msg -t pre-push ${GIT_TEMPLATE_DIR} (as ${AGENT_USER})"
else
    runuser -l "$AGENT_USER" -c "pre-commit init-templatedir -t pre-commit -t commit-msg -t pre-push ${GIT_TEMPLATE_DIR}"
    info "Rendered pre-commit hook shims into ${GIT_TEMPLATE_DIR} for ${AGENT_USER}"
fi

# ===========================================================================
# Step 4.8: lefthook (agent user)
# ===========================================================================
echo ""
echo "--- Step 4.8: lefthook (agent user) ---"

LEFTHOOK_EXE="${AGENT_HOME}/.local/bin/lefthook"

if [[ -f "$LEFTHOOK_EXE" ]] && lefthook_ver="$(runuser -l "$AGENT_USER" -c 'lefthook version' 2>/dev/null)"; then
    skip "lefthook ${lefthook_ver} already installed for ${AGENT_USER} — already configured"
elif $DRY_RUN; then
    would "install lefthook as a uv tool for ${AGENT_USER} (uv tool install lefthook)"
else
    runuser -l "$AGENT_USER" -c 'uv tool install lefthook'
    lefthook_ver="$(runuser -l "$AGENT_USER" -c 'lefthook version' 2>/dev/null)" || die "lefthook installed but 'lefthook version' failed for ${AGENT_USER}"
    info "Installed lefthook ${lefthook_ver} for ${AGENT_USER}"
fi

# ===========================================================================
# Step 4.9: Dev toolchain (Rust tools + gitleaks, agent user)
# ===========================================================================
# Step 4.8 installs the lefthook *runner*; this step installs the tools a
# dispatched repo's hooks and gate command actually shell out to (harness-design's
# lefthook.yml calls cog, typos, cargo-sort, cargo-deny, cargo-llvm-cov,
# cargo-machete, cargo-nextest and gitleaks). Dispatched agents run as the
# unprivileged AGENT_USER and cannot install anything, so provisioning belongs here.
#
# DESIGN DECISION (item 75b88467, AC-1) — this step does NOT require --with-talos.
# It unconditionally bootstraps rustup + cargo-binstall for AGENT_USER when absent,
# reusing ensure_rustup() / ensure_cargo_binstall() — the same install commands
# Step 4.5b-B/C uses, defined once (AC-2). Rationale: claude-code-* dispatches are
# the majority of traffic and need this toolchain just as much as talos runs do.
# Gating it behind --with-talos (a talos-engine-only opt-in) would leave most hosts
# unprovisioned and defeat the purpose of this step.
#
# The tool list is data, not code: see templates/dev-toolchain.sh. Adding a tool a
# future repo needs is a one-line change there — never edit the loop below.
# ===========================================================================
echo ""
echo "--- Step 4.9: Dev toolchain (Rust tools + gitleaks, agent user) ---"

TOOLCHAIN_CONF="${TMPL_DIR}/dev-toolchain.sh"
if [[ ! -f "$TOOLCHAIN_CONF" ]]; then
    die "Dev toolchain config not found at ${TOOLCHAIN_CONF} — cannot provision the dev toolchain"
fi
# shellcheck source=templates/dev-toolchain.sh
source "$TOOLCHAIN_CONF"

# Sub-action A: rustup + cargo-binstall (shared helpers; no-ops when 4.5b ran them)
ensure_rustup
ensure_cargo_binstall

# Sub-action B: one `cargo binstall -y <crate>` per entry in the data file.
# Skipped per-package when the binary is already on AGENT_USER's PATH, and each
# package logs its own [OK]/[SKIP]/[DRY] line (same one-line-per-entry shape as
# Step 4.6's MCP server loop).
_agent_cargo="${AGENT_HOME}/.cargo/bin/cargo"
_tc_failed=()
for _tc_entry in "${DEV_TOOLCHAIN_CARGO_PKGS[@]}"; do
    _tc_crate="${_tc_entry%%|*}"
    _tc_bin="${_tc_entry#*|}"
    if runuser -l "$AGENT_USER" -c "command -v '${_tc_bin}'" >/dev/null 2>&1; then
        skip "${_tc_bin} (${_tc_crate}) already on ${AGENT_USER}'s PATH — already configured"
    elif $DRY_RUN; then
        would "runuser -l ${AGENT_USER} -- ${_agent_cargo} binstall -y ${_tc_crate}  (provides '${_tc_bin}')"
    else
        runuser -l "$AGENT_USER" -c "'${_agent_cargo}' binstall -y '${_tc_crate}'" \
            || { warn "cargo binstall -y ${_tc_crate} failed for ${AGENT_USER} — continuing"; _tc_failed+=("${_tc_crate}"); continue; }
        if runuser -l "$AGENT_USER" -c "command -v '${_tc_bin}'" >/dev/null 2>&1; then
            info "Installed ${_tc_crate} (${_tc_bin}) for ${AGENT_USER}"
        else
            warn "cargo binstall -y ${_tc_crate} ran but '${_tc_bin}' is not on ${AGENT_USER}'s PATH — continuing"
            _tc_failed+=("${_tc_crate}")
        fi
    fi
done
if [[ ${#_tc_failed[@]} -gt 0 ]]; then
    warn "Dev toolchain incomplete for ${AGENT_USER}: ${_tc_failed[*]} — provisioning continued; re-run 'sudo ./setup-dispatch-host.sh' to retry"
fi

# Sub-action C: gitleaks — pinned GitHub release archive, not a cargo crate.
# Installed with the same `install -m 0755 -o/-g` idiom as the talos binary (4.5b-F).
_gl_dest="${AGENT_HOME}/.local/bin/gitleaks"
_gl_machine="$(uname -m)"
_gl_arch=""
for _gl_map in "${GITLEAKS_ARCH_MAP[@]}"; do
    if [[ "${_gl_map%%|*}" == "$_gl_machine" ]]; then
        _gl_arch="${_gl_map#*|}"
    fi
done
_gl_url="${GITLEAKS_URL_TEMPLATE//\{version\}/${GITLEAKS_VERSION}}"
_gl_url="${_gl_url//\{arch\}/${_gl_arch}}"
# Installed version, normalised: `gitleaks version` prints a bare version string,
# but tolerate a leading 'v' and surrounding whitespace across releases.
_gl_have="$(runuser -l "$AGENT_USER" -c "'${_gl_dest}' version" 2>/dev/null | tr -d '[:space:]' | sed 's/^v//' || true)"

if [[ -z "$_gl_arch" ]]; then
    warn "Unsupported architecture '${_gl_machine}' for gitleaks (supported: x86_64, aarch64) — skipping gitleaks; add a GITLEAKS_ARCH_MAP entry in ${TOOLCHAIN_CONF} if this host should be supported"
elif [[ "$_gl_have" == "$GITLEAKS_VERSION" ]]; then
    skip "gitleaks ${GITLEAKS_VERSION} already installed at ${_gl_dest} — already configured"
elif $DRY_RUN; then
    would "download ${_gl_url}, extract 'gitleaks', then install -m 0755 -o ${AGENT_USER} -g ${AGENT_GROUP} → ${_gl_dest}"
else
    _gl_tmp="$(mktemp -d /tmp/gitleaks.XXXXXX)"
    curl -fsSL "$_gl_url" -o "${_gl_tmp}/gitleaks.tar.gz" \
        || { rm -rf "$_gl_tmp"; die "Failed to download gitleaks ${GITLEAKS_VERSION} from ${_gl_url}"; }
    tar -xzf "${_gl_tmp}/gitleaks.tar.gz" -C "$_gl_tmp" gitleaks \
        || { rm -rf "$_gl_tmp"; die "Downloaded gitleaks archive did not contain a 'gitleaks' binary (${_gl_url})"; }
    mkdir -p "${AGENT_HOME}/.local/bin"
    install -m 0755 -o "${AGENT_USER}" -g "${AGENT_GROUP}" "${_gl_tmp}/gitleaks" "$_gl_dest"
    rm -rf "$_gl_tmp"
    _gl_now="$(runuser -l "$AGENT_USER" -c "'${_gl_dest}' version" 2>/dev/null | tr -d '[:space:]' | sed 's/^v//' || true)"
    if [[ "$_gl_now" == "$GITLEAKS_VERSION" ]]; then
        info "Installed gitleaks ${GITLEAKS_VERSION} (${_gl_arch}) for ${AGENT_USER}: ${_gl_dest}"
    else
        die "gitleaks installed at ${_gl_dest} but reports '${_gl_now}' instead of the pinned ${GITLEAKS_VERSION}"
    fi
fi

# ===========================================================================
# Step 5a: Claude symlink (must precede sudoers so the path exists when
#           visudo validates the fragment)
# ===========================================================================
echo ""
echo "--- Step 5a: Claude symlink ---"

CLAUDE_LINK="/usr/local/bin/claude"

if $SINGLE_USER; then
    skip "single-user mode — no sudoers boundary; skipping claude symlink at ${CLAUDE_LINK}"
elif [[ -L "$CLAUDE_LINK" ]] && [[ "$(readlink -f "$CLAUDE_LINK" 2>/dev/null)" == "$(readlink -f "$CLAUDE_SRC" 2>/dev/null)" ]]; then
    skip "${CLAUDE_LINK} already points to ${CLAUDE_SRC} — already configured"
elif $DRY_RUN; then
    would "create symlink ${CLAUDE_LINK} -> ${CLAUDE_SRC}"
else
    if [[ ! -f "$CLAUDE_SRC" ]]; then
        warn "Agent claude binary not found at ${CLAUDE_SRC} — skipping symlink (install claude as ${AGENT_USER} first)"
    else
        ln -sf "$CLAUDE_SRC" "$CLAUDE_LINK"
        info "Created symlink ${CLAUDE_LINK} -> ${CLAUDE_SRC}"
    fi
fi

# ===========================================================================
# Step 5b: Sudoers fragment
# ===========================================================================
echo ""
echo "--- Step 5b: Sudoers ---"

if $SINGLE_USER; then
    skip "single-user mode — no sudoers boundary; skipping sudoers fragment (${SUDOERS_FILE} not created)"
elif [[ -f "$SUDOERS_FILE" ]]; then
    current_sudoers="$(cat "$SUDOERS_FILE")"
    rendered_sudoers="$(_render_sudoers)"
    if [[ "$current_sudoers" == "$rendered_sudoers" ]]; then
        skip "${SUDOERS_FILE} already up to date — already configured"
    else
        warn "${SUDOERS_FILE} exists but content differs — will overwrite"
        if $DRY_RUN; then
            would "overwrite ${SUDOERS_FILE} with rendered sudoers template"
        else
            _install_sudoers
        fi
    fi
elif $DRY_RUN; then
    would "render sudoers template → validate with visudo -c → install ${SUDOERS_FILE} (mode 0440)"
else
    _install_sudoers
fi

# ===========================================================================
# Step 6: Systemd unit
# ===========================================================================
echo ""
echo "--- Step 6: Systemd ---"

if [[ -f "$SYSTEMD_UNIT" ]]; then
    current_unit="$(cat "$SYSTEMD_UNIT")"
    rendered_unit="$(_render_unit)"
    if [[ "$current_unit" == "$rendered_unit" ]]; then
        skip "${SYSTEMD_UNIT} already up to date — already configured"
    else
        warn "${SYSTEMD_UNIT} exists but content differs — will update"
        if $DRY_RUN; then
            would "overwrite ${SYSTEMD_UNIT} with rendered unit"
            would "systemctl daemon-reload && enable && restart ${SERVICE_NAME}"
        else
            _install_unit
        fi
    fi
elif $DRY_RUN; then
    would "render ${TMPL_DIR}/dispatch-api.service.tmpl → ${SYSTEMD_UNIT}"
    would "systemctl daemon-reload && systemctl enable && systemctl restart ${SERVICE_NAME}"
else
    _install_unit
fi

# ===========================================================================
# Step 6.5: Service environment freshness
# ===========================================================================
# systemd reads EnvironmentFile ONCE at service start. A setup step that
# mutates $SERVICE_ENV (Step 3, 3.5, 4.5b-G, --with-postgres) does not by
# itself change what the *running* dispatch-api process sees — Step 6 only
# restarts when the rendered unit *file* differs, not when the env file
# content changes. This step makes that staleness impossible to miss: it
# compares the installed env file against the running process's actual
# environment (never the file alone) and reports drift loudly. It NEVER
# restarts blindly while runs may be in flight (kb-01486: a restart looks
# like run completion to the status poller) — restart only happens with
# --restart-if-stale AND a zero-active-runs probe.
# ===========================================================================
echo ""
echo "--- Step 6.5: Service environment freshness ---"

# Two-consumer warning: the exact set of $SERVICE_ENV keys Step 4.6 reads and
# bakes into ${AGENT_HOME}/.claude.json (see the _read_env_var calls in that
# step). A restart of ${SERVICE_NAME} does NOT refresh that file — only a
# re-run of Step 4.6 does — so a stale key from this set needs an extra line
# telling the operator that restarting alone will not fix the agent side.
STEP46_KEYS=(AGENT_GTD_URL AGENT_GTD_API_KEY AGENT_GTD_MCP_SRC PERSONAL_KB_URL PERSONAL_KB_API_KEY TEAM_KB_URL TEAM_KB_API_KEY PERSONAL_KB_MCP_SRC)

if $DRY_RUN; then
    would "compare ${SERVICE_ENV} against the running ${SERVICE_NAME} process environment (/proc/<MainPID>/environ) and, with --restart-if-stale and zero active runs, restart ${SERVICE_NAME}"
    ENV_FRESHNESS_STATE="dry-run"
else
    _main_pid="$(systemctl show -p MainPID --value "${SERVICE_NAME}" 2>/dev/null || true)"
    [[ "$_main_pid" =~ ^[0-9]+$ ]] || _main_pid=0

    if [[ "$_main_pid" -eq 0 ]]; then
        skip "${SERVICE_NAME} is not running — no stale environment is possible"
        ENV_FRESHNESS_STATE="not-running"
    else
        [[ -x "${SCRIPT_DIR}/scripts/env-staleness-check.sh" ]] \
            || die "Missing or non-executable ${SCRIPT_DIR}/scripts/env-staleness-check.sh — this ships in the repo; its absence means a broken checkout"

        _stale_out=""; _stale_rc=0
        _stale_out="$("${SCRIPT_DIR}/scripts/env-staleness-check.sh" --env-file "$SERVICE_ENV" --environ "/proc/${_main_pid}/environ")" || _stale_rc=$?

        _check_kind="proc"
        _is_stale=false
        _names_known=true
        _stale_names=()

        case "$_stale_rc" in
            0) : ;;
            1)
                _is_stale=true
                while IFS= read -r _n; do [[ -n "$_n" ]] && _stale_names+=("$_n"); done <<< "$_stale_out"
                ;;
            3)
                _check_kind="mtime-fallback"
                _svc_start="$(systemctl show -p ActiveEnterTimestamp --value "${SERVICE_NAME}" 2>/dev/null || true)"
                _svc_start_epoch=""
                if [[ -n "$_svc_start" && "$_svc_start" != "n/a" ]]; then
                    _svc_start_epoch="$(date -d "$_svc_start" +%s 2>/dev/null || true)"
                fi
                if [[ -z "$_svc_start_epoch" ]]; then
                    warn "Cannot determine ${SERVICE_NAME} start time (ActiveEnterTimestamp='${_svc_start}') — treating service environment as UNVERIFIED"
                    ENV_FRESHNESS_STATE="unverified"
                else
                    _env_mtime="$(stat -c %Y "$SERVICE_ENV")"
                    if [[ "$_env_mtime" -gt "$_svc_start_epoch" ]]; then
                        _is_stale=true
                        if (( ${#ENV_MUTATED_VARS[@]} == 0 )); then
                            _names_known=false
                            _stale_names=("<unknown: /proc unreadable>")
                        else
                            _stale_names=(${ENV_MUTATED_VARS[@]+"${ENV_MUTATED_VARS[@]}"})
                        fi
                        warn "${SERVICE_ENV} mtime is newer than ${SERVICE_NAME}'s ActiveEnterTimestamp — falling back to an mtime comparison because /proc/${_main_pid}/environ was unreadable"
                    fi
                fi
                ;;
            2) die "env-staleness-check.sh exited 2 (usage error) — installer bug in the Step 6.5 invocation" ;;
            *) die "env-staleness-check.sh exited unexpectedly (rc=${_stale_rc}) — installer bug in the Step 6.5 invocation" ;;
        esac

        if [[ "${ENV_FRESHNESS_STATE}" != "unverified" ]]; then
            if ! $_is_stale; then
                info "dispatch-api process environment matches ${SERVICE_ENV} — ${SERVICE_NAME} (PID ${_main_pid})"
                ENV_FRESHNESS_STATE="current"
            else
                _stale_count=${#_stale_names[@]}
                if $_names_known; then
                    _suffix=" (${_stale_count} vars)"
                else
                    _suffix=" (vars unknown)"
                fi

                echo ""
                printf "${RED}========================================${RESET}\n"
                printf "${RED}  STALE SERVICE ENVIRONMENT            ${RESET}\n"
                printf "${RED}========================================${RESET}\n"
                echo ""
                echo "  ${SERVICE_ENV} was written since ${SERVICE_NAME} last started."
                echo "  The running process still has the OLD values for:"
                echo ""
                for _n in "${_stale_names[@]}"; do
                    echo "    ${_n}"
                done
                echo ""
                for _n in "${_stale_names[@]}"; do
                    for _s46 in "${STEP46_KEYS[@]}"; do
                        if [[ "$_n" == "$_s46" ]]; then
                            echo "  NOTE: ${_n} is also consumed by the agent's \${AGENT_HOME}/.claude.json,"
                            echo "        which Step 4.6 already refreshed this invocation — a service"
                            echo "        restart does not refresh that file."
                            break
                        fi
                    done
                done
                echo "  Check for in-flight runs first:"
                echo "    curl -s http://localhost:${API_PORT}/health"
                echo "  Then pick up the new values with:"
                echo "    systemctl restart ${SERVICE_NAME}"
                echo ""

                _health_json=""
                _active_runs="unknown"
                if _health_json="$(curl -sf --max-time 5 "http://localhost:${API_PORT}/health" 2>/dev/null)"; then
                    _active_runs="$(printf '%s' "$_health_json" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
    v = d.get('active_runs')
    print(int(v)) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else print('unknown')
except Exception:
    print('unknown')
" 2>/dev/null || echo unknown)"
                fi

                if ! $RESTART_IF_STALE; then
                    warn "Not restarting ${SERVICE_NAME} — pass --restart-if-stale to allow an automatic restart when no runs are in flight (observed active_runs=${_active_runs}; kb-01486: a blind restart looks like run completion to the status poller)"
                    ENV_FRESHNESS_STATE="refused-no-flag${_suffix}"
                elif [[ "$_active_runs" == "unknown" ]]; then
                    warn "Cannot verify active_runs (health probe failed or returned a non-integer) — refusing to restart ${SERVICE_NAME} (kb-01486)"
                    ENV_FRESHNESS_STATE="refused-probe-failed${_suffix}"
                elif [[ "$_active_runs" -gt 0 ]]; then
                    warn "Refusing to restart ${SERVICE_NAME} — active_runs=${_active_runs} (kb-01486: a restart looks like run completion to the status poller)"
                    ENV_FRESHNESS_STATE="refused-in-flight${_suffix}"
                else
                    info "active_runs=0 and --restart-if-stale was passed — restarting ${SERVICE_NAME}"
                    systemctl restart "${SERVICE_NAME}"

                    _restart_ok=false
                    _rattempts=0
                    while (( _rattempts < 10 )); do
                        if curl -sf --max-time 5 "http://localhost:${API_PORT}/health" &>/dev/null; then
                            _restart_ok=true
                            break
                        fi
                        _rattempts=$((_rattempts + 1))
                        sleep 3
                    done
                    if ! $_restart_ok; then
                        warn "Restarted ${SERVICE_NAME} but /health did not return 200 within 30s — check: journalctl -u ${SERVICE_NAME} -n 50"
                    fi

                    _new_main_pid="$(systemctl show -p MainPID --value "${SERVICE_NAME}" 2>/dev/null || true)"
                    [[ "$_new_main_pid" =~ ^[0-9]+$ ]] || _new_main_pid=0
                    _post_out=""; _post_rc=0
                    if [[ "$_new_main_pid" -gt 0 ]]; then
                        _post_out="$("${SCRIPT_DIR}/scripts/env-staleness-check.sh" --env-file "$SERVICE_ENV" --environ "/proc/${_new_main_pid}/environ")" || _post_rc=$?
                    else
                        _post_rc=1
                        _post_out="<unknown: process not found after restart>"
                    fi

                    if [[ "$_post_rc" -eq 0 ]]; then
                        info "Post-restart verification passed — ${SERVICE_NAME} (PID ${_new_main_pid}) environment now matches ${SERVICE_ENV}"
                        ENV_FRESHNESS_STATE="restarted-verified"
                    else
                        _post_names=()
                        while IFS= read -r _n; do [[ -n "$_n" ]] && _post_names+=("$_n"); done <<< "$_post_out"
                        echo ""
                        printf "${RED}========================================${RESET}\n"
                        printf "${RED}  STALE SERVICE ENVIRONMENT            ${RESET}\n"
                        printf "${RED}========================================${RESET}\n"
                        echo ""
                        echo "  ${SERVICE_NAME} was restarted but still does not match ${SERVICE_ENV} for:"
                        echo ""
                        for _n in "${_post_names[@]}"; do
                            echo "    ${_n}"
                        done
                        echo ""
                        echo "  Check for in-flight runs first:"
                        echo "    curl -s http://localhost:${API_PORT}/health"
                        echo "  Then pick up the new values with:"
                        echo "    systemctl restart ${SERVICE_NAME}"
                        echo ""
                        _post_suffix=" (${#_post_names[@]} vars)"
                        ENV_FRESHNESS_STATE="restarted-still-stale${_post_suffix}"
                    fi
                fi
            fi
        fi

        # _check_kind is already "proc" or "mtime-fallback" — this branch only
        # runs once a check was actually attempted against a live PID.
        _log_stale_vars="none"
        if [[ ${#_stale_names[@]} -gt 0 ]]; then
            _log_stale_vars="$(IFS=,; echo "${_stale_names[*]}")"
        fi
        if command -v logger &>/dev/null; then
            logger -t dispatch-setup -p user.warning \
                "env-freshness decision=${ENV_FRESHNESS_STATE} service=${SERVICE_NAME} env_file=${SERVICE_ENV} main_pid=${_main_pid} check=${_check_kind} stale_count=${#_stale_names[@]} stale_vars=${_log_stale_vars} restart_if_stale=${RESTART_IF_STALE} active_runs=${_active_runs:-unknown}"
        fi
    fi
fi

# ===========================================================================
# Step 7: Health check
# ===========================================================================
echo ""
echo "--- Step 7: Health check ---"

if $DRY_RUN; then
    would "poll http://localhost:${API_PORT}/health until 200 OK (max 30s, backoff 3s)"
else
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        _health_check
    else
        warn "Service ${SERVICE_NAME} is not active — skipping health check"
        warn "Start it with: systemctl start ${SERVICE_NAME}"
    fi
fi

# ===========================================================================
# Step 8: Smoke test (optional)
# ===========================================================================
echo ""
echo "--- Step 8: Smoke test ---"

if ! $SMOKE; then
    skip "Smoke test skipped (pass --smoke to enable)"
elif $DRY_RUN; then
    would "GET http://localhost:${API_PORT}/health and assert HTTP 200 with 'status' key"
    would "GET http://localhost:${API_PORT}/info and assert HTTP 200 with 'version' key"
else
    _smoke_test
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
_banner_color="$GREEN"
_banner_text="Setup complete"
if [[ ${#_tc_failed[@]} -gt 0 ]]; then
    _banner_color="$YELLOW"
    _banner_text="Setup complete (with warnings)"
fi
printf "${_banner_color}========================================${RESET}\n"
printf "${_banner_color}  ${_banner_text}${RESET}\n"
printf "${_banner_color}========================================${RESET}\n"
echo ""
echo "  Agent user:   ${AGENT_USER}  (${AGENT_HOME})"
echo "  Service user: ${SERVICE_USER}  (${SERVICE_HOME})"
echo "  Wheel index:  ${DISPATCH_WHEEL_INDEX:-https://pypi.lab.jasonweddington.com/simple/}"
echo "  Env file:     ${SERVICE_ENV}"
echo "  Service:      ${SERVICE_NAME}  (port ${API_PORT})"
echo "  Env freshness: ${ENV_FRESHNESS_STATE}"
if [[ ${#_tc_failed[@]} -gt 0 ]]; then
    echo "  Dev toolchain: INCOMPLETE — missing: ${_tc_failed[*]}"
else
    echo "  Dev toolchain: complete"
fi
if $DRY_RUN; then
    would "audit: run cargo --version as ${AGENT_USER}"
else
    _audit_cargo="$(runuser -l "${AGENT_USER}" -c "'${AGENT_HOME}/.cargo/bin/cargo' --version" 2>/dev/null || true)"
    if [[ -n "$_audit_cargo" ]]; then
        echo "  Rust toolchain: ${_audit_cargo}"
    else
        warn "AUDIT: cargo is not runnable for ${AGENT_USER} at end of provisioning despite the Step 4.5b/4.9 guard — the default-toolchain invariant regressed"
    fi
fi
echo ""
if [[ ! -f "$SERVICE_ENV" ]] || grep -qE '^(ANTHROPIC_API_KEY=sk-ant-\.\.\.|AGENT_GTD_API_KEY=agtd_\.\.\.)' "$SERVICE_ENV" 2>/dev/null; then
    echo "  NEXT STEPS:"
    echo "  1. Fill in real values in ${SERVICE_ENV}"
    echo "  2. systemctl restart ${SERVICE_NAME}"
    echo "  3. Run again with --smoke to verify end-to-end"
    echo ""
else
    active_state="$(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || echo 'unknown')"
    echo "  Service status: ${active_state}"
    echo ""
fi
