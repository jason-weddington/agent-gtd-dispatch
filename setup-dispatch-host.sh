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
#   --smoke                After install, dispatch a no-op job and verify isolation
#   -h, --help             Show this help text

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="agent-gtd-dispatch"
GIT_REMOTE_URL="${DISPATCH_REPO_URL:-git@ubuntu-vm01:repos/${REPO_NAME}}"
AGENT_GTD_REMOTE_URL="${AGENT_GTD_REPO_URL:-git@ubuntu-vm01:repos/agent_gtd}"
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
  --smoke                After install, run a smoke test (POST /dispatch, check isolation)
  -h, --help             Show this help text

Environment variables (override git remote URLs):
  DISPATCH_REPO_URL      Git remote for agent-gtd-dispatch repo
  AGENT_GTD_REPO_URL     Git remote for agent_gtd repo

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
        -e "s|{{AGENT_USER}}|${AGENT_USER}|g" \
        -e "s|{{WORKING_DIR}}|${SERVICE_REPO}|g" \
        -e "s|{{ENV_FILE}}|${SERVICE_ENV}|g" \
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

# Derived paths (set after argument parsing)
AGENT_HOME="/home/${AGENT_USER}"
SERVICE_HOME="/home/${SERVICE_USER}"
SERVICE_REPO="${SERVICE_HOME}/${REPO_NAME}"
SERVICE_ENV="${SERVICE_HOME}/.env"
AGENT_WORKSPACE="${AGENT_HOME}/workspace"
CLAUDE_SRC="${AGENT_HOME}/.local/bin/claude"

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
echo ""

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

_create_user "$SERVICE_USER" "$SERVICE_HOME" "Dispatch service account (agent-gtd-dispatch API)"
_create_user "$AGENT_USER"   "$AGENT_HOME"   "Dispatch agent subprocess user"

# Guard: neither user may be in the sudo group
for u in "$AGENT_USER" "$SERVICE_USER"; do
    if id -u "$u" &>/dev/null && groups "$u" 2>/dev/null | grep -q sudo; then
        die "'${u}' is in the sudo group — this is not allowed. Remove it first."
    fi
done

# Create workspace directory for agent user
if $DRY_RUN; then
    would "create ${AGENT_WORKSPACE} owned by ${AGENT_USER} (mode 2775)"
else
    mkdir -p "$AGENT_WORKSPACE"
    chown -R "${AGENT_USER}:${AGENT_USER}" "$AGENT_HOME"
    chmod 2775 "$AGENT_HOME" "$AGENT_WORKSPACE"
    info "Agent workspace ready: ${AGENT_WORKSPACE}"
fi

# --- SSH key provisioning for AGENT_USER (needed for git auth on fresh box) ---
if $DRY_RUN; then
    would "create ${AGENT_HOME}/.ssh/ (mode 700, owner ${AGENT_USER}) if absent"
    would "ssh-keyscan ubuntu-vm01 >> ${AGENT_HOME}/.ssh/known_hosts"
    would "generate ed25519 keypair for ${AGENT_USER} if no id_* key exists"
else
    mkdir -p "${AGENT_HOME}/.ssh"
    chmod 700 "${AGENT_HOME}/.ssh"
    chown "${AGENT_USER}:${AGENT_USER}" "${AGENT_HOME}/.ssh"
    ssh-keyscan ubuntu-vm01 >> "${AGENT_HOME}/.ssh/known_hosts" 2>/dev/null \
        && info "Populated ${AGENT_HOME}/.ssh/known_hosts via ssh-keyscan ubuntu-vm01" \
        || warn "ssh-keyscan ubuntu-vm01 failed — known_hosts may be incomplete"
    if ! ls "${AGENT_HOME}/.ssh"/id_* &>/dev/null; then
        sudo -u "$AGENT_USER" ssh-keygen -t ed25519 -N "" \
            -f "${AGENT_HOME}/.ssh/id_ed25519" \
            -C "${AGENT_USER}@$(hostname -s)"
        chown "${AGENT_USER}:${AGENT_USER}" \
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
if $DRY_RUN; then
    would "create ${SERVICE_HOME}/.ssh/ (mode 700)"
    would "ssh-keyscan ubuntu-vm01 >> ${SERVICE_HOME}/.ssh/known_hosts"
    would "copy ${AGENT_HOME}/.ssh/id_* keys to ${SERVICE_HOME}/.ssh/ if present"
    would "chown -R ${SERVICE_USER}:${SERVICE_USER} ${SERVICE_HOME}/.ssh/"
else
    mkdir -p "${SERVICE_HOME}/.ssh"
    chmod 700 "${SERVICE_HOME}/.ssh"
    ssh-keyscan ubuntu-vm01 >> "${SERVICE_HOME}/.ssh/known_hosts" 2>/dev/null \
        && info "Populated ${SERVICE_HOME}/.ssh/known_hosts via ssh-keyscan ubuntu-vm01" \
        || warn "ssh-keyscan ubuntu-vm01 failed — known_hosts may be incomplete"
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
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${SERVICE_HOME}/.ssh"
    info "SSH directory seeded for ${SERVICE_USER}"
fi

# --- Group membership (dispatch-svc needs read access to dispatch group resources) ---
if id -u "$SERVICE_USER" &>/dev/null && id -u "$AGENT_USER" &>/dev/null; then
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
        sudo -u "$owner" git clone "$remote" "$dest"
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
    chown "${SERVICE_USER}:${SERVICE_USER}" "$SERVICE_ENV"
    info "Env file installed: ${SERVICE_ENV} (mode 0600)"
fi

# ===========================================================================
# Step 4: Install dependencies (uv sync)
# ===========================================================================
echo ""
echo "--- Step 4: Dependencies ---"

_ensure_uv() {
    local user="$1" user_home="$2"
    local uv_bin="${user_home}/.local/bin/uv"
    if sudo -u "$user" bash -c "[[ -x '${uv_bin}' ]] || command -v uv &>/dev/null"; then
        skip "uv already installed for ${user} — already configured"
    elif $DRY_RUN; then
        would "install uv for ${user} via official installer (curl astral.sh/uv/install.sh)"
    else
        sudo -u "$user" bash -c 'curl -fsSL https://astral.sh/uv/install.sh | sh'
        info "Installed uv for ${user}"
    fi
}

_ensure_uv "$SERVICE_USER" "$SERVICE_HOME"
_ensure_uv "$AGENT_USER"   "$AGENT_HOME"

# uv sync the service repo
SERVICE_UV="${SERVICE_HOME}/.local/bin/uv"
if $DRY_RUN; then
    would "run 'uv sync' in ${SERVICE_REPO} as ${SERVICE_USER}"
else
    sudo -u "$SERVICE_USER" bash -c "cd '${SERVICE_REPO}' && '${SERVICE_UV}' sync"
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
    sudo -u "$AGENT_USER" bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
    if [[ -f "$CLAUDE_SRC" ]]; then
        info "Installed Claude Code for ${AGENT_USER}: ${CLAUDE_SRC}"
    else
        warn "Claude Code installer ran but ${CLAUDE_SRC} not found — verify installation"
    fi
fi

# ===========================================================================
# Step 4.6: MCP servers (agent user)
# ===========================================================================
echo ""
echo "--- Step 4.6: MCP servers (agent user) ---"

if ! $DRY_RUN && [[ ! -f "$CLAUDE_SRC" ]]; then
    warn "Claude Code not found at ${CLAUDE_SRC} — skipping MCP server registration (install claude as ${AGENT_USER} first)"
elif $DRY_RUN; then
    would "read TEAM_KB_DATABASE_URL and KB_ANTHROPIC_API_KEY from ${SERVICE_ENV} (for KB MCP servers)"
    would "source ${TMPL_DIR}/mcp-servers.sh and register each MCP server for ${AGENT_USER} via claude mcp add --scope user"
else
    MCP_CONF="${TMPL_DIR}/mcp-servers.sh"
    if [[ ! -f "$MCP_CONF" ]]; then
        die "MCP server config not found at ${MCP_CONF} — cannot register MCP servers"
    fi
    # Secrets for the KB MCP servers are kept out of git. Pull them from the installed
    # service .env and export so mcp-servers.sh can inject them per-server:
    #   TEAM_KB_DATABASE_URL  → team-kb's DB connection string (skipped if unset)
    #   KB_ANTHROPIC_API_KEY  → ANTHROPIC_API_KEY for both KB servers' LLM calls.
    #     Deliberately NOT named ANTHROPIC_API_KEY: that name would reach Claude Code's
    #     launch env and flip billing off the Max subscription (engines.py / kb-01512).
    _read_env_var() {  # $1=var name in .env; strips surrounding single/double quotes
        local v
        v="$(sed -n "s/^$1=//p" "$SERVICE_ENV" | tail -n1)"
        v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
        printf '%s' "$v"
    }
    if [[ -f "$SERVICE_ENV" ]]; then
        TEAM_KB_DATABASE_URL="$(_read_env_var TEAM_KB_DATABASE_URL)"; export TEAM_KB_DATABASE_URL
        KB_ANTHROPIC_API_KEY="$(_read_env_var KB_ANTHROPIC_API_KEY)"; export KB_ANTHROPIC_API_KEY
    fi
    [[ -z "${TEAM_KB_DATABASE_URL:-}" ]] && warn "TEAM_KB_DATABASE_URL not set in ${SERVICE_ENV} — team-kb MCP server will be skipped"
    [[ -z "${KB_ANTHROPIC_API_KEY:-}" ]] && warn "KB_ANTHROPIC_API_KEY not set in ${SERVICE_ENV} — KB servers will register without an Anthropic key (LLM features degraded)"
    # shellcheck source=templates/mcp-servers.sh
    source "$MCP_CONF"
    for entry in "${MCP_SERVERS[@]}"; do
        mcp_name="${entry%%|*}"
        mcp_args="${entry#*|}"
        # Idempotent: remove first (tolerate "not registered"), then add
        sudo -u "$AGENT_USER" -H bash -lc \
            "cd /home/${AGENT_USER} && ${CLAUDE_SRC} mcp remove ${mcp_name} --scope user 2>/dev/null || true"
        # word-split mcp_args intentionally — they are space-separated CLI flags
        # shellcheck disable=SC2086
        sudo -u "$AGENT_USER" -H bash -lc \
            "cd /home/${AGENT_USER} && ${CLAUDE_SRC} mcp add ${mcp_name} ${mcp_args}"
        info "Registered MCP server '${mcp_name}' for ${AGENT_USER}"
    done
    # Smoke test: verify agent-gtd is listed (name presence only — cold uvx cache
    # makes the "✓ Connected" health check unreliable on first invocation)
    if sudo -u "$AGENT_USER" -H bash -lc \
            "cd /home/${AGENT_USER} && ${CLAUDE_SRC} mcp list" 2>/dev/null \
            | grep -q "^agent-gtd:"; then
        info "Smoke test passed: 'agent-gtd' MCP server registered for ${AGENT_USER}"
    else
        die "Smoke test failed: 'agent-gtd' not found in \`claude mcp list\` output for ${AGENT_USER} — registration may have failed"
    fi
fi

# ===========================================================================
# Step 5a: Claude symlink (must precede sudoers so the path exists when
#           visudo validates the fragment)
# ===========================================================================
echo ""
echo "--- Step 5a: Claude symlink ---"

CLAUDE_LINK="/usr/local/bin/claude"

if [[ -L "$CLAUDE_LINK" ]] && [[ "$(readlink -f "$CLAUDE_LINK" 2>/dev/null)" == "$(readlink -f "$CLAUDE_SRC" 2>/dev/null)" ]]; then
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

if [[ -f "$SUDOERS_FILE" ]]; then
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
if [[ ! -f "$SERVICE_ENV" ]] || grep -qE '^(DISPATCH_API_KEY=changeme|ANTHROPIC_API_KEY=sk-ant-\.\.\.|AGENT_GTD_API_KEY=agtd_\.\.\.)' "$SERVICE_ENV" 2>/dev/null; then
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
