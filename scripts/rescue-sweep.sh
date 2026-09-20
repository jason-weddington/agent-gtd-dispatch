#!/usr/bin/env bash
# rescue-sweep.sh — push committed-but-unpushed work out of live dispatch clones.
#
# Runs ON a dispatch host as the AGENT user, on a systemd timer. No agent, no
# control-plane session, no Monitor to re-arm.
#
# WHY THIS EXISTS, and why the worker's own rescue does not replace it.
#
# An agent that commits its own work leaves the clone with a clean tree and a
# moved HEAD. If the run then ends without pushing, those commits exist ONLY in
# that clone, and the clone is deleted at run exit. The dispatch worker now
# commits and pushes abandoned work to the run's branch before teardown, which
# covers any run whose worker REACHES its teardown.
#
# Three cases it cannot cover, and this sweep is the only thing watching them:
#
#   1. A worker that never reaches teardown at all — SIGKILL, OOM, power loss, a
#      wedged service, or a clone orphaned between workspace creation and run
#      registration. Until agent-gtd-dispatch 2.0.3 this also included EVERY
#      service restart, because shutdown cancelled the run tasks and returned
#      without awaiting them, so the rescue's own `await`s never ran.
#   2. The entire span of a live run. Teardown-time protection does nothing for
#      the hours before teardown, and that is exactly when a long run is holding
#      the most unpushed work.
#   3. Uncommitted work. Neither mechanism can push what was never committed;
#      this sweep sees the commit graph and nothing else. Stated so nobody reads
#      a clean sweep as "nothing is at risk".
#
# THE BOUNDING RULE, which is what makes it safe to run unattended: it may only
# do what the worker was already going to do. It pushes `feat/*` to `origin` —
# the same branch, to the same remote, that the worker pushes on its success
# path. It is a retry of an existing access, not a new one. Never `main`, never
# a force-push, never a branch the worker would not have created.
#
# HEARTBEAT: every pass logs a line even when it finds nothing, because a silent
# watchdog and a dead one are indistinguishable. Absence of a recent line means
# this loop died, NOT that the fleet is clean. Check with:
#   journalctl -t dispatch-rescue-sweep --since -1h

set -uo pipefail

TAG="dispatch-rescue-sweep"
WORKSPACE_ROOT="${DISPATCH_WORKSPACE_ROOT:-$HOME/workspace}"

log() { logger -t "$TAG" -- "$*"; printf '%s\n' "$*"; }

swept=0
rescued=0
failed=0
skipped_remote=0

# BOTH clone layouts. A monorepo run clones to `repos-<project>-<runid>/`; a
# WORKSPACE run nests one level deeper at `ws-<runid>/<repo>/`, one directory per
# repo. A glob of `*/` alone matches the workspace ROOT, which has no `.git`, so
# every repo of every workspace dispatch would be skipped — silently, because the
# sweep simply finds nothing there.
for d in "$WORKSPACE_ROOT"/repos-*/ "$WORKSPACE_ROOT"/ws-*/*/; do
    [ -d "$d/.git" ] || continue
    swept=$((swept + 1))

    branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null) || continue
    # feat/* only. The worker never pushes anything else, so neither do we.
    case "$branch" in feat/*) ;; *) continue ;; esac

    # Push only to `origin`, and only when `origin` is the remote the worker
    # cloned from. A workspace run holds SEVERAL remotes side by side, so a check
    # keyed on the project would authorise one and silently ignore the rest.
    remote_url=$(git -C "$d" remote get-url origin 2>/dev/null) || continue
    if [ -z "$remote_url" ]; then
        skipped_remote=$((skipped_remote + 1))
        log "skip: $d has no origin remote"
        continue
    fi

    local_sha=$(git -C "$d" rev-parse HEAD 2>/dev/null) || continue
    remote_sha=$(git -C "$d" ls-remote origin "refs/heads/$branch" 2>/dev/null | cut -f1)
    [ "$local_sha" = "$remote_sha" ] && continue

    # Counts commits reachable from HEAD that NO remote ref has. Deliberately not
    # a `<base>..HEAD` range: the range form makes you choose a baseline and every
    # wrong choice fails quietly. The clone's own `origin/main` is frozen at clone
    # time, so it counts already-pushed commits as unpushed — a permanent false
    # positive that re-reports the same work on every pass. `--not --remotes`
    # takes no baseline, so there is no baseline to get wrong.
    ahead=$(git -C "$d" rev-list --count HEAD --not --remotes 2>/dev/null)
    [ "${ahead:-0}" -gt 0 ] || continue

    log "unpushed: $d branch=$branch commits=$ahead remote=$remote_url"

    # --no-verify: this is a feature-branch push of work whose own gate has
    # already run, and it is fully re-verified at merge. A pre-push coverage hook
    # can take minutes, which at this interval would be most of the duty cycle
    # spent re-proving something. The push is free even when unnecessary — if the
    # worker already pushed, git refuses with a ref-lock rejection rather than
    # doing damage, so there is no reason to hesitate over whether it is needed.
    if git -C "$d" push --no-verify origin "HEAD:refs/heads/$branch" >/dev/null 2>&1; then
        rescued=$((rescued + 1))
        log "RESCUED $d branch=$branch commits=$ahead"
    else
        failed=$((failed + 1))
        log "FAILED to push $d branch=$branch — left in place for a human"
    fi
done

log "pass complete: clones=$swept rescued=$rescued failed=$failed no_remote=$skipped_remote"

# Exit non-zero only on a push failure, so `systemctl status` and OnFailure= show
# something went wrong. Finding nothing is success.
[ "$failed" -eq 0 ]
