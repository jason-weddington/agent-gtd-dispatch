# Agent GTD Dispatch

Dispatch worker API that runs headless Claude Code agents on isolated infrastructure.

## Setup (do this first)

```bash
uv sync                  # install all deps including dev group
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

## Commands

```bash
uv run pytest -v                              # run tests
uv run pytest --cov --cov-report=term-missing  # tests + coverage report
uv run ruff check src/ tests/                 # lint
uv run ruff format src/ tests/                # auto-format
uv run mypy src/                              # type check
uv run pre-commit run --all-files             # run all pre-commit hooks
```

## Project structure

```
src/agent_gtd_dispatch/
  main.py          # FastAPI app — endpoints, lifespan, background dispatch worker
  models.py        # Pydantic models: Run, RunStatus, DispatchRequest, RunResponse
  db.py            # SQLite persistence (aiosqlite) for dispatch runs
  dispatch.py      # Core logic: workspace prep, prompt building, agent invocation
  engines.py       # Per-engine CLI command builders + env filtering (claude, kiro, ...)
  gtd_client.py    # HTTP client for the Agent GTD API (items, projects, comments)
  config.py        # Env-var config with load() — shared service config only

tests/
  test_api.py        # API endpoint tests via FastAPI TestClient with mocked gtd_client
  test_dispatch.py   # Unit tests for dispatch logic + engine command/env builders
  test_gtd_client.py # HTTP client tests with mocked httpx
```

## Key patterns

- **Config**: Module-level globals in `config.py`, loaded via `config.load()` at startup. Tests patch env vars and call `config.load()` — see `_env` fixture in `test_api.py`.
- **Mocking**: Tests use `unittest.mock.patch` and `AsyncMock`. API tests patch `agent_gtd_dispatch.main.gtd_client` and `agent_gtd_dispatch.main.dispatch` modules.
- **Async tests**: `asyncio_mode = "auto"` in pyproject.toml — no need for `@pytest.mark.asyncio`.
- **Test style**: `from __future__ import annotations`, test classes with `Test` prefix, `-> None` on methods, docstrings on source but not tests (D rules suppressed for `tests/**`).
- **Attribution**: `POST /dispatch` accepts `attribution: str | None`. When set, the spawned agent subprocess gets `AGENT_GTD_AGENT_NAME=<attribution>` in its env, so it posts GTD comments under that identity (e.g. `claude-build-abc12345`) rather than the default lead.

## Git workflow

- Branch from main: `git checkout -b feat/description` (or `fix/`, `chore/`)
- Conventional commits enforced on main (hook). Feature branches are free-form.
- Squash merge to main: `git checkout main && git merge --squash feat/x && git commit`
- Push to origin freely; `./deploy.sh` deploys current main to pironman01.
- `./release.sh` cuts a version (semantic-release), pushes main + tags to origin and github, then deploys.
- Pre-push hook runs full test suite with coverage (threshold from `[tool.coverage.report]` in `pyproject.toml`).
- All `uv run` in hooks uses `--frozen` to avoid rebuilding mid-hook.

## Coverage

- See `[tool.coverage.run] omit` in `pyproject.toml` for files excluded from the threshold.
- Threshold lives in `[tool.coverage.report] fail_under` — ratchet it up when you add tests.

## Deployment hosts & env files (two-user split)

Runs on three hosts (`pironman01`, `ubuntu-pi-01`, `r7-research`). On each: the API
runs as **`dispatch-svc`**; it launches the Claude Code agent as **`dispatch`** via
`sudo -u dispatch -H`.

- **`/home/dispatch-svc/.env` is the canonical (and only) env file.** It is the systemd
  `EnvironmentFile` (→ the service's `os.environ`; `config.py` reads `os.environ` only,
  no dotenv) *and* the file `setup-dispatch-host.sh` reads at provision time to inject
  KB secrets into the agent's `~/.claude.json`.
- **`/home/dispatch/.env` is vestigial** (pre-split leftover) — nothing reads it. Don't
  put vars there; safe to delete where it lingers.
- **`setup-dispatch-host.sh` runs as ROOT** (`sudo`, asserts `EUID==0`), not as
  `dispatch-svc`. It creates both users, installs the unit + sudoers, and reads
  `/home/dispatch-svc/.env` to register MCP servers (Step 4.6, driven by
  `templates/mcp-servers.sh`).
- **KB MCP secrets** live in `/home/dispatch-svc/.env` as `TEAM_KB_DATABASE_URL` and
  `KB_ANTHROPIC_API_KEY` (NOT `ANTHROPIC_API_KEY` — that name would reach the agent's
  env and flip Claude Code off Max/OAuth billing). They are injected into the per-server
  `env` blocks of `personal-kb`/`team-kb` at provision time; `mcp-servers.sh` references
  the env vars, never literals (gitleaks-safe).

Full references: **`kb-01598`** (env-file + provisioning model — which var goes where),
`kb-01583` (how env crosses the sudo boundary at runtime), `kb-01512` (OAuth vs API
billing), `kb-01537` (install procedure).
