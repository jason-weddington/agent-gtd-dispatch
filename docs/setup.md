# Agent GTD Dispatch — Setup Guide

## Developer Setup

### 1. Install Dependencies

```bash
uv sync
```

This installs all production and dev dependencies (pytest, ruff, mypy, pre-commit, etc.)
from `uv.lock` using the frozen lock file. The `dev` dependency group is included by default.

### 2. Install Pre-commit Hooks

```bash
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

This installs three hook types:

| Hook type | Runs on | Checks |
|---|---|---|
| `pre-commit` | `git commit` | ruff lint + format, mypy, conventional commit format |
| `commit-msg` | every commit | Conventional commit message format (`feat:`, `fix:`, `chore:`, …) |
| `pre-push` | `git push` | Full test suite with coverage threshold |

The `pre-push` hook runs `uv run pytest --cov` with `--frozen` to avoid rebuilding the
environment mid-hook.

### 3. Run the Service Locally

Set the required environment variables (see below), then:

```bash
uv run uvicorn agent_gtd_dispatch.main:app --reload --port 8100
```

The `--reload` flag enables hot-reload for development. Omit it for a production-like run.

### Installing as a service on your own machine (single-user mode)

Running uvicorn by hand is fine for hacking on the code, but if you want a real
installed service on a developer/engineer machine — systemd unit, auto-minted
`DISPATCH_API_KEY`, MCP server registration, pre-commit template directory, lefthook —
without the production two-user split, use **single-user mode**:

```bash
# Canonical form: name the var explicitly so sudo's env-stripping doesn't drop it
sudo --preserve-env=DISPATCH_SINGLE_USER DISPATCH_SINGLE_USER=1 \
    ./setup-dispatch-host.sh --env-file /tmp/dispatch.env
```

Everything runs under your own login account (no extra users, no sudoers fragment —
and therefore no POSIX isolation between service and agents). See
[install.md — Single-user mode](install.md#single-user-mode) for details, including
the mode-mismatch guards.

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `DISPATCH_API_KEY` | Bearer token that callers must supply to the REST API |
| `AGENT_GTD_URL` | Agent GTD API base URL — replace with your own Agent GTD instance, e.g. `https://<your-agent-gtd-host>:8443` |
| `AGENT_GTD_API_KEY` | Agent GTD API key (`agtd_…` prefix) |

> **Where to obtain `CLAUDE_CODE_OAUTH_TOKEN` and `AGENT_GTD_API_KEY`**: see
> [docs/install.md — Authentication & pairing](install.md#authentication--pairing) for
> step-by-step instructions on running `claude setup-token`, minting an `agtd_…` key in
> the Agent GTD Settings UI, and which env file each value belongs in (two-user vs
> single-user).

### Optional — Agent Subprocess Behavior

| Variable | Default | Description |
|---|---|---|
| `DISPATCH_AGENT_SUBPROCESS_USER` | `""` (disabled) | If set, agent subprocesses run as this user via `sudo -u <user> -H`. Leave empty in dev. |
| `DISPATCH_WORKSPACE_ROOT` | `~/workspace` (relative to agent user) | Override the workspace root directory |
| `DISPATCH_MAX_TURNS` | `100` | Default turn cap for agent subprocesses |
| `DISPATCH_TIMEOUT_SECONDS` | `1800` | Build/plan agent wall-clock timeout in seconds (30 minutes) |
| `DISPATCH_MANAGE_TIMEOUT_SECONDS` | `2700` | Manage-mode wall-clock backstop in seconds (45 minutes, sized for a short-lived reviewer; only used when a manage dispatch carries no `timeout_minutes`) |
| `DISPATCH_CANCEL_GRACE_SECONDS` | `5` | Seconds between SIGTERM and SIGKILL on cancel |
| `DISPATCH_POST_RUN_GATE_TIMEOUT_SECONDS` | 3600 | Dedicated post-run gate budget in seconds, independent of build time used; the effective budget is max(this, DISPATCH_LAND_GATE_TIMEOUT_SECONDS, DISPATCH_POST_RUN_GATE_MIN_SECONDS) |
| `DISPATCH_POST_RUN_GATE_MIN_SECONDS` | `600` | Lower bound of the post-run gate budget |
| `DISPATCH_GATE_INSTALL_TIMEOUT_SECONDS` | `300` | Per-subprocess timeout for the pre-launch gate install (hook-manager install, hook-dir lookup, or `.agent-gtd/setup`) |

### Optional — Concurrency

| Variable | Default | Description |
|---|---|---|
| `DISPATCH_MAX_CONCURRENT_RUNS` | `32` | Maximum simultaneous running dispatches; also sizes the ThreadPoolExecutor |

### Optional — Ollama Backend

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `""` (disabled) | Root URL of a local Ollama instance (e.g. `http://10.0.0.5:11434`). If empty, the `claude-code-ollama` engine is not available. |
| `OLLAMA_DEFAULT_MODEL` | `qwen3.6:35b` | Default Ollama model passed to `claude --model` |
| `OLLAMA_API_KEY` | `ollama` | Dummy auth value (Ollama ignores auth; value is injected as `ANTHROPIC_AUTH_TOKEN`) |
| `OLLAMA_TIMEOUT_MULTIPLIER` | `2.0` | Multiplier applied to `TIMEOUT_SECONDS` for Ollama runs (local inference is slower) |
| `DISPATCH_ENGINE_SLOTS` | `""` (no caps) | Per-engine concurrency caps, comma-separated `<engine>=<int>` pairs (e.g. `talos-qwen=1`). Engine names must be valid build engines; duplicates and non-digit values are rejected at startup. A cap of `0` hides the engine and refuses its BUILD dispatches. `talos-qwen` is advertised only with a cap >= 1. |
| `DISPATCH_ENGINES_ONLY` | `""` (no allowlist) | Optional allowlist of engines this host serves: comma-separated engine names from the engine registry. Unknown or duplicate names, or a list with no names, fail startup naming the variable. Unlisted engines are not advertised in `/info` and `/dispatch` refuses them with 422 `engine_not_allowed`. It only restricts and never enables an engine: a listed engine still needs its credentials and a non-zero slot cap. |
| `TALOS_QWEN_BASE_URL` | none | Ollama root URL used by `talos-qwen` on this host. `talos-qwen` is advertised only when this is non-empty; a `127.0.0.1` URL is valid. The endpoint is never probed. There is no fallback to `OLLAMA_BASE_URL`. |
| `TALOS_QWEN_MODEL` | `qwen3.8:27b` | Model for `talos-qwen`; empty means the default. |
| `TALOS_QWEN_NUM_CTX` | `262144` | Context window for `talos-qwen` (integer >= 1). |
| `TALOS_QWEN_KEEP_ALIVE` | none | Optional. How long this host's Ollama keeps the talos-qwen model loaded after a run; passed through verbatim to talos as `TALOS_OLLAMA_KEEP_ALIVE`. Accepts integer seconds (`-1` or any negative value keeps it loaded, `0` unloads immediately, `300`) or a duration such as `10m` or `1h30m`; anything else fails startup naming the variable. Empty or unset sends nothing, so the Ollama server's own default applies. Takes effect only with a talos build that reads TALOS_OLLAMA_KEEP_ALIVE (reinstall talos on the host with setup-dispatch-host.sh --with-talos if it predates that support) and only after a deploy of this release, which refreshes the sudoers env_keep. To verify, run a talos-qwen job and check `GET <TALOS_QWEN_BASE_URL>/api/ps`: `expires_at` should be about one keep-alive after the run ended, or the model absent for 0; if it shows the server default, the installed talos predates the support. |

**Operator note:** Stage `DISPATCH_ENGINE_SLOTS` and the `TALOS_QWEN_*` variables on GPU hosts BEFORE deploying this release; they are inert on older builds. After this release a host without them stops advertising `talos-qwen`, because it requires `DISPATCH_ENGINE_SLOTS` naming `talos-qwen` with a cap of 1 or more plus a non-empty `TALOS_QWEN_BASE_URL`. The template only seeds new hosts, so edit the existing service env.

**Operator note: configuring an engine-restricted host.** To make a host take only `talos-qwen` work, set `DISPATCH_ENGINES_ONLY=talos-qwen`, `DISPATCH_MAX_CONCURRENT_RUNS=1`, `DISPATCH_ENGINE_SLOTS=talos-qwen=1` and `TALOS_QWEN_BASE_URL=<ollama root url>`. Do not set a general cap of 0, because a `talos-qwen` run also takes a general slot. A host receives rollout loops and plan/manage runs only if it lists the engine the board routes them with (the project's `manage_engine`, default `claude-code`; for plan, the item's build engine). The rollout reviewer engine (default `claude-code-sonnet`), the plan/manage swap of `claude-code-ollama` / `claude-code-glm` / `claude-code-deepseek` to `claude-code` and the `claude-code-ollama` health-check fallback run in-host and are NOT restricted by the allowlist. `/info.engine` is the host's static default and is not filtered, and a dispatch that omits `engine` falls back to `claude-code`, which an allowlist without it refuses with 422. `/info.engine_slots` is not filtered. The allowlist is read at startup, so changing it needs a service restart and affects only new `/dispatch` requests (rollout loops re-adopted at startup and already-admitted runs keep running; drain the host first to move them). The board needs no change. In the rare race where a host is restarted with a new allowlist between the board's `/info` read and its POST, an auto-routed run fails with a `dispatch_http_error` instead of retrying another host and the operator re-dispatches.

### Notes on `ANTHROPIC_API_KEY`

**Optional.** It used to be required at startup for the LLM rollout planner; that
planner is gone and the service starts fine without the key. It is now read only as
an engine credential: `talos.py` puts it in the per-engine env overlay for
`talos-haiku` / `talos-sonnet` / `talos-opus`, and those engines are simply not
advertised via `/info` when it is unset.

It is deliberately **not** forwarded to Claude Code subprocesses. If Claude Code
received `ANTHROPIC_API_KEY`, it would prefer pay-as-you-go API billing over the
user's Max subscription — see kb-01512.

Claude Code subprocesses receive `CLAUDE_CODE_OAUTH_TOKEN` only (from the environment of
the service account or the subprocess user).

---

## Dev Workflow

```bash
# Run tests
uv run pytest -v

# Run tests with coverage report
uv run pytest --cov --cov-report=term-missing

# Lint
uv run ruff check src/ tests/

# Auto-format
uv run ruff format src/ tests/

# Type check
uv run mypy src/

# Run all pre-commit hooks manually (same as CI)
uv run pre-commit run --all-files
```

---

## Git Workflow

- Branch from main: `git checkout -b feat/<description>` (or `fix/`, `chore/`)
- Commit messages must follow Conventional Commits on main (`feat:`, `fix:`, `chore:`, etc.)
- Feature branches are free-form (hook is enforced only on merge to main)
- Squash merge to main: `git checkout main && git merge --squash feat/x && git commit`
- Push to origin freely
- `./release.sh` cuts a semantic-release version, builds both wheels, runs the gitignored `release.local.sh` publish hook (where your own publish/deploy glue lives), then pushes main + tags

---

## Full Host Deployment

For production installation on a Linux host (systemd unit, two-user split, SSH keypair
generation, sudoers fragment), see [docs/install.md](install.md). For an installed
service on a personal/dev machine without the two-user split, use single-user mode
(`DISPATCH_SINGLE_USER=1`) — see [install.md — Single-user mode](install.md#single-user-mode)
and the subsection above.
