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
#   -h, --help             Show this help text

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="agent-gtd-dispatch"
# Defaults clone from public GitHub (anonymous https). Point at a fork or a
# self-hosted origin via DISPATCH_REPO_URL / AGENT_GTD_REPO_URL. NOTE: GitHub is
# release-cadence; a host that must run tip-of-main should override to the origin
# that carries it.
GIT_REMOTE_URL="${DISPATCH_REPO_URL:-https://github.com/jason-weddington/${REPO_NAME}}"
AGENT_GTD_REMOTE_URL="${AGENT_GTD_REPO_URL:-https://github.com/jason-weddington/agent-gtd}"

# Derive the git host(s) to seed into known_hosts from the configured remotes,
# so this works against any git server (homelab, GitHub, enterprise) — not just
# the homelab default. Handles scp-style (git@host:path) and ssh:// URLs.
git_host_from_url() {
    local url="${1#ssh://}"   # drop ssh:// scheme if present
    url="${url#*@}"           # drop user@ if present
    printf '%s' "${url%%[:/]*}"  # take up to the first ':' or '/'
}
GIT_HOSTS="$(printf '%s\n%s\n' \
    "$(git_host_from_url "$GIT_REMOTE_URL")" \
    "$(git_host_from_url "$AGENT_GTD_REMOTE_URL")" | sort -u | tr '\n' ' ')"
SERVICE_NAME="dispatch-api"
API_PORT=8100
SUDOERS_FILE="/etc/sudoers.d/dispatch-svc"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
TMPL_DIR="${SCRIPT_DIR}/templates"

# --- Defaults (overridden by CLI flags) ---
AGENT_USER="dispatch"
SERVICE_USER="dispatch-svc"
ENV_FILE_SRC=""
DRY_RUN=false
SMOKE=false

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
  -h, --help             Show this help text

Environment variables:
  DISPATCH_REPO_URL      Git remote for agent-gtd-dispatch repo
  AGENT_GTD_REPO_URL     Git remote for agent_gtd repo
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
        -e "s|{{WORKING_DIR}}|${SERVICE_REPO}|g" \
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
        (( attempts++ ))
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

# ===========================================================================
# Argument parsing
# ===========================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-user)   AGENT_USER="$2";    shift 2 ;;
        --service-user) SERVICE_USER="$2";  shift 2 ;;
        --env-file)     ENV_FILE_SRC="$2";  shift 2 ;;
        --dry-run)      DRY_RUN=true;       shift   ;;
        --smoke)        SMOKE=true;         shift   ;;
        -h|--help)      usage ;;
        *) die "Unknown option: $1  (run with --help for usage)" ;;
    esac
done

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
echo "  Repo:         ${SERVICE_REPO}"
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
        chown -R "${AGENT_USER}:${AGENT_GROUP}" "$AGENT_HOME"
        chmod 2775 "$AGENT_HOME" "$AGENT_WORKSPACE"
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
        ssh-keyscan "$gh" >> "${AGENT_HOME}/.ssh/known_hosts" 2>/dev/null \
            && info "Populated ${AGENT_HOME}/.ssh/known_hosts via ssh-keyscan ${gh}" \
            || warn "ssh-keyscan ${gh} failed — known_hosts may be incomplete"
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
        ssh-keyscan "$gh" >> "${SERVICE_HOME}/.ssh/known_hosts" 2>/dev/null \
            && info "Populated ${SERVICE_HOME}/.ssh/known_hosts via ssh-keyscan ${gh}" \
            || warn "ssh-keyscan ${gh} failed — known_hosts may be incomplete"
    done
    # Copy SSH key files from agent user if present (enables git auth for SERVICE_USER)
    key_copied=false
    for key in "${AGENT_HOME}/.ssh"/id_*; do
        [[ -f "$key" ]] || continue
        cp "$key" "${SERVICE_HOME}/.ssh/"
        chmod 600 "${SERVICE_HOME}/.ssh/$(basename "$key")"
        key_copied=true
    done
    $key_copied && info "Copied SSH key(s) from ${AGENT_HOME}/.ssh/ to ${SERVICE_HOME}/.ssh/" \
        || warn "No id_* keys found in ${AGENT_HOME}/.ssh/ — git clone may fail without auth"
    chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${SERVICE_HOME}/.ssh"
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
    else
        chmod g+rw "$DB_PATH"
        info "Fixed dispatch.db group permissions: ${DB_PATH}"
    fi
fi

# ===========================================================================
# Step 2: Clone repos
# ===========================================================================
echo ""
echo "--- Step 2: Repos ---"

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

_clone_repo "$GIT_REMOTE_URL"       "$SERVICE_REPO"                   "$SERVICE_USER"
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
    echo "  BEFORE Step 6 restarts the service, register this key in:"
    echo "    Agent GTD Settings → Dispatch hosts → this host's API Key"
    echo "  Dispatches will return 401 until this is done."
    echo ""
    echo "  The key takes effect on:"
    echo "    systemctl restart ${SERVICE_NAME}"
    echo "  (Step 6 will do this — finish app-side registration first, or"
    echo "   accept a brief 401 window if Step 6 runs before you register.)"
    echo ""
    info "Minted and installed DISPATCH_API_KEY in ${SERVICE_ENV}"
fi
fi  # end: if [[ ! -f "$SERVICE_ENV" ]]; else

# ===========================================================================
# Step 4: Install dependencies (uv sync)
# ===========================================================================
echo ""
echo "--- Step 4: Dependencies ---"

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

# uv sync the service repo
SERVICE_UV="${UV_BIN}"
if $DRY_RUN; then
    would "run 'uv sync' in ${SERVICE_REPO} as ${SERVICE_USER}"
else
    runuser -u "$SERVICE_USER" -- bash -c "cd '${SERVICE_REPO}' && '${SERVICE_UV}' sync"
    info "uv sync complete in ${SERVICE_REPO}"
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
# Step 4.5b: talos binary (agent user) — MANUAL PROVISIONING NOTE
# ===========================================================================
# The talos-* engine family (talos-haiku/sonnet/opus/qwen/glm) invokes the
# `talos` binary as a subprocess (see src/agent_gtd_dispatch/talos.py). The
# binary is NOT auto-installed in 0.3.5 — build and place it manually on
# the AGENT_USER's PATH (or point TALOS_BIN at an absolute path in
# /home/dispatch-svc/.env). On the aarch64 Pi 5 host:
#
#     sudo -u "$AGENT_USER" bash <<'EOF'
#     set -e
#     cd "$HOME"
#     [ -d harness-design ] || git clone <harness-design-origin> harness-design
#     cd harness-design
#     git pull
#     cargo build --release -p talos
#     ln -sf "$PWD/target/release/talos" "$HOME/.local/bin/talos"
#     talos --version
#     EOF
#
# Verify: `sudo -u dispatch talos --version` succeeds. Skip this step when
# rolling out on hosts that will not dispatch talos-* engines; the /info
# advertisement (is_engine_available) will simply omit them.
echo ""
echo "--- Step 4.5b: talos binary (agent user, MANUAL) ---"
info "talos binary is NOT auto-installed — build via 'cargo build --release -p talos' in a harness-design clone and place it on ${AGENT_USER}'s PATH (or set TALOS_BIN in ${SERVICE_ENV}). See comment above for the recipe. Skip when dispatching only claude-code engines."

# ===========================================================================
# Step 4.6: MCP servers (agent user)
# ===========================================================================
echo ""
echo "--- Step 4.6: MCP servers (agent user) ---"

if ! $DRY_RUN && [[ ! -f "$CLAUDE_SRC" ]]; then
    warn "Claude Code not found at ${CLAUDE_SRC} — skipping MCP server registration (install claude as ${AGENT_USER} first)"
elif $DRY_RUN; then
    would "read AGENT_GTD_URL, AGENT_GTD_API_KEY, AGENT_GTD_MCP_SRC, KB_DATABASE_URL, TEAM_KB_DATABASE_URL, KB_ANTHROPIC_API_KEY from ${SERVICE_ENV}"
    would "source ${TMPL_DIR}/mcp-servers.sh and register each MCP server for ${AGENT_USER} via claude mcp add --scope user"
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
    #   KB_DATABASE_URL       → personal-kb connection string (skipped if unset).
    #   TEAM_KB_DATABASE_URL  → team-kb DB connection string (skipped if unset).
    #   KB_ANTHROPIC_API_KEY  → ANTHROPIC_API_KEY for both KB servers' LLM calls.
    #     Deliberately NOT named ANTHROPIC_API_KEY: that name would reach Claude Code's
    #     launch env and flip billing off the Max subscription (engines.py / kb-01512).
    if [[ -f "$SERVICE_ENV" ]]; then
        AGENT_GTD_URL="$(_read_env_var AGENT_GTD_URL)";                 export AGENT_GTD_URL
        AGENT_GTD_API_KEY="$(_read_env_var AGENT_GTD_API_KEY)";         export AGENT_GTD_API_KEY
        AGENT_GTD_MCP_SRC="$(_read_env_var AGENT_GTD_MCP_SRC)";         export AGENT_GTD_MCP_SRC
        KB_DATABASE_URL="$(_read_env_var KB_DATABASE_URL)";             export KB_DATABASE_URL
        TEAM_KB_DATABASE_URL="$(_read_env_var TEAM_KB_DATABASE_URL)";   export TEAM_KB_DATABASE_URL
        KB_ANTHROPIC_API_KEY="$(_read_env_var KB_ANTHROPIC_API_KEY)";   export KB_ANTHROPIC_API_KEY
    fi
    # agent-gtd warnings are elevated (LOAD-BEARING for step-4 verification)
    [[ -z "${AGENT_GTD_URL:-}" ]]     && warn "AGENT_GTD_URL not set in ${SERVICE_ENV} — agent-gtd MCP will launch without a URL; Step 4 verification will fail"
    [[ -z "${AGENT_GTD_API_KEY:-}" ]] && warn "AGENT_GTD_API_KEY not set in ${SERVICE_ENV} — agent-gtd MCP will launch without credentials; Step 4 verification will fail"
    [[ -z "${TEAM_KB_DATABASE_URL:-}" ]] && warn "TEAM_KB_DATABASE_URL not set in ${SERVICE_ENV} — team-kb MCP server will be skipped"
    [[ -z "${KB_DATABASE_URL:-}" ]]      && warn "KB_DATABASE_URL not set in ${SERVICE_ENV} — personal-kb MCP server will be skipped"
    [[ -z "${KB_ANTHROPIC_API_KEY:-}" ]] && warn "KB_ANTHROPIC_API_KEY not set in ${SERVICE_ENV} — KB servers will register without an Anthropic key (LLM features degraded)"
    # shellcheck source=templates/mcp-servers.sh
    source "$MCP_CONF"
    for entry in "${MCP_SERVERS[@]}"; do
        mcp_name="${entry%%|*}"
        mcp_args="${entry#*|}"
        # Idempotent: remove first (tolerate "not registered"), then add
        runuser -l "$AGENT_USER" -c \
            "cd '${AGENT_HOME}' && ${CLAUDE_SRC} mcp remove ${mcp_name} --scope user 2>/dev/null || true"
        # word-split mcp_args intentionally — they are space-separated CLI flags
        # shellcheck disable=SC2086
        runuser -l "$AGENT_USER" -c \
            "cd '${AGENT_HOME}' && ${CLAUDE_SRC} mcp add ${mcp_name} ${mcp_args}"
        info "Registered MCP server '${mcp_name}' for ${AGENT_USER}"
    done
    # Smoke test: verify agent-gtd is listed (name presence only — cold uvx cache
    # makes the "✓ Connected" health check unreliable on first invocation)
    if runuser -l "$AGENT_USER" -c \
            "cd '${AGENT_HOME}' && ${CLAUDE_SRC} mcp list" 2>/dev/null \
            | grep -q "^agent-gtd:"; then
        info "Smoke test passed: 'agent-gtd' MCP server registered for ${AGENT_USER}"
    else
        die "Smoke test failed: 'agent-gtd' not found in \`claude mcp list\` output for ${AGENT_USER} — registration may have failed"
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
printf "${GREEN}========================================${RESET}\n"
printf "${GREEN}  Setup complete${RESET}\n"
printf "${GREEN}========================================${RESET}\n"
echo ""
echo "  Agent user:   ${AGENT_USER}  (${AGENT_HOME})"
echo "  Service user: ${SERVICE_USER}  (${SERVICE_HOME})"
echo "  Repo:         ${SERVICE_REPO}"
echo "  Env file:     ${SERVICE_ENV}"
echo "  Service:      ${SERVICE_NAME}  (port ${API_PORT})"
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
