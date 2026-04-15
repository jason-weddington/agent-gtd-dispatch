# agent-gtd-dispatch

Dispatch worker API for [Agent GTD](https://github.com/jason-weddington/agent-gtd) — runs headless Claude Code agents on isolated infrastructure.

## What it does

Receives dispatch requests via a REST API, clones the target project repo, runs Claude Code as a headless subprocess, and reports results back to the GTD system.

## Dev setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url>
cd agent-gtd-dispatch
uv sync
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type post-commit --hook-type pre-push
```

## Running locally

The API requires these environment variables:

```bash
export DISPATCH_API_KEY="your-api-key"
export AGENT_GTD_URL="https://your-gtd-instance"
export AGENT_GTD_API_KEY="your-gtd-api-key"
export ANTHROPIC_API_KEY="your-anthropic-key"       # optional if using OAuth
export DISPATCH_WORKSPACE_ROOT="/path/to/workspaces" # default: ~/workspace
```

```bash
uv run uvicorn agent_gtd_dispatch.main:app --host 0.0.0.0 --port 8001
```

## Tests

```bash
uv run pytest -v
uv run pytest --cov --cov-report=term-missing
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check + active run count |
| POST | `/dispatch` | Start a dispatch run (body: `item_id`, `max_turns`) |
| GET | `/runs` | List runs (query: `item_id`, `status`, `limit`) |
| GET | `/runs/{run_id}` | Get a specific run |
| POST | `/runs/{run_id}/cancel` | Cancel a running dispatch |

All endpoints except `/health` require Bearer token auth matching `DISPATCH_API_KEY`.

## License

MIT
