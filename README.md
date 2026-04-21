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

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Health check + active run count |
| GET | `/info` | None | Engine identity and service version |
| GET | `/agents` | Bearer | List agents advertised by `list_agents.sh` |
| POST | `/dispatch` | Bearer | Start a dispatch run (body: `item_id`, `max_turns`) |
| GET | `/runs` | Bearer | List runs (query: `item_id`, `status`, `limit`) |
| GET | `/runs/{run_id}` | Bearer | Get a specific run |
| POST | `/runs/{run_id}/cancel` | Bearer | Cancel a running dispatch |

All endpoints marked **Bearer** require an `Authorization: Bearer <DISPATCH_API_KEY>` header.

### `/info` response

```json
{ "engine": "claude-code", "version": "1.6.0" }
```

### `/agents` response

```json
{ "agents": [ { "name": "planner", "description": "Designs implementation plans" } ] }
```

Returns an empty list if `list_agents.sh` is missing, not executable, times out, or exits non-zero. Never returns a 5xx.

## Agent Discovery

The `/agents` endpoint delegates to a user-supplied shell script so that each deployment can expose engine-specific agent lists without leaking engine internals into this OSS repo.

### `list_agents.sh` location

The dispatch service looks for the script at:

```
~/.config/agent-dispatch/list_agents.sh
```

where `~` resolves to the dispatch service user's home directory. The path is hard-coded; no configuration knob is provided.

### Script contract

- **Invocation**: no arguments, empty stdin, 5-second wall-clock timeout. Working directory is the script's parent directory (`~/.config/agent-dispatch/`).
- **Output (stdout)**: one agent per line. Two valid line shapes:
  ```
  <name>
  <name><TAB><description>
  ```
  - `<name>` must match `^[A-Za-z0-9_-]+$`. Invalid names drop the line.
  - `<description>` is everything after the first tab, whitespace-trimmed. Internal tabs are normalised to spaces.
  - Blank lines and lines whose first non-whitespace character is `#` are ignored.
  - Lines longer than 4 KiB are truncated.
- **Exit codes**: `0` = success; anything else = failure (empty list returned, stderr logged).
- **Encoding**: UTF-8 expected. Invalid UTF-8 lines are dropped.

**Example stdout**:
```
code-reviewer	Reviews PRs for quality issues
planner	Designs implementation plans
# comment — this line is ignored
scratch
```

### Reference implementation for claude-code

`examples/list_agents.claude-code.sh` scans `~/.claude/agents/*.md` and `<cwd>/.claude/agents/*.md`, extracts `name` and `description` from YAML frontmatter using `awk`, and emits one line per agent. Copy it to `~/.config/agent-dispatch/list_agents.sh` and `chmod +x` to enable agent discovery.

### Private / work engines

Deployments wrapping a non-public engine (e.g. an internal AWS Kiro instance) can supply their own `list_agents.sh` without modifying this repo. The script just needs to emit lines in the contract format above.

## License

MIT
