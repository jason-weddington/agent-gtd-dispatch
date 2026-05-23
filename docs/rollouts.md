# Rollouts and the manage mode

This document is the reference for the rollout/manager subsystem in
`agent-gtd-dispatch`: what a rollout is, how its DAG is built, how a manage-mode
agent drives the wave loop end-to-end, the state and recovery contracts, and the
guardrails that protect `main` while the lead is asleep.

The canonical source code lives in:

- `src/agent_gtd_dispatch/rollout_planner.py` — DAG construction (Sonnet call).
- `src/agent_gtd_dispatch/dispatch.py` — prompt builders, including
  `_build_manage_prompt` and `_MANAGE_ALLOWED_TOOLS`.
- `src/agent_gtd_dispatch/main.py` — `/dispatch` and `/plan` endpoints, the
  `_dispatch_worker` background coroutine, and `_maybe_relaunch_manage` (the
  manage-mode auto-recovery path).
- `src/agent_gtd_dispatch/gtd_client.py` — the HTTP calls into agent-gtd that
  back rollout operations (`advance_rollout`, `complete_in_rollout`,
  `halt_rollout`, `relaunch_manage_rollout`).

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
- The lead calls `dispatch_item(mode="manage", rollout_id=...)` once to start
  the manager.
- The **manager** dispatches each child build agent, runs quality gates,
  squash-merges to `main`, and advances the rollout — with no lead involvement
  per item.
- The lead only intervenes if the rollout halts.

Default to rollouts whenever you have two or more items in the same project
that benefit from a shared narrator and uniform merge cadence. Single
dispatches are for one-off items or cross-project pairs that can't share a
rollout.

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
  `agent_gtd_dispatch_protocol.branches.make_branch_name`).
- **Job:** implement the item per its acceptance criteria, run tests, push the
  feature branch, post a final comment, set item status to `review`.
- **Output:** a pushed feature branch waiting for review/merge. The build
  agent **does not merge to main** — that's either the lead's job (singleton
  dispatch) or the manager's job (rollout child).

### `mode="manage"` — driving a rollout

- **Input:** one `rollout_id` (the `item_id` field on the request is unused;
  see "Launch item_id — Ignore It" below).
- **Workspace:** shallow clone (`--depth=50`) of the project's auto-detected
  default branch — **not** a feature branch. The manager merges to this
  branch.
- **Job:** drive the rollout from start to terminal state — dispatch each
  child build agent, poll them to completion, reconcile AC, run quality
  gates, squash-merge clean builds, delete branches, advance the rollout.
- **Output:** a sequence of squash-merge commits on `main` (one per item) and
  a closed rollout (`completed`, `halted`, or — in failure modes — left
  `running` for auto-recovery).

Manage-mode dispatches always use `claude-code` (Opus). If `claude-code-ollama`
is requested, the same engine swap as plan-mode applies. The default timeout
is `MANAGE_TIMEOUT_SECONDS` (4 hours) versus `TIMEOUT_SECONDS` (30 min) for
build/plan.

Manage-mode subprocesses get a restricted MCP tool allowlist (the
`_MANAGE_ALLOWED_TOOLS` tuple in `dispatch.py`) plus `Bash`, `Read`, `Write`,
`Edit`, `Glob`, `Grep`. The build tools used by plan/build mode (e.g.
`add_item`, `add_blocker`, `add_note`) are deliberately *not* in the allowlist
— the manager can read, dispatch, comment, and update rollout state, but it
cannot reshape the backlog.

---

## How a rollout flows

```
lead                       /plan endpoint                manage agent
 │                                │                            │
 │ plan_rollout(item_ids=[...])   │                            │
 ├──────────────────────────────▶│                            │
 │                                │  fetch items concurrently  │
 │                                │  call Sonnet planner       │
 │                                │  return RolloutPlan        │
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

The `/plan` endpoint (`POST /plan`, body `PlanRequest{item_ids: [...]}`)
returns a `RolloutPlan`:

```python
class RolloutPlan(BaseModel):
    nodes: list[str]
    edges: list[DagEdge]
    planner_model: str

class DagEdge(BaseModel):
    from_item_id: str
    to_item_id: str   # to_item_id must wait for from_item_id
```

The handler is a thin wrapper:

```python
@app.post("/plan", response_model=RolloutPlan)
async def plan_rollout_endpoint(body: PlanRequest, ...):
    return await rollout_planner.plan_rollout(body.item_ids)
```

Errors from the planner are wrapped as a 502 with `detail`, `planner_model`,
and `item_count` so the caller can distinguish them from auth failures.

### Inputs the planner sees

For every item in `item_ids`, `rollout_planner.plan_rollout` concurrently
fetches the full item dict from agent-gtd (`gtd_client.get_item`) and reads:

| Field | Source of edge signal |
|---|---|
| `blockers` (list of item IDs) | Explicit dependency: each blocker becomes an edge from blocker → this item. The planner is told to honour these. |
| `files_to_modify` (list of `{path, change}`) | Structural overlap. If two items both modify `src/foo.py`, that's a candidate edge — the planner serialises them. |
| `acceptance_criteria` (list of strings) | Context for the planner to judge whether two items really conflict or just touch nearby code. |
| `description` (free text) | Context only — the validator on the agent-gtd side reads structured fields, not description prose. |
| `title` | Header in the planner prompt. |

The full prompt template lives in `_build_context` in `rollout_planner.py`:

```python
lines: list[str] = [
    "You are a planning assistant. Given a list of work items, identify "
    "dependency edges between them.\n"
    "An edge {from_item_id: A, to_item_id: B} means B must wait for A to "
    "complete.\n"
    "Derive edges from declared blockers and shared file paths in the "
    "structured `files_to_modify` field. Same file path across two items "
    "= candidate edge.\n"
    "Only reference item_ids from the provided list.\n",
]
```

Each item is rendered as:

```
## Item <id>: <title>
Blockers (must complete first): <comma-separated ids, or "none">
Files to modify: <comma-separated paths, or "none">
Acceptance criteria:
<one AC per line, or "none">
Description:
<description>
---
```

### The planner call

```python
client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
response = await client.messages.create(
    model=config.PLANNER_MODEL,         # default: claude-sonnet-4-6
    max_tokens=1024,
    tools=[PRODUCE_DAG_TOOL],
    tool_choice={"type": "tool", "name": "produce_dag"},
    messages=[{"role": "user", "content": context}],
)
```

The planner is forced to call a single tool, `produce_dag`, with a list of
`edges`. The tool schema constrains the output to objects of shape
`{from_item_id, to_item_id}` — no node list, no commentary, no metadata. Both
endpoints are required.

The Sonnet model is used (not Opus) because:

- The planning task is structured and well-bounded — Sonnet has the
  reasoning headroom for it.
- The latency hit of Opus on every `plan_rollout` call would compound across
  re-plans.
- Cost matters more here than for build/manage, since this call is the
  one part of the rollout that doesn't pay for itself in implementation
  output.

`PLANNER_MODEL` is configurable via `DISPATCH_PLANNER_MODEL`; in production
it's `claude-sonnet-4-6`.

### Edge validation

`_extract_edges` filters the tool input through `valid_ids`:

```python
def _extract_edges(tool_input, valid_ids):
    raw_edges = tool_input.get("edges", [])
    ...
    for raw in raw_edges:
        ...
        if from_id in valid_ids and to_id in valid_ids:
            edges.append(DagEdge(from_item_id=str(from_id), to_item_id=str(to_id)))
    return edges
```

This is the defence against the planner hallucinating item IDs that aren't in
the rollout. Any edge that references an unknown ID is silently dropped — the
DAG never gains a phantom node.

The planner returns nodes only as the original `item_ids` list (no extra
nodes are introduced) and edges that survived validation.

### What the planner does NOT do

- It does not assign **waves** explicitly. The DAG is the wave plan; the
  agent-gtd side's `advance_rollout` derives "what's ready now" from the DAG
  + item statuses at each call.
- It does not pick engines. Engine selection is per-item, done by plan-mode
  dispatches (see the Engine-Selection Rubric in `_build_plan_prompt`).
- It does not deduplicate edges or break cycles. **Neither does the
  agent-gtd side** — `rollout_service.plan_rollout` persists the edges
  list as JSON in the `rollout_plans.edges` column with no schema-level
  acyclicity constraint and no application-level cycle check. The
  legality contract (`validate_legality_contract`) only checks per-item
  fields and project scoping; it never inspects the planner's edge
  output.

  In practice the planner is well-behaved enough that this hasn't
  bitten us, but if a cycle did land in `rollout_plans.edges`, the
  downstream effect would be silent: `advance_rollout` does the readiness
  check by asking "are all of my predecessors terminal?" — for cyclic
  items, the answer is "no, forever," so they'd stay in `blocked` and
  the rollout would never reach `graph_complete=true`. The manager
  would loop on `advance_rollout` returning the same `in_progress` /
  `blocked` sets and eventually halt itself on a 3x advance failure or
  a manage timeout. No corruption, but a wasted manage budget. If we
  ever observe this in the wild, the fix is a cycle check in
  `_extract_edges` on the dispatch side.

---

## The manager's lifecycle

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
   - Install pre-commit hooks if `.pre-commit-config.yaml` exists.
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
a halt candidate; see the **Halt path** below.

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

## Replanning a live rollout: `replan_rollout`

`replan_rollout` is a deliberately-narrow tool that rebuilds the DAG
**over the rollout's remaining items** (those still in `pending` or
`ready`) without disturbing items that have already reached a terminal
status. It exists for the case where the lead has learned something
mid-rollout that invalidates the original plan — a new dependency
discovered while reviewing a halted item, a decision to drop an item
that subsequent items don't actually need, an AC change that introduces
a new file overlap.

### What it does

`rollout_service.replan_rollout` (in agent-gtd) requires
`wave.status == "running"`. Given an optional `from_item` parameter:

- **Without `from_item`:** rescans every remaining `pending`/`ready`
  item, calls the dispatch-side planner with that subset of item IDs,
  persists a new `rollout_plans` row at `version = old_version + 1`,
  and re-derives readiness from the new edges.
- **With `from_item`:** restricts the replan to that item's downstream
  subgraph (DFS over the current edge map) before invoking the planner.
  Useful for surgical changes that shouldn't reshuffle the whole tail
  of the wave.

After persisting the new plan, the function walks the remaining items:
items whose new predecessors are all terminal flip from `pending` to
`ready`, and items whose new plan re-blocks them (i.e. they were
`ready` but the new DAG introduces a new predecessor) revert from
`ready` to `pending`. A `wave_replanned` event is appended with
`old_version` and `new_version` in the payload.

Terminal items (`completed`, `halted`, `skipped`) are not touched. The
new plan can reference them as predecessors of remaining items (they're
already terminal, so they don't block anything), but their statuses
won't change.

### Who calls it — lead-only in practice

`replan_rollout` is in `_MANAGE_ALLOWED_TOOLS` and listed under "MCP
Tools Available" in the manage prompt, but **the manage prompt body
never instructs the manager to call it.** There is no condition in the
wave loop, AC reconciliation, halt path, or sensitive-area guidance
that suggests a replan. The manager's options when something looks
wrong are: inline fix and merge, or halt. Replan is not in the
manager's playbook.

In practice replans are lead-initiated, typically via the MCP tool
directly:

```
mcp__agent-gtd__replan_rollout(rollout_id=..., from_item=...)
```

The two cases where it gets used:

1. **After a halt, before re-dispatching the manager.** The lead
   removes or restructures items, then calls `replan_rollout` to
   rebuild the DAG over what's left, and finally re-dispatches manage
   mode (or completes the remaining items inline).
2. **Mid-wave course corrections.** The lead notices that two
   in-flight items will collide on a file the planner didn't see, or
   that an upcoming item is now redundant. Calling `replan_rollout`
   updates the readiness map without halting the rollout — the running
   manager will see the new DAG on its next `advance_rollout` call.

The tool stays in the manage allowlist because removing it would
require a manage-prompt change with no benefit; leaving it is harmless
since the manager is never told to call it. If a future workflow
genuinely needs the manager to replan (e.g. an AC-reconciliation path
that promotes a "soft block" into a hard dependency), the prompt body
is the place to add that instruction — the tool surface is already
ready.

---

## Manage-mode allowed tools

The manage subprocess gets a restricted MCP tool allowlist defined in
`dispatch.py`:

```python
_MANAGE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__agent-gtd__advance_rollout",
    "mcp__agent-gtd__complete_item_in_rollout",
    "mcp__agent-gtd__halt_rollout",
    "mcp__agent-gtd__replan_rollout",
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
  starts; only `replan_rollout` can rewrite it.
- `complete_item` — the manager completes items through `complete_item_in_rollout`
  exclusively, so the rollout state stays consistent.

`Write`, `Edit`, and `Bash` are present because the manager performs inline
quality-gate fixes (formatting, single-line tweaks, coverage ratchet) and
runs `git` commands.

---

## Recovery semantics: when a manage agent exits unexpectedly

Manage runs are subject to:
- Wall-clock timeouts (`MANAGE_TIMEOUT_SECONDS`, default 4h).
- Subprocess crashes.
- Network failures during a long MCP call.
- Human cancellation.

The dispatch worker handles all of these via the
`_maybe_relaunch_manage` flow in `main.py`. Called from `_dispatch_worker`'s
`finally` block on **any non-cancellation exit**:

```python
if run.mode == "manage" and run.rollout_id and not _human_cancelled:
    await _maybe_relaunch_manage(
        run, max_turns, engine_used, timeout_seconds, attribution
    )
```

### The recovery decision

1. Fetch the current rollout state via `get_rollout(rollout_id)`.
2. If status is in `_CLEAN_EXIT_STATUSES = {"completed", "halted", "cancelled", "crashed"}`:
   the rollout reached a terminal state on its own. **Do nothing.**
3. Otherwise the manage agent exited unexpectedly while the rollout was
   still `running` or `pending`. Call
   `gtd_client.relaunch_manage_rollout(rollout_id)` which atomically
   increments `manage_retry_count` on the agent-gtd side and returns the
   updated rollout.
4. If the new `manage_retry_count > MAX_MANAGE_RETRIES` (config default: 2):
   halt the rollout with reason `"manage_relaunch_cap_exceeded"` and stop.
5. Otherwise sleep `MANAGE_RETRY_BACKOFF_SECONDS` (30s) and spawn a fresh
   `_dispatch_worker` for a new manage run, passing `manage_retry_count`
   into the new prompt.

### Recovery prompt prelude

When `manage_retry_count > 0`, `_build_manage_prompt` prepends a recovery
block to the system prompt:

```python
recovery_block = textwrap.dedent(
    f"""\
    ## ⚠️ Recovery Context

    You are a *recovery* manage agent — a previous manager for this rollout
    exited unexpectedly (retry attempt {manage_retry_count} of
    {config.MAX_MANAGE_RETRIES}). The rollout is already in `running`
    state. Read its current state via `advance_rollout` and continue
    normally. Items already terminal may have unmerged work waiting;
    process those first before dispatching new ones.
    """
)
```

The recovery agent has the same prompt body, the same allowed tools, and the
same workspace setup as a fresh manager — but it knows to read state from
agent-gtd rather than starting from scratch. Critically, no in-memory state
from the previous manager is carried over: the recovery is **rebuilt
entirely from rollout state in agent-gtd**.

This means a recovery agent might re-run a quality gate on a branch that
the previous manager had already gated and merged. The commit-count guard
handles this correctly: a branch that was already merged + deleted will fail
the guard (or fail `git checkout`), and the manager will treat it as an
inline-fix or halt candidate. The cost of an occasional duplicate gate is
much smaller than the cost of dropped work.

### `manage_retry_count` cap

The default `MAX_MANAGE_RETRIES = 2` means a rollout gets up to **three**
manage attempts total (the initial dispatch plus two automatic retries).
Beyond that, the rollout halts and the lead must intervene — typically by
finishing the remaining items inline or grooming the rollout into a
follow-up wave.

The cap is configurable via the `MAX_MANAGE_RETRIES` env knob; it's
re-exported from `main.py` for the test suite.

### Human cancellation bypasses recovery

The `_dispatch_worker`'s `except asyncio.CancelledError` branch sets
`_human_cancelled = True`. The `finally` block checks this before calling
`_maybe_relaunch_manage`:

```python
if run.mode == "manage" and run.rollout_id and not _human_cancelled:
    await _maybe_relaunch_manage(...)
```

So a `POST /runs/{run_id}/cancel` on a manage run will NOT trigger a
relaunch — the lead has explicitly said "stop". The rollout itself isn't
automatically halted by the cancel (the rollout's status is the lead's
business at that point); only the run is marked `cancelled`.

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
  - A build agent ended `failed` / `timed_out` / `cancelled`.
  - Quality gates failed and an inline fix didn't work.
  - Diff touched a sensitive area (auth, deploy, infra) and the change
    looked non-trivial.
  - Commit-count guard fired (build branch was empty).
  - Coverage guardrail violation detected on the branch.
  - `_maybe_relaunch_manage` exhausted `MAX_MANAGE_RETRIES`.
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

`crashed` appears in the dispatch-side `_CLEAN_EXIT_STATUSES` tuple but
**no current agent-gtd code path sets `status="crashed"` on a rollout.**
The state was originally set by a "wave reaper" background task that
was removed in `feat/99eaab2d-remove-the-wave-reaper`. The cleanup
migration `scripts/migrate_remove_reaper.sql` reclassifies any
historical `crashed` rollouts to `cancelled` and deletes the
reaper-emitted events:

```sql
UPDATE autonomous_wave_runs
SET status = 'cancelled',
    halt_reason = COALESCE(halt_reason, 'crashed (legacy)')
WHERE status = 'crashed';

DELETE FROM wave_events
WHERE actor = 'reaper' OR kind = 'wave_crashed';
```

The dispatch side retains `crashed` in `_CLEAN_EXIT_STATUSES` as a
defensive belt-and-braces — if a future agent-gtd version brings back
some flavour of automatic crash detection, the dispatch side will
already treat it as a clean exit and won't try to relaunch. For now,
treat `crashed` as a state you will never observe in production data.

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
| **Recovery agent** | A new manage subprocess spawned by `_maybe_relaunch_manage` after an unexpected previous exit. Sees `manage_retry_count > 0` in its prompt. |
| **Replan** | Re-running the planner on a live rollout to rebuild the DAG over its remaining (`pending`/`ready`) items. See "Replanning a live rollout" below for the full picture. |

---

## Pointers into the code

| Thing you want to know | File and symbol |
|---|---|
| The full manage prompt | `src/agent_gtd_dispatch/dispatch.py::_build_manage_prompt` |
| The recovery prelude | `src/agent_gtd_dispatch/dispatch.py::_build_manage_prompt`, conditional on `manage_retry_count > 0` |
| Allowed MCP tools for manage | `src/agent_gtd_dispatch/dispatch.py::_MANAGE_ALLOWED_TOOLS` |
| `_dispatch_worker` lifecycle | `src/agent_gtd_dispatch/main.py::_dispatch_worker` |
| Auto-recovery | `src/agent_gtd_dispatch/main.py::_maybe_relaunch_manage` |
| Manage timeout default | `src/agent_gtd_dispatch/config.py::MANAGE_TIMEOUT_SECONDS` (4h) |
| Retry cap | `src/agent_gtd_dispatch/config.py::MAX_MANAGE_RETRIES` (default 2) |
| Planner LLM call | `src/agent_gtd_dispatch/rollout_planner.py::plan_rollout` |
| Planner prompt template | `src/agent_gtd_dispatch/rollout_planner.py::_build_context` |
| Planner edge validation | `src/agent_gtd_dispatch/rollout_planner.py::_extract_edges` |
| Planner model | `src/agent_gtd_dispatch/config.py::PLANNER_MODEL` (`claude-sonnet-4-6`) |
| `advance_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::advance_rollout` |
| `complete_item_in_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::complete_in_rollout` |
| `halt_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::halt_rollout` |
| `relaunch_manage_rollout` HTTP call | `src/agent_gtd_dispatch/gtd_client.py::relaunch_manage_rollout` |
| Wire-contract models | `packages/protocol/src/agent_gtd_dispatch_protocol/models.py` |
| Branch-name helper | `packages/protocol/src/agent_gtd_dispatch_protocol/branches.py::make_branch_name` |
