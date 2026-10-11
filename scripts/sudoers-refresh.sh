#!/usr/bin/env bash
set -euo pipefail

# sudoers-refresh.sh — Render templates/sudoers-dispatch-svc.tmpl and install it
# as /etc/sudoers.d/dispatch-svc when (and only when) it differs from the live
# file. This is the ONLY code that renders or installs the sudoers template; it
# is called by setup-dispatch-host.sh (provision, --create) and deploy.sh (every
# deploy, refresh-only).
#
# Usage:
#   sudoers-refresh.sh --template PATH [--sudoers-file PATH] [--service-user NAME]
#                      [--agent-user NAME] [--visudo PATH] [--owner NAME]
#                      [--group NAME] [--create] [--render-only]
#
# Exit codes:
#   0  up to date ([OK]), refreshed ([CHANGED]) or skipped ([SKIP])
#   1  visudo validation failed, the post-install check failed, or any other step
#      failed (always with an [ERR] line on stderr); a failure before the final
#      mv leaves the live file byte-identical to its pre-run state
#   2  usage error — unknown argument, or --template missing
#   3  --template path does not exist or is unreadable
#   4  --service-user or --agent-user does not exist on this host

PROG="$(basename "$0")"
TEMPLATE=""
SUDOERS_FILE="/etc/sudoers.d/dispatch-svc"
SERVICE_USER="dispatch-svc"
AGENT_USER="dispatch"
VISUDO="visudo"
OWNER="root"
GROUP="root"
CREATE=false
RENDER_ONLY=false
TMP=""

usage() {
    cat >&2 <<USAGE
Usage: ${PROG} --template PATH [--sudoers-file PATH] [--service-user NAME]
       [--agent-user NAME] [--visudo PATH] [--owner NAME] [--group NAME]
       [--create] [--render-only]
USAGE
}

fail() {
    echo "[ERR]  $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$TMP" ]]; then rm -f "$TMP"; fi
}
trap cleanup EXIT
trap 'echo "[ERR]  unexpected failure at line ${LINENO}" >&2' ERR

while [[ $# -gt 0 ]]; do
    case "$1" in
        --template|--sudoers-file|--service-user|--agent-user|--visudo|--owner|--group)
            if [[ $# -lt 2 ]]; then usage; exit 2; fi
            case "$1" in
                --template) TEMPLATE="$2" ;;
                --sudoers-file) SUDOERS_FILE="$2" ;;
                --service-user) SERVICE_USER="$2" ;;
                --agent-user) AGENT_USER="$2" ;;
                --visudo) VISUDO="$2" ;;
                --owner) OWNER="$2" ;;
                --group) GROUP="$2" ;;
            esac
            shift 2 ;;
        --create) CREATE=true; shift ;;
        --render-only) RENDER_ONLY=true; shift ;;
        *) usage; exit 2 ;;
    esac
done

if [[ -z "$TEMPLATE" ]]; then usage; exit 2; fi
if [[ ! -f "$TEMPLATE" || ! -r "$TEMPLATE" ]]; then
    echo "[ERR]  template not readable: ${TEMPLATE}" >&2
    exit 3
fi

render() {
    sed \
        -e "s|{{SERVICE_USER}}|${SERVICE_USER}|g" \
        -e "s|{{AGENT_USER}}|${AGENT_USER}|g" \
        "$TEMPLATE"
}

if $RENDER_ONLY; then
    render
    exit 0
fi

if [[ ! -e "$SUDOERS_FILE" ]] && ! $CREATE; then
    echo "[SKIP] ${SUDOERS_FILE} not present (single-user host or not provisioned) — not created"
    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        echo "[WARN] ${SUDOERS_FILE} absent but service user ${SERVICE_USER} exists — two-user host missing sudoers; run setup-dispatch-host.sh" >&2
    fi
    exit 0
fi

for _u in "$SERVICE_USER" "$AGENT_USER"; do
    if ! getent passwd "$_u" >/dev/null 2>&1; then
        echo "[ERR]  user ${_u} does not exist on this host — ${SUDOERS_FILE} NOT modified" >&2
        exit 4
    fi
done

TMP=$(mktemp "${SUDOERS_FILE}.tmp.XXXXXX") || fail "mktemp failed in $(dirname "$SUDOERS_FILE")"
render > "$TMP" || fail "could not write rendered sudoers to ${TMP}"
sha=$(sha256sum < "$TMP" | cut -c1-12) || fail "sha256sum failed"

if [[ -f "$SUDOERS_FILE" ]] && cmp -s "$TMP" "$SUDOERS_FILE"; then
    echo "[OK]   ${SUDOERS_FILE} up to date (sha256 ${sha})"
    exit 0
fi

chmod 0440 "$TMP" || fail "chmod failed on ${TMP}"
chown "${OWNER}:${GROUP}" "$TMP" || fail "chown failed on ${TMP}"

_vout=""
if ! _vout=$("$VISUDO" -c -f "$TMP" 2>&1); then
    echo "[ERR]  visudo rejected rendered sudoers — ${SUDOERS_FILE} NOT modified" >&2
    if [[ -n "$_vout" ]]; then printf '%s\n' "$_vout" | sed 's/^/[ERR]   /' >&2; fi
    exit 1
fi

backup=""
if [[ -f "$SUDOERS_FILE" ]]; then
    _ts=$(date +%Y%m%dT%H%M%S)
    backup="${SUDOERS_FILE}.bak.${_ts}"
    _n=0
    while [[ -e "$backup" ]]; do
        _n=$((_n + 1))
        backup="${SUDOERS_FILE}.bak.${_ts}.${_n}"
    done
    cp -p "$SUDOERS_FILE" "$backup" || fail "could not back up ${SUDOERS_FILE} to ${backup}"
fi

# Diff old against new BEFORE the mv; printed after the [CHANGED] line.
_old="/dev/null"
if [[ -n "$backup" ]]; then _old="$backup"; fi
_diff=$(diff -u --label old --label new "$_old" "$TMP" || true)

mv -f "$TMP" "$SUDOERS_FILE" || fail "could not install ${TMP} as ${SUDOERS_FILE}"
TMP=""

_pout=""
if ! _pout=$("$VISUDO" -c 2>&1); then
    if [[ -n "$_pout" ]]; then printf '%s\n' "$_pout" | sed 's/^/[ERR]   /' >&2; fi
    if [[ -n "$backup" ]]; then
        if mv -f "$backup" "$SUDOERS_FILE"; then
            echo "[ERR]  full sudoers check failed after install — ${SUDOERS_FILE} restored from ${backup}" >&2
        else
            echo "[ERR]  RESTORE FAILED — ${SUDOERS_FILE} may be invalid; backup at ${backup}" >&2
        fi
    else
        rm -f "$SUDOERS_FILE"
        echo "[ERR]  full sudoers check failed after install — ${SUDOERS_FILE} removed" >&2
    fi
    exit 1
fi

echo "[CHANGED] ${SUDOERS_FILE} refreshed from template (sha256 ${sha}, backup: ${backup:-none})"
if [[ -n "$_diff" ]]; then printf '%s\n' "$_diff" | sed 's/^/[DIFF] /'; fi
exit 0
