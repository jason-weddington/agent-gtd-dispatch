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

### `reap_workspace_processes()`

Called from the run teardown `finally` after the rescue commit/push and before
`cleanup_workspace()` (also when the workspace is kept), and from rollout-workspace teardown.
Scans `/proc` for processes whose `exe` or `cwd` lies under the workspace (path-component
match only, never by name), sends SIGTERM, waits up to a grace period, then SIGKILLs
survivors. The worker and its ancestors are never signalled. Each reaped process is logged
and appended to the run's `error` text without changing its status. This catches detached
daemons (own session) that killing the agent's process tree misses.

---

## Gate Install (Build + Manage Modes)

`gates.py` runs a deterministic, worker-enforced hook install for every cloned repo, right
after the workspace clone and **before** the prompt is built, the dispatch comment is posted,
or the agent launches. It replaces the old prompt-driven warm-up step ("run `pre-commit
install`"), which was advisory only — an agent could skip it, and lefthook/husky repos had no
prompt coverage at all. Today a dispatched agent's clone has active hooks only through the
host `init.templateDir` pre-commit shims (Setup Step 4.7; they call `pre-commit` with
`--skip-on-missing-config`), so lefthook repos (harness-design) and husky repos (grit-mile)
ended up with **no active hooks** — an agent could push a tree that fails the gate while the
run reported success.

### Detection (precedence order)

For each repo, `detect_gate_steps()` picks the **first** match, filesystem-only (no
subprocess calls):

| # | Marker | Manager | Install command |
|---|--------|---------|------------------|
| 1 | committed `.agent-gtd/setup` file | override | `bash .agent-gtd/setup` |
| 2 | `lefthook.yml` / `.lefthook.yml` / `lefthook.yaml` / `.lefthook.yaml` | lefthook | `lefthook install` |
| 3 | `.pre-commit-config.yaml` | pre-commit | `pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push` |
| 4 | `.husky/` directory | husky | `git config core.hooksPath .husky` |
| — | none of the above | — | no-op (not a failure) |

A repo with an override skips auto-detection entirely, even if lefthook/pre-commit/husky
markers are also present (that combination logs a `shadowed-markers` warning, but the
override still wins). A repo with an unsupported hook-manager config (e.g. `lefthook.toml`,
`.githooks`) and no override logs a `no-manager-unsupported-config` warning — it still needs
an `.agent-gtd/setup` override to get gate coverage.

### The `.agent-gtd/setup` override contract

A committed `.agent-gtd/setup` file in a repo's root REPLACES auto-detection for that repo.
Contract:

- Run with `bash` (no exec bit required) — `bash .agent-gtd/setup`.
- `cwd` is the repo root.
- Runs as the agent subprocess user, via the same sudo wrapper (`_sudo_wrap`) every other
  dispatch subprocess uses.
- Timeout: `config.GATE_INSTALL_TIMEOUT_SECONDS` (env `DISPATCH_GATE_INSTALL_TIMEOUT_SECONDS`,
  default `300`).
- Exit `0` within the timeout = success. The override step is **not verified** — there is no
  live-hook check afterward (see Verification below).
- Runs with the same minimal, credential-free env every gate-install subprocess gets (see
  Env below) — no `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `AGENT_GTD_API_KEY`, or
  `DISPATCH_API_KEY`.
- Must not commit, switch branches, or modify tracked files — it runs before the agent's own
  work and its output isn't reviewed by anything downstream.
- A repo that wants to opt entirely out of gate install can commit an override that just runs
  `exit 0`.

### Why `/bin/bash` and `git`, not `pre-commit`/`lefthook` directly

Every gate-install argv is routed through `/bin/bash -c "<command>"` (or, for husky, `git`
directly) rather than exec'ing `pre-commit`/`lefthook` as the argv\[0\]. This is because the
sudoers NOPASSWD allowlist (`templates/sudoers-dispatch-svc.tmpl`) permits `/bin/bash` and
`/usr/bin/git` for the dispatch-svc → dispatch user transition, but not arbitrary binaries
like `pre-commit` or `lefthook`.

### Env

`gates.agent_shell_env()` builds a minimal env for every gate-install subprocess (the hook
manager install, the `.agent-gtd/setup` script, and the `git rev-parse --git-path hooks`
verification call): only `GATE_ENV_KEYS` (`PATH`, `HOME`, `USER`, `LANG`, `TERM`, `SHELL`) are
copied from the parent process env — no credentials ever reach a gate-install subprocess.
`PATH` is built via the same `engines.prepend_agent_bin_dirs()` helper `engines.build_env`
uses, so gate installs see the same `~/.local/bin` (uvx/MCP binaries) and `~/.cargo/bin`
(rustup toolchain binaries — cargo, cog, typos, cargo-sort, etc.) prefix the agent subprocess
itself gets.

### Verification — "verify, don't trust"

The templateDir shims mean `.git/hooks/pre-commit` exists in **every** clone regardless of
whether a hook manager is installed, so existence alone proves nothing. After a non-override
install exits 0, `run_gate_steps()` resolves the *effective* hooks directory with
`git rev-parse --git-path hooks` (honouring `core.hooksPath`, so husky's redirect is
respected) and checks that the live hook actually belongs to the declared manager:

- **pre-commit**: every one of `pre-commit`, `commit-msg`, `pre-push` must be a regular file
  in the hooks dir containing `File generated by pre-commit` and **not** containing
  `--skip-on-missing-config` (that flag appears only in the untouched host shim — running
  `pre-commit install` rewrites the shim in place and drops the flag).
- **lefthook**: at least one git client hook (of the ten standard names) directly inside the
  hooks dir must be a regular file containing the bytes `lefthook`. `lefthook install` renames
  each shim it replaces to `<hook>.old` (which lefthook never executes); shims it doesn't
  replace (e.g. `commit-msg`) stay in place and still no-op via `--skip-on-missing-config`. Old
  backups and untouched pre-commit shims never match.
- **husky**: `core.hooksPath` must resolve to `.husky/` (exactly — not some other directory),
  and at least one git client hook inside it must be a regular file with the owner-execute bit
  set. For husky v9 repos, an agent's later `npm install` runs husky's `prepare` script, which
  re-points `core.hooksPath` to `.husky/_`; hooks stay live through husky's wrappers, and the
  worker does **not** re-verify after the agent launches.
- **override**: not verified. Exit 0 within the timeout is success.

Gate verification proves a hook is installed by the declared manager — it does **not** prove
the tools that hook's commands invoke are on the agent's `PATH`. Every tool referenced by a
repo's hook config (e.g. harness-design's `cog`, `typos`, `cargo-sort`, `cargo-deny`,
`cargo-llvm-cov`, `cargo-machete`, `gitleaks`) must be separately provisioned for the agent
user on every dispatch host.

The old manage-mode prompt also asked the agent to install the `post-commit` hook type; the
worker deliberately does not (`PRE_COMMIT_HOOK_TYPES` is exactly `pre-commit`, `commit-msg`,
`pre-push`).

### Failure policy

An install that exits non-zero, times out, or fails verification stops the run **before** the
agent launches — no agent tokens are spent:

- **Build mode**: the run is marked `failed` and one comment is posted on the GTD item naming
  the repo, the step, and the tail of its output (`gates.format_failure_comment`).
- **Manage mode**: the run is marked `failed` **and** the rollout is halted
  (`gtd_client.halt_rollout(..., comment=...)`) with the same failure detail attached to the
  halt comment. The worker tries the run's per-run `callback_token` first, then falls back to
  the static service key if that attempt raises.

In both modes `should_cleanup` stays `True`, so the workspace is still removed in the `finally`
block — a gate failure means the workspace never got real agent work, unlike a build-mode push
verification failure (which preserves the workspace because it holds unpushed commits).

### Exemptions

- **Talos engines**: skipped entirely (`decision=skipped reason=talos`). Talos self-gates via
  the project's `gate_command` and commits its own output exactly once; installed hooks —
  especially fixers — would mutate or block that commit (kb-03099).
- **Plan mode**: skipped entirely (`decision=skipped reason=plan`). A plan run's clone is never
  committed into by the agent.

### Logging

Every decision emits a structured `gate: ...` line (`logger.info`/`logger.warning`) with a
`decision=` token: `selected`, `no-manager`, `shadowed-markers`, `no-manager-unsupported-config`
(all from `detect_gate_steps`); `installed-verified`, `override-ok`, `failed` (from
`run_gate_steps` and the worker); and `skipped reason=<plan|talos|no-workspace>` (from the
worker, before `detect_gate_steps` is even called).

### Toolchain requirement

`lefthook` itself must be on the agent's `PATH` on every dispatch host — provisioned by
`setup-dispatch-host.sh` (sibling item 25bcf1a9). This module never installs `lefthook` or
`pre-commit` themselves; it only runs `lefthook install` / `pre-commit install` assuming the
binary is already present.

The tools a repo's hooks and gate command then *invoke* — `cog`, `typos`, `cargo-sort`,
`cargo-deny`, `cargo-llvm-cov`, `cargo-machete`, `cargo-nextest`, `cargo-release` and
`gitleaks` — are provisioned by the same script's **Step 4.9** (item 75b88467), which also
bootstraps `rustup` + `cargo-binstall` for the agent user unconditionally (it does *not*
require `--with-talos`) and guarantees a usable default toolchain for the agent user. The
tool list is data in `templates/dev-toolchain.sh`, shared by
`setup-dispatch-host.sh` and `deploy.sh`; see
[docs/install.md — Dev toolchain (Step 4.9)](install.md#dev-toolchain-step-49). No `PATH`
change is needed for these binaries: the agent/gate `PATH` already prepends `~/.local/bin`
and `~/.cargo/bin`.

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

### Worker push backstop

Before declaring failure, the worker gets one chance to finish the push itself. This
exists because an agent can commit correct, finished work and then background its
`git push` (e.g. to avoid blocking on a slow pre-push hook) and exit before the push
completes — the commit is real, but nothing ever reached origin. The build-mode prompts
now instruct agents to always push in the foreground and wait for exit 0, but the worker
backstop covers the case anyway.

For each repo classified `unpushed` with a non-`None` `local_sha` (i.e. `verify_pushes`
itself didn't fail-closed on a git error — those are left to the normal failure path),
the worker:

1. Computes `remaining = timeout_seconds - (now - run_start_time)`. If
   `remaining <= config.PUSH_BACKSTOP_MIN_SECONDS` (env `DISPATCH_PUSH_BACKSTOP_MIN_SECONDS`,
   default 10s), the backstop is skipped entirely — not enough budget left to plausibly
   succeed.
2. Otherwise, for each eligible repo in turn (recomputing `remaining` immediately before
   each attempt), calls `dispatch.push_unpushed_repo(repo_path, branch_name, remaining)`
   **exactly once** via `loop.run_in_executor(_executor, ...)` — never inline on the event
   loop, since a pre-push hook can block for minutes. Hooks stay **enabled**; the helper
   never passes `--no-verify`. Any exception/timeout from the attempt is caught and logged,
   not propagated.
3. Re-runs `dispatch.verify_pushes()` to get the post-backstop classification.

If a worker push (this backstop, the talos push or the teardown rescue) is rejected because
origin's branch already holds an older run's commits — a re-dispatch reusing the item's
branch name, detected by a fetch plus `git merge-base --is-ancestor origin/<branch> HEAD`
failing, never by parsing git's stderr — the same HEAD is pushed to the first free
`<branch>-r<N>` (N ≥ 2) instead, the old branch is never forced or deleted, and that name
becomes the run's recorded `branch_name` (and so the board's `feature_branch` and the
branch the rollout reviewer is handed); a workspace run falls back per repo.

If **any** repo is (still) `unpushed` after the backstop attempt (or after it was skipped):

- The run is flipped to `RunStatus.failed` with error `"push verification failed: ..."`.
- The per-repo results are serialized as JSON into the `push_results` column on the run row.
- The workspace is **preserved** (the commits exist only in the clone).
- A per-repo status comment is posted to the GTD item (including a `[dirty working tree]`
  marker when tracked files were left modified).
- This failure output is **identical** whether or not a backstop attempt was made — the
  backstop is invisible on the failure path.

If no repo is unpushed after the backstop, the run proceeds down the normal success path.
When at least one backstop attempt actually ran and rescued a repo, the worker additionally
posts one comment to the item:

```
Push verification found unpushed work after the agent exited (run `{run.id}`). The dispatch worker completed the push before the run would have failed:
- {repo_name}: pushed by worker ({commits_ahead} commit(s), {local_sha[:8]})
```

with one bullet per rescued repo, using that repo's pre-backstop `commits_ahead`/`local_sha`.

The backstop only fires on the exit-code-0 success branch (the reported incident was an
agent that exited cleanly with an incomplete push) — the `TimeoutExpired` "linger success"
path, and plan/manage modes (`_verify_repos is None`), are unchanged. Talos-engine build
runs return before `run_agent`/`verify_pushes` are ever reached (talos mints its own
commit + push, with `--no-verify`, by design), so the backstop cannot apply to them.

Plan and manage modes are exempt — `_verify_repos` is `None` for those, so verification is
skipped entirely. See `tests/test_push_verification.py` for the full behavior matrix.

### Post-run gate (non-talos build runs)

Hooks can be bypassed (`--no-verify`, a hook that skips itself), so the real guarantee
against "pushed a tree that fails the gate while reporting success" is the harness
running the gate. Talos already runs `project.gate_command` as its own definition of
Done (its engine binary, not the dispatch worker, runs it — see the Gate Install section
above for how Talos and non-talos engines are gated differently before launch). The
dispatch worker runs the same command, once, for every **non-talos BUILD run** — after
push verification (including the push backstop and the zero-commits guard) has already
succeeded.

**Placement.** The gate step sits at the very end of the exit-0, `_verify_repos is not
None` branch — after the backstop's `unpushed` failure path and the zero-commits guard,
and before the run is marked `succeeded`. It therefore only ever runs on a branch that
has genuinely landed on origin.

**Eligibility.** The worker reads `project.gate_command` (the same key Talos reads) and
strips it. The gate is skipped — logged, no item comment — when either:

- no repo in the run actually reached `pushed` status (`skipped_no_pushed_repo`), or
- `gate_command` is empty/unset (`skipped_no_gate_command`).

**Timeout.** `gate_timeout = max(timeout_seconds - elapsed_since_run_start,
config.POST_RUN_GATE_MIN_SECONDS)` — the gate always gets at least
`POST_RUN_GATE_MIN_SECONDS` (env `DISPATCH_POST_RUN_GATE_MIN_SECONDS`, default 600s),
even when the build agent used almost the entire run timeout.

**Launch shim.** The gate does not run the project's command directly. It runs
`/bin/bash -c 'exec timeout --kill-after="$1" "$2" /bin/sh -c "$3"' agent-gtd-gate
<CANCEL_GRACE_SECONDS>s <gate_timeout>s <gate_command>`, wrapped in `sudo -u <agent
user> -H` when the two-user split is active. Two things force this shape:

- The sudoers `NOPASSWD` list authorizes `/bin/bash`, not `/bin/sh` — so the outer shell
  has to be bash even though the gate command itself runs under `/bin/sh -c`.
- GNU `timeout` sends its signal to the command's whole process group, so a
  gate that forks background children still gets torn down. `--kill-after` escalates to
  `SIGKILL` if the gate ignores the initial `SIGTERM`.

Output (stdout+stderr combined) goes to a `tempfile.TemporaryFile()` outside the
workspace, never a pipe — a backgrounded grandchild holding a pipe's write end open
can't hold up `proc.wait()`. `stdin=subprocess.DEVNULL`. The subprocess env is filtered
to `dispatch.GATE_ENV_KEYS` (`engines.COMMON_ENV_KEYS` minus `AGENT_GTD_URL` /
`AGENT_GTD_API_KEY`, and `engines.FORBIDDEN_ENV_KEYS` — production KB DSNs that never reach any subprocess) — an arbitrary project-authored gate command has
no business touching GTD credentials. The gate registers itself as the run's active
subprocess (the same `popen_callback` hook `run_agent` uses), so `cancel_run`'s SIGTERM
reaches it.

**Dirty-tree stash.** Any repo the run's push verification found `dirty` (pushed or
no-changes) is stashed — `git stash push --message agent-gtd-post-run-gate` as the
`agent-gtd-dispatch` git identity — before the gate launches, so the gate only ever
checks committed work. The stash is left in place afterward (not popped); the pass/fail
comment names which repos were stashed.

**Outcomes.** The worker classifies the result into one of four failure shapes, each
with a pinned first line on the GTD comment: gate **timed out**, gate **exited
non-zero**, gate **killed by signal** (negative return code), or the gate **could not be
launched** (a `Popen` `OSError`, e.g. missing binary). Every failure comment also
includes the last 3000 chars of combined gate output in a fenced block. A pass posts a
short confirmation comment instead. Both cases mention the stashed repos when
applicable. Every decision — pass, each failure shape, or a skip — is recorded in a
single structured `post-run gate: ...` INFO log line, so fleet-wide gate health is
greppable even though outcomes aren't (yet) persisted in the runs table.

**Exemptions.** Talos build runs never reach this code (they return from `_run_talos`
before `run_agent`/verification) — Talos self-gates via its own `TaskSpec.gate_command`
and `TALOS_GATE_TIMEOUT_SECS`. Plan and manage runs never run the post-run gate
(`_verify_repos` is `None` for both). The `TimeoutExpired` "linger success" path
(agent process outlives the wall clock but every repo is already pushed) is exempt too,
mirroring the push backstop's own scope.

A rollout manager gets one exception to the usual "any non-`succeeded` child run is a
halt candidate" rule: when a child run's `error_msg` starts with `post-run gate`, the
branch **was** pushed and only the project gate failed — the manager re-runs the gate
itself as part of its own quality-gate step instead of halting immediately. See
`docs/rollouts.md` for the manager-side wording.

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
| `claude-code` | `claude` | `CLAUDE_CODE_OAUTH_TOKEN` (subprocess auth; `ANTHROPIC_API_KEY` is service-side only, see kb-01512) | Default; moving alias `opus` (`--model opus`) |
| `claude-code-sonnet` | `claude` | same as above | Moving alias `sonnet` (`--model sonnet`) |
| `claude-code-haiku` | `claude` | same as above | Moving alias `haiku` (`--model haiku`) |
| `claude-code-ollama` | `claude` | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | Routes to local Ollama endpoint |
| `kiro` | `kiro-cli` | `KIRO_API_KEY` | Writes system prompt to `system_prompt.md` |

Engine availability is evaluated **per request**, not at startup: `GET /info` calls
`get_available_engine_names()` (which runs `is_engine_available()` against the current
environment) at request time. Claude Code engines are **always reported available** — the
`claude` binary may be authenticated externally (enterprise/managed distribution, internal
wrapper, Bedrock-backed login), so the gate does not require `CLAUDE_CODE_OAUTH_TOKEN` or
`ANTHROPIC_API_KEY`; an unauthenticated host fails at exec time. Kiro and the Ollama-routed
engine are still gated on their configured credentials. Nothing in `lifespan` checks engine
credentials.

---

## Transcript Streaming

The agent subprocess writes combined stdout+stderr to `transcript.txt` in the workspace:

```python
proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=f, stderr=subprocess.STDOUT)
```

This means `GET /runs/{run_id}/transcript` can serve live output while the agent is still
running. The endpoint reads the tail of the file (default 200 lines, configurable up to 5000).

---

## Rollout Liveness (no manager to relaunch)

A manage dispatch starts the worker-driven wave loop (`_start_rollout_loop`) in this
service; it launches one short-lived REVIEW-mode agent per completed build. There is no
resident manager agent, so there is nothing to relaunch:

- **The loop owns liveness.** While builds are in flight a tick returns `_TICK_WAIT` and the
  loop sleeps `ROLLOUT_LOOP_POLL_SECONDS`. Its only liveness halt ("rollout stalled")
  requires nothing to be in flight, so it never strands a running build. A crashed loop fails
  its own run and does not halt the rollout.
- **The reviewer is bounded** by `REVIEW_TIMEOUT_SECONDS` (45 min).
- **A restart is covered by the re-adoption sweep** (`_readopt_running_rollouts`), which runs
  once at startup and resumes ONLY rollouts this host holds a local MANAGE run row for.
- **A lead's resume relaunches the loop.** When a loop reads a halted rollout it exits. After
  the lead resolves the halt, `resume_rollout` (board side) puts the rollout back to running
  and dispatches a new MANAGE run pinned to the rollout's OWNING host (the host that last
  drove it). Keeping every loop for a rollout on one host is what makes the per-host guard
  below sufficient: re-adoption only ever happens where the local MANAGE rows are.

### One loop per rollout (per host)

Two loops on one rollout halted it falsely (rollout fe1e828d), so a host refuses a second:

- **Single choke point.** `_start_rollout_loop(run)` is the only place a loop is
  registered. With no await between check and registration it refuses (returns `False`,
  creates no task) when `run.rollout_id` is already in `_rollout_loops`; otherwise it sets
  the map entry BEFORE `asyncio.create_task`. `/dispatch`, `_try_start_pending` and
  `_readopt_running_rollouts` all go through it; a refused local row is marked `cancelled`.
- **409 on `/dispatch`.** A MANAGE dispatch for a rollout with a live loop or a queued
  MANAGE entry returns HTTP 409 naming the rollout and the live run id, before
  `db.insert_run`. The board maps that 409 to a `cancelled` run; the live loop keeps driving.
- **Synchronous deregistration.** When `_drive_rollout` decides to exit (`_TICK_DONE` /
  `_TICK_HALTED`) it pops its own `_rollout_loops` entry at that decision point, before any
  further await, so a relaunch arriving while the old loop is still doing bookkeeping
  starts a fresh loop instead of being refused. The worker's `finally` pops only an entry
  that still maps to its own run.

The resident-manager keep-alive machinery (exit-path relaunch ladder `_maybe_relaunch_manage`,
shared `_do_manage_recovery`, the staleness watchdog, its in-memory tallies and the
`MAX_MANAGE_RETRIES` / free-relaunch caps) was retired in item 7baf77d0. The watchdog scanned
every running rollout fleet-wide and relaunched any whose heartbeat was stale, including
rollouts another host's loop was still driving (rollout fe1e828d landed one item twice).

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
      → asyncio.create_task(_retention_loop())            # age-based pruning
      → asyncio.create_task(_readopt_running_rollouts())  # resume this host's loops
  → yield  (service accepting requests)
  → lifespan.__aexit__
      → cancel the retention and re-adoption tasks
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
