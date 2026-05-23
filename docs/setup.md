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

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `DISPATCH_API_KEY` | Bearer token that callers must supply to the REST API |
| `AGENT_GTD_URL` | Agent GTD API base URL (e.g. `https://r7-research:8443`) |
| `AGENT_GTD_API_KEY` | Agent GTD API key (`agtd_…` prefix) |
| `ANTHROPIC_API_KEY` | Anthropic API key — used by the rollout planner in-process; **not** exposed to Claude Code subprocesses |

### Optional — Agent Subprocess Behavior

| Variable | Default | Description |
|---|---|---|
| `DISPATCH_AGENT_SUBPROCESS_USER` | `""` (disabled) | If set, agent subprocesses run as this user via `sudo -u <user> -H`. Leave empty in dev. |
| `DISPATCH_WORKSPACE_ROOT` | `~/workspace` (relative to agent user) | Override the workspace root directory |
| `DISPATCH_MAX_TURNS` | `100` | Default turn cap for agent subprocesses |
| `DISPATCH_TIMEOUT_SECONDS` | `1800` | Build/plan agent wall-clock timeout in seconds (30 minutes) |
| `DISPATCH_MANAGE_TIMEOUT_SECONDS` | `14400` | Manage agent wall-clock timeout in seconds (4 hours) |
| `DISPATCH_CANCEL_GRACE_SECONDS` | `5` | Seconds between SIGTERM and SIGKILL on cancel |

### Optional — Concurrency

| Variable | Default | Description |
|---|---|---|
| `DISPATCH_MAX_CONCURRENT_RUNS` | `32` | Maximum simultaneous running dispatches; also sizes the ThreadPoolExecutor |

### Optional — Planner

| Variable | Default | Description |
|---|---|---|
| `DISPATCH_PLANNER_MODEL` | `claude-sonnet-4-6` | Anthropic model used by the rollout planner (`POST /plan`) |

### Optional — Ollama Backend

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `""` (disabled) | Root URL of a local Ollama instance (e.g. `http://10.0.0.5:11434`). If empty, the `claude-code-ollama` engine is not available. |
| `OLLAMA_DEFAULT_MODEL` | `qwen3.6:35b` | Default Ollama model passed to `claude --model` |
| `OLLAMA_API_KEY` | `ollama` | Dummy auth value (Ollama ignores auth; value is injected as `ANTHROPIC_AUTH_TOKEN`) |
| `OLLAMA_TIMEOUT_MULTIPLIER` | `2.0` | Multiplier applied to `TIMEOUT_SECONDS` for Ollama runs (local inference is slower) |

### Notes on `ANTHROPIC_API_KEY`

The service reads this key **in-process** for the rollout planner only. It is deliberately
**not** forwarded to Claude Code subprocesses. If Claude Code received `ANTHROPIC_API_KEY`,
it would prefer pay-as-you-go API billing over the user's Max subscription — see kb-01512.

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
- Push to origin freely; `./deploy.sh` deploys current main to pironman01
- `./release.sh` cuts a semantic-release version, pushes main + tags, and deploys

---

## Full Host Deployment

For production installation on a Linux host (systemd unit, two-user split, SSH keypair
generation, sudoers fragment), see [docs/install.md](install.md).
