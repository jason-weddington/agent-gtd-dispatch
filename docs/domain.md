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
| `engine_actual` | `str \| None` | Engine actually used; set on **every** run at creation (equals `engine` unless a plan/manage ollama swap or runtime ollama-health fallback occurred) |
| `agent_name` | `str \| None` | Agent identity passed via `--agent` flag |
| `mode` | `str` | `"build"`, `"plan"`, or `"manage"` |
| `rollout_id` | `str \| None` | Rollout this run belongs to (manage and rollout builds) |
| `workspace_path` | `str \| None` | Absolute path to the cloned workspace on disk |
| `push_results` | `list[RepoPushStatus] \| None` | Per-repo push verification results for build runs (persisted as JSON in the `runs` table); `None` for plan/manage runs |
| `status` | `RunStatus` | Current lifecycle state (see below) |
| `started_at` | `datetime \| None` | When the agent subprocess started |
| `completed_at` | `datetime \| None` | When the run reached a terminal state |
| `exit_code` | `int \| None` | Subprocess exit code (0 = success) |
| `error` | `str \| None` | Error message or last 500 bytes of transcript on failure |
| `created_at` | `datetime` | When the Run record was created in the DB |

---

### PushStatus / RepoPushStatus

`PushStatus` and `RepoPushStatus` (defined in `models.py`) describe the per-repo outcome
of build-mode push verification (`dispatch.verify_pushes()`):

| `PushStatus` value | Meaning |
|---|---|
| `pushed` | Local HEAD matches origin's SHA for the feature branch |
| `no_changes` | No commits beyond the base SHA captured before the agent started |
| `unpushed` | Local commits not on origin — or any git command failed (fail-closed) |

`RepoPushStatus` carries `repo_name`, `branch`, `status`, `local_sha`, `remote_sha`,
`commits_ahead`, and `dirty` (tracked-file modifications left in the working tree).

---

### RunStatus

`RunStatus` is an enum defined in `agent_gtd_dispatch_protocol.models`. Values:

| Value | Meaning |
|---|---|
| `pending` | Run created; waiting for a capacity slot (`_pending_queue`) |
| `running` | Agent subprocess is active |
| `succeeded` | Subprocess exited with code 0 (and, for build runs, push verification passed and — when the project has a non-empty `gate_command` and at least one repo was pushed — the post-run gate passed) |
| `failed` | Subprocess exited non-zero, an unhandled exception occurred, a build run exited 0 but push verification found unpushed commits, or a build run's post-run gate failed/timed out (error starts with `post-run gate`) |
| `timed_out` | Subprocess exceeded the timeout wall-clock limit |
| `cancelled` | Human called `POST /runs/{run_id}/cancel` |

**Status transitions:**

```
pending → running    (slot available, _dispatch_worker starts)
running → succeeded  (exit code 0; build runs additionally require push verification — and,
                       when gate_command is set and a repo was pushed, the post-run gate — to pass)
running → failed     (exit code != 0, exception, exit 0 + unpushed repo in build mode, or
                       exit 0 + push verified + post-run gate failed/timed out in build mode)
running → timed_out  (subprocess.TimeoutExpired)
running → cancelled  (asyncio.CancelledError from cancel endpoint)
pending → cancelled  (cancel called before slot was available)
```

**Note**: exit code 0 does **not** imply `succeeded` for build runs — push verification
can transition an exit-0 run to `failed` (with `push_results` populated and the workspace
preserved). When push verification passes, a build run whose project has a non-empty
`gate_command` and at least one pushed repo still has to clear the post-run gate
(`dispatch.run_gate_command()`) before it's `succeeded` — a gate failure or timeout also
transitions the run to `failed`, with an `error` that starts with `post-run gate` and the
branch left on origin for inspection.

Orphaned `pending`/`running` runs (from a service restart) are forced to `failed` by
`db.reconcile_orphans()` on startup.

---

## Dispatch Modes

Every run has a `mode` that controls the system prompt and workspace lifecycle.

**Repo modes** — orthogonal to dispatch mode, each project has a `repo_mode`:

- **Default (single-repo)**: `repo_mode` absent/`None`/unrecognized. The project's
  `git_origin` is cloned via `prepare_workspace()` (build/plan) or
  `prepare_manage_workspace()` (manage).
- **Workspace (multi-repo)**: `repo_mode == "workspace"` with a non-empty
  `workspace_repos` URL list. Build/plan use `prepare_workspace_multi()` — every repo is
  cloned into `ws-<run_id>/<dir>` and the **same feature branch** is created service-side
  in each repo (the shared-branch-name invariant). Manage uses
  `prepare_manage_workspace_multi()` — each repo shallow-cloned (`--depth=50`) on its
  default branch under `repos-<run_id>/<dir>`. All three mode prompts get a
  workspace-layout section listing the per-repo directories.

### `build` (default)

**Purpose**: Implement a GTD item on a feature branch and push for review.

- Creates a fresh clone + feature branch (`prepare_workspace()`, or
  `prepare_workspace_multi()` for workspace projects).
- System prompt instructs the agent to fetch the item, implement per acceptance criteria,
  run tests, commit, push, and set the item status to `review`.
- Timeout: `TIMEOUT_SECONDS` (default 30 minutes).
- **Push verification**: on exit 0, `verify_pushes()` checks every repo against the base
  SHA captured before the agent started. If any repo has unpushed commits the run becomes
  `failed`, `push_results` is recorded, a per-repo comment is posted to the item, and the
  workspace is **preserved** (the commits exist only in the clone). Only when all repos
  are `pushed` or `no_changes` is the run `succeeded` and the workspace cleaned up.
- **Gate install**: after clone and before launch, the worker installs and verifies each
  repo's hook manager (or runs its `.agent-gtd/setup`); failure → run `failed` before the
  agent starts
- **Post-run gate**: after push verification succeeds, when the project has a non-empty
  `gate_command` and at least one repo was pushed, the worker (for every non-talos engine)
  runs that command once from the workspace root, bounded by the run's remaining timeout
  (floored at `POST_RUN_GATE_MIN_SECONDS`). Exit 0 → the run proceeds to `succeeded` as
  above. A non-zero exit, a timeout, or a launch error → the run is `failed` (error starts
  with `post-run gate`) even though the branch is already on origin. Talos engines are
  exempt — they self-gate via their own `gate_command` run before minting the commit.

### `plan`

**Purpose**: Groom a GTD item — write acceptance criteria, identify files to modify,
define scope, and select a build engine — without writing any code.

- Creates a fresh clone + feature branch (`prepare_workspace()`, or
  `prepare_workspace_multi()` for workspace projects).
- System prompt instructs the agent to read the codebase, search the KB, write structured
  fields on the item (`acceptance_criteria`, `files_to_modify`, `scope_out`), and set
  item status to `ready`.
- Uses the same engine as build mode (with the same Ollama → Anthropic swap rule).
- Timeout: `TIMEOUT_SECONDS` (default 30 minutes).
- No push verification (plan agents don't push code).

### `manage`

**Purpose**: Orchestrate a rollout — dispatch build agents wave by wave, run quality gates,
squash-merge passing branches, and complete items in the rollout.

- Shallow-clones the default branch (`prepare_manage_workspace()`, or
  `prepare_manage_workspace_multi()` for workspace projects — one shallow clone per repo).
- System prompt is a detailed multi-step protocol for the manage agent executor. For
  workspace projects the prompt is workspace-aware: per-repo merge semantics and per-repo
  halt conditions.
- Only `claude-code` (Opus) is allowed; `claude-code-ollama` is swapped to `claude-code`.
- Timeout: `MANAGE_TIMEOUT_SECONDS` (default 45 minutes, sized for a short-lived reviewer).
- On failure: workspace is **preserved** (not cleaned up) for post-mortem debugging.
- No auto-relaunch: the worker drives the wave loop itself, so there is no resident
  manager to resurrect (see Rollout Liveness below).
- **Gate install**: after clone and before launch, the worker installs and verifies each
  repo's hook manager (or runs its `.agent-gtd/setup`); failure → run `failed` before the
  agent starts and the rollout is halted

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
| `claude-code` | `claude` | Moving alias `opus` (`--model opus`) via Anthropic | `CLAUDE_CODE_OAUTH_TOKEN` (subprocess auth; `ANTHROPIC_API_KEY` is service-side only, see kb-01512) |
| `claude-code-sonnet` | `claude` | Moving alias `sonnet` (`--model sonnet`) via Anthropic | same |
| `claude-code-haiku` | `claude` | Moving alias `haiku` (`--model haiku`) via Anthropic | same |
| `claude-code-ollama` | `claude` | Local Ollama endpoint (model from `OLLAMA_DEFAULT_MODEL`) | Injected via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| `kiro` | `kiro-cli` | Kiro cloud agent | `KIRO_API_KEY` |

**Note on `ANTHROPIC_API_KEY`**: It is deliberately **not** exposed to Claude Code
subprocesses. If it leaked, Claude Code would prefer pay-as-you-go API billing over the
user's Max subscription. It is read in-process only as the talos Anthropic engines'
credential (see `talos.talos_env_overlay`); the Claude Code subprocess only receives
`CLAUDE_CODE_OAUTH_TOKEN`.

### Engine Availability

`is_engine_available(engine)` reports whether an engine can be attempted on the host.
Claude Code engines (`claude-code`, `claude-code-sonnet`, `claude-code-haiku`) are
**always available** — the `claude` binary may be authenticated externally (enterprise/
managed distribution, internal wrapper, Bedrock-backed login), so the gate does not
require `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`; a truly-unauthenticated host
fails at exec time instead. Kiro and the Ollama-routed engine still gate on their
configured credentials. Availability is evaluated per request (not at startup): `GET /info`
returns only the currently-available engines in its `engines` list.

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
1. Agent GTD derives the dependency DAG itself, deterministically, from item blockers
   plus `files_to_modify` overlap (`agent_gtd.services.rollout_dag.derive_edges`). No
   LLM and no dispatch-service round-trip are involved — the `POST /plan` endpoint and
   its LLM planner were deleted.
2. The Agent GTD system creates a rollout record and dispatches a manage-mode run.
3. The manage agent calls `advance_rollout`, dispatches build runs wave by wave, runs
   quality gates, squash-merges branches, and calls `complete_item_in_rollout`.

See [docs/rollouts.md](rollouts.md) for the full rollout orchestration protocol.

---

## Rollout Liveness

A manage dispatch runs the worker-driven wave loop inside the dispatch service; a
short-lived REVIEW-mode agent reviews and merges each completed build. Nothing relaunches
a manager, because there is no resident manager. The relaunch ladder, the staleness
watchdog, their in-memory tallies and the `MAX_MANAGE_RETRIES` / free-relaunch caps were
retired in item 7baf77d0. The `autonomous_rollouts.manage_retry_count` column still exists
on the GTD side but nothing writes it.

- **Liveness:** the loop polls every `ROLLOUT_LOOP_POLL_SECONDS` while builds are in
  flight and halts as "stalled" only when nothing is in flight, so it never strands a
  running build.
- **Restart:** the startup re-adoption sweep resumes only rollouts this host holds a local
  MANAGE run row for.

**Orphan reconciliation** (on startup): any run stuck in `pending` or `running` at
startup is marked `failed`. A running rollout this host was driving is then resumed by the
re-adoption sweep.

---

## Attribution

When `POST /dispatch` includes `attribution: str`, the spawned subprocess receives `AGENT_GTD_AGENT_NAME=<attribution>`. The agent's agent-gtd MCP server is the board's HTTP `/mcp` endpoint at `<AGENT_GTD_URL>/mcp`, registered by `scripts/kb-mcp-refresh.sh` with an env-only bearer `headersHelper` (no key file, by design: per-run identity) that also forwards `AGENT_GTD_AGENT_NAME` as the `X-Agent-Name` header. The board uses that name (e.g. `claude-build-abc12345`) as the comment author only when the bearer is not a run token; a run's callback token already identifies the run, and the board derives `created_by` from it server-side.

This prevents the service account from appearing as the author of agent-written comments
and enables per-agent audit trails in the GTD item comment history.
