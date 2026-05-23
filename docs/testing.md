# Agent GTD Dispatch — Testing Guide

## Test Commands

```bash
# Run all tests
uv run pytest -v

# Run tests with coverage report (terminal output)
uv run pytest --cov --cov-report=term-missing

# Run a single test file
uv run pytest tests/test_api.py -v

# Run a single test class
uv run pytest tests/test_api.py::TestDispatch -v

# Run a single test
uv run pytest tests/test_api.py::TestDispatch::test_dispatch_success -v

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy src/
```

---

## Test Infrastructure

### `asyncio_mode = "auto"`

`pyproject.toml` sets:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

All `async def test_*` functions are collected and run as coroutines automatically.
**Do not** add `@pytest.mark.asyncio` to individual tests — it is redundant and triggers a
`DeprecationWarning` that is treated as an error (see `filterwarnings` in pyproject.toml).

### Coverage Threshold

```toml
[tool.coverage.report]
fail_under = 84
```

The pre-push hook runs `uv run pytest --cov` and will fail if coverage drops below 84%.
**Ratchet the threshold up** when you add tests that increase coverage — never edit it
downward. The manage rollout agent enforces this rule and will halt a rollout if it detects
a commit that lowers `fail_under`.

Files excluded from the coverage measurement are listed under `[tool.coverage.run] omit` in
`pyproject.toml`.

---

## Test File Layout

| File | What it tests |
|---|---|
| `test_api.py` | FastAPI endpoint tests via `TestClient` — health, auth, dispatch, runs, cancel |
| `test_dispatch.py` | Unit tests for `dispatch.py` — workspace prep, prompt building, engine command builders, env filtering |
| `test_gtd_client.py` | HTTP client tests with mocked `httpx` — all `gtd_client.py` functions |
| `test_cancel.py` | Cancel endpoint tests — idempotence, SIGTERM/SIGKILL sequence |
| `test_db.py` | Database migration and CRUD tests |
| `test_engine_fallback.py` | Ollama health-check failure → `claude-code` fallback |
| `test_manage_recovery.py` | Manage-mode auto-recovery: retry cap, relaunch, halt |
| `test_capabilities.py` | `GET /info` and `GET /agents` endpoint tests |
| `test_attachments_staging.py` | Attachment download, sanitization, and staging |
| `test_branches.py` | Branch name generation (via protocol package) |
| `test_protocol_exports.py` | Smoke test that protocol package re-exports are intact |
| `test_rollout_planner.py` | Rollout planner LLM call and DAG output validation |

---

## Key Fixtures

### `_env` (autouse in `test_api.py`)

Sets the required environment variables and loads config. Marked `autouse=True` so every
test in the module inherits a clean config pointing at a temp workspace:

```python
@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        from agent_gtd_dispatch import config
        config.load()
        yield
```

### `client`

Wraps the FastAPI app in a `TestClient`. Must be used as a fixture (not constructed inline)
so the lifespan context manager runs:

```python
@pytest.fixture
def client():
    from agent_gtd_dispatch.main import app
    with TestClient(app) as c:
        yield c
```

### `auth_headers`

Provides the `Authorization` header matching the test API key:

```python
@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key"}
```

---

## Mocking Patterns

### Mocking GTD Client and Dispatch Module

API tests patch the **module-level references** in `main.py`, not the original definitions:

```python
@patch("agent_gtd_dispatch.main.gtd_client")
@patch("agent_gtd_dispatch.main.dispatch")
def test_dispatch_success(self, mock_dispatch, mock_gtd, client, auth_headers):
    mock_gtd.get_item = AsyncMock(
        return_value={"id": "abc123", "title": "Fix bug", "project_id": "proj1"}
    )
    mock_gtd.get_project = AsyncMock(
        return_value={"id": "proj1", "name": "MyProject", "git_origin": "git@host:repo"}
    )
    mock_dispatch.prepare_workspace = MagicMock(return_value=Path("/tmp/ws"))
    mock_dispatch.branch_name_for_item = MagicMock(return_value="feat/abc123-fix-bug")
    mock_dispatch.run_agent = AsyncMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    ...
```

**Why `agent_gtd_dispatch.main.gtd_client` and not `agent_gtd_dispatch.gtd_client`?**

Python resolves names at import time. `main.py` does `from . import gtd_client`, which
binds the local name `gtd_client` in `main`'s namespace. Patching the original module
has no effect on that already-resolved reference. You must patch the reference at its
point of use.

### Async vs Sync Mocks

- **Async functions** (any `async def` in gtd_client, dispatch, etc.) → use `AsyncMock`.
- **Sync functions** (workspace prep, subprocess calls) → use `MagicMock` (or `patch`).

```python
from unittest.mock import AsyncMock, MagicMock

mock_gtd.get_item = AsyncMock(return_value={...})   # async def get_item()
mock_dispatch.prepare_workspace = MagicMock(...)     # def prepare_workspace()
```

If you use `MagicMock` for an `async def` function, the test will fail with
`TypeError: object MagicMock can't be used in 'await' expression`.

---

## Test Style Conventions

Every test file starts with:

```python
from __future__ import annotations
```

Test classes use a `Test` prefix. Test methods have explicit `-> None` return annotations:

```python
class TestDispatch:
    def test_dispatch_at_capacity(self, client, auth_headers) -> None:
        ...
```

Docstrings are **not required** on test functions (the `D` pydocstyle rules are suppressed
for `tests/**` in pyproject.toml). Source files under `src/` do require docstrings.

---

## Pre-push Coverage Gate

The pre-push hook runs the full test suite with coverage. If coverage falls below the
threshold, the push is rejected:

```
FAILED tests/ ... (1 failed)
FAILED required test coverage of 84% not reached. Total coverage: 83.2%
```

To check coverage before pushing:

```bash
uv run pytest --cov --cov-report=term-missing
```

Look at the `MISS` column to find uncovered lines. Add tests, then update `fail_under` in
`pyproject.toml` to lock in the new baseline.
