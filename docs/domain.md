# Agent GTD Dispatch — Domain Model

## What This Service Does

Agent GTD Dispatch is the execution layer between the Agent GTD task-management system
and the AI coding agents that do the actual work. When a human (or the GTD manage agent)
decides a task is ready to implement, it calls `POST /dispatch`. The dispatch service:

1. Validates the request and fetches the item + project from the GTD API.
2. Clones the project repo into an isolated workspace.
3. Builds a system prompt tailored to the dispatch mode.
4. Launches the agent CLI as a subprocess.
5. Streams the agent's output to `transcript.txt`.
6. Updates run status in SQLite and posts comments to the GTD item.

---

## Core Entities

### Run

A **Run** is a single agent invocation. It is the atomic unit of work in the dispatch
service. Every `POST /dispatch` call creates exactly one Run record.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | 12-hex-char UUID fragment (e.g. `"a4f7c3b21e09"`) |
| `item_id` | `str \| None` | GTD item being implemented (`None` for manage-mode runs) |
| `project_name` | `str` | Human-readable project name (from GTD) |
| `branch_name` | `str \| None` | Feature branch for build/plan mode; `None` for manage mode |
| `engine` | `str` | Engine requested by the caller (may differ from `engine_actual`) |
| `engine_actual` | `str \| None` | Engine actually used (set when an engine swap occurs) |
| `agent_name` | `str \| None` | Agent identity passed via `--agent` flag |
| `mode` | `str` | `"build"`, `"plan"`, or `"manage"` |
| `rollout_id` | `str \| None` | Rollout this run belongs to (manage and rollout builds) |
| `workspace_path` | `str \| None` | Absolute path to the cloned workspace on disk |
| `status` | `RunStatus` | Current lifecycle state (see below) |
| `started_at` | `datetime \| None` | When the agent subprocess started |
| `completed_at` | `datetime \| None` | When the run reached a terminal state |
| `exit_code` | `int \| None` | Subprocess exit code (0 = success) |
| `error` | `str \| None` | Error message or last 500 bytes of transcript on failure |
| `created_at` | `datetime` | When the Run record was created in the DB |

---

### RunStatus

`RunStatus` is an enum defined in `agent_gtd_dispatch_protocol.models`. Values:

| Value | Meaning |
|---|---|
| `pending` | Run created; waiting for a capacity slot (`_pending_queue`) |
| `running` | Agent subprocess is active |
| `succeeded` | Subprocess exited with code 0 |
| `failed` | Subprocess exited non-zero, or an unhandled exception occurred |
| `timed_out` | Subprocess exceeded the timeout wall-clock limit |
| `cancelled` | Human called `POST /runs/{run_id}/cancel` |

**Status transitions:**

```
pending → running    (slot available, _dispatch_worker starts)
running → succeeded  (exit code 0)
running → failed     (exit code != 0, or exception)
running → timed_out  (subprocess.TimeoutExpired)
running → cancelled  (asyncio.CancelledError from cancel endpoint)
pending → cancelled  (cancel called before slot was available)
```

Orphaned `pending`/`running` runs (from a service restart) are forced to `failed` by
`db.reconcile_orphans()` on startup.

---

## Dispatch Modes

Every run has a `mode` that controls the system prompt and workspace lifecycle.

### `build` (default)

**Purpose**: Implement a GTD item on a feature branch and push for review.

- Creates a fresh clone + feature branch (`prepare_workspace()`).
- System prompt instructs the agent to fetch the item, implement per acceptance criteria,
  run tests, commit, push, and set the item status to `review`.
- Timeout: `TIMEOUT_SECONDS` (default 30 minutes).
- On completion: workspace is cleaned up.

### `plan`

**Purpose**: Groom a GTD item — write acceptance criteria, identify files to modify,
define scope, and select a build engine — without writing any code.

- Creates a fresh clone + feature branch (`prepare_workspace()`).
- System prompt instructs the agent to read the codebase, search the KB, write structured
  fields on the item (`acceptance_criteria`, `files_to_modify`, `scope_out`), and set
  item status to `ready`.
- Uses the same engine as build mode (with the same Ollama → Anthropic swap rule).
- Timeout: `TIMEOUT_SECONDS` (default 30 minutes).

### `manage`

**Purpose**: Orchestrate a rollout — dispatch build agents wave by wave, run quality gates,
squash-merge passing branches, and complete items in the rollout.

- Shallow-clones the default branch (`prepare_manage_workspace()`).
- System prompt is a detailed multi-step protocol for the manage agent executor.
- Only `claude-code` (Opus) is allowed; `claude-code-ollama` is swapped to `claude-code`.
- Timeout: `MANAGE_TIMEOUT_SECONDS` (default 4 hours).
- On failure: workspace is **preserved** (not cleaned up) for post-mortem debugging.
- Auto-recovery: if the manage agent exits before the rollout reaches a terminal state,
  the service relaunches it (see Manage Recovery below).

---

## Engines

An **Engine** is a headless AI coding agent backend. Each engine has:
- A `name` (used in API requests and DB records).
- A `binary` (the CLI command to invoke).
- Auth credential keys to expose to the subprocess.
- A `build_command()` factory that produces the CLI argument list.

### Registered Engines

| Name | Binary | Model / Route | Auth |
|---|---|---|---|
| `claude-code` | `claude` | Default Claude (Opus) via Anthropic | `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` |
| `claude-code-sonnet` | `claude` | `claude-sonnet-4-6` via Anthropic | same |
| `claude-code-haiku` | `claude` | `claude-haiku-4-5-20251001` via Anthropic | same |
| `claude-code-ollama` | `claude` | Local Ollama endpoint (model from `OLLAMA_DEFAULT_MODEL`) | Injected via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| `kiro` | `kiro-cli` | Kiro cloud agent | `KIRO_API_KEY` |

**Note on `ANTHROPIC_API_KEY`**: It is deliberately **not** exposed to Claude Code
subprocesses. If it leaked, Claude Code would prefer pay-as-you-go API billing over the
user's Max subscription. The planner (`rollout_planner.py`) reads it in-process; the
subprocess only receives `CLAUDE_CODE_OAUTH_TOKEN`.

### Engine Availability

`is_engine_available(engine)` checks whether the required credential is present in the
environment. Only available engines are returned by `GET /info` and `GET /agents`.

### Engine Swap

If a caller requests `claude-code-ollama` for `mode=plan` or `mode=manage`, the service
silently swaps it to `claude-code`. The `RunResponse` includes an `engine_swap` field:

```json
{
  "engine_swap": {
    "from_engine": "claude-code-ollama",
    "to_engine": "claude-code",
    "reason": "plan/manage mode does not support ollama"
  }
}
```

---

## Rollouts

A **Rollout** is a wave-ordered execution plan for a set of related GTD items. It is
managed by the Agent GTD system but dispatched via this service.

The rollout lifecycle is:
1. A plan agent calls `POST /plan` with a list of item IDs.
2. The dispatch service runs `rollout_planner.plan_rollout()` (an LLM call) to produce a
   dependency DAG (`RolloutPlan` with `DagEdge` entries).
3. The Agent GTD system creates a rollout record and dispatches a manage-mode run.
4. The manage agent calls `advance_rollout`, dispatches build runs wave by wave, runs
   quality gates, squash-merges branches, and calls `complete_item_in_rollout`.

See [docs/rollouts.md](rollouts.md) for the full rollout orchestration protocol.

---

## Manage Recovery Semantics

Manage-mode agents can time out or crash before a rollout reaches a terminal state. The
dispatch service detects this and automatically relaunches:

| Parameter | Default | Config var |
|---|---|---|
| Max auto-relaunches | 2 | `MAX_MANAGE_RETRIES` (`DISPATCH_MAX_MANAGE_RETRIES` env) |
| Backoff between relaunches | 30 seconds | `MANAGE_RETRY_BACKOFF_SECONDS` (hardcoded) |

**Relaunch flow** (in `_maybe_relaunch_manage()`):

1. Fetch the rollout from the GTD API.
2. If the rollout is already in `completed`, `halted`, or `cancelled`: nothing to do.
3. Call `relaunch_manage_rollout()` to atomically increment `manage_retry_count`.
4. If `manage_retry_count > MAX_MANAGE_RETRIES`: call `halt_rollout()` with reason
   `"manage_relaunch_cap_exceeded"` and stop.
5. Otherwise: sleep 30 s, create a new Run record, spawn a new `_dispatch_worker` with
   `manage_retry_count` injected into the system prompt as a recovery warning.

**Orphan reconciliation** (on startup): any run stuck in `pending` or `running` at
startup is marked `failed`. For manage runs, this triggers the relaunch check the next time
the rollout is advanced (the manage agent's caller handles that).

---

## Attribution

When `POST /dispatch` includes `attribution: str`, the spawned subprocess receives
`AGENT_GTD_AGENT_NAME=<attribution>`. The agent uses this as its identity when posting GTD
comments — comments appear under the attribution name (e.g. `claude-build-abc12345`) rather
than the dispatch service's default (`agent-gtd-dispatch`).

This prevents the service account from appearing as the author of agent-written comments
and enables per-agent audit trails in the GTD item comment history.
