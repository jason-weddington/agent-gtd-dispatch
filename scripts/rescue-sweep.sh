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
# Abandoned clone (its run is no longer live) -> `<branch>`, or `<branch>-r<N>` as today. When <branch> on origin holds commits this clone does not contain, the push goes to <branch>-r<N> instead (an existing one whose tip is already in HEAD, else the first free N in 2..50): the same fallback the worker applies in dispatch.push_with_fallback.
# Live clone (its run is still going) -> `<branch>-rescue-<run key>[-<N>]` only, never `<branch>` and never `<branch>-r<N>`: the -r<N> namespace means "the worker's own fallback for this run", so a mid-run snapshot there would read as the run's deliverable.
#
# LIVENESS. The run's tmp dir `$RUN_TMP_ROOT/rt-<key>` is the primary signal. The
# marker process (a /proc/<pid>/environ entry AGENT_GTD_RUN_KEY=rt-<key>) is a
# fallback that only covers phases where a marked process is alive. Only that one
# entry is matched; nothing else is read from another process's environ.
# FAIL CLOSED off procfs: when the marker check cannot work on this platform
# (no readable $PROC_ROOT/<pid>/environ, e.g. macOS), a clone whose rt dir is
# gone cannot be proved dead, so it is treated as LIVE (live_by=unverifiable)
# and only ever snapshotted to `<branch>-rescue-<run key>`, never pushed as a
# deliverable `<branch>` or `<branch>-r<N>`.
#
# SNAPSHOTS are recorded in the journal (`journalctl -t dispatch-rescue-sweep`,
# SNAPSHOT lines) and NOT on the board, because the sweep has no board URL, no
# API key and only an 8-char item prefix. Live snapshots are pushed by URL, so no
# remote-tracking ref appears in the run's clone.
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
fellback=0
failed=0
skipped_remote=0
snapshots=0
snapshot_current=0

# Run keys are Run.id = uuid4().hex[:12]. Twelve hex glob classes, spelled out.
HEX12='[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
# Mirrors config.RUN_TMP_ROOT's default (WORKSPACE_ROOT.parent / "run-tmp").
RUN_TMP_ROOT="${DISPATCH_RUN_TMP_ROOT:-$(dirname "$WORKSPACE_ROOT")/run-tmp}"
degraded=0
if [ -d "$RUN_TMP_ROOT" ]; then
    log "config: run_tmp_root=$RUN_TMP_ROOT exists=yes"
else
    degraded=1
    log "config: run_tmp_root=$RUN_TMP_ROOT exists=no"
    log "WARN run_tmp_root missing — liveness relies on /proc marker only"
fi
PROC_ROOT="${DISPATCH_PROC_ROOT:-/proc}"

# probe_marker_liveness: can marker_alive work here? A child carrying a known
# marker must find that entry in its own $PROC_ROOT/<pid>/environ.
probe_marker_liveness() {
    # shellcheck disable=SC2016  # $1 and $$ expand in the child sh, not here.
    if AGENT_GTD_RUN_KEY=rt-selfprobe sh -c 'tr "\0" "\n" <"$1/$$/environ" | grep -qxF AGENT_GTD_RUN_KEY=rt-selfprobe' sh "$PROC_ROOT" 2>/dev/null; then
        marker_liveness=available
    else
        marker_liveness=unavailable
    fi
}
probe_marker_liveness
unverifiable=0
log "config: marker_liveness=$marker_liveness"
if [ "$marker_liveness" != available ]; then
    degraded=1
    log "WARN marker liveness unavailable on this platform - every clone without a run-tmp dir is treated as live (snapshot only)"
fi

# marker_alive <key>: true when some process carries AGENT_GTD_RUN_KEY=rt-<key>.
marker_alive() {
    local f pid
    for f in "$PROC_ROOT"/[0-9]*/environ; do
        pid=${f#"$PROC_ROOT"/}
        pid=${pid%/environ}
        case "$pid" in '' | *[![:digit:]]*) continue ;; esac
        if tr '\0' '\n' <"$f" 2>/dev/null | grep -qxF "AGENT_GTD_RUN_KEY=rt-$1"; then
            return 0
        fi
    done
    return 1
}

# choose_target <clone> <candidate prefix>: the shared suffix selection. Lists
# `<prefix>*` on origin and sets target/mode/tip: reuse an existing `<prefix><N>`
# whose tip is an ancestor of HEAD (fewest commits behind, then lowest N), else
# the first free N in 2..50. Leaves target empty when none.
choose_target() {
    local dir=$1 prefix=$2 glob_ok=1 listing taken best_count best_n best_sha sha ref n cnt
    listing=$(git -C "$dir" ls-remote origin "refs/heads/$prefix*" 2>/dev/null) || glob_ok=0
    target=""
    mode=""
    tip="none"
    [ "$glob_ok" -eq 1 ] || return 0
    taken=" "
    best_count=""
    best_n=""
    best_sha=""
    while IFS=$(printf '\t') read -r sha ref; do
        [ -n "$ref" ] || continue
        n=${ref#"refs/heads/$prefix"}
        [ "$n" = "$ref" ] && continue
        case "$n" in '' | 0* | *[![:digit:]]*) continue ;; esac
        if [ "$n" -lt 2 ] || [ "$n" -gt 50 ]; then continue; fi
        taken="$taken$n "
        git -C "$dir" merge-base --is-ancestor "$sha" HEAD 2>/dev/null || continue
        cnt=$(git -C "$dir" rev-list --count "$sha..HEAD" 2>/dev/null) || continue
        if [ -z "$best_n" ] || [ "$cnt" -lt "$best_count" ] ||
            { [ "$cnt" -eq "$best_count" ] && [ "$n" -lt "$best_n" ]; }; then
            best_count=$cnt
            best_n=$n
            best_sha=$sha
        fi
    done <<LISTING
$listing
LISTING
    if [ -n "$best_n" ]; then
        target="$prefix$best_n"
        mode="reuse"
        tip=$(printf '%s' "$best_sha" | cut -c1-12)
    else
        n=2
        while [ "$n" -le 50 ]; do
            case "$taken" in
            *" $n "*) ;;
            *)
                target="$prefix$n"
                mode="first-free"
                break
                ;;
            esac
            n=$((n + 1))
        done
    fi
}

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
    ahead=$(git -C "$d" rev-list --count HEAD --not --exclude="origin/*-rescue-$HEX12" --exclude="origin/*-rescue-$HEX12-*" --remotes 2>/dev/null)
    [ "${ahead:-0}" -gt 0 ] || continue

    # Run key and liveness, only for clones with something unpushed.
    case "$d" in
    "$WORKSPACE_ROOT"/repos-*)
        base=$(basename "$d")
        key=${base##*-}
        ;;
    *)
        key=$(basename "$(dirname "$d")")
        key=${key#ws-}
        ;;
    esac
    if printf '%s' "$key" | LC_ALL=C grep -Eq '^[0-9a-f]{12}$'; then
        if [ -d "$RUN_TMP_ROOT/rt-$key" ]; then
            liveness=live
            live_by=rt
        elif [ "$marker_liveness" != available ]; then
            # Cannot prove the run dead on this platform: fail closed.
            liveness=live
            live_by=unverifiable
            unverifiable=$((unverifiable + 1))
        elif marker_alive "$key"; then
            liveness=live
            live_by=proc
        else
            liveness=abandoned
            live_by=none
        fi
        run_tok=$key
    else
        key=""
        liveness=unknown
        live_by=none
        run_tok=none
    fi

    log "unpushed: $d branch=$branch commits=$ahead remote=$remote_url liveness=$liveness live_by=$live_by run=$run_tok"

    if [ "$liveness" = "live" ]; then
        # A live run: never <branch>, never <branch>-r<N>. Snapshot only.
        tip12=$(printf '%s' "$local_sha" | cut -c1-12)
        target=""
        mode=""
        if ! snaps=$(git -C "$d" ls-remote origin "refs/heads/$branch-rescue-$key*" 2>/dev/null); then
            failed=$((failed + 1))
            log "FAILED to snapshot $d branch=$branch target=none run=$key — left in place"
            continue
        fi
        current=$(printf '%s\n' "$snaps" | awk -v h="$local_sha" '$1 == h { print $2; exit }')
        if [ -n "$current" ]; then
            snapshot_current=$((snapshot_current + 1))
            log "snapshot-current: $d branch=${current#refs/heads/} tip=$tip12 run=$key"
            continue
        fi
        base_sha=$(printf '%s\n' "$snaps" | awk -v r="refs/heads/$branch-rescue-$key" '$2 == r { print $1; exit }')
        if [ -z "$base_sha" ] || git -C "$d" merge-base --is-ancestor "$base_sha" HEAD 2>/dev/null; then
            target="$branch-rescue-$key"
            mode="direct"
        else
            choose_target "$d" "$branch-rescue-$key-"
        fi
        case "$target" in
        "$branch-rescue-$key"*) ;;
        *)
            if [ -n "$target" ]; then
                log "INVARIANT live-target-violation $d target=$target run=$key"
                failed=$((failed + 1))
                continue
            fi
            ;;
        esac
        if [ -z "$target" ]; then
            failed=$((failed + 1))
            log "FAILED to snapshot $d branch=$branch target=none run=$key — left in place"
            continue
        fi
        # Push by URL: creates no refs/remotes/origin/* entry in the live clone.
        if git -C "$d" push --no-verify "$remote_url" "HEAD:refs/heads/$target" >/dev/null 2>&1; then
            snapshots=$((snapshots + 1))
            log "SNAPSHOT $d branch=$target mode=$mode tip=$tip12 run=$key commits=$ahead kind=mid-run-snapshot board=not-noted note=\"mid-run snapshot, not the run's result\""
        else
            failed=$((failed + 1))
            log "FAILED to snapshot $d branch=$branch target=$target run=$key — left in place"
        fi
        continue
    fi

    # --no-verify: this is a feature-branch push of work whose own gate has
    # already run, and it is fully re-verified at merge. A pre-push coverage hook
    # can take minutes, which at this interval would be most of the duty cycle
    # spent re-proving something. The push is free even when unnecessary — if the
    # worker already pushed, git refuses with a ref-lock rejection rather than
    # doing damage, so there is no reason to hesitate over whether it is needed.
    if git -C "$d" push --no-verify origin "HEAD:refs/heads/$branch" >/dev/null 2>&1; then
        rescued=$((rescued + 1))
        log "RESCUED $d branch=$branch commits=$ahead"
        continue
    fi

    # Non-fast-forward, decided WITHOUT fetching: origin holds a tip that HEAD
    # does not contain (exit 1, or 128 when the object is not even local).
    if [ -z "$remote_sha" ] || git -C "$d" merge-base --is-ancestor "$remote_sha" HEAD 2>/dev/null; then
        failed=$((failed + 1))
        log "FAILED to push $d branch=$branch — left in place for a human"
        continue
    fi

    # Same rule as dispatch.push_with_fallback: reuse an existing <branch>-r<N>
    # whose tip is an ancestor of HEAD (fewest commits behind, then lowest N),
    # else the first free N in 2..50. Never force, never delete.
    choose_target "$d" "$branch-r"
    if [ -z "$target" ]; then
        failed=$((failed + 1))
        log "FAILED to push $d branch=$branch fallback=none reason=no-free-suffix — left in place for a human"
        continue
    fi

    log "fallback: $d requested=$branch target=$target mode=$mode tip=$tip"
    if git -C "$d" push --no-verify origin "HEAD:refs/heads/$target" >/dev/null 2>&1; then
        rescued=$((rescued + 1))
        fellback=$((fellback + 1))
        log "RESCUED $d branch=$target commits=$ahead fallback_from=$branch mode=$mode"
    else
        failed=$((failed + 1))
        log "FAILED to push $d branch=$branch fallback=$target reason=push-rejected — left in place for a human"
    fi
done

log "pass complete: clones=$swept rescued=$rescued fellback=$fellback failed=$failed no_remote=$skipped_remote snapshots=$snapshots snapshot_current=$snapshot_current degraded=$degraded marker_liveness=$marker_liveness unverifiable=$unverifiable"

# Exit non-zero only on a push failure, so `systemctl status` and OnFailure= show
# something went wrong. Finding nothing is success.
[ "$failed" -eq 0 ]
