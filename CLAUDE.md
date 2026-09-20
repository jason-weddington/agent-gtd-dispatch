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
  main.py                # FastAPI app — endpoints, lifespan, background dispatch worker
  models.py              # Service-side Pydantic models: Run, RunResponse, EngineSwap,
                         #   InfoResponse, PushStatus, RepoPushStatus
  db.py                  # SQLite persistence (aiosqlite) for dispatch runs
  dispatch.py            # Core logic: workspace prep (incl. multi-repo), prompt
                         #   building, agent invocation, push verification
  engines.py             # Per-engine CLI command builders + env filtering (claude, kiro, ...)
  gates.py               # Pre-launch per-repo gate install: hook-manager detection, .agent-gtd/setup override, live-hook verification
  gtd_client.py          # HTTP client for the Agent GTD API (items, projects, comments)
  completion.py          # Build completion evidence: CLI result-envelope parsing
  retention.py           # Verdict-free evidence capture + age-based pruning
  config.py              # Env-var config with load() — shared service config only
  agent_discovery.py     # /agents endpoint backing — runs list_agents.sh
  rollout_planner.py     # POST /plan — in-process Anthropic call that builds rollout DAGs
  show_run_transcript.py # CLI helper to dump a run's transcript
  wave_manager/          # Manage-mode wave loop (rollout driving, recovery, watchdog)

packages/protocol/       # agent_gtd_dispatch_protocol — wire-contract models shared
                         #   with callers (RunStatus, DispatchMode, DispatchRequest,
                         #   RunResponse, PlanRequest, DagEdge, RolloutPlan,
                         #   make_branch_name); uv workspace member

tests/
  test_*.py              # One file per subsystem (api, dispatch, gtd_client, branches,
                         #   cancel, rollout_planner, manage_recovery, manage_watchdog,
                         #   push_verification, protocol_exports, ...)
```

## Key patterns

- **Config**: Module-level globals in `config.py`, loaded via `config.load()` at startup. Tests patch env vars and call `config.load()` — see `_env` fixture in `test_api.py`.
- **Mocking**: Tests use `unittest.mock.patch` and `AsyncMock`. API tests patch `agent_gtd_dispatch.main.gtd_client` and `agent_gtd_dispatch.main.dispatch` modules.
- **Async tests**: `asyncio_mode = "auto"` in pyproject.toml — no need for `@pytest.mark.asyncio`.
- **Test style**: `from __future__ import annotations`, test classes with `Test` prefix, `-> None` on methods, docstrings on source but not tests (D rules suppressed for `tests/**`).
- **Attribution**: `POST /dispatch` accepts `attribution: str | None`. When set, the spawned agent subprocess gets `AGENT_GTD_AGENT_NAME=<attribution>` in its env, so it posts GTD comments under that identity (e.g. `claude-build-abc12345`) rather than the default lead.
- **Workspace dispatch & push verification**: when the GTD project carries a `workspace_repos` list, `dispatch.py` prepares a multi-repo workspace (`prepare_workspace_multi` / `prepare_manage_workspace_multi`) with service-side branch creation, and verifies pushes service-side after the run (`dispatch.verify_pushes`, `PushStatus`/`RepoPushStatus` in `models.py`).

## Build completion contract

A BUILD run's terminal is built from MECHANICAL EVIDENCE ONLY — three things the WORKER observes for itself. Nothing the agent originates is read.

**The rule, because it has been broken twice at two different addresses:** an agent-originated signal (a comment, a file on disk, an MCP call) is a REQUEST, not a guarantee. It may inform a human. It may NOT move a run's status. No code may branch on whether the agent said anything.

The first violation was `len(comments) >= 2`, which counted GTD comments as proof of work — it counted the worker's own dispatch comment plus the agent's first `Implementing...` progress comment, so it was permanently open and recorded dead runs as successes. The fix for that was a "completion artifact": a `.dispatch/completion.json` the agent wrote declaring its own disposition. That reproduced the identical mistake one layer down, and it took a three-tier cascade (asserted -> LLM classifier -> mechanical derivation) to paper over the fact that agents frequently didn't write it. All of that is deleted. See "Why the artifact contract was deleted" below — the ending is worse than the premise.

**Leg 1 — the CLI's own result envelope.** Every claude-code argv builder emits `--output-format json` immediately before `--print`, so the transcript ends with a `{"type":"result",...}` object. This is harness-emitted, not agent-authored, which is exactly what makes it admissible. `completion.parse_result_envelope` brace-scans the last 1 MiB of the transcript (stderr is merged into the same file, so `json.loads` on the whole file cannot work) and returns the LAST parseable result object. `completion.envelope_verdict` classifies it as `ok` / `no_result_envelope` / `result_is_error` / `max_turns_exhausted`.

The max-turns branch is evaluated BEFORE the `is_error` branch, and keys on `subtype == "error_max_turns"` OR `terminal_reason == "max_turns"` — never on `stop_reason`. A real exhausted envelope carries `is_error=True` and `stop_reason='tool_use'`, so both of those are load-bearing, not style.

**Leg 2 — commits that reached origin.** `dispatch.verify_pushes` compares each repo's local HEAD against the LIVE remote ref.

**Leg 3 — the project quality gate**, re-run by the worker after push verification, so a hook bypass (`--no-verify`, a self-skipping hook) cannot slip a gate-failing tree past dispatch as a success.

The decision, in `main._dispatch_worker`:

1. envelope missing / unparseable / `is_error` / max-turns -> `failed`, under the matching prefix.
2. **zero commits across every repo -> `failed`, and the gate does NOT run.** Decided before the gate for a reason given below.
3. commits pushed + gate `passed` or `skipped_no_gate_command` -> `succeeded`.
4. commits pushed + any other gate decision -> `failed`.

**Why zero commits terminates immediately, without gating.** An unchanged tree passes a quality gate TRIVIALLY — the tree IS the base commit, and the base is green. So a green gate on a commit-less run is a fact about the base commit and says nothing whatever about the run. Treating that green as corroboration is not a hypothetical error: the worker used to `git stash push` the agent's uncommitted work before gating, so four agents that had done real work but not committed it had their trees stashed away, gated the resulting pristine base, got the trivial green, and were recorded "already satisfied" and torn down. Roughly $45 and 560 agent-turns, destroyed and reported as success. Returning at step 2 also saves the ~6 minutes a gate run costs to learn nothing.

The invariant behind all of it: **a zero-commit BUILD run is never `succeeded`.** `main._record_build_terminal` is the single choke point in front of every terminal write and coerces a violating status to `failed` with an `invariant_zero_commit_success:` prefix plus an `INVARIANT VIOLATION` ERROR log. A runtime tripwire, not a construction-time convention, because the rule already regressed once during a port and stayed invisible for weeks.

`RunStatus.already_satisfied` still exists but is reachable from **talos exit 30 ONLY** (`main._run_talos` -> `_route_already_satisfied_item`). Talos runs the project's checks itself before emitting that verdict, so the no-op is VERIFIED rather than asserted. A claude-code build can never produce it — for that engine the only thing that ever distinguished a deliberate no-op from a silent failure was the agent's own claim.

**The gate runs against the tree AS THE AGENT LEFT IT.** No stashing, ever. Stashing is a mutation performed on evidence during the act of judging it. A dirty tree is handled by the rescue path at teardown instead.

## Rescuing abandoned work

A dispatch clone is deleted at run exit, so anything the agent finished but did not push — self-made commits it never pushed, or work still in the working tree — is destroyed at that moment regardless of the run's recorded status. `main._rescue_before_teardown` runs in the teardown `finally`, before `cleanup_workspace`, on EVERY terminal path (the failure paths most of all: that is where unpushed work is likeliest).

Per repo, `dispatch.rescue_abandoned_work` detects unpushed commits or a dirty tree, then `git add -A`, `git commit --no-verify`, `git push --no-verify -u origin <branch>`. One terse GTD comment names the branch and says plainly that this is unreviewed partial work. **If the push fails the workspace is RETAINED** and the run's `error` says so — the clone is then the only copy left.

Three properties make it safe to run unattended:

- **`--no-verify` is deliberate here and deliberately UNLIKE the normal path.** `main._commit_with_retry` must run hooks so fixer hooks can fix; do not merge the two. This path commits work that is by definition incomplete and will usually fail a hook — and a blocked commit here does not produce a cleaner tree, it produces no tree at all.
- **`feat/*` branches only.** It is a retry of an access the worker already has on its success path, not a new one. Never a default branch, never a force-push.
- **Detection compares against the LIVE remote ref** (`git ls-remote`), never the clone's own `origin/main`, which is frozen at clone time and would report already-pushed work as unpushed forever.

## Turn discipline (shared by every prompt)

`dispatch._turn_discipline_block()` renders ONE block into the build prompt and both manage prompt variants. It is written as an INVARIANT about the process — you are `claude --print`, your process IS your turn, nothing can wake you after it ends — with `git push` and the project gate as ILLUSTRATIONS.

That framing is the whole point. The build prompt used to carry two bullets scoped to `git push`, and an agent that followed them perfectly still lost its run by backgrounding the project gate: **a rule that names specific commands reads as a whitelist of everything it does not name.** Do not re-narrow it.

## Why the artifact contract was deleted

Worth stating because the premise looked well-evidenced and was not. The contract motivated a three-tier disposition cascade on the strength of a measured "nine of ten claude-code runs skip the artifact" — including Opus and Sonnet, which made model compliance implausible but was rationalised anyway.

The real cause: the worker read the file with `sudo -u dispatch cat`, and **`cat` was never in the sudoers NOPASSWD list**. Every read had been denied since the contract shipped — 163 denials across three hosts, with roughly 57 of 62 runs having the file on disk the whole time. The denial surfaced to the caller as `absent`. The "agents are 0% compliant" signal was a permissions bug, and an entire LLM-classifier tier was built to guess what agents meant by a file the worker was refusing to open.

Two durable lessons, both now enforced in code:

1. **No branching on agent-originated signals.** A comment in `main`'s terminal region says so at the exact address where it has twice been violated.
2. **`tests/test_dispatch.py::TestBuildEnv::test_every_sudo_wrapped_command_is_in_sudoers_nopasswd`** parses the NOPASSWD line and asserts every literal `_sudo_wrap([...])` argv[0] in `src/` is authorised. This bug class is invisible in dev and CI because `_sudo_wrap` is the identity function when `AGENT_SUBPROCESS_USER` is empty, so an unauthorised command passes every test and is denied only on a provisioned host.

Triage classes live in `error`-string prefixes, never in new protocol members: `no_result_envelope`, `result_is_error`, `max_turns_exhausted`, `zero_commits`, `invariant_zero_commit_success`. The GTD comment on a failed build starts with `Build run failed (<prefix>)`.

Every build terminal logs one `build completion:` key=value line and persists the same evidence into the runs table's nullable `completion` column — the only durable carrier on a succeeded run, where `error` is NULL.

## Operator-facing error text

Run `error` strings carry ONE budget, `dispatch.ERROR_TEXT_MAX_CHARS` (2000), mirrored by `agent_gtd.dispatch_worker.ERROR_MSG_MAX_CHARS` on the GTD side. Both columns are TEXT; the caps are policy, not schema. Keep them equal — when they differed (a 300-char excerpt under a 500-char clip) the lower one bound silently and a fix to the other would have half-survived.

`dispatch.git_output_excerpt(proc)` builds every git/hook failure excerpt. It combines stdout AND stderr (stdout first — git forwards hook stdout on its own stream, and a stderr-only excerpt threw it away) and keeps the HEAD, eliding the middle with a marker naming the dropped character count. The head is what matters: the first failing pre-commit hook and git's own message are at the top, while a tail-only excerpt of a long hook run shows nothing but `(no files to check) Skipped` lines. `retention.py`'s `[-200:]` tail is NOT this surface and correctly keeps the tail.

## Retention

`retention.py` is deliberately verdict-free: no function in it accepts, reads, or branches on a `RunStatus`, a gate result or a push result, and `tests/test_retention.py::test_retention_is_verdict_free` asserts that against the module source and every public annotation string. The runs whose evidence matters most are exactly the ones whose outcome was recorded wrongly, so capture must not be conditional on the outcome.

`retention.capture_evidence(run_id, workspace, repos)` copies `transcript.txt`, a per-repo `patch.diff` (and an agent-written `completion.json` if one happens to be there — opportunistic, evidence-only, nothing branches on it) into `<EVIDENCE_ROOT>/<run_id>/` before any `cleanup_workspace` call, on every terminal path. Every step is individually wrapped so capture can never raise into teardown and abort the terminal DB write.

`retention.prune(active_run_ids)` is age-based only — never free-space-based. Workspace directories older than `DISPATCH_WORKSPACE_RETENTION_HOURS` are removed unless the name ends with `-<run_id>` for a live run (workspace names come in three shapes: `{repo}-{run_id}`, `ws-{run_id}` and `repos-{run_id}`, so a prefix match would protect nothing). Evidence directories older than `DISPATCH_EVIDENCE_RETENTION_DAYS` are removed unless the name equals a live run id. Config: `DISPATCH_EVIDENCE_ROOT`, `DISPATCH_EVIDENCE_RETENTION_DAYS` (30), `DISPATCH_WORKSPACE_RETENTION_HOURS` (48), `DISPATCH_RETENTION_INTERVAL_SECONDS` (3600).

Every filesystem read of an agent-created path and every git invocation added here goes through `dispatch._sudo_wrap`, because the artifact is written by the `dispatch` agent user while the worker runs as `dispatch-svc`. `transcript.txt` is NOT a precedent for this — it is opened by the parent process and is service-owned.

`show_run_transcript` falls back to `<EVIDENCE_ROOT>/<run_id>/transcript.txt` when the live-workspace glob misses, so a transcript stays retrievable after teardown. Its output now ends in a single JSON object; pipe through `jq`.

## Git workflow

- Branch from main: `git checkout -b feat/description` (or `fix/`, `chore/`)
- Conventional commits enforced on main (hook). Feature branches are free-form.
- Squash merge to main: `git checkout main && git merge --squash feat/x && git commit`
- Push to origin freely; `./deploy.sh` deploys current main to all dispatch hosts (`DISPATCH_HOSTS`, default: `pironman01 r7-research r7-server`; set `DISPATCH_HOST` to target a single host).
- `./release.sh` cuts a version (semantic-release), pushes main + tags to origin and github, then deploys.
- Pre-push hook runs full test suite with coverage (threshold from `[tool.coverage.report]` in `pyproject.toml`).
- All `uv run` in hooks uses `--frozen` to avoid rebuilding mid-hook.
- Pre-commit hooks CHECK; they never mutate files. Agents FIX. A mutating hook (e.g. `ruff --fix`, `end-of-file-fixer`) would silently destroy a talos worker's single commit attempt, since talos has no retry loop the way Claude Code's commit-retry does. See kb-03099.

## Coverage

- See `[tool.coverage.run] omit` in `pyproject.toml` for files excluded from the threshold.
- Threshold lives in `[tool.coverage.report] fail_under` — ratchet it up when you add tests.

## Deployment hosts & env files (two-user split)

Runs on two hosts (`pironman01`, `r7-research`; `ubuntu-pi-01` was removed from the
rotation 2026-06-10 — too slow). On each: the API
runs as **`dispatch-svc`**; it launches the Claude Code agent as **`dispatch`** via
`sudo -u dispatch -H`.

- **`/home/dispatch-svc/.env` is the canonical (and only) env file** on two-user-split
  hosts. (Single-user mode installs — `DISPATCH_SINGLE_USER=1` — put the env file at the
  login user's `~/.env` and strip `DISPATCH_AGENT_SUBPROCESS_USER`; see
  `docs/install.md#single-user-mode`.) It is the systemd
  `EnvironmentFile` (→ the service's `os.environ`; `config.py` reads `os.environ` only,
  no dotenv) *and* the file `setup-dispatch-host.sh` reads at provision time to inject
  KB secrets into the agent's `~/.claude.json`.
- **`/home/dispatch/.env` is vestigial** (pre-split leftover) — nothing reads it. Don't
  put vars there; safe to delete where it lingers.
- **`setup-dispatch-host.sh` runs as ROOT** (`sudo`, asserts `EUID==0`), not as
  `dispatch-svc`. It creates both users, installs the unit + sudoers, and reads
  `/home/dispatch-svc/.env` to register MCP servers (Step 4.6, driven by
  `templates/mcp-servers.sh`).
- **KB MCP secrets** live in `/home/dispatch-svc/.env` as `PERSONAL_KB_URL` /
  `PERSONAL_KB_API_KEY` / `TEAM_KB_URL` / `TEAM_KB_API_KEY` (both KB MCP servers are
  thin HTTP clients of a hosted KB web service, so no `ANTHROPIC_API_KEY` is injected
  here — that name would reach the agent's env and flip Claude Code off Max/OAuth
  billing). They are injected into the per-server `env` blocks of
  `personal-kb`/`team-kb` at provision time; `mcp-servers.sh` references the env vars,
  never literals (gitleaks-safe).

Full references: **`kb-01598`** (env-file + provisioning model — which var goes where),
`kb-01583` (how env crosses the sudo boundary at runtime), `kb-01512` (OAuth vs API
billing), `kb-01537` (install procedure).

### `--with-postgres`: local Postgres + pgvector for headless test gating

Pass `--with-postgres` to `setup-dispatch-host.sh` to provision a local PostgreSQL server
with the pgvector extension on a dispatch host. This step is **opt-in and idempotent** —
re-running on an already-provisioned host is a no-op. Without this flag, no Postgres work
is performed and the existing host state is unchanged.

Postgres is reachable **only** over the local Unix socket with peer auth — no TCP
listener, no `pg_hba.conf` edits, no `listen_addresses` change. Postgres's packaged
defaults are left alone; the only server-side objects created are the role and the
`template1` extension. (An earlier revision of this step created a separate `kbtest`
role with loopback trust auth and edited `pg_hba.conf`/`listen_addresses` — that was
reworked per a pinned decision: trust-on-loopback would let any local account on the
host connect as a CREATEDB role, which is worse than the ambient-DSN risk it was
meant to avoid.)

What it does:
1. Installs `postgresql` + `postgresql-contrib` via the OS package manager (apt or dnf/yum).
2. Installs pgvector — tries the distro package (`postgresql-XX-pgvector` on apt;
   `pgvector_XX` on dnf) and falls back to building from source if unavailable.
3. Enables and starts the `postgresql` systemd service.
4. Creates a PG role named **`AGENT_USER`** (default: `dispatch`) with `LOGIN CREATEDB` —
   no superuser, no password. The role name intentionally matches the OS agent user so
   **peer authentication works on the Unix socket with no password and no
   `pg_hba.conf` changes**.
5. Runs `CREATE EXTENSION IF NOT EXISTS vector` in **`template1`** (as superuser, since
   pgvector is not a trusted extension) so every database created afterwards —
   including the throwaway `kb_test_<uuid8>` databases the test suite creates and
   drops itself — inherits it automatically. No dedicated test database is created
   here: kb-core's `conftest.py::pg_temp_db` fixture connects to the maintenance
   `postgres` database, does its own `CREATE DATABASE` / `DROP DATABASE ... WITH
   (FORCE)`, and the unprivileged test role never needs to run `CREATE EXTENSION`.
6. Writes **`KB_TEST_DATABASE_URL=postgresql:///postgres`** and
   **`KB_REQUIRE_POSTGRES_TESTS=1`** into `/home/dispatch-svc/.env` (the canonical
   service env file) using the same Python line-rewrite pattern as `TALOS_BIN` —
   preserving all other keys.
7. When `--smoke` is also passed: connects **as the `AGENT_USER` OS user over the
   socket** (peer auth), creates a throwaway database, confirms the `vector` extension
   is present WITHOUT running `CREATE EXTENSION` (proving `template1` inheritance,
   not a per-database install), creates a table with a `vector(1024)` column, inserts
   and selects one row, then drops the database.

**`KB_TEST_DATABASE_URL`** is the DSN headless build agents must receive for the
`@pytest.mark.postgres` test suite (e.g. kb-core) to run instead of skip.
Connection format: Unix socket, maintenance DB, OS user = PG role → no password needed;
the suite creates/drops its own throwaway databases against this DSN.
**`KB_REQUIRE_POSTGRES_TESTS`** is the flag a consuming repo's `conftest.py` can read to
turn a missing DSN into a hard failure instead of a silent skip (wiring that flag into
kb-core's own conftest is a separate, later item — see the GTD board). Both vars are
written to the service env file **and** wired into `engines.py` `COMMON_ENV_KEYS` and
the sudoers `env_keep` (`templates/sudoers-dispatch-svc.tmpl`), so they reach the agent
subprocess — including the post-run gate (`ruff`/`mypy`/`pytest`) — during dispatch
runs; writing the env file alone is a no-op, since `sudo -u dispatch` strips any var
not in `env_keep`. A host's `dispatch-api` service must be **restarted**
(`systemctl restart dispatch-api`) after (re-)provisioning for a freshly-written env
var to take effect if the systemd unit file itself didn't change (Step 6 only
restarts when the rendered unit differs from what's installed).

**To provision on a dispatch host:**
```bash
# On pironman01 or r7-research (re-run with existing args + --with-postgres):
sudo ./setup-dispatch-host.sh --with-postgres --smoke

# Dry-run preview (no mutations):
sudo ./setup-dispatch-host.sh --with-postgres --dry-run
```

## talos-update.sh — fast binary bump for the dispatch fleet

`talos-update.sh` is the **operator-run fast bump channel** for the `talos` engine binary.
It pulls a versioned, pre-built binary from pi-04's artifact store and installs it on every
dispatch host, per-arch and version-checked.  No cargo build, no Rust toolchain required.

### What it does

1. Resolves the target version from `<TALOS_ARTIFACT_BASE>/latest` (or `--version <TOKEN>`).
2. SSHes into each dispatch host in `DISPATCH_HOSTS`.
3. On each host: maps `uname -m` → arch, reads the installed `talos --version` token.
4. **Skips** the host if already at the target version (fully idempotent).
5. Downloads `<BASE>/<TOKEN>/<arch>/talos` via `curl -fsSL` to a tempfile.
6. Installs the binary via `sudo install -m 0755 -o <AGENT_USER> -g <AGENT_GROUP>`.
7. Verifies the freshly-installed binary reports the expected version token.
8. **Does NOT restart dispatch-api** — talos is a fresh subprocess per dispatch run,
   so the new binary is picked up automatically on the next run.

### Pi-04 artifact contract

```
<BASE>/latest                       → one-line file: current version token (e.g. 0.1.0-ga1b2c3d)
<BASE>/<TOKEN>/<arch>/talos         → pre-built binary  (arch ∈ {x86_64, aarch64})
```

`talos --version` output format: `talos <TOKEN>` — extract with `awk '{print $2}'`.

### Environment variables

| Variable              | Default                                        | Notes |
|-----------------------|------------------------------------------------|-------|
| `DISPATCH_HOSTS`      | `pironman01 r7-research r7-server`                       | Space-separated SSH targets |
| `DISPATCH_HOST`       | *(unset)*                                      | Single-host override (back-compat) |
| `TALOS_ARTIFACT_BASE` | `https://pypi.lab.jasonweddington.com/talos`   | **PROVISIONAL** — update once the pi-04 Caddy URL is finalised (pypi.lab vs talos.lab) |
| `AGENT_USER`          | `dispatch`                                     | OS user that owns the binary |
| `AGENT_GROUP`         | *(same as AGENT_USER)*                         | OS group for the binary |

### Usage

```bash
./talos-update.sh                          # bump all hosts to latest
./talos-update.sh --version 0.1.0-ga1b2c3d  # pin a specific version
DISPATCH_HOST=pironman01 ./talos-update.sh   # single host
TALOS_ARTIFACT_BASE=https://talos.lab.jasonweddington.com ./talos-update.sh
```

### Relationship to setup-dispatch-host.sh --with-talos

`setup-dispatch-host.sh --with-talos` is the **from-scratch bootstrap/fallback**: it clones
`harness-design`, runs `cargo build --release -p talos`, and installs the locally-built
binary (works on a fresh host with no artifacts pre-published).

`talos-update.sh` is the **fast bump channel**: operator runs it after a new talos version
is published to pi-04 (by the harness-design CI).  No Rust toolchain, no source clone,
no cargo build — just a curl + install.  Use `setup-dispatch-host.sh --with-talos` for
initial provisioning or as a fallback if the artifact server is unreachable.

### Verification caveat

End-to-end testing requires a binary to be published on pi-04 (harness-design item
953fd927).  Until artifacts exist, verify the script locally with:
```bash
bash -n talos-update.sh          # syntax check
shellcheck talos-update.sh       # static analysis
./talos-update.sh --help         # usage output
```
