# agent-gtd-dispatch

Dispatch worker API for [Agent GTD](https://github.com/jason-weddington/agent-gtd) — runs headless Claude Code or Kiro CLI agents on isolated infrastructure.

## What it does

Receives dispatch requests via a REST API, clones the target project repo, runs a coding agent as a headless subprocess, and reports results back to the GTD system.

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

## Steering your "Tech Lead" Agent

Agent GTD + this dispatch service are designed for a two-tier agent workflow: an interactive **tech lead** agent in your terminal (Claude Code or equivalent) grooms tasks, dispatches the well-scoped ones to headless agents here, and reviews the resulting branches. The tech lead is the control plane; the headless agents are the muscle.

Copy the block below into your project's `CLAUDE.md` (or equivalent agent instructions) to steer the tech lead into this pattern. Adjust anything that doesn't match your setup — it's a starting point, not gospel.

```markdown
## Working with headless dispatch

This project uses Agent GTD + agent-gtd-dispatch for a two-tier agent workflow:

- **Interactive (this session)** — planning, architecture, ambiguous bugs,
  reviewing dispatched branches. The control plane.
- **Autonomous dispatch** — clearly-scoped tasks (mechanical refactors,
  known-root-cause bug fixes, test additions, small features with an existing
  pattern to copy) get dispatched to headless agents via
  `dispatch_item(item_id=…)`. They push feature branches and comment on the
  task when done.

Vague tasks waste turns. Well-groomed tasks get one-shotted.

### Grooming before dispatching

Tasks in GTD start in `new`. Grooming moves them to `ready`. A task is ready
when:

- Acceptance criteria are clear and testable
- Files to modify are identified
- Scope boundaries are explicit (what *not* to touch)
- Verification steps are defined

If intent is ambiguous, clarify before grooming — don't guess. Watch for
dispatch opportunities as you work interactively and capture them as groomed
items; an insight discovered in flow is almost free to turn into a
shovel-ready task.

### Dispatching

Once a task is groomed, call `dispatch_item(item_id=…)` directly — no UI
detour. Before dispatching, confirm:

1. The task is groomed (AC, file paths, scope boundaries present).
2. It doesn't conflict with files being edited in the interactive session.

### Autonomous wave rollout

When a feature has been broken into sub-tasks and it's time to ship
end-to-end:

1. **Size each task.** Inline in the interactive session only if trivial
   (<5 lines) or complex enough to need the live context. Everything else
   dispatches.
2. **Sequence for concurrency.** Read each task's "files to modify" list.
   Non-overlapping tasks run in parallel up to the dispatch concurrency cap.
   File overlaps or dependency chains become successive waves.
3. **Pick sensible first-wake timers.** Most well-groomed backend/frontend
   tasks finish in 10–20 minutes. Don't poll shorter than ~20 minutes on
   re-checks — short polls thrash the interactive session's prompt cache;
   idle wait is cheaper than cache churn.
4. **Each wake cycle (in order):**
   - Poll every dispatched run.
   - For each success: fetch branch → diff → squash-merge to `main` with a
     clean conventional-commit message.
   - Fix lint / format / merge conflicts **inline** — only redispatch if
     the agent's logic is wrong or missed scope.
   - Push `origin main`.
   - Mark items complete; delete local + remote feature branches.
   - Dispatch the next wave.
   - Set the next wakeup.
5. **Between waves,** don't touch anything with shared blast radius —
   deployments, force pushes, tags, production promotions — without
   explicit approval.
6. **Stop condition:** all sub-items merged. Summarize what shipped and
   hand back.

### Branch hygiene

After merging a dispatched branch, delete both local and remote copies:

    git branch -D feat/<branch>             # local, if checked out
    git push origin --delete feat/<branch>  # remote — the important one

Headless agents push to origin; stale `feat/` branches pile up fast
otherwise.

### Repo bootstrapping

Every repo that may be dispatched needs a `CLAUDE.md` and `README.md` so
the headless agent can orient in one read instead of burning 15 turns
exploring:

- **`CLAUDE.md`** — build/test commands, project layout, key patterns,
  where to put new code. One page.
- **`README.md`** — dev setup (install deps, env vars, run tests). The
  agent clones fresh every time.

Don't over-document; under-document and the agent wastes tokens grepping
around.
```

## License

MIT
