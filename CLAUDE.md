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
                         #   (leg 1) and the agent completion artifact (leg 2)
  disposition.py         # Three-tier run disposition: the inferred (bounded-evidence
                         #   classification) and derived (mechanical) tiers beneath
                         #   the agent's own assertion
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

A BUILD run's terminal comes from three independent legs. None of them is "did the agent post a comment" — that heuristic counted the worker's own dispatch comment plus the agent's first `Implementing...` progress comment, so it was permanently open and recorded dead runs as successes.

Leg 1 is the CLI's own result envelope. Every claude-code argv builder emits `--output-format json` immediately before `--print`, so the transcript ends with a `{"type":"result",...}` object. `completion.parse_result_envelope` brace-scans the last 1 MiB of the transcript (stderr is merged into the same file, so `json.loads` on the whole file cannot work) and returns the LAST parseable result object. `completion.envelope_verdict` classifies it as `ok` / `no_result_envelope` / `result_is_error` / `max_turns_exhausted`.

The max-turns branch is evaluated BEFORE the `is_error` branch, and keys on `subtype == "error_max_turns"` OR `terminal_reason == "max_turns"` — never on `stop_reason`. A real exhausted envelope carries `is_error=True` and `stop_reason='tool_use'`, so both of those are load-bearing, not style.

Leg 2 is the agent-authored artifact at the fixed absolute path `<workspace>/.dispatch/completion.json`. `completion.read_completion_artifact` returns `(artifact, reason_literal)` and rejects with an explicit literal (`absent`, `not_json`, `not_object`, `unknown_schema_version`, `unknown_disposition`, `missing_reason`, `missing_decision_needed`, `oversize`, `ambiguous_location`) so telemetry distinguishes malformed from absent while the terminal stays binary. The build prompt interpolates the ABSOLUTE path because the build steps tell the agent to `cd` into a repo directory; as belt-and-braces the reader also accepts exactly one `*/.dispatch/completion.json` hit under the workspace.

Leg 3 is the invariant: a zero-commit BUILD run is never `succeeded`. `main._record_build_terminal` is the single choke point in front of every terminal write and coerces a violating status to `failed` with an `invariant_zero_commit_success:` prefix plus an `INVARIANT VIOLATION` ERROR log. This is a runtime tripwire, not a construction-time convention, because the rule already regressed once during a port and stayed invisible for weeks.

Leg 2 has exactly one exception, added after three `claude-code-glm` runs that implemented their item, pushed, passed the gate and were still recorded `failed`: an ABSENT or unparseable artifact on a run that DID push commits is not fatal by itself. Writing the artifact is a cooperative act and weak models intermittently skip it, so such a run takes the `_unasserted_path` — it falls through to the post-run gate, and the gate decides. Gate `passed` -> `succeeded` (same success writer) with `unasserted: true` in the completion blob, a WARNING `unasserted build run:` line carrying the engine for later per-engine aggregation, an explanatory GTD comment, and a guarded best-effort nudge of the item to `review` (never when it is already `review`/`done`). `skipped_no_gate_command` is admitted on the same terms: the project has NO `gate_command`, which is inconclusive rather than negative, and the ASSERTED path already succeeds there (`_gate_result` is None, so the "gate did not pass" branch never fires) — treating it as negative made the same project, the same absent gate and the same pushed commits produce opposite verdicts based only on whether the agent happened to write a file. Every OTHER non-passing decision — `failed`, `timed_out`, `launch_error`, `skipped_no_pushed_repo` — is `failed` under the existing `stopped_without_assertion` prefix naming the decision; those are verdicts ABOUT the tree, not the absence of one. Zero commits no longer fails outright — it now goes through the three tiers below, which is how an unwritten `already_satisfied` is recovered — and a non-`ok` envelope verdict still outranks all of this: the envelope is the CLI's own statement about how the process ended, and the leniency applies only to the agent-authored artifact. The root cause was first diagnosed as model compliance; that was WRONG and must not be re-derived. Measured against the honest denominator (runs created after the contract shipped), nine of ten claude-code-family runs skipped the artifact, including Opus and Sonnet. `_build_build_prompt` contradicted itself: the `## Completion Artifact` section opened with "Your LAST action on EVERY path" and was then followed by sixty-odd lines ending in Reporting's numbered on-success list, so agents correctly finished on the last instruction they were given. The prompt is now ordered Rules -> Reporting -> Important -> Completion Artifact (with the No-Op Case as a subsection of it), Reporting's on-success list hands off to the artifact as its last step, and the stale "recorded as a FAILURE regardless of what it pushed" threat is replaced by the true consequence. Text guards in `tests/test_dispatch.py::TestBuildPromptCompletionArtifact` pin that ordering — no behavioural test can catch a prompt-text regression. The `_unasserted_path` remains a safety net, not a replacement for the agent's own assertion.

The only non-failure zero-commit outcome is the `already_satisfied` terminal: artifact present with `disposition: already_satisfied` and a non-empty `reason`, zero commits, and a gate that did not fail. On that path the post-run gate RUNS despite zero pushed repos (a no-op claim on a red repo is a failure), the worker sets the item to `review` best-effort, and the item is never completed. An ungated project records `gate=skipped_no_gate_command` and still lands `already_satisfied`.

The agent's terminal actions are collapsed to ONE bounded pick: the artifact's `disposition`. Everything that follows from that pick is materialized by the worker, not by prompt text. The build prompt no longer tells the agent to set the item status, to verify the remote ref with `git ls-remote` after pushing (push verification already does that, more rigorously, and can fail the run), to post scripted `Implementing...` / `Running tests...` milestones, or to avoid `git add`-ing the staged attachments directory (`_setup_git_exclude` takes a per-call `extra_lines` and excludes `{run_id}-attachments/` before the subprocess starts — the module-level `_GIT_EXCLUDE_LINES` stays static so no run's paths leak into the next). The foreground-push rule SURVIVES: it is turn discipline, not verification. So does the agent's final summary comment, which carries judgment the worker cannot synthesize.

Disposition -> item status, owned by the worker: `done` -> `review` and an unasserted no-artifact success -> `review`, both via `_nudge_item_to_review` (read-then-guard, never regressing an item already `review`/`done`, PATCH failure never flips the terminal); `already_satisfied` -> `review` via `_route_already_satisfied_item`; `blocked`/`failed` -> item UNTOUCHED, because a blocked run that also set the item to `review` was two signals for one meaning. Every successful build terminal also posts `build_completion_comment` — branch, per-repo commit counts, push outcomes, gate decision, enriched with the artifact's `summary`/`reason`/`decision_needed` when there is one — so a run that writes no artifact still leaves the reviewer the mechanical facts instead of silence.

## Three-tier run disposition (asserted -> inferred -> derived)

`disposition.py` closes the gap the completion contract leaves open: the artifact is a volunteered side-effect file, and a volunteered side effect can always be skipped. A BUILD run's disposition now comes from the first tier that can supply one, and the TIER IS RECORDED alongside the verdict.

1. **asserted** — the agent's artifact parsed. Always wins, and `disposition.py` is not consulted AT ALL: no cost, no latency, no second opinion. `tests/test_post_run_gate.py::TestThreeTierDispositionRouting` asserts no classification call is made and that an artifact-present run behaves exactly as it did before.
2. **inferred** — the artifact is absent or unparseable, so the WORKER (never the agent — that is what makes it unskippable) hands a bounded evidence envelope to a small model and asks for a pick from the closed set `done` / `already_satisfied` / `blocked` / `failed` plus a one-line reason. Anything else — prose, a fifth value, a truncated object — is a classification FAILURE, not a disposition.
3. **derived** — the classifier was disabled, had no credential, timed out, errored or answered unusably, so `derive()` computes from the FULL gate-decision table, enumerated explicitly so a future decision string cannot fall silently into the wrong bucket (`GATE_DECISIONS` pins the set, and an unrecognized value derives `failed` with a WARNING): `passed` + commits -> `done`; **no commits -> `failed`, whatever the gate said**; `skipped_no_gate_command` + commits -> `done` (no gate configured is INCONCLUSIVE, not negative — the verdict rests on the pushed commits alone, and the reason text says so rather than claiming a gate "did not pass" when none ran); a gate that RAN and `failed`/`timed_out`/`launch_error` -> `failed`; `skipped_no_pushed_repo` -> `failed`. Its range is TWO values, `done` and `failed`, under every combination of inputs. It NEVER returns `blocked` — nothing mechanical distinguishes "a human must decide" from "it broke", and a wrong `blocked` parks an item on a human with no question to answer. It NEVER returns `already_satisfied` either, for the same reason and more strongly: it shipped mapping zero commits + a passing gate to `already_satisfied` and within hours reported a run that produced nothing (run `c2a8072e2e25`) to its lead as work already done. An unchanged tree passes a test-suite gate TRIVIALLY — it is the base commit, and the base is green — so "already done" and "did nothing" leave byte-identical mechanical evidence, and `already_satisfied` is not a neutral label: it routes the item to `review`, records a rollout child as skipped so the wave ADVANCES, and tells a human the work exists. `failed` is loud and re-dispatchable; a false `already_satisfied` is a silent hole in a wave that looks like progress. The zero-commit derived comment says plainly that the outcome is UNKNOWN and the item needs a human or a re-dispatch — it speculates in neither direction. Both remaining tiers keep the disposition: an ASSERTED `already_satisfied` with a reason, and an INFERRED one from a classifier that read the transcript, are unchanged. Honest as a fallback; as a primary design it would delete judgment.

The envelope is bounded by named constants and nothing else: `TRANSCRIPT_TAIL_BYTES` (24 KiB — transcripts run to hundreds of MB and the tail is where an agent says why it stopped), `GATE_OUTPUT_TAIL_BYTES` (4 KiB), `ACCEPTANCE_CRITERIA_BYTES` (4 KiB, included only because the worker already holds the item dict — no extra fetch) and `MAX_PROMPT_BYTES` (48 KiB) over the whole composition. An unbounded prompt against a million-token model is a silent per-run cost multiplier, not a crash.

Latency is bounded too: `config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS` (45 s) per attempt and `CLASSIFIER_MAX_ATTEMPTS` = 2, so ~90 s worst case — well under `POST_RUN_GATE_MIN_SECONDS` (600 s). This runs in the worker's terminal path, so a hung call delays the run (and the rollout wave behind it). Nothing in the module raises into that path: every failure is a `ClassificationFailure` and `resolve()` always returns a usable verdict.

Model/provider are pure config — `DISPATCH_DISPOSITION_CLASSIFIER_MODEL` (default `glm-5.3:cloud`), `DISPATCH_DISPOSITION_CLASSIFIER_BASE_URL` (default `https://ollama.com`), `DISPATCH_DISPOSITION_CLASSIFIER_API_KEY_ENV` (default `OLLAMA_CLOUD_API_KEY` — the env var NAME, so switching provider needs no re-plumbing), `DISPATCH_DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS`, `DISPATCH_DISPOSITION_CLASSIFIER_ENABLED`. The indirection exists for MODEL CHURN (GLM 5.4/6 will land soon and must be an env change on the hosts, no code edit, no redeploy of logic); provider OUTAGE is the derived tier's job.

Routing is the EXISTING routing (kb-03296 — the same decision must not live in two callers): the resolved verdict just sets the flags the asserted path already sets. `already_satisfied` -> `_route_already_satisfied_item`; `done` -> the normal success writer plus `_nudge_item_to_review`; `blocked`/`failed` -> the existing `stopped_without_assertion` prefix. That prefix, not `agent_reported_*`: the agent never said anything, so attributing a verdict to it would be a lie — the same reason every GTD comment on a non-asserted run states the tier in plain words ("inferred ... by `<model>` — it is NOT the agent's own word"). No new `RunStatus` member, no new `BUILD_FAILURE_PREFIXES` entry.

Because tiers 2 and 3 both take the gate decision as evidence, resolution happens AFTER the post-run gate, and the gate now runs on a zero-commit unasserted run (as it already did on the asserted `already_satisfied` path). That is what lets an agent which found the work already done, and exited without writing the artifact, land `already_satisfied` instead of `failed` — via TIER 2 only, since tier 3 cannot derive that verdict. The pre-existing guard survives on top: with nobody asserting anything, a green gate is the only corroboration there is, so a non-passing gate fails the run whatever tier 2 concluded.

**Transport/model compatibility — the trap that cost a silent feature.** The classifier speaks the ANTHROPIC-COMPATIBLE wire (`POST /v1/messages`, via the `anthropic` SDK). Ollama Cloud exposes two wires and they do NOT serve the same models: the native `/api/chat` serves `glm-5.3-flash:cloud` (which is why the talos engines pin it and work fine), while `/v1/messages` does NOT serve flash and DOES serve `glm-5.3:cloud`. Worse, Ollama Cloud answers **401, not 404,** for a model it will not serve on an endpoint — so a model-availability fault is indistinguishable from an auth fault without testing both. The first shipped default (`glm-5.3-flash` on `/v1/messages`) therefore never once returned 200 in production and read like a bad credential; every unasserted run silently fell through to the derived tier. A model named as the default must have been observed returning 200 ON THIS ENDPOINT. The full note lives next to the defaults in `config.py`.

Because a misconfigured classifier degrades INVISIBLY by design, `disposition.warn_if_unreachable_once()` issues one minimal probe and logs a single WARNING carrying class, status, model and base URL when it fails. It is fired as a background task from `lifespan`, is never awaited and is never fatal — the derived tier is the designed fallback and the service must start with a dead provider.

Failures are diagnosable, not four words. `ClassificationFailure` carries the failure CLASS (`disabled`, `no_credential`, `timeout`, `http_status`, `transport`, `parse` — see `FAILURE_CLASSES`), the HTTP status when there is one, the model, the base URL and the attempt number; `.detail` renders the full line for the blob and the WARNING, `.summary` the short `http_status 401` form for operator-facing text. Exception MESSAGES are never recorded (they can echo the prompt or the credential) — only the exception's type. A refusal (any 4xx outside `_RETRYABLE_STATUSES`) is not retried: it will be the same refusal twice.

The completion blob carries `disposition`, `disposition_provenance`, `disposition_reason`, `classifier_model`, `classifier_failure`, `classifier_failure_class`, `classifier_status_code` and `classifier_latency_s`, and one WARNING `unasserted build run:` line per non-asserted run carries run id, engine, tier, verdict and latency — so "how often does each tier fire, per engine" is a query, not a guess. `classifier_model` is recorded on FAILURE too: it was `null` on the run that exposed a tier 2 which had never worked, discarding the one field that pointed at the cause.

`tests/conftest.py` force-disables the classifier suite-wide so no test can make a real inference call; `tests/test_disposition.py` opts back in with every model call mocked.

Triage classes live in `error`-string prefixes, never in new protocol members: `no_result_envelope`, `result_is_error`, `max_turns_exhausted`, `stopped_without_assertion`, `agent_reported_blocked`, `agent_reported_failed`, `done_claim_zero_commits`, `already_satisfied_gate_failed`, `invariant_zero_commit_success`. `RunStatus` gained exactly one member, `already_satisfied`.

Every build terminal logs one `build completion:` key=value line and persists the same triple into the runs table's nullable `completion` column — the only durable carrier on a succeeded run, where `error` is NULL.

## Operator-facing error text

Run `error` strings carry ONE budget, `dispatch.ERROR_TEXT_MAX_CHARS` (2000), mirrored by `agent_gtd.dispatch_worker.ERROR_MSG_MAX_CHARS` on the GTD side. Both columns are TEXT; the caps are policy, not schema. Keep them equal — when they differed (a 300-char excerpt under a 500-char clip) the lower one bound silently and a fix to the other would have half-survived.

`dispatch.git_output_excerpt(proc)` builds every git/hook failure excerpt. It combines stdout AND stderr (stdout first — git forwards hook stdout on its own stream, and a stderr-only excerpt threw it away) and keeps the HEAD, eliding the middle with a marker naming the dropped character count. The head is what matters: the first failing pre-commit hook and git's own message are at the top, while a tail-only excerpt of a long hook run shows nothing but `(no files to check) Skipped` lines. `retention.py`'s `[-200:]` tail is NOT this surface and correctly keeps the tail.

## Retention

`retention.py` is deliberately verdict-free: no function in it accepts, reads, or branches on a `RunStatus`, a gate result or a push result, and `tests/test_retention.py::test_retention_is_verdict_free` asserts that against the module source and every public annotation string. The runs whose evidence matters most are exactly the ones whose outcome was recorded wrongly, so capture must not be conditional on the outcome.

`retention.capture_evidence(run_id, workspace, repos)` copies `transcript.txt`, `completion.json` and a per-repo `patch.diff` into `<EVIDENCE_ROOT>/<run_id>/` before any `cleanup_workspace` call, on every terminal path. Every step is individually wrapped so capture can never raise into teardown and abort the terminal DB write.

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
