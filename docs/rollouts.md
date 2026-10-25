# Rollouts and the manage mode

This document is the reference for the rollout/manager subsystem in
`agent-gtd-dispatch`: what a rollout is, how its DAG is built, how a manage-mode
agent drives the wave loop end-to-end, the state and recovery contracts, and the
guardrails that protect `main` while the lead is asleep.

The canonical source code lives in:

- `src/agent_gtd_dispatch/dispatch.py` — prompt builders, including
  `_build_manage_prompt` and `_MANAGE_ALLOWED_TOOLS`.
- `src/agent_gtd_dispatch/main.py` — the `/dispatch` endpoint, the
  worker-driven wave loop (`_drive_rollout`, `_rollout_wave_tick`), the land
  step, and the startup re-adoption sweep (`_readopt_running_rollouts`).
- `src/agent_gtd_dispatch/gtd_client.py` — the HTTP calls into agent-gtd that
  back rollout operations (`advance_rollout`, `complete_in_rollout`,
  `halt_rollout`, `list_running_rollouts`).

The agent-gtd repo owns the database schema and business logic for rollouts
themselves (planning, status transitions, blocker resolution); this repo
implements the execution side. Where the docs below mention a tool or endpoint
on the agent-gtd side, that's intentional — those are the contracts the manage
agent uses to drive the rollout forward.

---

## What a rollout is

A **rollout** is a planned, executed wave of work spanning one or more GTD
items in a single project. It bundles:

- a **DAG of items** with dependency edges (some items must finish before
  others can start),
- a **mutable execution state** that the manage agent publishes as it runs
  (`phase`, `current_item_id`, `current_step`, `last_updated`),
- a **terminal status** (`completed`, `failed`, `halted`, `cancelled`), and
- a single **manage-mode dispatch run** that drives the whole thing.

A rollout is **project-scoped**. Every item in a rollout belongs to the same
project, and the manage agent's workspace is a clone of that project's git
origin. Cross-project waves cannot share a rollout — they need parallel
rollouts in each project, coordinated at a higher layer (typically the
interactive lead agent).

### Rollout vs single dispatch

A single dispatch (`mode="build"` with no `rollout_id`) is the simpler shape:
one item, one feature branch, one build agent. The lead picks it up after the
branch is pushed, runs review + merge themselves, and deletes the branch.

A rollout is the wave shape:

- The lead calls `plan_rollout(item_ids=[...])` once to build the DAG.
- The lead starts the rollout once. On the dispatch host, that request (a `/dispatch` with `mode="manage"` and the `rollout_id`) no longer launches a resident manager agent: the dispatch worker itself drives the wave loop in-process (`_rollout_loop_worker` → `_drive_rollout` → `_rollout_wave_tick` in `main.py`).
- Each tick dispatches the items that are ready, waits on in-flight builds, and launches ONE short-lived reviewer per completed build. The reviewer reconciles the acceptance criteria, runs the merge bar and, if it approves, creates the item's squash commit LOCALLY — it never pushes.
- The worker then lands that commit itself (see "The worker-owned land step" below): it fetches origin, rebases the squash onto current `origin/<default_branch>`, runs the project's `gate_command` on that exact tree, and pushes only on green. Only after the push does it record the item complete and advance the rollout — with no lead involvement per item.
- The lead only intervenes if the rollout halts.

Default to rollouts whenever you have two or more items in the same project that benefit from a uniform review and merge cadence. Single dispatches are for one-off items or cross-project pairs that can't share a rollout.

---

## The three dispatch modes

Every `/dispatch` request carries a `mode` field. The same `_dispatch_worker`
background coroutine runs all three, but the prompt, the allowed tools, the
default timeout, and the cleanup policy differ.

### `mode="plan"` — grooming a single item

- **Input:** one `item_id`.
- **Workspace:** fresh clone on a feature branch (same as `build`), but the
  agent is forbidden from writing code or pushing.
- **Job:** read the codebase, fill in `acceptance_criteria`, `files_to_modify`,
  and `scope_out` structured fields via `update_item`, pick a build engine via
  the rubric in the prompt, and move the item to `ready`.
- **Output:** an item that's ready to dispatch as `mode="build"`.

Plan-mode dispatches never use `claude-code-ollama`; the `/dispatch` endpoint
swaps the engine to `claude-code` and reports the swap via `EngineSwap` in the
response (`src/agent_gtd_dispatch/main.py:563-567`).

### `mode="build"` — implementing a single item

- **Input:** one `item_id` (and optionally a `rollout_id` if this build is a
  child of a rollout — see the manage flow below).
- **Workspace:** fresh clone on a feature branch named
  `feat/<item_id_short>-<title_slug>` (see
  `agent_gtd_dispatch_protocol.branches.make_branch_name`). If that branch
  already holds an older run's commits, the worker and the teardown rescue
  push to `feat/<...>-r<N>` instead, and the reviewer is handed that name.
- **Job:** implement the item per its acceptance criteria, run tests, push the
  feature branch, post a final comment, set item status to `review`.
- **Output:** a pushed feature branch waiting for review/merge. The build
  agent **does not merge to main** — that's either the lead's job (singleton
  dispatch) or the manager's job (rollout child).

### `mode="manage"` — driving a rollout

- **Input:** one `rollout_id` (the `item_id` field on the request is unused).
- **What runs:** no long-lived agent. The dispatch worker drives a wave loop in-process for the life of the rollout and occupies one run slot while it does.
- **Workspace:** persistent per-rollout review SLOTS (`dispatch.prepare_rollout_workspace(..., slot=k)`): slot 0 is `rollout-<rollout_id>`, slot `k >= 1` is `rollout-<rollout_id>-s<k>`. A slot is prepared lazily, the first time that index is needed, so a rollout that never has two builds reviewable at once only ever clones slot 0. Each slot is single-writer: the loop assigns it to one item at a time (that item's review, then its land) and fetches and hard-resets it to `origin/<default_branch>` in every repo before every reviewer launch, so no reviewer inherits another's tree. A slot whose root was pruned by retention is prepared again. Every slot is cleaned up when the rollout completes and kept as evidence when it halts.
- **Concurrent reviews:** completed builds are reviewed concurrently, each reviewer in its own slot and running as a background task, so a long review never blocks the loop. The cap is HOST-WIDE: `DISPATCH_ROLLOUT_MAX_CONCURRENT_REVIEWS` (default 3, minimum 1) bounds the loop-started reviewers in flight across every rollout loop in the process, and is also the per-rollout slot count. Reviewers run on a dedicated thread pool of that size (`dispatch._review_executor`), so they never starve builds, land gates or reaping on the shared pool. Ready items are dispatched on EVERY tick, whatever reviews or lands are in flight; `next_ready` comes from the board's blocker graph, and a merged predecessor is recorded complete only after its land has pushed and audited the gated sha, so a dependent is never dispatched against a base missing its predecessor. Reviews running at the same time do not see each other's merge notes; only a reviewer started after a land does.
- **Halt and drain:** a halt (a reviewer's `halt` verdict, a land halt, or the board reporting the rollout halted) cancels every review in flight (the reviewer's session is terminated and its run recorded `cancelled`), drops queued merge verdicts unlanded (the halt reason names them; they are re-reviewed on resume), and never cancels a land that is already running: the loop waits for it, then exits. A draining loop host starts no new review and dispatches nothing, but lets in-flight reviews and queued lands finish before it parks. Review and land state is in memory only: after a service restart the re-adopted loop re-reviews every item that is still `dispatched` with a terminal build, after checking origin for a squash commit carrying both that item's and this rollout's trailers (`Item:` and `Rollout:`); one found in every repo is recorded instead of re-reviewed.
- **Observability:** the phase banner is advisory and last-writer-wins (a reviewer's own `reviewing` publish can overwrite the land's `merging` until the next land heartbeat). The `rollout-loop:` log events are the source of truth: `review-start`, `review-harvest`, `land-enqueue`, `land-start`, `land-done`, `halt-teardown`, `drain-finishing`, `pre-review-landed-check`, `concurrency-invariant-violation`, and a `concurrency-summary` line at loop exit.
- **Per completed build:** one short-lived reviewer (`mode=review`, engine from the rollout's `reviewer_engine` or `DISPATCH_REVIEWER_ENGINE`) reviews ONE build and writes a verdict from a closed set — `merge`, `re-dispatch`, `skip`, `halt` — to `.dispatch/verdict.json`. On `merge` it has created the squash commit locally on the default branch, with the `Rollout: <rollout_id>` and `Item: <item_id>` trailers, and has pushed nothing. The worker executes every verdict; the reviewer cannot dispatch, record outcomes or (by contract, and detected if it tries) push.
- **Output:** a sequence of squash commits on `main` (one per item), each pushed by the worker after its own land gate, and a closed rollout (`completed` or `halted`).

`gate_command` is read from the project record snapshotted when the rollout starts; a change to a project's `gate_command` mid-rollout takes effect on the next rollout.

The wave loop rides out board outages of up to `ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS` (env `DISPATCH_ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS`, default 3600). Only transient errors count: any `httpx.TransportError` (`ReadTimeout`, `ConnectTimeout`, `ConnectError`, `RemoteProtocolError`, ...) or an HTTP 502, 503 or 504 from the board, raised out of the loop's initial `get_rollout`/`get_project` reads or out of one tick. On such an error the loop logs a `rollout-loop: board-unavailable` warning (rollout id, exception class, consecutive-failure count), sleeps `ROLLOUT_LOOP_POLL_SECONDS` and re-runs the tick from the top; a clean tick ends the streak. Every other error, including any other 4xx/5xx, fails the loop run at once, as before. The window is wall-clock time from the first error of the current streak; past it the last error is re-raised and the loop run fails with `rollout_loop_error: ...`. The rollout then stays `running` with no driver and needs manual recovery, because `dispatch_rollout` is one-shot (it accepts only a `pending` rollout).

### The worker-owned land step

Items that were gate-green on their own clones can still turn `main` red together when one item carries a contract another item changes (a test in one asserting state the other moves). File-overlap planning cannot see that. So the land is deterministic and owned by the worker (`_land_reviewed_merge` in `main.py`), never by the reviewer prompt.

The land is strictly SERIAL per rollout, even though reviews run concurrently: a `merge` verdict joins the rollout's land queue, at most one land runs at a time, and lands start in the order their verdicts were harvested. The land runs in the reviewed item's own slot. Because each land rebases onto the LIVE origin tip and gates that exact tree, a review done against an older base is still gated on the post-merge tip. Concurrent lands or a merge queue are deliberately out of scope: the gate must run on the post-merge tip.

After a `merge` verdict, and before anything is recorded on the board, the worker:

1. Takes a per-remote lock for every repo in the workspace, keyed by (normalized origin URL, default branch), in sorted order, and holds them through any fix relaunch until the final push or halt. Two lands to the same remote branch on one host are serialized, so they can never both pass the gate against the same old `main`. The lock is per host only; there is no cross-host lock service.
2. Fetches origin and runs a reviewer-pushed guard: a commit for this item that reached origin after the review started halts the rollout with nothing pushed.
3. Reads the landing set from git, not from the reviewer: the repos exactly one trailer-bearing commit ahead of `origin/<default_branch>`, with HEAD on the default branch.
4. Runs the conflict guard. The reviewer may resolve a squash-merge conflict when every conflicting path is documentation, and reports those paths in the verdict's `resolved_conflict_paths`; any other conflict it aborts and reports as `merge_conflict`. The worker does not trust that claim. For each landing repo it recomputes the reviewer's squash-merge conflicts with `git merge-tree --write-tree`, merging the feature branch (local, else `origin/<branch>`) into the squash commit's parent (`<default_branch>~1`, what the reviewer merged onto, not the freshly fetched origin). It halts with nothing pushed if any non-documentation path conflicted, if the reviewer reported resolving a non-documentation path, or if conflict markers (`<<<<<<<`, `|||||||`, `>>>>>>>` lines) remain in a conflicted documentation file. Documentation means a path ending in `.md`, `.mdx`, `.rst` or `.txt`, case-insensitive, at any depth (`dispatch.DOCUMENTATION_EXTENSIONS`). A documentation conflict the reviewer did not report is logged as `land-conflict-unreported` and the land proceeds. Every repo checked logs one `rollout-loop: land-conflict-check ...` line with its `decision` (`clean`, `doc_only`, `non_doc_halt`, `markers_halt`), and a land that saw documentation conflicts says so in its item comment.
5. Rebases every not-yet-pushed landing repo onto the freshly fetched `origin/<default_branch>`. In a workspace project, every repo NOT in the landing set is reset to its origin, so the compound gate sees current siblings.
6. Runs the project's gate install step (hook managers) once, then the `gate_command` from the workspace root on that exact tree, and records the gated sha of each landing repo.
7. Pushes exactly the gated sha (`git push --no-verify origin <gated_sha>:refs/heads/<default_branch>`; `--no-verify` because the land gate just judged that sha). It never forces. A project with no `gate_command` skips the gate and pushes with the repo's own hooks instead.
8. Audits each push with `git ls-remote`, deletes the feature branch (best effort), posts one `Landed <sha> on <repo>/<branch> ...` comment on the item, and only then records the item complete.

The conflict guard needs git >= 2.38 (`git merge-tree --write-tree`) on every dispatch host. On older git every land halts with `land bad local state` naming merge-tree.

A non-fast-forward rejection (another host pushed first) re-fetches, re-rebases, resets the siblings and re-gates before pushing again, bounded by `MAX_LAND_PUSH_ATTEMPTS` (3) rejections. This push rejection plus regate is the only cross-host race protection.

A red land gate before any repo has been pushed gets ONE fix relaunch (`MAX_LAND_FIX_RELAUNCHES`): the reviewer is relaunched in FIX MODE with the gate output, may amend the squash commit in place (`git commit --amend --no-edit`), and the worker re-checks, re-rebases and re-gates. A second red gate, a red gate after any repo has pushed, or a fix verdict other than `merge` halts. A gate timeout, a gate launch error or a gate install failure halts without a relaunch.

While it waits on locks or runs a gate, the land republishes `phase=merging` every `LAND_HEARTBEAT_SECONDS`, so the rollout monitor's "last update" keeps moving through a long gate. Nothing reads that heartbeat for liveness (the staleness watchdog was retired).

Every halt reason from the land starts with one of these prefixes:

- `commit for item <item_id> appeared on origin during review, before the worker's land` — the reviewer pushed.
- `land bad local state for item <item_id>` — not exactly one trailer-bearing commit ahead, HEAD off the default branch, a squash the rebase emptied, or HEAD moved during the gate.
- `merge verdict but no local squash commit for item <item_id>` — nothing to land in any repo.
- `land refused for item <item_id>` — the conflict guard found a non-documentation conflict (outcome code `non_doc_conflict`) or leftover conflict markers (outcome code `conflict_markers`); nothing is pushed.
- `land conflict check failed for item <item_id>` — the verdict names no feature branch, or the branch exists neither locally nor on origin (outcome code `conflict_check_failed`); nothing is pushed.
- `land gate install failed for item <item_id>`
- `land gate failed for item <item_id>: ` — followed by the TAIL of the gate output.
- `land gate timed out for item <item_id>`
- `land gate launch error for item <item_id>`
- `land rebase conflict for item <item_id>` — names the repo and every conflicting path; the rebase is aborted and nothing is pushed.
- `land push rejected <N> times for item <item_id>`
- `land push failed for item <item_id>`
- `land audit mismatch: remote <remote_sha> != gated <gated_sha>`
- `land git timed out` — names the git command.

In a workspace project every landing repo is rebased before anything is pushed, the gate runs once from the workspace root, and pushes go in repo order. A halt after the first push lists each landing repo as `<repo>: pushed` or `<repo>: not pushed`. On every halt the feature branch is left in place.

Each push attempt writes one `rollout-loop: land ...` log line per landing repo carrying `base_before`, `base_after`, `origin_advanced`, `gated_sha`, `pushed_sha`, the gate decision and an `outcome`.

---

## How a rollout flows

```
lead                     agent-gtd (in-process)          manage agent
 │                                │                            │
 │ plan_rollout(item_ids=[...])   │                            │
 ├──────────────────────────────▶│                            │
 │                                │  read blockers +           │
 │                                │  files_to_modify           │
 │                                │  derive_edges() — no LLM   │
 │◀──────────────────────────────│                            │
 │                                                             │
 │ dispatch_item(mode="manage",   │                            │
 │   rollout_id=...)              │                            │
 ├────────────────────────────────────────────────────────────▶│
 │                                                             │
 │                                       phase="warm_up"       │
 │                                       advance_rollout       │
 │                                       dispatch wave 1       │
 │                                       (build agents push    │
 │                                        feature branches)    │
 │                                       quality gates         │
 │                                       squash merge → main   │
 │                                       complete_in_rollout   │
 │                                       advance_rollout (next)│
 │                                       ... loop ...          │
 │                                       phase="completed"     │
 │◀────────────── rollout terminal: completed / halted ────────│
```

The lead's involvement is ~15-20 minutes of upfront planning. After
`dispatch_item(mode="manage", ...)` returns, the rollout runs for hours
without further intervention unless it halts.

---

## DAG construction: `plan_rollout`

**There is no LLM in this path, and no dispatch-side endpoint.** The
planner (`POST /plan`, `rollout_planner.py`, `PlanRequest` / `DagEdge` /
`RolloutPlan`) was deleted; the DAG is derived deterministically on the
agent-gtd side by `agent_gtd.services.rollout_dag.derive_edges`, called
from `rollout_service.plan_rollout`.

Edges come from exactly two sources, merged and deduplicated:

| Source | Rule |
|---|---|
| Item **blockers** | Each unresolved blocker of item B that is also in the rollout becomes an edge blocker → B. Explicit, declared, no inference. |
| **File overlap** in `files_to_modify` | If two items name a path in common (or one names a directory containing the other's path), they are serialised in the caller's `item_ids` order. This is what keeps two build agents off the same file. |

`derive_edges` then runs `assert_acyclic` over the merged list and raises
rather than persisting a cyclic graph — the old LLM path had no cycle
check at all.

`rollout_plans.planner_model` is NOT NULL, so rows written by this path
carry the sentinel `"derived-from-blockers"`
(`rollout_dag.DERIVED_PLANNER_MODEL`); the frontend
(`RolloutActivityTab.tsx`) special-cases that value when rendering plan
provenance.

`acceptance_criteria` and `description` no longer influence the graph at
all — nothing reads prose. Only `blockers` and `files_to_modify` matter,
which means a missing `files_to_modify` entry is now the one way two
items can be scheduled concurrently against the same file.

### What DAG construction does NOT do

- It does not assign **waves** explicitly. The DAG is the wave plan; the
  agent-gtd side's `advance_rollout` derives "what's ready now" from the DAG
  + item statuses at each call.
- It does not pick engines. Engine selection is per-item, done by plan-mode
  dispatches (see the Engine-Selection Rubric in `_build_plan_prompt`).

---

## The manager's lifecycle

> **Retired.** The resident manager agent described from here down to "Recovery semantics" is no longer launched; the worker-driven loop and land step above replace it. The text is kept as a reference for the legacy prompt (`_build_manage_prompt`), which is still in the codebase. Its keep-alive machinery (relaunch ladder, watchdog, retry caps, recovery prompt) was removed in item 7baf77d0.

A manage-mode dispatch is a single Claude Code subprocess that runs for up to
4 hours of wall-clock time. The full system prompt is generated by
`_build_manage_prompt` in `dispatch.py` — read that function for the
authoritative spec. This section walks through the phases and what each one
does.

### Launch: positional `item_id` is a placeholder

The `/dispatch` endpoint requires `rollout_id` for `mode="manage"` but does
**not** require `item_id` (`main.py:574-578` and the conditional that follows).
The `Run` row stores `item_id=None` for manage runs; the dispatch worker
derives the project from the rollout via `gtd_client.get_rollout(rollout_id)`
and then `get_project(project_id)`.

The prompt explicitly tells the agent to ignore the launch `item_id` because
the dispatch protocol historically required one:

> The `item_id` you received as the dispatch trigger is a positional
> placeholder, not a rollout item to act on. **Ignore it entirely.** Your
> sole source of truth for which items to dispatch is the rollout plan —
> read it via `advance_rollout`.

This guidance survives because some legacy paths still emit an `item_id` on
the dispatch trigger comment. The agent treats `advance_rollout` as the only
source of truth for what to work on.

### Phase 1 — Warm-up

The warm-up phase runs **once** at the start, *concurrently with the
wave-1 builds*. The prompt is explicit about ordering:

> IMPORTANT: Dispatch all wave-1 items first (Phase 2 Step 1 below), THEN run
> warm-up steps while waiting for those builds to complete.

So the actual flow is:

1. `advance_rollout` to get wave 1.
2. Dispatch every wave-1 item as `mode="build"` with `rollout_id` set.
3. While those builds run, do the warm-up tasks:
   - `update_rollout_state(phase="warm_up", current_step="Verifying main is green")`.
   - Install Python deps (`[ -f pyproject.toml ] && uv sync`).
   - Install JS deps (`[ -f package.json ] && npm install`).
   - Git hooks are installed and verified by the dispatch worker before the manager
     launches (see architecture.md § Gate Install) — the manager does not install them.
   - Read `CLAUDE.md` and `README.md` to learn the project's test/lint
     commands and coverage threshold — this becomes the **merge bar** the
     manager applies to every child build.
   - Run the test + lint commands against the cloned `main`. If they fail,
     `halt_rollout` with the failure detail and STOP — the project isn't in
     a mergeable state.

The warm-up establishes that `main` is in a state worth merging into. If the
test suite is already red on `main`, no amount of careful per-item gating
will save the rollout — and a halt early is much cheaper than running 6
wave-1 builds that all fail their gates.

### Phase 2 — Wave loop

The wave loop is the heart of manage mode. It repeats until `advance_rollout`
reports `graph_complete=true`. Each iteration has seven steps.

#### Step 1 — Advance

```
mcp__agent-gtd__advance_rollout(rollout_id=...)
```

Returns:

```python
{
    "next_ready": [item_id, ...],   # items unblocked & ready to dispatch
    "in_progress": [item_id, ...],  # items currently being built
    "blocked": [item_id, ...],      # informational
    "graph_complete": bool,          # all items terminal?
}
```

`advance_rollout` is the **source of truth** for what to dispatch next. The
manager never derives readiness from item statuses directly — agent-gtd is
the authority on which items are unblocked and the manager treats the
response as the contract.

Failure handling: retry up to 3 times with 30s sleep, then `halt_rollout`
with reason `"advance_rollout failed 3 times"` and EXIT. If `graph_complete`
is true and `next_ready` is empty, EXIT with success — the rollout is done.

#### Step 2 — Dispatch ready items

For each `item_id` in `next_ready`:

```
update_rollout_state(phase="dispatching", current_item_id=item_id,
                     current_step=f"Dispatching {item_id}")
dispatch_item(item_id=item_id, mode="build", rollout_id="...")
```

The `rollout_id` parameter is **required** on every child dispatch — it's
how agent-gtd attributes the build to the rollout and counts it toward
completion. The manager records the returned `run_id` alongside the
`item_id` for the polling step.

#### Step 3 — Poll to completion (event-driven)

The manager arms one background Bash poller per dispatched run:

```bash
until s=$(agent-gtd run-status <run_id> | jq -r .status 2>/dev/null) \
      && [ -n "$s" ] && [ "$s" != "running" ] && [ "$s" != "pending" ]; do
  sleep 30
done
echo "DONE <run_id> status=$s"
```

The Claude Code harness delivers a `<task-notification>` event when each
poller exits, so the manager doesn't burn turns on foreground sleeps. The
`[ -n "$s" ]` guard prevents a transient empty status (e.g. during an
agent-gtd service bounce) from triggering a false-DONE.

When a notification arrives, the manager:

1. Confirms status via `get_run_status(<run_id>)` (MCP) in case the CLI's
   auth env drifted.
2. Continues with Step 4 (AC reconciliation) and onward for **that one
   run** — without waiting for other runs in the same wave.

If a run ended `failed`, `timed_out`, or `cancelled`, that run is treated as
a halt candidate; see the **Halt path** below — except a failed run whose
error starts with `post-run gate` (branch pushed, only the project gate
failed): the manager re-runs the project gate command on the branch in its
quality-gate step, inline-fixes if small, and merges only once it exits 0.

#### Step 4 — AC reconciliation

```
update_rollout_state(phase="reconciling_ac",
                     current_step="Checking downstream AC impact")
```

After a child build merges, the manager checks whether the merged change
invalidates the acceptance criteria of any later wave item that touches the
same module or interface. If so, it patches the later item via `update_item`
and posts a comment explaining the change:

```
add_comment(item_id=<later_item_id>,
            content_markdown="AC updated: <what changed and why>")
```

This is a judgment call (the manager uses `get_item` on plausibly-affected
items and decides) — there's no programmatic dependency tracking beyond
what the planner already encoded. The prompt frames it as "did this merge
rename a class or change a config key that a later item's AC mentions?"

#### Step 5 — Quality gates

```
update_rollout_state(phase="reviewing", current_item_id=item_id,
                     current_step=f"Running quality gates on <branch>")
```

The manager fetches the feature branch and runs the test + lint commands
captured during warm-up:

```bash
git fetch origin <branch_name>
git checkout <branch_name>
# run test + lint commands from warm-up
```

It also inspects the diff for **unrelated manifest changes** — additions to
`package.json`, `package-lock.json`, `pyproject.toml`, or `uv.lock` that are
not directly tied to the item's stated scope. These are typically defensive
workarounds the build agent added to silence host warnings; the manager
reverts them via `git checkout HEAD -- <file>` and re-runs gates.

If gates pass with no unrelated manifest changes remaining, proceed to
Step 6 (squash merge).

If gates fail, the manager attempts an **inline fix** for narrow categories:

- Formatting (a `ruff format` pass).
- A single missing import.
- A one-line change to a stale test assertion that the current change makes
  correct.
- A coverage ratchet bump (raising `fail_under` to lock in the new floor).

If the fix succeeds, gates are re-run. If the fix fails or the failure is
non-trivial, the manager calls `halt_rollout` with detail of which command
failed on which branch.

#### Step 6 — Squash merge

```
update_rollout_state(phase="merging", current_item_id=item_id,
                     current_step=f"Merging <branch_name> → main")
```

Before merging, a **commit-count guard** runs:

```bash
git fetch origin <branch_name>
commit_count=$(git rev-list origin/<default_branch>..<branch_name> --count)
```

If `commit_count` is 0, the build agent reported success but pushed no
commits. The manager halts with:

```
halt_rollout(rollout_id=..., reason="build agent reported success but
pushed no commits: <branch> has no commits beyond origin/<default_branch>")
```

This catches a known build-agent failure mode (agent says "done" but the
branch is empty) without producing a no-op merge commit.

If the guard passes, the manager performs the merge itself:

```bash
git checkout <default_branch>
git merge --squash <branch_name>
git commit -F - <<'COMMITEOF'
feat(<item_id short>): <item title>

Rollout: <rollout_id>
Item: <item_id>
COMMITEOF
git push origin <default_branch>
git push origin --delete <branch_name>
git branch -D <branch_name>
```

The merge is **always a squash** with a conventional-commit message and a
trailer linking the rollout and item. The feature branch is deleted from
both origin and local after a successful push.

This is the contract that distinguishes rollouts from direct builds: **the
manager does the merge and push**, not the lead.

#### Step 7 — Complete in rollout

```
result = complete_item_in_rollout(
    rollout_id=..., item_id=item_id,
    outcome="completed",
    merge_actor="manager-autonomous",
    decision_rule="agent-judgment",
)
```

`complete_item_in_rollout` is the rollout-aware completion call. On
`outcome="completed"` it does two things:

1. **Cascades the item's GTD status to `done`.** The manager does NOT need
   to call `complete_item` separately.
2. **Auto-closes the rollout if this was the last terminal item.** The
   response includes `graph_complete: true` when the rollout has flipped
   to `completed`.

If `result["graph_complete"]` is true, the manager:

1. Deletes the manage branch on origin as a courtesy (the manage workspace
   was on the default branch, but agent-gtd may have created a tracking
   branch — `feat/<rollout_id[:8]>-manage`):

   ```bash
   git push origin --delete feat/<rollout_id[:8]>-manage || true
   ```

2. EXITs with success. It must NOT call `advance_rollout` again — the
   agent-gtd side rejects calls on a completed rollout.

Otherwise (the rollout still has work), the manager loops back to Step 1
(advance) for the next wave or next unblocked items.

### Halt path

Before halting, the manager publishes the halt state:

```
update_rollout_state(phase="halted", current_step=<reason>)
```

It posts a comment to the offending item (NOT the launch placeholder
`item_id`):

```
add_comment(item_id=<offending_rollout_item_id>,
            content_markdown="Rollout halted: <reason>")
```

If there's no specific offending item (e.g. `advance_rollout` itself failed),
the comment goes to the project. Then:

```
halt_rollout(rollout_id=..., reason=<reason>)
```

And the manager STOPs. A halted rollout is recoverable — the lead can fix
the underlying issue, manually advance items, and either re-dispatch the
manager or finish the wave inline. A merged regression is not recoverable
without a revert and a chain of follow-ups.

### Phase 3 — Sensitive-area guidance

Before merging, the manager inspects each diff for patterns that warrant a
halt rather than auto-merge. This is **judgment guidance, not a hard
predicate** — the manager decides whether the change is routine (e.g. a
typo fix in a Dockerfile comment) or substantively risky.

Patterns flagged by the prompt:

| Area | Glob patterns |
|---|---|
| Auth code | `**/auth.py`, `**/auth_routes.py`, route authentication modules |
| Deploy/release scripts | `deploy.sh`, `release.sh`, `start.sh` |
| CI/hooks | `.github/**`, `.pre-commit-config.yaml` |
| Infrastructure units | `*.service`, `Dockerfile*`, `nginx*.conf` |
| Env/secrets | `.env*`, `.envrc*` |

If the diff touches any of these and the change isn't trivial, the manager
halts and posts a comment on the offending item explaining why. The lead
reviews and decides.

### Guardrails — never lower the quality bar

The prompt declares these absolute:

- **Coverage threshold ratchets up only.** Never lower
  `[tool.coverage.report] fail_under` in `pyproject.toml`. If a build fails
  the coverage gate, the only legitimate responses are: add tests to recover
  coverage, or halt the rollout. A `chore: lower coverage threshold` commit
  is a guardrail violation; the manager is told to revert it before merging
  if it sees one.
- No commenting out `pytest` hooks or skipping the test suite.
- No `--skip` flags on lint steps or removing lint steps.
- No blanket `# type: ignore` suppressions.
- No `git push --no-verify` to bypass pre-push hooks.

When in doubt: halt. A halted rollout recovers. A merged regression doesn't.

---

## State publishing: `update_rollout_state`

The manager publishes its state continuously via
`mcp__agent-gtd__update_rollout_state`. The four fields:

| Field | Type | Notes |
|---|---|---|
| `phase` | string | High-level state — `warm_up`, `dispatching`, `polling`, `reconciling_ac`, `reviewing`, `merging`, `halted`. |
| `current_item_id` | string \| null | Which item is currently being worked on. |
| `current_step` | string | One-line human-readable status (e.g. `"Merging feat/abc12345-foo → main"`). |
| `last_updated` | datetime | Set by agent-gtd on each call. |

### Replacement semantics — read this carefully

Every call to `update_rollout_state` **REPLACES all four fields**. Fields you
omit are reset to None. The prompt is explicit:

> NOTE on `update_rollout_state`: each call REPLACES all four state fields
> (phase, current_item_id, current_step, last_updated). Fields you omit
> are reset to None. If you want to preserve `current_item_id` across a
> phase change, pass it in every subsequent call.

So when the manager moves from `dispatching` → `polling` for the same item,
it must explicitly repeat `current_item_id` in the polling call:

```
update_rollout_state(
    rollout_id=...,
    phase="polling",
    current_item_id=item_id,             # repeat
    current_step="Waiting for build runs",
)
```

If `current_item_id` is omitted, observers (the UI, the lead's status page)
will see the manager "lose" its current item — confusing but recoverable.

The replacement contract is intentional: it avoids drift from partial
updates and makes each call a complete snapshot.

---

## `advance_rollout` contract

`advance_rollout` is the manager's source of truth for what to dispatch next.
It's a `GET /rollouts/{rollout_id}/advance` on the agent-gtd side; the dispatch
service calls it via `gtd_client.advance_rollout`.

Return shape:

```python
{
    "next_ready": [item_id, ...],   # unblocked, not yet dispatched, ready now
    "in_progress": [item_id, ...],  # dispatched, still running
    "blocked": [item_id, ...],      # blocked on incomplete dependencies (informational)
    "graph_complete": bool,          # all items in terminal status
}
```

Contract notes:

- `next_ready` is the **only** input the manager uses to pick what to
  dispatch. It never picks from `blocked` (the DAG enforces serialisation)
  or from `in_progress` (those are already running).
- `graph_complete=true` with `next_ready=[]` means the rollout is done.
  Closing the rollout itself is `complete_item_in_rollout`'s job on the
  last terminal item, not the manager's.
- Calling `advance_rollout` on a rollout that's already in a terminal state
  (`completed`, `halted`, `cancelled`) is an error — the manager must not
  call it after observing `graph_complete=true` from a
  `complete_item_in_rollout` response.

The manager treats `advance_rollout` as idempotent — calling it repeatedly
without state changes returns the same result. The 3-retries-with-backoff
policy handles transient agent-gtd 5xxs without amplifying them into halts.

---

## `complete_item_in_rollout` semantics

The manager's final per-item action is:

```
result = complete_item_in_rollout(
    rollout_id=...,
    item_id=item_id,
    outcome="completed",                  # or "halted", "skipped"
    merge_actor="manager-autonomous",
    decision_rule="agent-judgment",
)
```

The valid outcomes are enforced by the `RolloutItemOutcome` enum on the
API (`completed`, `halted`, `skipped`). There is no `failed` outcome —
build-agent failures that bubble up to the manager are surfaced via
`halt_rollout`, not `complete_item_in_rollout`.

On **`outcome="completed"`**, agent-gtd:

1. Cascades the item's GTD status to `done` by calling `complete_item`
   internally. The manager does NOT call `complete_item` separately.
2. Marks the rollout_items row terminal (status = `"completed"`).
3. Releases the per-item rollout lock.
4. Unblocks downstream items whose only remaining non-terminal
   predecessor was this item.
5. Checks whether all items in the rollout are now terminal. If so,
   closes the rollout (sets autonomous_rollouts.status to `completed`)
   and returns `graph_complete: true` in the response.

On **`outcome="halted"`** or **`outcome="skipped"`**:

1. The rollout_items row status is set to the literal outcome value
   (e.g. `"skipped"`).
2. The lock is released.
3. **The item's GTD status is NOT cascaded.** Only `completed` triggers
   `complete_item`. The underlying item stays in whatever GTD status it
   was in (typically `dispatched` or `review`); the lead is expected to
   reconcile it later.
4. Downstream unblocking still runs — `halted` and `skipped` are members
   of `_TERMINAL_STATUSES = {"completed", "halted", "skipped"}`, so
   successors whose only remaining predecessor was this item become
   `ready`. This is deliberate: a `skipped` item is "we're not going to
   do this, move on," and a per-item `halted` outcome is "this one
   failed, but the rest of the wave can still proceed if the manager
   chooses to continue" (in practice the manager almost always pairs
   this with a full `halt_rollout` call, which freezes everything).
5. Rollout closure still applies: if every item in the rollout is in
   `_TERMINAL_STATUSES`, the autonomous_rollouts row flips to
   `completed` and `graph_complete: true` is returned — note this means
   a rollout where every item was `skipped` still ends as
   `status="completed"`, not as `"skipped"`. The terminal rollout
   status is a property of the DAG, not the per-item outcomes.

`merge_actor` distinguishes who did the merge (manager-autonomous vs
manager-allowlist vs lead-manual). `decision_rule` captures the rule that
authorised the merge — for autonomous manager merges, this is
`"agent-judgment"`.

---

## Manage-mode allowed tools

The manage subprocess gets a restricted MCP tool allowlist defined in
`dispatch.py`:

```python
_MANAGE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__agent-gtd__advance_rollout",
    "mcp__agent-gtd__complete_item_in_rollout",
    "mcp__agent-gtd__halt_rollout",
    "mcp__agent-gtd__update_rollout_state",
    "mcp__agent-gtd__dispatch_item",        # dispatch child build runs
    "mcp__agent-gtd__add_comment",
    "mcp__agent-gtd__get_item",
    "mcp__agent-gtd__update_item",          # AC reconciliation
    "mcp__agent-gtd__list_items",
    "mcp__agent-gtd__get_run_status",
    "mcp__agent-gtd__list_runs",
    "mcp__agent-gtd__list_comments",        # read final agent comment
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
)
```

The allowlist is passed to the `claude` binary via `--allowedTools` (inserted
before `--print` in `run_agent`).

Notable absences:
- `add_item` — the manager can't create new items mid-rollout.
- `add_blocker` / `remove_blocker` — the DAG is frozen once the rollout
  starts and nothing can rewrite it in place. (`replan_rollout`, which
  could, was deleted with the LLM planner.)
- `complete_item` — the manager completes items through `complete_item_in_rollout`
  exclusively, so the rollout state stays consistent.

`Write`, `Edit`, and `Bash` are present because the manager performs inline
quality-gate fixes (formatting, single-line tweaks, coverage ratchet) and
runs `git` commands.

---

## Recovery semantics: retired with the resident manager

There is no resident manager to relaunch. The exit-path relaunch ladder
(`_maybe_relaunch_manage`), the shared `_do_manage_recovery`, the staleness
watchdog, the in-memory free-relaunch tallies, the `MAX_MANAGE_RETRIES` cap, the
`## Recovery Context` prompt block and the GTD-side
`POST /api/rollouts/{id}/relaunch-manage` route were all removed in item
7baf77d0. The `autonomous_rollouts.manage_retry_count` column and the
`manage_relaunched` event kind remain; the column is historical, and the event
kind is now written by a lead's resume (below).

What replaces them:

- **The loop owns liveness.** While builds are in flight a tick returns
  `_TICK_WAIT`; the only liveness halt ("rollout stalled") requires nothing in
  flight, so a running build is never stranded. A crashed loop fails its own
  run and does not halt the rollout.
- **The reviewer is bounded** by `REVIEW_TIMEOUT_SECONDS`.
- **Restarts** are covered by the startup re-adoption sweep, which resumes only
  rollouts this host holds a local MANAGE run row for. The watchdog it replaced
  scanned every running rollout fleet-wide; on rollout fe1e828d it started a
  second loop on a host that had never owned the rollout, and the two loops
  landed the same item.
- **A lead's resume relaunches the loop.** A loop that reads `halted` exits.
  `resume_rollout` (MCP tool / `agent-gtd resume-rollout` / `POST
  /api/rollouts/{id}/resume`) puts the rollout back to `running` and creates a
  new MANAGE run pinned to the rollout's owning host (its pinned host, else the
  host of its latest MANAGE run), emitting `manage_relaunched`. On a `running`
  rollout it only relaunches a missing loop (one that crashed). Pinning keeps
  every loop for a rollout on one host, so re-adoption after a restart cannot
  put a second loop elsewhere.
- **One loop per rollout per host.** `_start_rollout_loop` is the single,
  await-free choke point that registers a loop in `_rollout_loops` (before the
  task is created) and refuses a duplicate. `POST /dispatch` returns 409 for a
  MANAGE dispatch whose rollout already has a live loop or a queued MANAGE
  entry; the board records that run as `cancelled` and the live loop keeps
  driving. A loop deregisters synchronously when it decides to exit, so a
  relaunch that arrives just after it read `halted` starts a fresh loop.
- **Never background a wait.** The loop awaits each poll interval and each
  reviewer in its own task; every agent prompt carries the shared
  turn-discipline block.

---

## Terminal states: halt vs cancel vs complete

A rollout can reach a terminal state through three paths.

### `completed` (success)

- **Who calls it:** agent-gtd, automatically, when `complete_item_in_rollout`
  marks the last item terminal.
- **When:** in the wave loop, after the manager's final successful merge.
- **Side effects:** rollout closes; `graph_complete: true` returned to the
  manager; the manager EXITs.

### `halted` (caught failure)

- **Who calls it:** the manage agent (via `halt_rollout`) on any of:
  - `advance_rollout` failed 3 times.
  - A build agent ended `failed` / `timed_out` / `cancelled` — except a
    failed run whose error starts with `post-run gate` (branch pushed,
    only the project gate failed): the manager re-runs the project gate
    command on the branch in its quality-gate step, inline-fixes if small,
    and merges only once it exits 0.
  - Quality gates failed and an inline fix didn't work.
  - Diff touched a sensitive area (auth, deploy, infra) and the change
    looked non-trivial.
  - Commit-count guard fired (build branch was empty).
  - Coverage guardrail violation detected on the branch.
- **Side effects:** rollout status flips to `halted`; manager posts a
  comment on the offending item (or the project, if no specific item is
  at fault) with the halt reason.
- **Recovery:** the lead reads the halt reason, decides how to proceed
  (fix inline + advance the rollout, re-dispatch the offending item,
  cancel the rest of the wave).

### `cancelled` (human stop)

- **Who calls it:** the lead or a human via the agent-gtd UI.
- **When:** at any point while the rollout is `pending` or `running`.
- **Side effects:** rollout status flips to `cancelled`; in-flight build
  agents continue (cancellation is per-rollout, not per-run); the manager
  exits its wave loop on the next `advance_rollout` (or on
  `complete_item_in_rollout` rejection).
- **Recovery semantics:** no auto-relaunch.

### `crashed` — legacy, no longer set

**No current agent-gtd code path sets `status="crashed"` on a rollout**, and the dispatch side no longer has any notion of a "clean manager exit" (`_CLEAN_EXIT_STATUSES` went with the relaunch ladder). The state was originally set by a "wave reaper" background task that was removed in `feat/99eaab2d-remove-the-wave-reaper`. The cleanup migration `scripts/migrate_remove_reaper.sql` reclassifies any historical `crashed` rollouts to `cancelled` and deletes the reaper-emitted events:

```sql
UPDATE autonomous_wave_runs
SET status = 'cancelled',
    halt_reason = COALESCE(halt_reason, 'crashed (legacy)')
WHERE status = 'crashed';

DELETE FROM wave_events
WHERE actor = 'reaper' OR kind = 'wave_crashed';
```

Run this migration on any database that predates the reaper's removal, after which `crashed` is a state you will never observe in production data — dispatch no longer special-cases it at all.

---

## Branch lifecycle

For each item in a rollout, the branch goes through:

1. **Created by build agent.** The build subprocess clones the repo into
   its workspace (`prepare_workspace`) and checks out
   `feat/<item_id_short>-<title_slug>` (see
   `agent_gtd_dispatch_protocol.branches.make_branch_name`).
2. **Pushed to origin by build agent.** The build agent's prompt requires:
   - `git push` the branch.
   - Verify the remote ref advanced via `git ls-remote origin refs/heads/<branch>`.
   - Compare returned SHA against `git rev-parse HEAD`. If they don't
     match, post a failure comment and do NOT mark the item `review`.
3. **Squash-merged by manager.** The manager fetches the branch into its
   own workspace, runs gates, and on success runs `git merge --squash`
   followed by `git commit -F -` with the rollout/item trailer.
4. **Deleted from origin by manager.** `git push origin --delete <branch>`
   removes the remote ref.
5. **Deleted locally by manager.** `git branch -D <branch>` removes the
   local ref in the manage workspace.

The build agent's workspace is cleaned up by `cleanup_workspace` after the
build run completes (success or failure). The manage workspace is cleaned
up after the manage run completes, except on manage failure (where
`should_cleanup = False` preserves it for debugging — see
`_dispatch_worker`'s exception handlers).

---

## Cross-repo constraint

A rollout is scoped to one project, which is scoped to one git origin. The
manager's workspace is a clone of that single origin. **There is no
mechanism for a rollout to merge into multiple repos.**

If a feature genuinely spans projects (e.g. agent-gtd plus
agent-gtd-dispatch plus a CLI consumer), the lead must:

1. Decompose the work into per-project sub-tasks.
2. Create a rollout in each project.
3. Coordinate the rollouts manually or via parallel plan-mode dispatches
   that report back via comments.

The dispatch service has no notion of cross-project orchestration; that's
the lead's job in the interactive session.

---

## Operational glossary

| Term | Meaning |
|---|---|
| **Rollout** | A planned, executed wave of GTD items in one project. |
| **DAG** | The dependency graph the planner produces; nodes = items, edges = "must complete first". |
| **Wave** | A set of items that `advance_rollout` reports as `next_ready` at one point in time. Not a stored entity — derived from DAG + statuses. |
| **Manage agent** | The Claude Code subprocess running `mode="manage"` for one rollout. |
| **Child build agent** | A `mode="build"` subprocess the manage agent dispatched to implement one item. |
| **Quality gate** | The test + lint + manifest-drift + coverage check the manager runs on each feature branch before merging. |
| **Sensitive area** | A file path pattern (auth, deploy, infra, secrets, CI) that triggers a halt instead of an auto-merge. |
| **Commit-count guard** | A `git rev-list` check that fails the merge if the build agent pushed no commits. |
| **Replan** | Re-running the planner on a live rollout to rebuild the DAG over its remaining (`pending`/`ready`) items. See "Replanning a live rollout" below for the full picture. |

---

## Pointers into the code

| Thing you want to know | File and symbol |
|---|---|
| The full manage prompt | `src/agent_gtd_dispatch/dispatch.py::_build_manage_prompt` |
| Allowed MCP tools for manage | `src/agent_gtd_dispatch/dispatch.py::_MANAGE_ALLOWED_TOOLS` |
| `_dispatch_worker` lifecycle | `src/agent_gtd_dispatch/main.py::_dispatch_worker` |
| Manage timeout default | `src/agent_gtd_dispatch/config.py::MANAGE_TIMEOUT_SECONDS` (45 min) |
| DAG derivation (blockers + file overlap) | agent_gtd: `src/agent_gtd/services/rollout_dag.py::derive_edges` |
| File-overlap serialisation | agent_gtd: `src/agent_gtd/services/rollout_dag.py::compute_overlap_edges` |
| Acyclicity check | agent_gtd: `src/agent_gtd/services/rollout_dag.py::assert_acyclic` |
| `advance_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::advance_rollout` |
| `complete_item_in_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::complete_in_rollout` |
| `halt_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::halt_rollout` |
| Re-adoption sweep (restart resume) | `src/agent_gtd_dispatch/main.py::_readopt_running_rollouts` |
| One-loop-per-rollout choke point | `src/agent_gtd_dispatch/main.py::_start_rollout_loop` |
| Wire-contract models | `packages/protocol/src/agent_gtd_dispatch_protocol/models.py` |
| Branch-name helper | `packages/protocol/src/agent_gtd_dispatch_protocol/branches.py::make_branch_name` |
