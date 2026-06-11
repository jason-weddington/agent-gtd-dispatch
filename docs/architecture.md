# Agent GTD Dispatch — Architecture

## Overview

Agent GTD Dispatch is a FastAPI service that runs headless AI coding agents (Claude Code,
Kiro, Ollama-backed Claude) on isolated infrastructure. It receives dispatch requests from
the Agent GTD system, clones the target repository into a workspace, invokes the agent CLI
as a subprocess, streams its transcript to disk, and reports status back via the GTD API.

```
┌─────────────────────┐        ┌──────────────────────────┐
│   Agent GTD (caller) │ ──────▶│  POST /dispatch           │
└─────────────────────┘        │  Agent GTD Dispatch API   │
                                │  (FastAPI + uvicorn)      │
                                └──────────┬───────────────┘
                                           │
                        ┌──────────────────▼───────────────────┐
                        │  asyncio event loop                   │
                        │  _dispatch_worker (Task per run)      │
                        │  _active_processes: dict[id, Task]    │
                        │  _pending_queue: list[_PendingDispatch]│
                        └──────────────────┬───────────────────┘
                                           │ run_in_executor
                                           ▼
                        ┌──────────────────────────────────────┐
                        │  ThreadPoolExecutor (blocking I/O)    │
                        │  subprocess.Popen(claude …)           │
                        │  stdout → transcript.txt              │
                        └──────────────────────────────────────┘
```

---

## Process Model

### uvicorn Entrypoint

The service is started by uvicorn (see `dispatch-api.service` systemd unit):

```bash
uv run uvicorn agent_gtd_dispatch.main:app --host 0.0.0.0 --port 8100
```

At startup the `lifespan` async context manager:
1. Calls `config.load()` to populate module-level globals from the environment.
2. Optionally runs `_check_service_repo()` to guard against deploying a dirty working copy.
3. Calls `dispatch.init_executor()` to size the `ThreadPoolExecutor` to `MAX_CONCURRENT_RUNS`.
4. Calls `db.init_db()` to create (or migrate) the `dispatch.db` SQLite database.
5. Calls `db.reconcile_orphans()` to mark any `pending`/`running` runs left over from a
   prior service crash as `failed`.

### asyncio Task Pool

Each dispatch run becomes an `asyncio.Task` stored in `_active_processes[run_id]`.
The capacity limit (`MAX_CONCURRENT_RUNS`, default 32) is enforced at `POST /dispatch`
with an **atomic capacity check** (see the Burst-Pending Race section in `docs/codebase.md`):

```
if len(_active_processes) >= config.MAX_CONCURRENT_RUNS:
    _pending_queue.append(...)  # queue for later
    return RunResponse(...)     # 200 immediately — run is pending
task = asyncio.create_task(_dispatch_worker(...))
_active_processes[run_id] = task
```

When a running task finishes (in its `finally` block), it calls `_try_start_pending()` to
promote the oldest queued item to a running task without any intervening `await`.

### ThreadPoolExecutor for Subprocess Isolation

Agent CLI processes are blocking (they run until the agent finishes). They must not block
the asyncio event loop. `dispatch.run_agent()` offloads the blocking `subprocess.Popen` +
`proc.wait()` call to a `ThreadPoolExecutor` thread:

```python
return await loop.run_in_executor(_executor, _stream)
```

The executor is sized to `MAX_CONCURRENT_RUNS` so threads never queue behind each other.

---

## Workspace Clone Lifecycle

### Build / Plan Mode — `prepare_workspace()`

Called for `mode=build` and `mode=plan`. Creates a fresh clone on a feature branch:

```
git clone <git_origin> <WORKSPACE_ROOT>/<repo-name>-<run_id>
git checkout -b <branch_name>
```

- Branch name is derived from `item_id + item_title` by the protocol library
  (`agent_gtd_dispatch_protocol.branches.make_branch_name`).
- `transcript.txt` is excluded from git via `.git/info/exclude` before the agent starts.
- Attachments are staged into `<run_id>-attachments/` inside the workspace.
- On success: `cleanup_workspace()` removes the directory with `rm -rf` (via `sudo` in
  production, via `shutil.rmtree` in dev).
- On failure: also cleaned up, **except for manage-mode failures** and **build-mode
  push-verification failures** (see Push Verification below), where the workspace is
  preserved — in the push-verification case the unpushed commits exist only in the clone.

### Manage Mode — `prepare_manage_workspace()`

Called for `mode=manage`. Shallow-clones the default branch:

```
git clone --depth=50 <git_origin> <WORKSPACE_ROOT>/repos-<run_id>
git remote set-head origin --auto
git symbolic-ref --short refs/remotes/origin/HEAD  → detect default branch
git checkout <default_branch>
```

The manage agent uses this workspace to run quality gates (`git fetch`, `git checkout branch`,
test suite) and to execute squash merges before pushing to the default branch.

### Workspace (Multi-Repo) Projects

The single-clone paths above are the **default** (`repo_mode` absent/`None`/unrecognized on
the project). Projects with `repo_mode == "workspace"` carry a `workspace_repos` list of git
URLs instead of a single `git_origin`, and the dispatch worker (`main.py`) selects the
multi-repo variants in `dispatch.py`:

- **Build / plan** — `prepare_workspace_multi(repo_urls, run_id, branch_name)`:
  - Workspace root is `<WORKSPACE_ROOT>/ws-<run_id>/`; created via a sudo-wrapped
    `mkdir -p` so the agent user owns it under the two-user split.
  - Each URL is cloned in order into `<root>/<repo_dir_from_url(url)>`.
  - The **same feature branch** is created service-side (`git checkout -b`) in every repo.
  - Raises `ValueError` before touching the filesystem if `workspace_repos` is empty, any
    URL yields an empty basename, or two URLs map to the same directory name.
- **Manage** — `prepare_manage_workspace_multi(repo_urls, run_id)`:
  - Workspace root is `<WORKSPACE_ROOT>/repos-<run_id>/`.
  - Each repo is cloned `--depth=50` into `<root>/<dir>` and checked out on its detected
    default branch (no feature branch). Same `ValueError` pre-validation as above.

The derived `workspace_repo_dirs` list is threaded into `build_system_prompt`, so build,
plan, **and** manage prompts each get a workspace-layout section describing the per-repo
directory structure (and, for manage, per-repo merge/halt semantics).

### `cleanup_workspace()`

Removes the workspace directory after a run completes. Guards against escaping the workspace
root with a `config.WORKSPACE_ROOT in workspace.parents` check before deleting.

---

## Push Verification (Build Mode)

A build run that exits 0 is **not** automatically `succeeded`. Before the agent starts, the
dispatch worker captures the base HEAD SHA of each cloned repo (`dispatch.get_head_sha()`).
After a build-mode agent exits 0, `dispatch.verify_pushes()` classifies each repo:

| `PushStatus` | Meaning |
|---|---|
| `no_changes` | `git rev-list <base_sha>..HEAD --count` is 0 — agent made no commits |
| `pushed` | Local HEAD SHA matches `origin`'s SHA for the feature branch (`git ls-remote`) |
| `unpushed` | Local commits exist but the remote branch is missing or behind — **or any git command failed** (fail-closed) |

If **any** repo is `unpushed`:

- The run is flipped to `RunStatus.failed` with error `"push verification failed: ..."`.
- The per-repo results are serialized as JSON into the `push_results` column on the run row.
- The workspace is **preserved** (the commits exist only in the clone).
- A per-repo status comment is posted to the GTD item (including a `[dirty working tree]`
  marker when tracked files were left modified).

Plan and manage modes are exempt — `_verify_repos` is `None` for those, so verification is
skipped entirely. See `tests/test_push_verification.py` for the full behavior matrix.

---

## Engine Routing

Engine selection is driven by the `engine` field on the `DispatchRequest`. The
`engines.py` module registers five engine instances and exposes a lookup:

```python
engine = get_engine(body.engine)  # raises ValueError for unknown names
```

**Automatic engine swap**: plan-mode and manage-mode runs cannot use the Ollama backend
(it is not a managed Anthropic endpoint). The dispatch handler swaps `claude-code-ollama`
→ `claude-code` before starting the task, logs a warning, and records `engine_actual` in
the run row. The `RunResponse` includes an `engine_swap` field describing the substitution.

### Available Engines

| Engine name | Binary | Auth | Notes |
|---|---|---|---|
| `claude-code` | `claude` | `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | Default; moving alias `opus` (`--model opus`) |
| `claude-code-sonnet` | `claude` | same as above | Moving alias `sonnet` (`--model sonnet`) |
| `claude-code-haiku` | `claude` | same as above | Moving alias `haiku` (`--model haiku`) |
| `claude-code-ollama` | `claude` | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | Routes to local Ollama endpoint |
| `kiro` | `kiro-cli` | `KIRO_API_KEY` | Writes system prompt to `system_prompt.md` |

Engine availability is evaluated **per request**, not at startup: `GET /info` calls
`get_available_engine_names()` (which runs `is_engine_available()` against the current
environment) at request time, so only engines with credentials present are returned.
Nothing in `lifespan` checks engine credentials.

---

## Transcript Streaming

The agent subprocess writes combined stdout+stderr to `transcript.txt` in the workspace:

```python
proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=f, stderr=subprocess.STDOUT)
```

This means `GET /runs/{run_id}/transcript` can serve live output while the agent is still
running. The endpoint reads the tail of the file (default 200 lines, configurable up to 5000).

---

## Manage-Mode Auto-Recovery

When a manage-mode `_dispatch_worker` exits (for any reason except human cancellation), the
`_maybe_relaunch_manage()` function:

1. Fetches the rollout status from the GTD API.
2. If the rollout is already in a terminal state (`completed`, `halted`, `cancelled`) — does nothing.
3. Otherwise, calls `relaunch_manage_rollout()` to atomically increment `manage_retry_count`.
4. If `retry_count > MAX_MANAGE_RETRIES` (default 2): halts the rollout with reason
   `"manage_relaunch_cap_exceeded"`.
5. Otherwise: sleeps `MANAGE_RETRY_BACKOFF_SECONDS` (30 s) then spawns a new `_dispatch_worker`
   with `manage_retry_count` set so the recovery prompt includes a warning header.

### Stale-Manager Watchdog

Exit-path recovery alone cannot catch a manage agent that is alive but stuck. `lifespan`
also starts a background `_manage_watchdog()` task that scans every
`WATCHDOG_INTERVAL_SECONDS` (default 180 s, `DISPATCH_WATCHDOG_INTERVAL_SECONDS` env) for
manage runs whose rollout state has not advanced in `MANAGE_STALE_THRESHOLD_SECONDS`
(default 2100 s / 35 min, `DISPATCH_MANAGE_STALE_THRESHOLD_SECONDS` env). When it finds a
stale manager it kills the task/subprocess and routes into the shared
`_do_manage_recovery()` — the same path used by `_maybe_relaunch_manage()` on exit — so
watchdog-triggered relaunches count against the same `MAX_MANAGE_RETRIES` cap. The stale
threshold is deliberately set above the longest build a manager may legitimately wait on
(its state timestamp only advances on real progress) and must stay below
`MANAGE_TIMEOUT_SECONDS`. See `tests/test_manage_watchdog.py`.

See [docs/rollouts.md](rollouts.md) for the full rollout orchestration protocol.

---

## SSE Event Bus

Each run has an in-memory `asyncio.Queue` stored in `_run_event_queues[run_id]`. Status-change
events are published via `_publish_run_event()` and consumed by any SSE subscriber watching
that run. The queue is created before the capacity check (so queued/pending runs can also
receive cancel events) and cleaned up in the dispatch worker's `finally` block.

---

## Service Initialization Sequence

```
uvicorn start
  → lifespan.__aenter__
      → config.load()           # read env vars
      → _check_service_repo()   # guard dirty working copy (prod only)
      → dispatch.init_executor() # size ThreadPoolExecutor
      → db.init_db()            # create/migrate dispatch.db
      → db.reconcile_orphans()  # mark stuck runs as failed
      → asyncio.create_task(_manage_watchdog())  # stale-manager scan loop
  → yield  (service accepting requests)
  → lifespan.__aexit__
      → cancel the watchdog task
      → cancel all _active_processes tasks
```

---

## Security: subprocess User Isolation

In production the service runs as `dispatch-svc`. Agent subprocesses run as `dispatch` via:

```bash
sudo -u dispatch -H <claude-binary> ...
```

The sudoers fragment (`/etc/sudoers.d/dispatch-svc`) enumerates exactly which commands
`dispatch-svc` may run as `dispatch`. See [docs/install.md](install.md) for the full
security model and the two-user architecture.

For a single-machine / development install (no two-user split, no sudo wrapping of agent
subprocesses), use **single-user mode**: run the setup script with `DISPATCH_SINGLE_USER=1`.
See [docs/install.md — Single-user mode](install.md#single-user-mode). This is the
recommended path for an engineer's workstation.
