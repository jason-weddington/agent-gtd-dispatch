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

Returns the service health status and the count of currently active (running) dispatches.

**Response 200:**
```json
{
  "status": "ok",
  "active_runs": 3
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
  "agents": ["claude-lead-abc", "claude-build-def"]
}
```

| Field | Type | Description |
|---|---|---|
| `engine` | `str` | Default engine name for this host |
| `version` | `str` | Service version (from `agent_discovery.SERVICE_VERSION`) |
| `max_concurrent_runs` | `int` | Configured concurrency limit |
| `active_runs` | `int` | Currently running (not queued) dispatches |
| `engines` | `list[str]` | Engine names with credentials present in the environment |
| `agents` | `list[str]` | Agent names from `list_agents.sh` |

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

### `POST /plan`

**Auth**: Required.

Calls the LLM-based rollout planner to produce a dependency DAG for a set of GTD items.
Called by the Agent GTD system before creating a rollout.

**Request body:**
```json
{
  "item_ids": ["uuid-1", "uuid-2", "uuid-3"]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `item_ids` | `list[str]` | ✓ | GTD item IDs to plan |

**Response 200** (`RolloutPlan`):
```json
{
  "item_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "edges": [
    {"from_item_id": "uuid-1", "to_item_id": "uuid-2"}
  ],
  "rationale": "uuid-2 depends on the interface introduced by uuid-1"
}
```

**Error responses:**
| Status | Condition |
|---|---|
| 502 | LLM call failed (includes `planner_model` and `item_count` in detail) |

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

**Automatic engine swap**: if `mode != "build"` and `engine == "claude-code-ollama"`,
the service swaps to `claude-code` silently and includes an `engine_swap` field in the
response.

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

**Error responses:**
| Status | Condition |
|---|---|
| 400 | Unknown engine, missing `item_id` for build/plan, missing `rollout_id` for manage, item has no project, project has no `git_origin` |
| 401 | Missing or invalid API key |
| 404 | Item, project, or rollout not found in GTD API |
| 502 | Upstream GTD API returned an error or malformed JSON |
| 503 | Upstream GTD API unreachable |

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
| `engine_actual` | Effective engine after any swap (null if no swap) |
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
