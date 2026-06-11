# Agent GTD Dispatch — Codebase Conventions

## Overview

This document captures the patterns to follow, the anti-patterns to avoid, and the fixes
already in place for known races and bugs. Read this before modifying any source file.

---

## Coding Patterns

### Config: Module-Level Globals + `config.load()`

All configuration lives in `src/agent_gtd_dispatch/config.py` as module-level globals
initialized to zero/empty values:

```python
# config.py
DISPATCH_API_KEY: str = ""
AGENT_GTD_URL: str = ""
MAX_CONCURRENT_RUNS: int = 32
```

`config.load()` reads from the environment and populates every global at once. It is called
exactly once, in the `lifespan` startup handler:

```python
# main.py
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    config.load()
    ...
```

**Do not** import config values at module load time (i.e., do not write
`MY_CONST = config.SOME_VALUE` at the top of a file). Always read from the module
(`config.SOME_VALUE`) at call time so that tests can reload config after patching env vars.

#### Pattern for tests

```python
# In a test fixture:
env = {"DISPATCH_API_KEY": "test-key", "AGENT_GTD_URL": "http://localhost:9999", ...}
with patch.dict(os.environ, env):
    from agent_gtd_dispatch import config
    config.load()
    yield
```

The `_env` fixture in `test_api.py` is `autouse=True` — every test in that module gets a
fresh config pointing at a tmp workspace.

---

### Mocking: `unittest.mock.patch` + `AsyncMock`

Async functions must be mocked with `AsyncMock`; regular functions with `MagicMock` (or the
default `Mock`). The primary pattern for API tests:

```python
@patch("agent_gtd_dispatch.main.gtd_client")
@patch("agent_gtd_dispatch.main.dispatch")
def test_something(self, mock_dispatch, mock_gtd, client, auth_headers):
    mock_gtd.get_item = AsyncMock(return_value={...})
    mock_gtd.get_project = AsyncMock(return_value={...})
    mock_dispatch.prepare_workspace = MagicMock(return_value=Path("/tmp/ws"))
    ...
```

**Critical**: patch the reference at the site of use (`agent_gtd_dispatch.main.gtd_client`),
NOT at the definition (`agent_gtd_dispatch.gtd_client`). Python's import system resolves
the name at import time — patching the definition after the module has already been imported
has no effect on `main.py`'s reference.

---

### asyncio Tests: `asyncio_mode = "auto"`

`pyproject.toml` sets:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This means all `async def test_*` functions are run as coroutines automatically. Do **not**
add `@pytest.mark.asyncio` decorators — they are redundant and will trigger a deprecation
warning (which is treated as an error via `filterwarnings = ["error::DeprecationWarning"]`).

---

### Test Class and Method Style

All test files follow this header pattern:

```python
from __future__ import annotations
```

Test functions have explicit `-> None` return type annotations:

```python
class TestHealth:
    def test_health(self, client) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
```

Classes use a `Test` prefix. Docstrings are not required on test functions (the `D` rules
are suppressed for `tests/**` in `pyproject.toml`).

---

### `from __future__ import annotations`

Every source and test file starts with:

```python
from __future__ import annotations
```

This enables PEP 563 postponed evaluation of annotations, allowing forward references and
keeping annotation syntax consistent across Python 3.10–3.13.

---

## Anti-Patterns (May 2025 Audit)

These issues were found in a May 2025 audit. They are documented here so they are not
re-introduced.

### 1. Duplicate Constants

**Problem**: `MAX_MANAGE_RETRIES` was defined in two places — once in `config.py` and
once re-assigned in `main.py`:

```python
# main.py — was wrong; shadowed the config value
MAX_MANAGE_RETRIES = 2  # ← duplicate, out of sync with config
```

**Fix**: The value now lives authoritatively in `config.py`. `main.py` re-exports it for
backward compatibility with tests but reads it from `config`:

```python
# main.py — correct
MAX_MANAGE_RETRIES = config.MAX_MANAGE_RETRIES  # re-export from config
```

**Rule**: Constants that are configurable via env vars belong in `config.py` only. Other
modules may re-export them but must not define their own copy.

---

### 2. Engine Name Mismatch

**Problem**: The Agent GTD system (caller) and the dispatch service used different string
literals for the same engine. Agent GTD was dispatching `"claude"` but the dispatch service
registered the engine as `"claude-code"`, causing all dispatches to fail with
`"Unknown engine: 'claude'"`.

**Fix**: Both sides now use `"claude-code"`. The DB migration in `db.py` renames any legacy
`'claude'` rows to `'claude-code'` on startup:

```python
await db.execute("UPDATE runs SET engine = 'claude-code' WHERE engine = 'claude'")
```

**Rule**: Engine names are a shared protocol. They are defined in
`agent_gtd_dispatch_protocol` (the `packages/protocol` workspace package) and must not
differ between the caller and the service. If you add an engine, add it to the protocol
package first.

---

### 3. Manage Mode Using the Wrong Timeout

**Problem**: Manage-mode runs were timing out after 30 minutes (`TIMEOUT_SECONDS = 1800`)
because the worker was passing the build timeout instead of the manage timeout.

**Fix**: The dispatch handler now reads `MANAGE_TIMEOUT_SECONDS` (default 4 hours) for
`mode=manage` — after first honoring a per-request `timeout_minutes` override
(`DispatchRequest.timeout_minutes` in the protocol package), which takes precedence over
both config values:

```python
if body.timeout_minutes:
    timeout_seconds = body.timeout_minutes * 60
elif body.mode == DispatchMode.MANAGE:
    timeout_seconds = config.MANAGE_TIMEOUT_SECONDS
else:
    timeout_seconds = config.TIMEOUT_SECONDS
```

**Rule**: Never hard-code timeout values. Always read from `config.*`. The manage timeout
is intentionally much longer than the build timeout — manage agents orchestrate multiple
build waves and spend most of their time waiting.

---

## Burst-Pending Race Fix

### The Race

Before the fix, two concurrent `POST /dispatch` requests could both pass the capacity check
if there was an `await` between the check and the task creation:

```python
# BROKEN (pre-fix):
if len(_active_processes) >= config.MAX_CONCURRENT_RUNS:
    ...
    return  # queue it
await db.insert_run(run)   # ← await here! concurrent request sneaks through
task = asyncio.create_task(...)
_active_processes[run_id] = task
```

Both requests would see `len(_active_processes) < MAX_CONCURRENT_RUNS`, both would skip
queuing, both would create tasks — exceeding the capacity limit by one.

### The Fix

The fix (in the `POST /dispatch` handler in `main.py`) moves `db.insert_run()` **before** the capacity check
and places the check immediately before `asyncio.create_task()` with no `await` between:

```python
# CORRECT (post-fix):
run = Run(...)
await db.insert_run(run)           # insert before check — run is in DB regardless
_run_event_queues[run.id] = asyncio.Queue()

# ATOMIC: no await between this check and create_task / _pending_queue.append
if len(_active_processes) >= config.MAX_CONCURRENT_RUNS:
    _pending_queue.append(...)     # queue it
    return RunResponse(...)        # 200 — queued

task = asyncio.create_task(_dispatch_worker(...))   # ← no await between check and here
_active_processes[run.id] = task
```

Because asyncio is single-threaded and there is no `await` between the check and the
`create_task`, no other coroutine can run between them. The capacity check and the task
registration are effectively atomic within one event-loop tick.

### Rule

Whenever you write `if len(_active_processes) >= MAX_CONCURRENT_RUNS`, there must be **zero
`await` expressions** between that check and the subsequent mutation of `_active_processes`
or `_pending_queue`. Inserting an `await` in that gap re-introduces the race.

---

## Module Map

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, endpoints, lifespan, `_dispatch_worker`, capacity logic |
| `dispatch.py` | Workspace prep (single- and multi-repo: `prepare_workspace_multi`, `prepare_manage_workspace_multi`, `repo_dir_from_url`), push verification (`verify_pushes`, `get_head_sha`), prompt building, `run_agent()`, attachment staging |
| `engines.py` | Engine registry, command builders, env filtering, availability checks |
| `models.py` | `Run`, `RunStatus`, `RunResponse`, `EngineSwap`, `InfoResponse` |
| `db.py` | SQLite persistence (aiosqlite), migrations, `reconcile_orphans()` |
| `gtd_client.py` | HTTP client for the Agent GTD API (items, projects, comments, rollouts) |
| `config.py` | Env-var config with module-level globals + `load()` |
| `rollout_planner.py` | LLM-based wave DAG planner for `POST /plan` |
| `agent_discovery.py` | Runs `list_agents.sh`, exposes `ENGINE_NAME` and `SERVICE_VERSION` |
| `show_run_transcript.py` | CLI helper: print a run's transcript (`python -m agent_gtd_dispatch.show_run_transcript <run_id>`) |
