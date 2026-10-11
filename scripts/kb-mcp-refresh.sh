#!/usr/bin/env bash
set -euo pipefail

# kb-mcp-refresh.sh — Register personal-kb, team-kb AND agent-gtd as hosted HTTP
# /mcp servers for the agent user (user scope, i.e. <agent-home>/.claude.json) and
# keep the two KB servers' 0600 key files in the agent home in sync. This is the ONLY
# code that registers these three servers (agent-gtd included); it is called by
# setup-dispatch-host.sh (Step 4.6) and by deploy.sh (every deploy, non-fatal), so
# template changes reach existing hosts.
#
# Each server is registered as {"type": "http", "url": "<base>/mcp",
# "headersHelper": "<shell snippet>"}. The helper reads the API key at connect
# time, so the key itself never lands in .claude.json. The KB helpers read the env
# and fall back to the key file. The agent-gtd helper reads ONLY the env
# (AGENT_GTD_API_KEY, set per run by the dispatch worker to the run's callback
# token) and forwards AGENT_GTD_AGENT_NAME as X-Agent-Name when set; there is no
# agent-gtd key file by design, since a static key would pin every run to one
# identity. The service URLs and API keys come ONLY from --env-file
# (PERSONAL_KB_URL, PERSONAL_KB_API_KEY, TEAM_KB_URL, TEAM_KB_API_KEY,
# AGENT_GTD_URL, AGENT_GTD_API_KEY), never from this script's own process
# environment. agent-gtd needs only AGENT_GTD_URL; AGENT_GTD_API_KEY is used only
# for the post-run leak check and the optional --probe.
#
# Usage:
#   kb-mcp-refresh.sh --env-file PATH [--agent-user NAME] [--agent-home DIR]
#                     [--agent-group NAME] [--claude PATH] [--probe PATH]
#
# --probe PATH: after registration, when agent-gtd is ok or changed, run the
# templates/mcp-probe.py at PATH against the agent-gtd registration (as the invoking
# user, key passed in the env). A probe failure prints a [WARN] and never changes
# the exit code.
#
# Defaults: --agent-user dispatch; --agent-home from `getent passwd <agent-user>`;
# --agent-group from `id -gn <agent-user>`; --claude <agent-home>/.local/bin/claude.
#
# Exit codes:
#   0  every server up to date ([OK]), refreshed ([CHANGED]) or skipped ([SKIP])
#   1  a claude call, a key-file write, a .claude.json parse, or the post-run
#      leak check failed (always with an [ERR] line on stderr)
#   2  usage error — unknown argument, --env-file missing, or --probe not a file
#   3  --env-file does not exist or is unreadable
#   4  --agent-user does not exist on this host
#   5  --claude path is not an executable file
# Checks for 2, 3, 4 and 5 run in that order, before any claude call or file write.
#
# The last stdout line (exit 0 and 1 only) is:
#   kb-mcp-refresh: personal-kb=<S> team-kb=<S> agent-gtd=<S> agent-gtd-probe=<P> rc=<N>
#     S: ok|changed|skipped|err    P: ok|fail|skipped

PROG="$(basename "$0")"
ENV_FILE=""
AGENT_USER="dispatch"
AGENT_HOME=""
AGENT_GROUP=""
CLAUDE=""
PROBE=""
PROBE_STATUS=skipped
FAIL=0
SUMMARY_ON=false
TMP=""
declare -A STATUS=([personal-kb]=err [team-kb]=err [agent-gtd]=err)

usage() {
    cat >&2 <<USAGE
Usage: ${PROG} --env-file PATH [--agent-user NAME] [--agent-home DIR]
       [--agent-group NAME] [--claude PATH] [--probe PATH]
USAGE
}

fail() {
    echo "[ERR]  $*" >&2
    exit 1
}

cleanup() {
    local rc=$?
    if [[ -n "$TMP" ]]; then rm -f "$TMP"; fi
    if $SUMMARY_ON && [[ $rc -le 1 ]]; then
        echo "kb-mcp-refresh: personal-kb=${STATUS[personal-kb]} team-kb=${STATUS[team-kb]} agent-gtd=${STATUS[agent-gtd]} agent-gtd-probe=${PROBE_STATUS} rc=${rc}"
    fi
}
trap cleanup EXIT
trap 'echo "[ERR]  unexpected failure at line ${LINENO}" >&2' ERR

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file|--agent-user|--agent-home|--agent-group|--claude|--probe)
            if [[ $# -lt 2 ]]; then usage; exit 2; fi
            case "$1" in
                --env-file) ENV_FILE="$2" ;;
                --agent-user) AGENT_USER="$2" ;;
                --agent-home) AGENT_HOME="$2" ;;
                --agent-group) AGENT_GROUP="$2" ;;
                --claude) CLAUDE="$2" ;;
                --probe) PROBE="$2" ;;
            esac
            shift 2 ;;
        *) usage; exit 2 ;;
    esac
done

if [[ -z "$ENV_FILE" ]]; then usage; exit 2; fi
if [[ -n "$PROBE" && ! -f "$PROBE" ]]; then usage; exit 2; fi
if [[ ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
    echo "[ERR]  env file not readable: ${ENV_FILE}" >&2
    exit 3
fi
if ! getent passwd "$AGENT_USER" >/dev/null 2>&1; then
    echo "[ERR]  agent user ${AGENT_USER} does not exist on this host" >&2
    exit 4
fi
if [[ -z "$AGENT_HOME" ]]; then AGENT_HOME="$(getent passwd "$AGENT_USER" | cut -d: -f6)"; fi
if [[ -z "$AGENT_GROUP" ]]; then AGENT_GROUP="$(id -gn "$AGENT_USER")"; fi
if [[ -z "$CLAUDE" ]]; then CLAUDE="${AGENT_HOME}/.local/bin/claude"; fi
if [[ ! -f "$CLAUDE" || ! -x "$CLAUDE" ]]; then
    echo "[ERR]  claude is not an executable file: ${CLAUDE}" >&2
    exit 5
fi

CLAUDE_JSON="${AGENT_HOME}/.claude.json"
SUMMARY_ON=true

# Same semantics as _read_env_var in setup-dispatch-host.sh: last NAME= line wins,
# one pair of surrounding quotes stripped.
read_env_var() {
    local v
    v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

PERSONAL_KB_URL="$(read_env_var PERSONAL_KB_URL)"
PERSONAL_KB_API_KEY="$(read_env_var PERSONAL_KB_API_KEY)"
TEAM_KB_URL="$(read_env_var TEAM_KB_URL)"
TEAM_KB_API_KEY="$(read_env_var TEAM_KB_API_KEY)"
AGENT_GTD_URL="$(read_env_var AGENT_GTD_URL)"
AGENT_GTD_API_KEY="$(read_env_var AGENT_GTD_API_KEY)"

PYHELP=$(cat <<'PY'
import json, os, sys

mode, path = sys.argv[1], sys.argv[2]


def load():
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read())
    except FileNotFoundError:
        return {}


def servers(data):
    s = data.get("mcpServers") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


if mode == "parse":
    try:
        load()
    except ValueError:
        sys.exit(3)
elif mode == "has":
    print("yes" if sys.argv[3] in servers(load()) else "no")
elif mode == "get":
    e = servers(load()).get(sys.argv[3])
    print("" if e is None else json.dumps(e))
elif mode == "prior-type":
    e = json.loads(os.environ["KB_PRIOR"])
    if isinstance(e, dict) and "type" in e:
        print(e["type"])
    elif isinstance(e, dict) and "command" in e:
        print("stdio")
    else:
        print("unknown")
elif mode == "build":
    print(json.dumps({"type": "http", "url": sys.argv[3], "headersHelper": sys.argv[4]}))
elif mode == "classify":
    name = sys.argv[3]
    desired = json.loads(os.environ["KB_DESIRED"])
    s = servers(load())
    if name not in s:
        print("absent absent")
    elif s[name] == desired:
        print("equal equal")
    else:
        e = s[name]
        if not isinstance(e, dict) or e.get("type") != "http" or "command" in e:
            r = "legacy-stdio"
        elif e.get("url") != desired["url"]:
            r = "url-changed"
        elif e.get("headersHelper") != desired["headersHelper"]:
            r = "helper-changed"
        else:
            r = "shape-changed"
        print("differs " + r)
elif mode == "readback":
    name = sys.argv[3]
    desired = json.loads(os.environ["KB_DESIRED"])
    e = servers(load()).get(name)
    if e != desired:
        keys = set(desired)
        if isinstance(e, dict):
            keys |= set(e)
            keys = {k for k in keys if e.get(k) != desired.get(k)}
        print(",".join(sorted(keys)))
elif mode == "leak":
    with open(path, "rb") as fh:
        raw = fh.read()
    for name, var in (
        ("personal-kb", "KB_LEAK_PERSONAL"),
        ("team-kb", "KB_LEAK_TEAM"),
        ("agent-gtd", "KB_LEAK_AGENT_GTD"),
    ):
        k = os.environ.get(var, "")
        if k and k.encode() in raw:
            print(name)
PY
)

py() { python3 -c "$PYHELP" "$@"; }

# Run claude as the agent user, cwd = agent home. Stdout is discarded so the
# summary stays the last stdout line; stderr passes through.
run_claude() {
    if [[ "$(id -un)" == "$AGENT_USER" ]]; then
        ( cd "$AGENT_HOME" && HOME="$AGENT_HOME" "$CLAUDE" "$@" >/dev/null )
    else
        ( cd "$AGENT_HOME" && sudo -n -u "$AGENT_USER" -H -- "$CLAUDE" "$@" >/dev/null )
    fi
}

# Reconcile the key file. Returns 0 on success (up to date or written), 1 on failure.
reconcile_key_file() {
    local file=$1 key=$2 reason="" st
    if [[ ! -e "$file" ]]; then
        reason="absent"
    elif ! printf '%s' "$key" | cmp -s - "$file"; then
        reason="content-changed"
    else
        st="$(stat -c '%U:%G %a' "$file")"
        if [[ "${st#* }" != "600" ]]; then
            reason="mode-${st#* }"
        elif [[ "${st%% *}" != "${AGENT_USER}:${AGENT_GROUP}" ]]; then
            reason="owner-changed"
        fi
    fi
    if [[ -z "$reason" ]]; then
        echo "[OK]   ${file} up to date"
        return 0
    fi
    TMP="$(mktemp "${AGENT_HOME}/.kb-key.XXXXXX")" || { echo "[ERR]  mktemp failed in ${AGENT_HOME}" >&2; TMP=""; return 1; }
    if ! { printf '%s' "$key" > "$TMP"; }; then
        echo "[ERR]  could not write temp key file for ${file}" >&2
        rm -f "$TMP"; TMP=""; return 1
    fi
    if ! chmod 0600 "$TMP"; then
        echo "[ERR]  chmod failed on temp key file for ${file}" >&2
        rm -f "$TMP"; TMP=""; return 1
    fi
    if ! chown "${AGENT_USER}:${AGENT_GROUP}" "$TMP"; then
        echo "[ERR]  chown failed on temp key file for ${file}" >&2
        rm -f "$TMP"; TMP=""; return 1
    fi
    if ! mv -f "$TMP" "$file"; then
        echo "[ERR]  could not install key file ${file}" >&2
        rm -f "$TMP"; TMP=""; return 1
    fi
    TMP=""
    echo "[CHANGED] ${file} written (mode 0600, reason: ${reason})"
    return 0
}

# process_server NAME URL KEY URLVAR KEYVAR KEYFILE HELPER
# An EMPTY KEYVAR means a URL-only server (agent-gtd): no key is required and no
# key file is reconciled — its helper reads the key from the agent's env only.
process_server() {
    local name=$1 url=$2 key=$3 urlvar=$4 keyvar=$5 keyfile=$6 helper=$7
    local missing=() cls state reason rc rrc desired target prior ptype

    if [[ -z "$url" ]]; then missing+=("$urlvar"); fi
    if [[ -n "$keyvar" && -z "$key" ]]; then missing+=("$keyvar"); fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "[SKIP] ${name}: missing ${missing[*]} — not registered"
        STATUS[$name]=skipped
        if [[ "$(py has "$CLAUDE_JSON" "$name")" == "yes" ]]; then
            rc=0
            run_claude mcp remove "$name" --scope user || rc=$?
            if [[ $rc -ne 0 ]]; then
                echo "[ERR]  claude mcp remove ${name} failed (rc=${rc})" >&2
                FAIL=1; STATUS[$name]=err
            else
                echo "[CHANGED] removed stale ${name} registration"
                STATUS[$name]=changed
            fi
        fi
        return 0
    fi

    STATUS[$name]=ok
    # (a) key file first; on failure skip the registration entirely.
    if [[ -n "$keyvar" ]]; then
        local before_key_out
        before_key_out="$(reconcile_key_file "$keyfile" "$key")" && rc=0 || rc=$?
        # reconcile_key_file's [ERR] lines go to stderr (not captured); echo its stdout.
        if [[ -n "$before_key_out" ]]; then printf '%s\n' "$before_key_out"; fi
        if [[ $rc -ne 0 ]]; then
            FAIL=1; STATUS[$name]=err
            return 0
        fi
        if [[ "$before_key_out" == "[CHANGED]"* ]]; then STATUS[$name]=changed; fi
    fi

    target="${url%/}/mcp"
    desired="$(py build "$CLAUDE_JSON" "$target" "$helper")"
    cls="$(KB_DESIRED="$desired" py classify "$CLAUDE_JSON" "$name")"
    state="${cls%% *}"
    reason="${cls#* }"
    if [[ "$state" == "equal" ]]; then
        echo "[OK]   ${name} registration up to date (${target})"
        REGISTERED[$name]="$desired"
        return 0
    fi

    # (b) capture the prior entry (no claude call), remove (failure tolerated) then
    # add-json, JSON as ONE argv element. A failed add restores the prior entry so a
    # host is never left with the server unregistered.
    prior="$(py get "$CLAUDE_JSON" "$name")"
    run_claude mcp remove "$name" --scope user 2>/dev/null || true
    rc=0
    run_claude mcp add-json "$name" "$desired" --scope user || rc=$?
    if [[ $rc -ne 0 ]]; then
        FAIL=1; STATUS[$name]=err
        if [[ -z "$prior" ]]; then
            echo "[ERR]  claude mcp add-json ${name} failed (rc=${rc})" >&2
            return 0
        fi
        ptype="$(KB_PRIOR="$prior" py prior-type "$CLAUDE_JSON")" || ptype="unknown"
        rrc=0
        run_claude mcp add-json "$name" "$prior" --scope user || rrc=$?
        if [[ $rrc -eq 0 ]]; then
            echo "[ERR]  claude mcp add-json ${name} failed (rc=${rc}); restored previous registration (type=${ptype}, reason was ${reason})" >&2
        else
            echo "[ERR]  claude mcp add-json ${name} failed (rc=${rc}); restore failed (rc=${rrc}) — ${name} is NOT registered" >&2
        fi
        return 0
    fi
    echo "[CHANGED] ${name} registered as http ${target} (reason: ${reason})"
    STATUS[$name]=changed
    REGISTERED[$name]="$desired"
    return 0
}

declare -A REGISTERED=()

if ! py parse "$CLAUDE_JSON"; then
    fail "cannot parse ${CLAUDE_JSON}"
fi

HELPER_PERSONAL=$'printf \'{"Authorization": "Bearer %s"}\' "${PERSONAL_KB_API_KEY:-$(cat ~/.personal_kb_hook_key 2>/dev/null)}"'
HELPER_TEAM=$'printf \'{"Authorization": "Bearer %s"}\' "${TEAM_KB_API_KEY:-$(cat ~/.team_kb_key 2>/dev/null)}"'
# agent-gtd: env-only bearer (the per-run callback token), no key file by design.
# AGENT_GTD_AGENT_NAME is forwarded as X-Agent-Name; the board uses it only when the
# bearer is not a run token.
HELPER_GTD=$'if [ -n "${AGENT_GTD_AGENT_NAME:-}" ]; then printf \'{"Authorization": "Bearer %s", "X-Agent-Name": "%s"}\' "${AGENT_GTD_API_KEY:-}" "$AGENT_GTD_AGENT_NAME"; else printf \'{"Authorization": "Bearer %s"}\' "${AGENT_GTD_API_KEY:-}"; fi'

process_server personal-kb "$PERSONAL_KB_URL" "$PERSONAL_KB_API_KEY" PERSONAL_KB_URL PERSONAL_KB_API_KEY \
    "${AGENT_HOME}/.personal_kb_hook_key" "$HELPER_PERSONAL"
process_server team-kb "$TEAM_KB_URL" "$TEAM_KB_API_KEY" TEAM_KB_URL TEAM_KB_API_KEY \
    "${AGENT_HOME}/.team_kb_key" "$HELPER_TEAM"
process_server agent-gtd "$AGENT_GTD_URL" "" AGENT_GTD_URL "" "" "$HELPER_GTD"

# Post-run readback: secret leak tripwire, then drift warning.
if [[ -e "$CLAUDE_JSON" ]]; then
    _leaks="$(KB_LEAK_PERSONAL="$PERSONAL_KB_API_KEY" KB_LEAK_TEAM="$TEAM_KB_API_KEY" \
        KB_LEAK_AGENT_GTD="$AGENT_GTD_API_KEY" py leak "$CLAUDE_JSON")" \
        || fail "cannot read ${CLAUDE_JSON} for leak check"
    for _n in $_leaks; do
        echo "[ERR]  ${_n}: API key found in .claude.json — secret leaked into registration" >&2
        FAIL=1; STATUS[$_n]=err
    done
    for _n in "${!REGISTERED[@]}"; do
        _diff="$(KB_DESIRED="${REGISTERED[$_n]}" py readback "$CLAUDE_JSON" "$_n")" || _diff="?"
        if [[ -n "$_diff" ]]; then
            echo "[WARN] ${_n} readback differs from desired (extra/changed keys: ${_diff}) — next run will re-register"
        fi
    done
fi

# Optional, non-fatal reachability probe of the agent-gtd registration. The key
# travels in the probe's env, never in argv.
if [[ -n "$PROBE" ]] && [[ "${STATUS[agent-gtd]}" == "ok" || "${STATUS[agent-gtd]}" == "changed" ]]; then
    _probe_rc=0
    _probe_out="$(AGENT_GTD_API_KEY="$AGENT_GTD_API_KEY" python3 "$PROBE" --claude-json "$CLAUDE_JSON" \
        --name agent-gtd --expect-tool add_item --timeout 30 2>&1)" || _probe_rc=$?
    if [[ $_probe_rc -eq 0 ]]; then
        echo "[OK]   agent-gtd probe passed (${AGENT_GTD_URL%/}/mcp)"
        PROBE_STATUS=ok
    else
        echo "[WARN] agent-gtd MCP probe failed (rc=${_probe_rc}) — dispatched agents on this host cannot reach the board: ${_probe_out%%$'\n'*}"
        PROBE_STATUS=fail
    fi
fi

exit "$FAIL"
