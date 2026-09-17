#!/usr/bin/env bash
set -euo pipefail

# env-staleness-check.sh — Compare a service env file against a running
# process's environment (e.g. /proc/<pid>/environ) and report which env-file
# keys are stale: absent from, or different in, the running process.
#
# One-directional: this only reports env-file keys that are missing or wrong
# in the environ dump. Keys that exist only in the environ dump (PATH, HOME,
# LANG, INVOCATION_ID, unit Environment= lines, ...) are never reported.
#
# Never requires root. Never writes any file. Never prints a value — only
# key NAMES, so it is safe to point at a file containing live secrets.
#
# Usage:
#   env-staleness-check.sh --env-file PATH --environ PATH
#
# Exit codes:
#   0  no key differs (stdout empty)
#   1  at least one key differs (stale names on stdout, one per line, sorted)
#   2  usage error — unknown argument or a missing required option
#   3  --env-file or --environ path missing or unreadable

PROG="$(basename "$0")"

usage() {
    cat >&2 <<EOF
Usage: ${PROG} --env-file PATH --environ PATH

Compare an env file (KEY=VALUE lines) against a NUL-separated environ dump
(the /proc/<pid>/environ format) and print the names of env-file keys that
are stale — absent from, or different in, the environ dump. One name per
line, sorted with LC_ALL=C sort. Never prints a value.

Exit codes:
  0  no key differs (stdout empty)
  1  at least one key differs (stale names on stdout)
  2  usage error — unknown argument or a missing required option
  3  --env-file or --environ path missing or unreadable
EOF
}

ENV_FILE=""
ENVIRON_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            if [[ $# -lt 2 ]]; then
                echo "${PROG}: --env-file requires an argument" >&2
                usage
                exit 2
            fi
            ENV_FILE="$2"
            shift 2
            ;;
        --environ)
            if [[ $# -lt 2 ]]; then
                echo "${PROG}: --environ requires an argument" >&2
                usage
                exit 2
            fi
            ENVIRON_FILE="$2"
            shift 2
            ;;
        *)
            echo "${PROG}: unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$ENV_FILE" || -z "$ENVIRON_FILE" ]]; then
    echo "${PROG}: both --env-file and --environ are required" >&2
    usage
    exit 2
fi

if [[ ! -r "$ENV_FILE" ]]; then
    echo "${PROG}: --env-file path missing or unreadable: ${ENV_FILE}" >&2
    exit 3
fi

if [[ ! -r "$ENVIRON_FILE" ]]; then
    echo "${PROG}: --environ path missing or unreadable: ${ENVIRON_FILE}" >&2
    exit 3
fi

# --- Parse --env-file -------------------------------------------------
# Only lines matching ^[A-Za-z_][A-Za-z0-9_]*= are considered. Blank lines
# and lines whose first non-whitespace character is '#' are ignored. The
# key is everything before the FIRST '=', the value is everything after it
# verbatim. Duplicate keys: the LAST occurrence wins.
declare -A ENV_VALUES=()
declare -A ENV_SKIP=()

while IFS= read -r _line || [[ -n "$_line" ]]; do
    [[ "$_line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$_line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        _key="${BASH_REMATCH[1]}"
        _raw_value="${BASH_REMATCH[2]}"
        if [[ "$_raw_value" == *'\'* ]]; then
            # systemd's EnvironmentFile parser unescapes C-style sequences,
            # so a byte comparison against the raw value would be
            # unreliable. Exclude the key from comparison entirely.
            ENV_SKIP["$_key"]=1
            unset -v 'ENV_VALUES[$_key]'
            continue
        fi
        unset -v 'ENV_SKIP[$_key]'
        # Byte-identical to _read_env_var (setup-dispatch-host.sh): strip a
        # trailing '"', then a leading '"', then a trailing ', then a
        # leading ' — each unconditionally and independently, with NO
        # matched-pair requirement.
        _norm="$_raw_value"
        _norm="${_norm%\"}"
        _norm="${_norm#\"}"
        _norm="${_norm%\'}"
        _norm="${_norm#\'}"
        ENV_VALUES["$_key"]="$_norm"
    fi
done < "$ENV_FILE"

for _skipped_key in "${!ENV_SKIP[@]}"; do
    echo "skipped (unsupported quoting): ${_skipped_key}"
done >&2

# --- Parse --environ ---------------------------------------------------
# NUL-separated KEY=VALUE records (the /proc/<pid>/environ format), split
# on the FIRST '='. Records containing no '=' are ignored, and the empty
# final field produced by the trailing NUL is ignored.
declare -A ENVIRON_VALUES=()

while IFS= read -r -d '' _record || [[ -n "$_record" ]]; do
    [[ "$_record" == *"="* ]] || continue
    ENVIRON_VALUES["${_record%%=*}"]="${_record#*=}"
done < "$ENVIRON_FILE"

# --- Compare (one-directional) -----------------------------------------
STALE_KEYS=()
for _key in "${!ENV_VALUES[@]}"; do
    if [[ -v "ENVIRON_VALUES[$_key]" ]]; then
        if [[ "${ENVIRON_VALUES[$_key]}" != "${ENV_VALUES[$_key]}" ]]; then
            STALE_KEYS+=("$_key")
        fi
    else
        STALE_KEYS+=("$_key")
    fi
done

if [[ ${#STALE_KEYS[@]} -eq 0 ]]; then
    exit 0
fi

printf '%s\n' "${STALE_KEYS[@]}" | LC_ALL=C sort
exit 1
