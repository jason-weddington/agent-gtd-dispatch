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
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

## Running locally

The API requires these environment variables:

```bash
export DISPATCH_API_KEY="your-api-key"
export AGENT_GTD_URL="https://your-gtd-instance"
export AGENT_GTD_API_KEY="your-gtd-api-key"
export ANTHROPIC_API_KEY="your-anthropic-key"        # required — see note below
export DISPATCH_WORKSPACE_ROOT="/path/to/workspaces" # default: ~/workspace
```

> **`ANTHROPIC_API_KEY` is required** — the service raises at startup without it. It
> powers the in-process rollout planner (`POST /plan`) and is deliberately **not**
> forwarded to Claude Code subprocesses; those authenticate via
> `CLAUDE_CODE_OAUTH_TOKEN` or an interactive `claude login`. See
> "Notes on `ANTHROPIC_API_KEY`" in [docs/setup.md](docs/setup.md).

Then start the API:

```bash
uv run uvicorn agent_gtd_dispatch.main:app --host 0.0.0.0 --port 8100
```

### Generating `DISPATCH_API_KEY`

`DISPATCH_API_KEY` is a shared secret — any high-entropy string the dispatch
service and its callers both know.

**Local dev**: mint one by hand and export it in your shell:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

**Installed hosts**: leave `DISPATCH_API_KEY` empty (or absent) in the env file —
Step 3.5 of `setup-dispatch-host.sh` auto-mints it into the service `.env` and
prints it in an ACTION REQUIRED banner for registration in the GTD UI. It never
clobbers an existing value. See
[docs/install.md](docs/install.md#dispatch_api_key-auto-minting-step-35).

Put the same value in every caller's config (e.g. the **Agent Dispatch** host
entries in the Agent GTD Settings page — paste it into the API Key field for
each host).

Rotating: on an installed host, clear the value in the service `.env` and re-run
the installer (Step 3.5 mints a fresh key); in dev, just pick a new value and
restart. Then update each caller. Mismatches show up as `401 Not authenticated`
on Bearer endpoints.

## Ollama local inference

Set `OLLAMA_BASE_URL` to route `claude-code-ollama` dispatches through a local Ollama instance instead of the Anthropic API. If Ollama is unreachable at dispatch time, the engine falls back to `claude-code` with a comment posted to the GTD item.

```bash
export OLLAMA_BASE_URL="http://192.168.1.52:11434"   # root URL — no /v1 suffix
export OLLAMA_DEFAULT_MODEL="qwen3.6:35b"            # default if omitted
```

The URL must be the **root** Ollama URL (e.g. `http://host:11434`). Do **not** append `/v1` or any path — Ollama 0.14+ exposes the Anthropic Messages API at the root path, while `/v1` is the OpenAI-compatible surface which Claude Code does not speak.

## Process model

The dispatch service uses a two-user architecture to enforce POSIX isolation between the service process and the agent subprocesses it spawns.

- **`dispatch-svc`** runs the FastAPI service and owns its working copy at `~/agent-gtd-dispatch`. No agent can write to this directory.
- **`dispatch`** is the unprivileged agent user. Agent subprocess workspaces live at `/home/dispatch/workspace/{run_id}/`. The service cannot write to the agent user's home directory.

When `DISPATCH_AGENT_SUBPROCESS_USER=dispatch` is set, the service spawns agent subprocesses via `sudo -u dispatch -H`, which sets `HOME=/home/dispatch` automatically. Git clone, checkout, and the agent CLI all run as `dispatch`. Workspace cleanup uses `sudo -u dispatch rm -rf` so only the agent user can delete its own files.

When `DISPATCH_AGENT_SUBPROCESS_USER` is empty (the default in dev/test), no user-switching occurs and the service runs everything under its own user — preserving the existing single-user behaviour.

## Deployment

The service uses a **two-user architecture** to enforce POSIX isolation between the API process and the agent subprocesses it spawns:

- **`dispatch-svc`** runs the FastAPI service and owns the working copy at `~/agent-gtd-dispatch`. No agent subprocess can write to this directory.
- **`dispatch`** is the unprivileged agent user. Agent workspaces live at `/home/dispatch/workspace/{run_id}/`. A narrow sudoers fragment (`/etc/sudoers.d/dispatch-svc`) allows `dispatch-svc` to spawn specific commands as `dispatch` — no `ALL=(ALL)` escalation.

To bootstrap a fresh host or migrate an existing one:

```bash
sudo ./setup-dispatch-host.sh --env-file /path/to/.env
```

> **Adapt this: the git remotes default to the maintainer's homelab git server.**
> `setup-dispatch-host.sh` clones two repos and defaults both remotes to
> `git@ubuntu-vm01:repos/...`. On any other machine, override them with
> `DISPATCH_REPO_URL` (this repo) and `AGENT_GTD_REPO_URL` (the agent_gtd repo):
>
> ```bash
> sudo DISPATCH_REPO_URL=git@your-git:org/agent-gtd-dispatch \
>      AGENT_GTD_REPO_URL=git@your-git:org/agent_gtd \
>      ./setup-dispatch-host.sh --env-file /path/to/.env
> ```
>
> Known limitation: Step 1/2 of the script run `ssh-keyscan ubuntu-vm01`
> unconditionally to populate `known_hosts` — that host is not overridable via env
> var. On a non-homelab git server the keyscan fails with a warning; pre-populate
> the service and agent users' `~/.ssh/known_hosts` with your git server's host key
> (or edit the script) before the clone steps run.

See **[docs/install.md](docs/install.md)** for the full install guide, env-file reference, rollback procedure, and troubleshooting.

### Single-user install (developer machines)

The two-user split needs dedicated `dispatch-svc`/`dispatch` accounts plus a sudoers
fragment. On a personal/developer machine where everything should run under your own
login account, install in **single-user mode** instead:

```bash
# Canonical form: name the var explicitly so sudo's env-stripping doesn't drop it
sudo --preserve-env=DISPATCH_SINGLE_USER DISPATCH_SINGLE_USER=1 \
    ./setup-dispatch-host.sh --env-file /tmp/dispatch.env
```

Trade-off: no extra users are created and no sudoers fragment is installed — which also
means **no POSIX isolation** between the service and the agent subprocesses it spawns.
You still get the systemd unit, auto-minted `DISPATCH_API_KEY`, MCP registration, and
pre-commit template setup. The installer guards against mixing modes on one host. Full
details: [docs/install.md — Single-user mode](docs/install.md#single-user-mode).

### MCP servers for the agent user

The installer registers three MCP servers for the `dispatch` (agent) user — plus a
fourth, `team-kb`, when `TEAM_KB_DATABASE_URL` is set in the service `.env` — so that
dispatched Claude Code agents have tool access to GTD, the knowledge bases,
and AWS documentation:

| Server | Purpose |
|---|---|
| `agent-gtd` | GTD items, comments, and dispatch — lets agents post comments and update items via MCP rather than raw `curl` |
| `personal-kb` | Knowledge base lookups (decisions, lessons learned, project conventions) |
| `team-kb` | Team knowledge base (same package, team database) — registered only when `TEAM_KB_DATABASE_URL` is present in the service `.env` |
| `aws-documentation-mcp-server` | AWS docs for any AWS-related implementation work |

Registration is **per-host and per-user** (`--scope user`, writes to
`/home/dispatch/.claude.json`). Step 4.6 of the installer handles this automatically.
It also injects `KB_ANTHROPIC_API_KEY` (from the service `.env`) into both KB servers'
per-server `env` blocks as their `ANTHROPIC_API_KEY` — the KB servers make their own
LLM calls. Never name this variable `ANTHROPIC_API_KEY` in the service `.env` itself:
that name would reach the agent subprocess env and flip Claude Code off OAuth/Max
billing onto pay-as-you-go API billing.

**Config file**: `templates/mcp-servers.sh`

This Bash-sourceable file defines a single `MCP_SERVERS` array. Each entry uses the
format `"<name>|<args-after-claude-mcp-add-NAME>"`. The installer reads this file and
runs `claude mcp add <name> <args>` for each entry (with an idempotent remove-first
pattern so re-running is safe).

**To add a new MCP server:**

1. Append an entry to `MCP_SERVERS` in `templates/mcp-servers.sh`.
2. Re-run `sudo ./setup-dispatch-host.sh` on each host — Step 4.6 will register the
   new server and leave existing registrations unchanged.

Alternatively, register it manually on a specific host:
```bash
sudo -u dispatch -H bash -lc "claude mcp add <name> --scope user <args>"
```

**To verify registration on a host:**
```bash
ssh <HOST> 'sudo -u dispatch -H bash -lc "cd /home/dispatch && claude mcp list"'
# → all registered servers listed (three, or four with team-kb)
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
| POST | `/plan` | Bearer | Plan a rollout DAG for a set of items (body: `PlanRequest`) |
| POST | `/dispatch` | Bearer | Start a dispatch run (body: protocol `DispatchRequest` — `item_id?` (null for manage runs), `max_turns`, `engine`, `mode`, `agent_name?`, `timeout_minutes?`, `rollout_id?`, `attribution?`) |
| GET | `/runs` | Bearer | List runs (query: `item_id`, `status`, `limit`) |
| GET | `/runs/{run_id}` | Bearer | Get a specific run |
| GET | `/runs/{run_id}/transcript` | Bearer | Get a run's agent transcript |
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

## Dispatch modes and rollouts

Every `/dispatch` call carries a `mode` field: `plan` (groom a single item), `build` (implement and push a feature branch for a single item), or `manage` (drive a whole rollout — a planned wave of items in one project — end-to-end). The manage mode dispatches each child build itself, runs quality gates, squash-merges to `main`, and advances the rollout DAG built by `POST /plan`. For the full reference — DAG construction, the manager's wave loop, the `update_rollout_state` replacement contract, recovery semantics on unexpected manage exits, quality gates and sensitive-area guardrails — see **[docs/rollouts.md](docs/rollouts.md)**.

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

## Protocol package

The `agent_gtd_dispatch_protocol` package lives in `packages/protocol/` and is the single source of truth for the dispatch wire contract. It exports eight names:

- `RunStatus` — run lifecycle enum
- `DispatchMode` — run mode enum (`plan` / `build` / `manage`)
- `DispatchRequest` — `POST /dispatch` request body
- `RunResponse` — run read model returned by run endpoints
- `PlanRequest` — `POST /plan` request body
- `DagEdge` — directed dependency edge in a rollout DAG
- `RolloutPlan` — planner output: nodes + edges + model name
- `make_branch_name` — canonical `feat/<id>-<slug>` branch naming helper

### Using from agent-gtd (or any external caller)

Add the package as a git subdirectory dependency in your `pyproject.toml`:

```toml
[project]
dependencies = [
    "agent-gtd-dispatch-protocol",
    ...
]

[tool.uv.sources]
agent-gtd-dispatch-protocol = { git = "<github-url>", subdirectory = "packages/protocol" }
```

Replace `<github-url>` with the URL of this repository. Both sides then validate against one schema definition — field renames or new required fields are caught immediately.

## License

MIT
