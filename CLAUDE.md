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
