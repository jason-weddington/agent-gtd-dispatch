# Agent GTD Dispatch — API Reference

## Authentication

All endpoints except `GET /health` and `GET /info` require a Bearer token in the
`Authorization` header:

```
Authorization: Bearer <DISPATCH_API_KEY>
```

Invalid or missing credentials return **401 Unauthorized**.

---

## Endpoints

### `GET /health`

**Auth**: None required.

Returns the service health status and the count of currently active (running) dispatches. `active_runs` excludes rollout loops parked behind a host drain, so a draining host reads 0 once its loops park and its other runs finish. `restart_blocking_runs` is the work a restart would lose (slot-holding runs, host-queued runs, rollout reviews in flight, lease-holding lands); it is always present as a JSON integer, and `active_runs` is unchanged.

**Response 200:**
```json
{
  "status": "ok",
  "active_runs": 3,
  "restart_blocking_runs": 1
}
```

---

### `GET /info`

**Auth**: None required.

Returns the service identity, version, capacity, and available engines/agents. Designed
to be consumed by multi-host routers that need to select a dispatch target without a
separate round-trip to `/agents`.

**Response 200:**
```json
{
  "engine": "claude-code",
  "version": "1.10.0",
  "max_concurrent_runs": 32,
  "active_runs": 3,
  "engines": ["claude-code", "claude-code-sonnet", "claude-code-haiku"],
  "agents": ["claude-lead-abc", "claude-build-def"],
  "parked_rollout_loops": 1,
  "slot_runs": 1,
  "active_loops": 2,
  "queued_runs": 0,
  "load_avg_1m": 0.42,
  "cpu_count": 32,
  "engine_slots": {"talos-qwen": {"max_concurrent_runs": 1, "active_runs": 0}}
}
```

| Field | Type | Description |
|---|---|---|
| `engine` | `str` | Default engine name for this host |
| `version` | `str` | Service version (from `agent_discovery.SERVICE_VERSION`) |
| `max_concurrent_runs` | `int` | Configured concurrency limit |
| `active_runs` | `int` | Currently running (not queued) dispatches, excluding rollout loops parked behind a host drain |
| `engines` | `list[str]` | Engine names with credentials present in the environment and, when `DISPATCH_ENGINES_ONLY` is set, listed in it |
| `agents` | `list[str]` | Agent names from `list_agents.sh` |
| `load_avg_1m` | `float \| null` | 1-minute load average from `os.getloadavg`; null when unavailable. Routers treat a host as overloaded above 1.5 x `cpu_count` |
| `cpu_count` | `int \| null` | Logical CPU count from `os.cpu_count`; null when unknown |
| `engine_slots` | `object` | Per-engine caps declared by this host (`DISPATCH_ENGINE_SLOTS`), one entry per capped engine, including cap-0 entries. Absent or empty means no per-engine caps. `engine_slots.<engine>.active_runs` counts runs holding a slot, both running and host-queued, so it differs from the top-level `active_runs`; only BUILD-mode runs admitted through `/dispatch` are counted |
| `engines_only` | `list[str] \| null` | Sorted `DISPATCH_ENGINES_ONLY` allowlist, or null when unset. `engine_slots` is not filtered by it |
| `parked_rollout_loops` | `int` | Rollout loops parked behind a host drain (informational). `active_runs` is unchanged; `restart_blocking_runs` is the restart-safety idle signal |
| `slot_runs` | `int` | Runs charged against `max_concurrent_runs` (build and plan runs; never rollout loops or the reviews they run, which are reported in restart_blocking.reviews_in_flight). The router's capacity input: free slots = `max_concurrent_runs - slot_runs`. Absent on old hosts (routers fall back to `max_concurrent_runs - active_runs - parked_rollout_loops`) |
| `restart_blocking_runs` | `int` | Work a restart would lose; equals the sum of `restart_blocking` values. Drain-and-deploy tooling should wait on this being 0 |
| `restart_blocking` | `object` | Components: `slot_runs` (build and plan runs holding a slot), `queued_runs` (host-queued dispatches), `reviews_in_flight` (live rollout reviewer tasks), `lease_holding_lands` (lands that hold, held or are requesting the land lease, or whose state is unknown) |
| `active_loops` | `int` | Live rollout loops (informational). Loops never count against `max_concurrent_runs` and are never queued |
| `queued_runs` | `int` | Dispatches waiting in the host queue (informational) |

---

### `GET /agents`

**Auth**: Required.

Executes `list_agents.sh` and returns the result. Always returns 200 — if the script is
missing, non-executable, exits non-zero, or times out, returns an empty list.

**Response 200:**
```json
{
  "agents": [
    {"name": "claude-lead-abc", "status": "available"},
    {"name": "claude-build-def", "status": "busy"}
  ]
}
```

The shape of each agent object is defined by the `list_agents.sh` script output (JSON
array of objects). The service does not validate or transform the entries.

---

### `POST /dispatch`

**Auth**: Required.

Creates a new dispatch run. Validates the request, fetches the item and project from the
GTD API, persists the Run to SQLite, and either starts the agent immediately or queues it
if the service is at capacity.

**Request body** (`DispatchRequest`):
```json
{
  "item_id": "gtd-item-uuid",
  "mode": "build",
  "engine": "claude-code-sonnet",
  "max_turns": 100,
  "timeout_minutes": 30,
  "attribution": "claude-build-abc12345",
  "rollout_id": null
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `item_id` | `str \| None` | `null` | GTD item ID (required for `build`/`plan`; omit for `manage`) |
| `mode` | `str` | `"build"` | `"build"`, `"plan"`, or `"manage"` |
| `engine` | `str` | `"claude-code"` | Engine name (see domain docs for available engines) |
| `max_turns` | `int` | `100` | Turn cap for the agent subprocess |
| `timeout_minutes` | `int \| None` | `null` | Override timeout (default: 30 min build / 240 min manage) |
| `attribution` | `str \| None` | `null` | Agent identity for GTD comments |
| `rollout_id` | `str \| None` | `null` | Rollout ID (required for `manage` mode) |
| `agent_name` | `str \| None` | `null` | Passed to agent via `--agent` flag |

**Automatic engine swap**: if `mode != "build"` and `engine` is `claude-code-ollama`, `claude-code-glm` or `claude-code-deepseek`, the service swaps to `claude-code` silently and includes an `engine_swap` field in the response.

**Response 200** (`RunResponse`):
```json
{
  "id": "a4f7c3b21e09",
  "item_id": "gtd-item-uuid",
  "project_name": "my-project",
  "branch_name": "feat/gtd-item-uuid-fix-the-bug",
  "engine": "claude-code-sonnet",
  "engine_actual": null,
  "mode": "build",
  "rollout_id": null,
  "status": "pending",
  "created_at": "2026-05-23T17:00:00Z",
  "started_at": null,
  "completed_at": null,
  "exit_code": null,
  "error": null,
  "workspace_path": null,
  "engine_swap": null
}
```

Note: `status` is `"pending"` when the service is at capacity (the run is queued). It
transitions to `"running"` when a slot becomes available. The response is **always 200**
for a valid request — there is no 503 at capacity.

**Per-engine caps (BUILD mode only).** A host that declares `DISPATCH_ENGINE_SLOTS`
refuses a BUILD dispatch whose engine has no free slot, with HTTP 503 and this body
(no run row is created):

```json
{"detail": {"detail": "Engine slot full", "reason": "engine_slot_full", "engine": "talos-qwen", "active_runs": 1, "max_concurrent_runs": 1}}
```

It differs from the upstream-unreachable 503 (whose `detail` has an `upstream_url`
key) and from general capacity (a 200 that queues the run). Plan, manage and review
dispatches are never capped. A capped run also consumes one general slot.

When the host sets `DISPATCH_ENGINES_ONLY` and the requested `engine` is not listed, the dispatch is refused before any GTD call, run row or slot reservation with a 422 whose body is distinct from the 503 above:

```json
{"detail": {"detail": "Engine not served by this host", "reason": "engine_not_allowed", "engine": "claude-code-sonnet", "allowed_engines": ["talos-qwen"]}}
```

**Error responses:**
| Status | Condition |
|---|---|
| 400 | Unknown engine, missing `item_id` for build/plan, missing `rollout_id` for manage, item has no project, project has no `git_origin` |
| 401 | Missing or invalid API key |
| 404 | Item, project, or rollout not found in GTD API |
| 422 | engine not in this host's `DISPATCH_ENGINES_ONLY` allowlist (reason `engine_not_allowed`) |
| 502 | Upstream GTD API returned an error or malformed JSON |
| 503 | Upstream GTD API unreachable, or `reason: "engine_slot_full"` (see above) |

---

### `GET /runs`

**Auth**: Required.

Lists dispatch runs, optionally filtered. Returns at most `limit` runs ordered by
`created_at` descending (newest first).

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `item_id` | `str` | — | Filter by GTD item ID |
| `status` | `RunStatus` | — | Filter by status (`pending`, `running`, `succeeded`, `failed`, `timed_out`, `cancelled`) |
| `limit` | `int` | `50` | Max results (1–200) |

**Response 200** — array of `RunResponse` objects (same shape as `POST /dispatch` response).

---

### `GET /runs/{run_id}`

**Auth**: Required.

Fetches a single run by its ID.

**Response 200** — `RunResponse`.

**Error responses:**
| Status | Condition |
|---|---|
| 404 | Run not found |

---

### `GET /runs/{run_id}/transcript`

**Auth**: Required.

Returns the last N lines of the agent's output transcript. The transcript is streamed
continuously during execution, so this endpoint can be polled for live output.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lines` | `int` | `200` | Number of lines to return from the tail (1–5000) |

**Response 200:**
```json
{
  "text": "...last 200 lines of transcript...",
  "last_modified": "2026-05-23T17:05:23Z",
  "total_lines": 1247
}
```

If the run has no workspace (not started yet) or the transcript file does not exist:
```json
{
  "text": "no transcript yet",
  "last_modified": null,
  "total_lines": 0
}
```

**Error responses:**
| Status | Condition |
|---|---|
| 404 | Run not found |

---

### `POST /runs/{run_id}/cancel`

**Auth**: Required.

Cancels a running or queued dispatch. The operation is **idempotent** — calling it on an
already-terminal run returns 200 with no side effects.

**Cancel sequence:**
1. Mark the asyncio task for cancellation (`task.cancel()`).
2. Send `SIGTERM` to the agent subprocess.
3. Wait `CANCEL_GRACE_SECONDS` (default 5 s).
4. If the subprocess has not exited, send `SIGKILL`.
5. Update the run status to `cancelled` in the DB.
6. Post a comment to the GTD item (best-effort; logged but not raised on failure).
7. Publish an SSE event to any subscribers.

**Response 200** — `RunResponse` with `status: "cancelled"`.

**Error responses:**
| Status | Condition |
|---|---|
| 404 | Run not found |

---

## Common Response Fields

All `RunResponse` objects include these fields:

| Field | Description |
|---|---|
| `id` | 12-char hex run identifier |
| `item_id` | GTD item ID (null for manage runs) |
| `project_name` | Project name from GTD |
| `branch_name` | Feature branch (null for manage runs) |
| `engine` | Requested engine name |
| `engine_actual` | Effective engine after any swap; `rollout-loop` for a MANAGE rollout-loop run. |
| `mode` | `"build"`, `"plan"`, or `"manage"` |
| `rollout_id` | Rollout ID or null |
| `status` | Current `RunStatus` value |
| `created_at` | ISO 8601 datetime |
| `started_at` | ISO 8601 datetime or null |
| `completed_at` | ISO 8601 datetime or null |
| `exit_code` | Subprocess exit code or null |
| `error` | Error message or transcript tail on failure, or null |
| `workspace_path` | Absolute path to workspace dir, or null |
| `engine_swap` | `{from_engine, to_engine, reason}` if engine was swapped, else null |

---

## Error Shape

All error responses use FastAPI's default error body:

```json
{
  "detail": "Human-readable description"
}
```

For upstream errors (502/503), `detail` is a dict with additional context:

```json
{
  "detail": {
    "detail": "Upstream error fetching item",
    "upstream_status": 500,
    "upstream_body_snippet": "Internal Server Error...",
    "upstream_url": "https://agent-gtd.example.com/api/items/abc"
  }
}
```
