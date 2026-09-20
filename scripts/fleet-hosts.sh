#!/usr/bin/env bash
# fleet-hosts.sh — resolve the dispatch fleet's SSH targets from the GTD registry.
#
# Sourced by deploy.sh and talos-update.sh. Prints nothing; sets HOSTS.
#
# WHY THIS EXISTS. Both scripts used to carry a hardcoded default host list.
# Fleet membership was a fact nobody owned, hand-copied into every consumer, and
# it drifted: on 2026-09-20 a fourth host joined and four separate consumers had
# to be edited by hand — two sweep scripts, this script's default, and a doc that
# still described the fleet as two hosts. The registry always knew.
#
# The staleness is not the dangerous part. A delivery pass that covers three of
# four hosts prints exactly what a complete one prints, so the failure is SILENT
# and the fleet quietly diverges. Deriving the list removes the staleness; the
# count guard below removes the silence. Both halves are needed and the guard is
# the cheaper one.
#
# Generalised: when a derived list drives coverage, an unexpectedly short list is
# a failure, not a small job.
#
# Resolution order:
#   DISPATCH_HOST   (singular) — exactly one host, skips derivation entirely
#   DISPATCH_HOSTS  (plural)   — explicit override, skips derivation entirely
#   otherwise                  — derived from `agent-gtd list-dispatch-hosts`
#
# The explicit overrides are the escape hatch for a board outage: if agent-gtd is
# down, derivation fails closed and tells you to set DISPATCH_HOSTS by hand. That
# is deliberate — a stale literal baked in as a silent fallback is the bug this
# file exists to remove.
#
# NOTE ON DRAIN: this deliberately does NOT filter drained hosts. Deploying to a
# drained host is the entire point of draining it — see docs/host-drain.md in the
# agent_gtd repo.

# Minimum plausible fleet size. Anything smaller aborts rather than running a
# partial pass. Tune only if the fleet genuinely shrinks to one host, and prefer
# DISPATCH_HOSTS for a deliberate one-host run.
FLEET_MIN_HOSTS="${FLEET_MIN_HOSTS:-2}"

resolve_fleet_hosts() {
    if [ -n "${DISPATCH_HOST:-}" ]; then
        HOSTS="${DISPATCH_HOST}"
        return 0
    fi

    if [ -n "${DISPATCH_HOSTS:-}" ]; then
        HOSTS="${DISPATCH_HOSTS}"
        return 0
    fi

    if ! command -v agent-gtd >/dev/null 2>&1; then
        echo "ABORTED: agent-gtd CLI not found, cannot derive the fleet." >&2
        echo "  Set DISPATCH_HOSTS='host1 host2 ...' explicitly to proceed." >&2
        return 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        echo "ABORTED: jq not found, required to parse the host registry." >&2
        echo "  Set DISPATCH_HOSTS='host1 host2 ...' explicitly to proceed." >&2
        return 1
    fi

    # jq, never a regex. The registry returns the whole array on ONE line, so a
    # greedy `.*` with a non-global `s///` matches only the LAST element — a peer
    # session swept exactly one host of four that way while printing a clean
    # heartbeat, reproducing the very bug it was fixing.
    HOSTS=$(
        agent-gtd list-dispatch-hosts 2>/dev/null |
            jq -r '.[].url' 2>/dev/null |
            sed -e 's|^[a-z]*://||' -e 's|:.*$||' |
            sort -u
    )

    local count
    count=$(printf '%s\n' ${HOSTS} | grep -c . || true)

    if [ "${count}" -lt "${FLEET_MIN_HOSTS}" ]; then
        echo "ABORTED: derived ${count} host(s) from the registry — refusing to run against a partial fleet." >&2
        echo "  Expected at least ${FLEET_MIN_HOSTS}. This usually means agent-gtd is unreachable" >&2
        echo "  or the registry is empty, NOT that the fleet is small." >&2
        echo "  Derived: ${HOSTS:-<none>}" >&2
        echo "  To proceed anyway: DISPATCH_HOSTS='host1 host2 ...' $0" >&2
        return 1
    fi

    return 0
}
