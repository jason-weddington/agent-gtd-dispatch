"""Dispatch worker API — runs headless coding agents."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

from . import config, db, dispatch, gates, gtd_client, rollout_planner, talos
from .agent_discovery import ENGINE_NAME, SERVICE_VERSION, run_list_agents_script
from .engines import (
    COMMON_ENV_KEYS,
    Engine,
    get_available_engine_names,
    get_engine,
    is_talos_engine,
)
from .models import (
    DispatchMode,
    DispatchRequest,
    EngineSwap,
    InfoResponse,
    PlanRequest,
    PushStatus,
    RepoPushStatus,
    RolloutPlan,
    Run,
    RunResponse,
    RunStatus,
)

logger = logging.getLogger(__name__)


def _check_service_repo() -> None:
    """Check that the service's own working copy is on main and clean.

    Skips silently when the working copy doesn't exist (wheel deploy).
    Raises SystemExit(1) if the repo is on a non-main branch or has
    uncommitted changes — prevents the service from running with a
    corrupted working copy.
    """
    repo = Path.home() / "agent-gtd-dispatch"
    if not repo.is_dir():
        return  # wheel deploy — no working copy to check
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception as exc:
        logger.error("Service repo health check failed: %s", exc)
        raise SystemExit(1) from exc
    if branch != "main" or dirty:
        logger.error(
            "Service repo not on main or dirty: branch=%r dirty=%r",
            branch,
            bool(dirty),
        )
        raise SystemExit(1)


async def _ollama_health_check() -> tuple[bool, str]:
    """Check if the Ollama endpoint is reachable.

    Returns (ok, reason). reason is non-empty only when ok=False.
    """
    import httpx  # already a dep; import at function scope for clarity

    if not config.OLLAMA_BASE_URL:
        return False, "OLLAMA_BASE_URL is not configured"
    url = f"{config.OLLAMA_BASE_URL}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=2.0)
            resp.raise_for_status()
        return True, ""
    except Exception as exc:
        return False, (
            f"Invalid OLLAMA_BASE_URL={config.OLLAMA_BASE_URL!r}: "
            f"health check to {url} failed: {exc}; expected format http://host:port"
        )


# Track running subprocesses for cancellation
_active_processes: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
_active_subprocesses: dict[str, subprocess.Popen[bytes]] = {}
_run_event_queues: dict[str, asyncio.Queue[dict]] = {}  # type: ignore[type-arg]


class _PendingDispatch(NamedTuple):
    """Queued dispatch waiting for a free slot."""

    run: Run
    engine: Engine
    max_turns: int
    timeout_seconds: int
    attribution: str | None


_pending_queue: list[_PendingDispatch] = []

# Watchdog state
_rollout_to_run: dict[str, Run] = {}  # rollout_id → active manage-mode Run
_watchdog_task: asyncio.Task[None] | None = None  # handle for clean shutdown
_watchdog_acted: dict[str, float] = {}  # rollout_id → monotonic() of last action

# rollout_id -> lifetime count of UNCOUNTED (free) manage relaunches granted while
# a child build run is healthy and still in flight. A lifetime tally: it is NEVER
# reset by manager progress alone (see _manage_last_terminal_count below), and is
# cleared only by (a) a counted recovery and (b) the clean-exit early return. A
# progress-reset tally keyed on the manager heartbeat alone would be unbounded,
# because a manager that refreshes manager_state_updated_at once per cycle and
# then dies would reset it every cycle.
_manage_free_relaunches: dict[str, int] = {}

# rollout_id -> last-seen count of items that have reached a terminal build
# outcome (left the rollout's in-flight-build set after having been observed
# there). GET /api/rollouts/{id} does not carry a `done_count` field (that is
# only computed by the project-active-rollout and list endpoints), so this is
# the "equivalent terminal-item count" derived from the field that IS already
# on the rollout dict `_maybe_relaunch_manage` holds: inFlightBuildRuns. Used to
# reset `_manage_free_relaunches` on real wave progress — bounding the free-relaunch
# budget per stuck item rather than per wave. Cleared together with
# `_manage_free_relaunches`.
_manage_last_terminal_count: dict[str, int] = {}

# rollout_id -> set of item_ids ever observed in the rollout's in-flight-build set.
# Bookkeeping for computing `_manage_last_terminal_count` above; cleared together
# with it.
_manage_seen_in_flight_item_ids: dict[str, frozenset[str]] = {}


def _publish_run_event(run_id: str, status: str, completed_at: str | None) -> None:
    """Publish a status-change event to the run's in-memory event queue."""
    queue = _run_event_queues.get(run_id)
    if queue is not None:
        queue.put_nowait(
            {
                "event": "run-status-change",
                "run_id": run_id,
                "status": status,
                "completed_at": completed_at,
            }
        )


# Manage subprocess auto-recovery settings
MAX_MANAGE_RETRIES = config.MAX_MANAGE_RETRIES  # re-exported for tests
MANAGE_RETRY_BACKOFF_SECONDS = 30

# Frozenset of rollout statuses that indicate a clean/terminal manage exit
_CLEAN_EXIT_STATUSES: frozenset[str] = frozenset({"completed", "halted", "cancelled"})

# `_maybe_relaunch_manage` decisions logged at WARNING (crash-loop / cap / backstop
# signals an operator should notice) rather than INFO (routine).
_ABNORMAL_DECISIONS: frozenset[str] = frozenset(
    {
        "counted-free-cap-exhausted",
        "counted-backstop-exceeded",
        "counted-run-timed-out",
    }
)


def _in_flight_build_runs(rollout: dict[str, Any]) -> list[Any]:
    """Return the rollout's in-flight (non-terminal) build runs.

    The GTD service (rollout_service._fetch_in_flight_build_runs) contractually
    pre-filters this field to non-terminal runs only — no dispatch-side filtering needed.
    """
    return rollout.get("inFlightBuildRuns") or []


security = HTTPBearer()


def _verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    if credentials.credentials != config.DISPATCH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Initialize config and DB on startup, cancel tasks on shutdown."""
    global _watchdog_task
    config.load()
    if config.AGENT_SUBPROCESS_USER:
        _check_service_repo()
    dispatch.init_executor()
    await db.init_db()
    orphaned_run_ids = await db.reconcile_orphans()
    if orphaned_run_ids:
        for run_id in orphaned_run_ids:
            logger.warning("Reconciled orphaned run: %s", run_id)
    else:
        logger.info("No orphaned runs found on startup")
    _watchdog_task = asyncio.create_task(_manage_watchdog())
    yield
    # Cancel watchdog and active dispatch tasks on shutdown
    if _watchdog_task is not None:
        _watchdog_task.cancel()
    for task in _active_processes.values():
        task.cancel()


app = FastAPI(title="Agent GTD Dispatch", lifespan=lifespan)


def start() -> None:  # pragma: no cover
    """Entry point for the `agent-gtd-dispatch` console script.

    Runs uvicorn against the module-level FastAPI app. Bound to 0.0.0.0
    because the systemd unit expects the service to accept LAN traffic.
    """
    uvicorn.run(app, host="0.0.0.0", port=8100)


# --- Background dispatch worker ---


async def _do_manage_recovery(
    rollout_id: str,
    run: Run | None,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    attribution: str | None,
    *,
    halt_reason: str,
    count_toward_cap: bool = True,
    known_retry_count: int = 0,
) -> None:
    """Shared manage-recovery: kill stale subprocess (if any), increment retry, relaunch or halt.

    Called from both _maybe_relaunch_manage (exit-path, run already finished) and
    _manage_watchdog (stale-detection path, run still alive). When the existing run
    is still in _active_processes the task is cancelled and its subprocess terminated
    before spawning the replacement.

    Args:
        rollout_id: The rollout to recover.
        run: The Run object for the stale/exited manage worker, or None if unknown.
        max_turns: Forwarded to the new _dispatch_worker.
        engine: Forwarded to the new _dispatch_worker.
        timeout_seconds: Forwarded to the new _dispatch_worker.
        attribution: Forwarded to the new _dispatch_worker.
        halt_reason: Reason string for halt_rollout when cap is exceeded.
        count_toward_cap: When False, this is a "free" relaunch granted while a
            healthy child build is still in flight — it does not call
            relaunch_manage_rollout, does not evaluate the retry cap, and never
            halts. When True (default), behaves exactly as before this feature.
        known_retry_count: retry_count to report/forward when count_toward_cap
            is False (relaunch_manage_rollout is not called on that path, so
            the caller must supply the rollout's current manage_retry_count).
    """
    source = "watchdog" if halt_reason == "manage_watchdog_stale" else "exit-path"
    run_id = run.id if run is not None else "none"

    # Kill stale task/subprocess if still active (watchdog path)
    run_killed = False
    if run is not None:
        existing_task = _active_processes.pop(run.id, None)
        if existing_task is not None:
            existing_task.cancel()
            run_killed = True
        existing_proc = _active_subprocesses.pop(run.id, None)
        if existing_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                existing_proc.terminate()
            run_killed = True

    logger.info(
        "manage-recovery: entry rollout_id=%s run_id=%s source=%s run_killed=%s "
        "count_toward_cap=%s",
        rollout_id,
        run_id,
        source,
        run_killed,
        count_toward_cap,
    )

    if count_toward_cap:
        # Clear the free-relaunch tally at this shared choke point so a
        # watchdog-triggered counted recovery also resets it, without editing
        # _watchdog_evaluate_rollout.
        _manage_free_relaunches.pop(rollout_id, None)
        _manage_last_terminal_count.pop(rollout_id, None)
        _manage_seen_in_flight_item_ids.pop(rollout_id, None)

        # manage-recovery deliberately uses the static service key: it can fire
        # from the watchdog (no owning user/run) or from the post-exit relaunch
        # path, and we want recovery to succeed even when the original Run's
        # callback_token has expired. Do NOT thread a per-run token here.
        try:
            updated = await gtd_client.relaunch_manage_rollout(rollout_id)
        except Exception:
            logger.exception(
                "Failed to increment manage_retry_count for rollout %s — skipping recovery",
                rollout_id,
            )
            return

        retry_count = int(updated["manage_retry_count"])
        logger.info(
            "manage-recovery: retry_count rollout_id=%s run_id=%s retry_count=%d cap=%d",
            rollout_id,
            run_id,
            retry_count,
            MAX_MANAGE_RETRIES,
        )

        if retry_count > MAX_MANAGE_RETRIES:
            logger.warning(
                "Manage retry cap exceeded for rollout %s (count=%d) — halting",
                rollout_id,
                retry_count,
            )
            try:
                await gtd_client.halt_rollout(rollout_id, reason=halt_reason)
            except Exception:
                logger.exception(
                    "Failed to halt rollout %s after cap exceeded", rollout_id
                )
            return

        logger.info(
            "Relaunching manage agent for rollout %s (retry %d/%d) after %ds",
            rollout_id,
            retry_count,
            MAX_MANAGE_RETRIES,
            MANAGE_RETRY_BACKOFF_SECONDS,
        )
    else:
        retry_count = known_retry_count
        logger.info(
            "manage-recovery: free-relaunch rollout_id=%s run_id=%s retry_count=%d "
            "cap=%d counted=false",
            rollout_id,
            run_id,
            retry_count,
            MAX_MANAGE_RETRIES,
        )

    await asyncio.sleep(MANAGE_RETRY_BACKOFF_SECONDS)

    new_run = Run(
        item_id=run.item_id if run else None,
        project_name=run.project_name if run else "",
        mode=DispatchMode.MANAGE,
        rollout_id=rollout_id,
        engine=run.engine if run else engine.name,
        agent_name=run.agent_name if run else None,
        callback_token=run.callback_token if run else None,
    )
    await db.insert_run(new_run)
    logger.info(
        "manage-recovery: relaunched rollout_id=%s prior_run_id=%s new_run_id=%s "
        "counted=%s retry_count=%d",
        rollout_id,
        run_id,
        new_run.id,
        count_toward_cap,
        retry_count,
    )
    task = asyncio.create_task(
        _dispatch_worker(
            new_run,
            max_turns,
            engine,
            timeout_seconds,
            attribution=attribution,
            manage_retry_count=retry_count,
            is_recovery=True,
        )
    )
    _active_processes[new_run.id] = task


async def _maybe_relaunch_manage(
    run: Run,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    attribution: str | None,
    *,
    manager_uptime_seconds: float,
    run_timed_out: bool,
) -> None:
    """Check rollout status on manage exit and relaunch or halt as appropriate.

    Called from _dispatch_worker's finally block (skipped when human-cancelled).
    - If rollout is in a clean terminal state: do nothing.
    - Otherwise, walk the ordered decision ladder below (first match wins) to
      decide whether this exit is a "free" relaunch (a manager that exited
      while a healthy child build is still in flight — does not consume
      MAX_MANAGE_RETRIES budget) or must count toward the cap as before.

    Args:
        run: The Run for the manage worker that just exited.
        max_turns: Forwarded to the (possible) new _dispatch_worker.
        engine: Forwarded to the (possible) new _dispatch_worker.
        timeout_seconds: Forwarded to the (possible) new _dispatch_worker.
        attribution: Forwarded to the (possible) new _dispatch_worker.
        manager_uptime_seconds: Wall-clock seconds between the agent subprocess
            launch and this exit (0.0 if the agent never launched).
        run_timed_out: True if this run ended via subprocess.TimeoutExpired.
    """
    assert run.rollout_id is not None  # noqa: S101 — caller guarantees this
    rollout_id = run.rollout_id
    # manage-recovery probe: deliberately on the static key (callback_token may
    # have expired by the time we relaunch). See _do_manage_recovery comment.
    try:
        rollout = await gtd_client.get_rollout(rollout_id)
    except Exception:
        logger.exception(
            "Failed to fetch rollout %s for relaunch check — skipping recovery",
            rollout_id,
        )
        return

    if rollout["status"] in _CLEAN_EXIT_STATUSES:
        logger.info(
            "manage-recovery: clean-exit rollout_id=%s run_id=%s rollout_status=%s",
            rollout_id,
            run.id,
            rollout["status"],
        )
        _manage_free_relaunches.pop(rollout_id, None)
        _manage_last_terminal_count.pop(rollout_id, None)
        _manage_seen_in_flight_item_ids.pop(rollout_id, None)
        return  # clean exit — nothing to do

    in_flight = _in_flight_build_runs(rollout)
    in_flight_ids = frozenset(str(r.get("itemId")) for r in in_flight)

    # Progress reset: an item that was in flight and has since left the
    # in-flight set has reached a terminal build outcome (completed/failed/
    # cancelled) — real forward progress, unlike a manager heartbeat which
    # only proves *a* manager is alive. Reset the free-relaunch tally whenever
    # this rollout's terminal-item count increases, so a long multi-item wave
    # is bounded per stuck item (max MAX_MANAGE_FREE_RELAUNCHES hand-offs
    # waiting on any single item) rather than per wave.
    previously_in_flight = _manage_seen_in_flight_item_ids.get(rollout_id, frozenset())
    newly_terminal = previously_in_flight - in_flight_ids
    last_terminal_count = _manage_last_terminal_count.get(rollout_id, 0)
    current_terminal_count = last_terminal_count + len(newly_terminal)
    if current_terminal_count > last_terminal_count:
        _manage_free_relaunches[rollout_id] = 0
    _manage_last_terminal_count[rollout_id] = current_terminal_count
    _manage_seen_in_flight_item_ids[rollout_id] = in_flight_ids

    manager_phase = rollout.get("manager_phase")
    manager_current_step = rollout.get("manager_current_step")
    updated_at_str: str | None = rollout.get("manager_state_updated_at")
    manager_state_age_seconds: str | int = "unknown"
    age_seconds: float | None = None
    if updated_at_str:
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
        except ValueError:
            age_seconds = None
        else:
            age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
    if age_seconds is not None:
        manager_state_age_seconds = int(age_seconds)

    free_relaunches = _manage_free_relaunches.get(rollout_id, 0)

    decision: str
    if run_timed_out:
        decision = "counted-run-timed-out"
    elif manager_phase != "polling":
        decision = "counted-not-polling"
    elif not in_flight:
        decision = "counted-no-build-in-flight"
    elif manager_uptime_seconds < config.MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS:
        decision = "counted-short-uptime"
    elif age_seconds is None:
        decision = "counted-unknown-manager-state-age"
    elif age_seconds > config.MANAGE_TIMEOUT_SECONDS:
        decision = "counted-backstop-exceeded"
    elif free_relaunches >= config.MAX_MANAGE_FREE_RELAUNCHES:
        decision = "counted-free-cap-exhausted"
    else:
        decision = "free-relaunch-build-in-flight"

    in_flight_build_run_ids = ",".join(str(r.get("runId")) for r in in_flight) or "none"
    log_fmt = (
        "manage-recovery: exit-path rollout_id=%s run_id=%s rollout_status=%s "
        "manager_phase=%s manager_current_step=%s in_flight_builds=%d "
        "in_flight_build_run_ids=%s uptime_seconds=%.0f manager_state_age_seconds=%s "
        "free_relaunches=%d decision=%s"
    )
    log_args = (
        rollout_id,
        run.id,
        rollout["status"],
        manager_phase,
        manager_current_step,
        len(in_flight),
        in_flight_build_run_ids,
        manager_uptime_seconds,
        manager_state_age_seconds,
        free_relaunches,
        decision,
    )
    if decision in _ABNORMAL_DECISIONS:
        logger.warning(log_fmt, *log_args)
    else:
        logger.info(log_fmt, *log_args)

    if decision == "free-relaunch-build-in-flight":
        new_free_relaunches = free_relaunches + 1
        _manage_free_relaunches[rollout_id] = new_free_relaunches
        # Mark acted-on so a watchdog tick within MANAGE_STALE_THRESHOLD_SECONDS
        # takes its existing skipped-idempotency branch instead of killing the
        # warming-up replacement manager and burning a counted retry — the same
        # mark-before-await pattern the watchdog itself uses.
        _watchdog_acted[rollout_id] = time.monotonic()

        comment_run_id = in_flight[0]["runId"]
        comment_item_id = in_flight[0]["itemId"]
        comment = (
            "manage-recovery: free relaunch — the rollout manager exited while "
            f"build run `{comment_run_id}` is still in flight. `manage_retry_count` "
            f"is unchanged at {int(rollout.get('manage_retry_count', 0))}; "
            f"free relaunch {new_free_relaunches}/{config.MAX_MANAGE_FREE_RELAUNCHES}; "
            f"manager uptime {manager_uptime_seconds:.0f}s."
        )
        try:
            await gtd_client.post_comment(
                comment_item_id, comment, created_by="agent-gtd-dispatch"
            )
        except Exception:
            logger.exception(
                "Failed to post free-relaunch comment for rollout %s", rollout_id
            )

        await _do_manage_recovery(
            rollout_id,
            run,
            max_turns,
            engine,
            timeout_seconds,
            attribution,
            halt_reason="manage_relaunch_cap_exceeded",
            count_toward_cap=False,
            known_retry_count=int(rollout.get("manage_retry_count", 0)),
        )
        return

    await _do_manage_recovery(
        rollout_id,
        run,
        max_turns,
        engine,
        timeout_seconds,
        attribution,
        halt_reason="manage_relaunch_cap_exceeded",
    )


async def _watchdog_evaluate_rollout(
    rollout: dict[str, Any], rollout_id: str, now: datetime
) -> None:
    """Evaluate one rollout for staleness and run recovery if needed.

    Skips rollouts in clean terminal states or with a fresh timestamp.
    Idempotency: rollouts acted on within the current staleness window are skipped.
    """
    status: str = rollout.get("status", "")
    manager_phase: str = rollout.get("manager_phase", "unknown")
    if status in _CLEAN_EXIT_STATUSES:
        logger.info(
            "watchdog: rollout_id=%s manager_phase=%s status=%s decision=skipped-terminal",
            rollout_id,
            manager_phase,
            status,
        )
        return

    updated_at_str: str | None = rollout.get("manager_state_updated_at")
    if not updated_at_str:
        return

    try:
        updated_at = datetime.fromisoformat(updated_at_str)
    except ValueError:
        return

    age_seconds = (now - updated_at).total_seconds()
    if age_seconds <= config.MANAGE_STALE_THRESHOLD_SECONDS:
        logger.info(
            "watchdog: rollout_id=%s manager_phase=%s age_seconds=%.0f threshold=%d decision=fresh",
            rollout_id,
            manager_phase,
            age_seconds,
            config.MANAGE_STALE_THRESHOLD_SECONDS,
        )
        return  # fresh enough

    # Polling + in-flight build short-circuit: a manager in the 'polling' phase
    # with at least one non-terminal child build run is presumed to be healthily
    # waiting on that build. The build's real status (which we own) — not the
    # manager's heartbeat (which we don't) — is the signal, so a stale
    # manager_state_updated_at does NOT mean stuck. Skip recovery regardless of
    # timestamp age, bounded only by MANAGE_TIMEOUT_SECONDS as the absolute
    # backstop (anchored on manager_state age) so a genuinely-wedged build can't
    # defer recovery forever. Non-polling phases fall straight through to the
    # existing timestamp-staleness recovery path below.
    if manager_phase == "polling":
        in_flight = _in_flight_build_runs(rollout)
        if in_flight:
            if age_seconds <= config.MANAGE_TIMEOUT_SECONDS:
                logger.info(
                    "watchdog: rollout_id=%s manager_phase=polling age_seconds=%.0f "
                    "in_flight_builds=%d decision=skipped-build-in-flight",
                    rollout_id,
                    age_seconds,
                    len(in_flight),
                )
                return  # healthily waiting on a still-running build
            logger.warning(
                "watchdog: rollout_id=%s manager_phase=polling age_seconds=%.0f "
                "in_flight_builds=%d exceeds MANAGE_TIMEOUT_SECONDS=%d "
                "decision=backstop-recovery",
                rollout_id,
                age_seconds,
                len(in_flight),
                config.MANAGE_TIMEOUT_SECONDS,
            )
            # Absolute backstop exceeded — fall through to recovery below.

    # Idempotency guard: skip if we already acted within the staleness window
    last_acted = _watchdog_acted.get(rollout_id, 0.0)
    if time.monotonic() - last_acted < config.MANAGE_STALE_THRESHOLD_SECONDS:
        logger.info(
            "watchdog: rollout_id=%s manager_phase=%s age_seconds=%.0f decision=skipped-idempotency",
            rollout_id,
            manager_phase,
            age_seconds,
        )
        return

    logger.warning(
        "Watchdog: rollout %s stale (age=%.0fs) — triggering recovery",
        rollout_id,
        age_seconds,
    )
    logger.info(
        "watchdog: rollout_id=%s manager_phase=%s age_seconds=%.0f threshold=%d decision=triggering-recovery",
        rollout_id,
        manager_phase,
        age_seconds,
        config.MANAGE_STALE_THRESHOLD_SECONDS,
    )

    # Mark acted-on BEFORE awaiting recovery (prevents a concurrent tick from double-acting)
    _watchdog_acted[rollout_id] = time.monotonic()

    existing_run = _rollout_to_run.get(rollout_id)
    await _do_manage_recovery(
        rollout_id,
        existing_run,
        config.MAX_TURNS,
        get_engine("claude-code"),
        config.MANAGE_TIMEOUT_SECONDS,
        None,  # attribution unknown from watchdog context
        halt_reason="manage_watchdog_stale",
    )


async def _watchdog_tick() -> None:
    """One pass of the watchdog: scan running rollouts and recover stale ones.

    Exposed at module level for direct invocation in tests.
    """
    # Watchdog deliberately uses the static service key: it has no owning user
    # or run, and must function for all rollouts regardless of who dispatched them.
    try:
        rollouts = await gtd_client.list_running_rollouts()
    except Exception:
        logger.exception("Watchdog failed to fetch running rollouts — skipping tick")
        return

    count = len(rollouts)
    logger.info("watchdog: tick start rollout_count=%d", count)
    now = datetime.now(UTC)
    for rollout in rollouts:
        rollout_id: str | None = rollout.get("id")
        if not rollout_id:
            continue
        try:
            await _watchdog_evaluate_rollout(rollout, rollout_id, now)
        except Exception:
            logger.exception(
                "Watchdog failed to evaluate rollout %s — continuing", rollout_id
            )
    logger.info("watchdog: tick done rollout_count=%d", count)


async def _manage_watchdog() -> None:
    """Background coroutine: periodically scan for stale manage-agent rollouts."""
    while True:
        await asyncio.sleep(config.WATCHDOG_INTERVAL_SECONDS)
        try:
            await _watchdog_tick()
        except Exception:
            logger.exception("Watchdog scan iteration failed — continuing")


def _try_start_pending() -> None:
    """Start as many queued dispatches as there are free slots.

    Called synchronously from _dispatch_worker's finally block after a run
    completes, freeing a slot. No await between slot-count check and task
    creation — stays atomic within the event-loop tick.
    """
    while _pending_queue and len(_active_processes) < config.MAX_CONCURRENT_RUNS:
        pending = _pending_queue.pop(0)
        task = asyncio.create_task(
            _dispatch_worker(
                pending.run,
                pending.max_turns,
                pending.engine,
                pending.timeout_seconds,
                attribution=pending.attribution,
            )
        )
        _active_processes[pending.run.id] = task


def _commit_with_retry(
    repo_dir: str,
    git_ident_flags: list[str],
    commit_msg: str,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git commit`` with bounded re-stage retries.

    A fixer-style pre-commit hook (end-of-file-fixer, trailing-whitespace,
    ruff --fix, black, prettier, …) may modify the staged files and exit
    non-zero on the first attempt — by design. The correct response is to
    re-stage the hook's output (``git add -A``) and retry the commit, NOT to
    skip hooks (``--no-verify``). Without this, a fully-completed item is lost
    the first time a fixer hook fires.

    Behaviour:
    - Run ``git commit`` (no ``--no-verify`` — hooks must be allowed to fix).
    - On rc 0, return immediately.
    - On non-zero rc, check ``git status --porcelain``: if the tree is CLEAN
      (nothing for the hook to have re-dirtied), give up and return the
      failing result immediately — there is nothing to re-stage.
    - If the tree is dirty, ``git add -A`` and retry the commit. Bound to at
      most 2 retries (3 commit invocations total). Return the last result on
      exhaustion.
    """
    max_retries = 2
    result = subprocess.run(
        dispatch._sudo_wrap(["git", *git_ident_flags, "commit", "-m", commit_msg]),
        cwd=repo_dir,
        check=False,
        capture_output=True,
    )
    for _ in range(max_retries):
        if result.returncode == 0:
            return result
        # Detect a dirty tree — the fixer-hook-modified case. A clean tree
        # means the hook did not (or could not) re-dirty anything, so there
        # is nothing to re-stage; give up on this failing commit.
        porcelain_rc = subprocess.run(
            dispatch._sudo_wrap(["git", "status", "--porcelain"]),
            cwd=repo_dir,
            check=False,
            capture_output=True,
        )
        if not porcelain_rc.stdout.decode("utf-8", errors="replace").strip():
            return result
        subprocess.run(
            dispatch._sudo_wrap(["git", "add", "-A"]),
            cwd=repo_dir,
            check=False,
            capture_output=True,
        )
        result = subprocess.run(
            dispatch._sudo_wrap(["git", *git_ident_flags, "commit", "-m", commit_msg]),
            cwd=repo_dir,
            check=False,
            capture_output=True,
        )
    return result


async def _run_talos(
    run: Run,
    engine: Engine,
    workspace: Path,
    item: dict[str, Any],
    project: dict[str, Any],
    timeout_seconds: int,
    *,
    attribution: str | None,
    register_cb: Callable[[subprocess.Popen[bytes]], None],
    workspace_repo_dirs: list[str] | None = None,
) -> None:
    """Talos execution branch: subprocess launch, commit/push, comment-back, status set.

    Entered from :func:`_dispatch_worker` when the resolved engine is in
    :data:`engines.TALOS_ENGINES`. Owns the full lifecycle:

    - Build the sudo-wrapped ``talos run …`` argv and pipe the TaskSpec JSON on
      stdin. Stdout and stderr are captured SEPARATELY (never merged like
      ``run_agent``'s transcript — the RunSummary is unparseable if streams mix).
    - Register the Popen so ``POST /runs/{id}/cancel`` can signal it.
    - Time out at ``timeout_seconds`` (kill + mark timed_out).
    - Missing-binary → engine-broke ``failed`` with ``'talos'`` in the error.
    - Exit 0: worker commits (``feat: <title>`` verbatim, ``-c user.name=<engine>
      -c user.email=<engine>@agent-gtd-dispatch``) and pushes, then verifies via
      :func:`dispatch.verify_pushes`. On successful push it PATCHes item status
      to ``review`` (best-effort; a failed status set does NOT flip the run to
      failed — mirrors the ollama-fallback comment-post's tolerance).
    - Exit 10/20/1 (or unpushed after exit 0): no commit, no push, no status set.
    - Every terminal exit posts a comment describing the outcome.
    - When ``workspace_repo_dirs`` is a non-empty list (workspace/multi-repo mode),
      the exit-0 git path loops per-repo subdir under ``workspace`` doing
      ``git add -A`` → ``git diff --cached --quiet`` staged-change detection →
      commit + push only for changed repos. No-change repos are skipped; if NO
      repo changed the run demotes to ``failed``. Verification is inline via each
      ``push_rc.returncode`` — the workspace path does NOT route through
      :func:`dispatch.verify_pushes`, and every terminal path returns before the
      tail ``build_comment_body`` block so exactly one comment fires.
    """
    item_id = run.item_id
    branch_name = run.branch_name
    assert item_id is not None  # noqa: S101 — talos is BUILD-only (validated at /dispatch)
    assert branch_name is not None  # noqa: S101 — set for BUILD mode

    # Serialize the TaskSpec — narrow projection (title, description,
    # acceptance_criteria, files_to_modify, gate_command). See talos.py.
    spec_json = talos.serialize_task_spec(item, project)

    # Build env from a base-filtered parent env + the per-engine overlay. The
    # overlay is the ONLY source of per-engine credentials; it never adds git
    # identity/credential keys (worker owns commit).
    filtered_base = {k: v for k, v in os.environ.items() if k in COMMON_ENV_KEYS}
    env = {**filtered_base, "HOME": str(Path.home())}
    env.update(talos.talos_env_overlay(engine.name))
    if attribution:
        env["AGENT_GTD_AGENT_NAME"] = attribution
    env["HEADLESS_BUILD_ENGINE"] = engine.name

    argv = talos.build_talos_argv(workspace, item_id, attempt=1)

    stdout_text = ""
    stderr_text = ""
    exit_code: int
    timed_out = False
    file_not_found = False

    def _launch_and_wait() -> tuple[int, str, str]:
        proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        register_cb(proc)
        try:
            out, err = proc.communicate(
                input=spec_json.encode("utf-8"), timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        return (
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    loop = asyncio.get_event_loop()
    try:
        exit_code, stdout_text, stderr_text = await loop.run_in_executor(
            None, _launch_and_wait
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
    except FileNotFoundError as exc:
        # config.TALOS_BIN not resolvable on PATH — mark failed with distinct
        # engine-broke wording naming the missing binary.
        file_not_found = True
        exit_code = -1
        stderr_text = f"talos binary not found: {exc}"

    now = datetime.now(UTC).isoformat()

    if timed_out:
        await db.update_run(
            run.id,
            status=RunStatus.timed_out,
            completed_at=now,
            error=f"Timed out after {timeout_seconds}s",
        )
        _publish_run_event(run.id, "timed_out", now)
        try:
            await gtd_client.post_comment(
                item_id,
                (
                    f"talos timed out after {timeout_seconds // 60} minutes "
                    f"(run `{run.id}`)."
                ),
                created_by=attribution or "agent-gtd-dispatch",
                token=run.callback_token,
            )
        except Exception:
            logger.warning("Failed to post talos timeout comment for run %s", run.id)
        return

    if file_not_found:
        await db.update_run(
            run.id,
            status=RunStatus.failed,
            completed_at=now,
            error=f"talos binary not found: {config.TALOS_BIN!r}",
        )
        _publish_run_event(run.id, "failed", now)
        try:
            await gtd_client.post_comment(
                item_id,
                (
                    "talos engine error (retryable/investigate): "
                    f"talos binary not found ({config.TALOS_BIN!r})."
                    f" run={run.id}"
                ),
                created_by=attribution or "agent-gtd-dispatch",
                token=run.callback_token,
            )
        except Exception:
            logger.warning(
                "Failed to post talos missing-binary comment for run %s", run.id
            )
        return

    # Take the last non-empty line of each stream. Talos writes exactly one JSON
    # RunSummary line on stdout on the success/blocked/failure paths and a
    # {"error": ...} line on stderr on the pre-run infra-error exit-1 path.
    def _last_line(text: str) -> str:
        for line in reversed(text.splitlines()):
            if line.strip():
                return line
        return ""

    stdout_line = _last_line(stdout_text)
    stderr_line = _last_line(stderr_text)

    status, should_push, comment_header = talos.map_talos_result(
        exit_code, stdout_line, stderr_line
    )

    if should_push:
        # Verified Done — the worker commits, pushes, verifies, and (if push
        # verification succeeds) PATCHes the item to review. Any failure below
        # demotes the run to `failed` and posts an appropriate comment.
        commit_msg = f"feat: {item['title']}"
        engine_ident = engine.name
        git_ident_flags = [
            "-c",
            f"user.name={engine_ident}",
            "-c",
            f"user.email={engine_ident}@agent-gtd-dispatch",
        ]

        if workspace_repo_dirs:
            # Workspace (multi-repo) path: per-repo add/detect/commit/push.
            # talos writes files across N side-by-side repos under `workspace`;
            # the worker owns git for each. Every terminal path below RETURNS
            # before the tail `build_comment_body` block so exactly one comment
            # fires. Verification is inline via each `push_rc.returncode` — the
            # workspace talos path does NOT route through `dispatch.verify_pushes`.
            committed: list[str] = []
            skipped: list[str] = []

            for repo_dir in workspace_repo_dirs:
                repo_path = workspace / repo_dir

                # (a) Stage every change in this repo subdir.
                add_rc = subprocess.run(
                    dispatch._sudo_wrap(["git", "add", "-A"]),
                    cwd=str(repo_path),
                    check=False,
                    capture_output=True,
                )
                if add_rc.returncode != 0:
                    _err = add_rc.stderr.decode("utf-8", errors="replace")[-300:]
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=now,
                        exit_code=exit_code,
                        error=f"git add failed in {repo_dir}: {_err}",
                    )
                    _publish_run_event(run.id, "failed", now)
                    try:
                        await gtd_client.post_comment(
                            item_id,
                            f"talos completed but `git add` failed in repo "
                            f"`{repo_dir}`: {_err}",
                            created_by=attribution or "agent-gtd-dispatch",
                            token=run.callback_token,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to post git-add failure comment for %s", run.id
                        )
                    return

                # (b) Staged-change detection: rc 0 → no staged changes (skip),
                # rc 1 → staged changes present (commit+push), rc>1 → error.
                diff_rc = subprocess.run(
                    dispatch._sudo_wrap(["git", "diff", "--cached", "--quiet"]),
                    cwd=str(repo_path),
                    check=False,
                    capture_output=True,
                )
                if diff_rc.returncode not in (0, 1):
                    _err = diff_rc.stderr.decode("utf-8", errors="replace")[-300:]
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=now,
                        exit_code=exit_code,
                        error=f"git diff failed in {repo_dir}: {_err}",
                    )
                    _publish_run_event(run.id, "failed", now)
                    try:
                        await gtd_client.post_comment(
                            item_id,
                            f"talos completed but `git diff --cached` failed in "
                            f"repo `{repo_dir}`: {_err}",
                            created_by=attribution or "agent-gtd-dispatch",
                            token=run.callback_token,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to post git-diff failure comment for %s", run.id
                        )
                    return

                if diff_rc.returncode == 0:
                    # No staged changes for this repo — skip commit/push.
                    skipped.append(repo_dir)
                    continue

                # (c) Staged changes present — commit + push this repo.
                commit_rc = _commit_with_retry(
                    str(repo_path), git_ident_flags, commit_msg
                )
                if commit_rc.returncode != 0:
                    _err = commit_rc.stderr.decode("utf-8", errors="replace")[-300:]
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=now,
                        exit_code=exit_code,
                        error=f"git commit failed in {repo_dir}: {_err}",
                    )
                    _publish_run_event(run.id, "failed", now)
                    try:
                        await gtd_client.post_comment(
                            item_id,
                            f"talos completed but `git commit` failed in repo "
                            f"`{repo_dir}`: {_err}",
                            created_by=attribution or "agent-gtd-dispatch",
                            token=run.callback_token,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to post git-commit failure comment for %s",
                            run.id,
                        )
                    return

                push_rc = subprocess.run(
                    dispatch._sudo_wrap(
                        ["git", "push", "--no-verify", "-u", "origin", branch_name]
                    ),
                    cwd=str(repo_path),
                    check=False,
                    capture_output=True,
                )
                if push_rc.returncode != 0:
                    _err = push_rc.stderr.decode("utf-8", errors="replace")[-300:]
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=now,
                        exit_code=exit_code,
                        error=f"git push failed in {repo_dir}: {_err}",
                    )
                    _publish_run_event(run.id, "failed", now)
                    try:
                        await gtd_client.post_comment(
                            item_id,
                            f"talos completed but `git push` failed in repo "
                            f"`{repo_dir}`: {_err}",
                            created_by=attribution or "agent-gtd-dispatch",
                            token=run.callback_token,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to post git-push failure comment for %s", run.id
                        )
                    return

                committed.append(repo_dir)

            # Per-repo loop complete — decide terminal state.
            if not committed:
                # No repo had staged changes — talos returned Done but produced
                # no committed work across any workspace repo. Demote to failed.
                await db.update_run(
                    run.id,
                    status=RunStatus.failed,
                    completed_at=now,
                    exit_code=exit_code,
                    error="talos Done but no committed changes across workspace repos",
                )
                _publish_run_event(run.id, "failed", now)
                try:
                    await gtd_client.post_comment(
                        item_id,
                        f"talos reported Done but produced no committed changes "
                        f"across any workspace repo (run `{run.id}`).",
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning("Failed to post no-changes comment for %s", run.id)
                return

            # At least one repo committed+pushed — mark run succeeded, set item
            # status best-effort, then post ONE summary comment naming committed
            # vs skipped repos. Returns before the tail `build_comment_body` block
            # so exactly one comment fires on the workspace success path.
            await db.update_run(
                run.id,
                status=RunStatus.succeeded,
                completed_at=now,
                exit_code=exit_code,
            )
            _publish_run_event(run.id, "succeeded", now)

            # Status-set is deliberately tolerant: a PATCH failure does NOT flip
            # the run to failed (mirrors the monorepo path's try/except).
            try:
                await gtd_client.set_item_status(
                    item_id, "review", token=run.callback_token
                )
            except Exception:
                logger.warning(
                    "Failed to set item %s status=review (run %s) — run stays succeeded",
                    item_id,
                    run.id,
                )

            skipped_fragment = (
                f"; skipped (no changes): {', '.join(skipped)}" if skipped else ""
            )
            try:
                await gtd_client.post_comment(
                    item_id,
                    f"talos Done — committed+pushed repos: {', '.join(committed)}"
                    f"{skipped_fragment} (run `{run.id}`, branch `{branch_name}`).",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )
            except Exception:
                logger.warning(
                    "Failed to post talos workspace success comment for %s", run.id
                )
            return

        # Monorepo path (workspace_repo_dirs is None): single-repo add/commit/push.
        # Stage every worktree change (talos-written files under workspace).
        add_rc = subprocess.run(
            dispatch._sudo_wrap(["git", "add", "-A"]),
            cwd=str(workspace),
            check=False,
            capture_output=True,
        )
        if add_rc.returncode != 0:
            _err = add_rc.stderr.decode("utf-8", errors="replace")[-300:]
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=now,
                exit_code=exit_code,
                error=f"git add failed: {_err}",
            )
            _publish_run_event(run.id, "failed", now)
            try:
                await gtd_client.post_comment(
                    item_id,
                    f"talos completed but `git add` failed: {_err}",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )
            except Exception:
                logger.warning("Failed to post git-add failure comment for %s", run.id)
            return

        commit_rc = _commit_with_retry(str(workspace), git_ident_flags, commit_msg)
        if commit_rc.returncode != 0:
            _err = commit_rc.stderr.decode("utf-8", errors="replace")[-300:]
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=now,
                exit_code=exit_code,
                error=f"git commit failed: {_err}",
            )
            _publish_run_event(run.id, "failed", now)
            try:
                await gtd_client.post_comment(
                    item_id,
                    f"talos completed but `git commit` failed: {_err}",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )
            except Exception:
                logger.warning(
                    "Failed to post git-commit failure comment for %s", run.id
                )
            return

        push_rc = subprocess.run(
            dispatch._sudo_wrap(
                ["git", "push", "--no-verify", "-u", "origin", branch_name]
            ),
            cwd=str(workspace),
            check=False,
            capture_output=True,
        )
        if push_rc.returncode != 0:
            _err = push_rc.stderr.decode("utf-8", errors="replace")[-300:]
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=now,
                exit_code=exit_code,
                error=f"git push failed: {_err}",
            )
            _publish_run_event(run.id, "failed", now)
            try:
                await gtd_client.post_comment(
                    item_id,
                    f"talos completed but `git push` failed: {_err}",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )
            except Exception:
                logger.warning("Failed to post git-push failure comment for %s", run.id)
            return

        # Push succeeded — mark the run succeeded, set item status best-effort,
        # then post the success comment.
        await db.update_run(
            run.id,
            status=RunStatus.succeeded,
            completed_at=now,
            exit_code=exit_code,
        )
        _publish_run_event(run.id, "succeeded", now)

        # Status-set is deliberately tolerant: a PATCH failure does NOT flip the
        # run to failed (mirrors the ollama-fallback comment-post's try/except).
        try:
            await gtd_client.set_item_status(
                item_id, "review", token=run.callback_token
            )
        except Exception:
            logger.warning(
                "Failed to set item %s status=review (run %s) — run stays succeeded",
                item_id,
                run.id,
            )
    else:
        # Every non-success terminal exit: mark failed with exit_code + error.
        # For exit 20 (task failed) with a parseable `{"Failed": {"mode": ...}}`
        # disposition, enrich the `error` so `agent-gtd run-status` surfaces the
        # actionable RE-DECOMPOSE-vs-RE-DISPATCH hint too (not only the GTD
        # comment). Parsing is defensive: any JSON/shape failure leaves `error`
        # as the plain header and never raises from this path.
        error = comment_header
        if exit_code == 20 and stdout_line:
            try:
                _summary = json.loads(stdout_line)
                _mode = _summary["disposition"]["Failed"]["mode"]
                if isinstance(_mode, str):
                    error = f"{comment_header}\n{talos.failure_mode_guidance(_mode)}"
            except (json.JSONDecodeError, KeyError, TypeError):
                error = comment_header
        await db.update_run(
            run.id,
            status=status,
            completed_at=now,
            exit_code=exit_code,
            error=error,
        )
        _publish_run_event(run.id, status.value, now)

    # Comment-back on every terminal exit — talos has no GTD access so this
    # comment is the reviewer's only surface for the mechanical verification
    # evidence embedded in the RunSummary.
    try:
        body = talos.build_comment_body(
            exit_code, stdout_line, stderr_line, branch_name
        )
        await gtd_client.post_comment(
            item_id,
            body,
            created_by=attribution or "agent-gtd-dispatch",
            token=run.callback_token,
        )
    except Exception:
        logger.warning("Failed to post talos outcome comment for run %s", run.id)


async def _dispatch_worker(
    run: Run,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    *,
    attribution: str | None = None,
    manage_retry_count: int = 0,
    is_recovery: bool = False,
) -> None:
    """Background task that executes a dispatch run."""
    _run_start_dt: datetime = datetime.now(UTC)
    now = _run_start_dt.isoformat()
    await db.update_run(run.id, status=RunStatus.running, started_at=now)
    _publish_run_event(run.id, "running", None)

    # Register manage-mode run so the watchdog can find it
    if run.mode == DispatchMode.MANAGE and run.rollout_id:
        _rollout_to_run[run.rollout_id] = run
        logger.info(
            "manage: spawn rollout_id=%s run_id=%s engine=%s retry_count=%d",
            run.rollout_id,
            run.id,
            engine.name,
            manage_retry_count,
        )

    # --- Ollama health check + fallback ---
    engine_used = engine  # may be replaced below
    if engine.name == "claude-code-ollama":
        ok, reason = await _ollama_health_check()
        if not ok:
            logger.warning(
                "Ollama health check failed for run %s: %s — falling back to claude",
                run.id,
                reason,
            )
            engine_used = get_engine("claude-code")
            # Persist fallback signal to DB BEFORE attempting comment post so
            # that a comment-post outage cannot leave the operator with zero info.
            await db.update_run(
                run.id,
                engine_actual=engine_used.name,
                error=f"ollama_fallback: {reason}",
            )
            fallback_msg = (
                f"⚠️ Engine fallback: {reason}. Using claude-code (Anthropic) instead."
            )
            if run.item_id is not None:
                try:
                    await gtd_client.post_comment(
                        run.item_id,
                        fallback_msg,
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post Ollama fallback comment for run %s", run.id
                    )
        else:
            timeout_seconds = int(timeout_seconds * config.OLLAMA_TIMEOUT_MULTIPLIER)

    mode = run.mode
    workspace = None
    # For manage mode: preserve workspace on failure for debugging.
    # For build/plan mode: always clean up.
    should_cleanup = True
    _human_cancelled = False
    _exit_code: int | None = None
    _run_timed_out = False
    _agent_start_dt: datetime | None = None

    try:
        # Fetch item and project.
        # manage-mode runs have item_id=None — derive project from the rollout instead.
        item: dict[str, Any] = {}
        if run.item_id is not None:
            # Preflight: verify the item is accessible with the run's own credential
            # BEFORE cloning the workspace or spawning the agent subprocess.
            # This is the fail-fast guard for the "agent can't see its own GTD item"
            # failure mode (see kb-03189). A transient 5xx error re-raises so that
            # existing generic error handling proceeds unchanged — no new failure class.
            try:
                item = await gtd_client.get_item(run.item_id, token=run.callback_token)
            except httpx.HTTPStatusError as _preflight_exc:
                if not gtd_client.is_authoritative_item_error(_preflight_exc):
                    raise  # transient (5xx etc.) — existing outer handler takes over
                # Authoritative failure (401/403/404): abort before clone/spawn.
                _credential_desc = (
                    "per-run callback_token"
                    if run.callback_token
                    else "static service key"
                )
                _preflight_error = (
                    f"preflight failed: item {run.item_id!r} is not visible "
                    f"to the run's credential ({_credential_desc}); "
                    f"upstream HTTP {_preflight_exc.response.status_code}. "
                    "Check that the dispatching user has access to this item."
                )
                _preflight_now = datetime.now(UTC).isoformat()
                await db.update_run(
                    run.id,
                    status=RunStatus.failed,
                    completed_at=_preflight_now,
                    error=_preflight_error,
                )
                _publish_run_event(run.id, "failed", _preflight_now)
                # Report through the service's own credential — try the run's
                # callback_token first (it may have POST access even if GET
                # failed), then fall back to the static service key (token=None
                # → _request uses config.AGENT_GTD_API_KEY). This keeps the
                # reporting channel independent of the broken read credential.
                _preflight_comment = (
                    f"Run `{run.id}` aborted (preflight failed): "
                    f"item `{run.item_id}` is not visible to the run's credential "
                    f"({_credential_desc}, "
                    f"HTTP {_preflight_exc.response.status_code}). "
                    "The dispatching user may not have access to this item."
                )
                _preflight_reported = False
                for _try_token in (run.callback_token, None):
                    try:
                        await gtd_client.post_comment(
                            run.item_id,
                            _preflight_comment,
                            created_by=attribution or "agent-gtd-dispatch",
                            token=_try_token,
                        )
                        _preflight_reported = True
                        break
                    except Exception as _comment_exc:
                        logger.debug(
                            "Preflight comment attempt failed (token=%s): %s",
                            "callback_token" if _try_token else "static",
                            _comment_exc,
                        )
                        continue
                if not _preflight_reported:
                    logger.warning(
                        "Preflight abort: failed to post error comment "
                        "run_id=%s item_id=%s",
                        run.id,
                        run.item_id,
                    )
                return  # Abort — do not clone workspace or spawn agent
            project_id = item.get("project_id")
            if not project_id:
                raise ValueError("Item has no project assigned")
            project = await gtd_client.get_project(project_id, token=run.callback_token)
        else:
            assert run.rollout_id is not None  # noqa: S101 — guaranteed by route handler
            rollout_info = await gtd_client.get_rollout(
                run.rollout_id, token=run.callback_token
            )
            project_id = rollout_info.get("project_id")
            if not project_id:
                raise ValueError("Rollout has no project assigned")
            project = await gtd_client.get_project(project_id, token=run.callback_token)

        # Build workspace
        is_workspace_mode = (project.get("repo_mode") or "") == "workspace"
        workspace_repo_dirs: list[str] | None = None
        # For BUILD mode: list of (repo_name, repo_path, base_sha) passed to
        # verify_pushes after the agent exits.  None means skip verification
        # (manage/plan mode or exceptions during workspace prep).
        _verify_repos: list[tuple[str, Path, str]] | None = None
        workspace_repos: list[str]

        if mode == DispatchMode.MANAGE:
            if is_workspace_mode:
                # Multi-repo workspace manage path
                workspace_repos = project.get("workspace_repos") or []
                if not workspace_repos:
                    raise ValueError(
                        "workspace_repos must be non-empty for workspace mode"
                    )
                workspace = dispatch.prepare_manage_workspace_multi(
                    workspace_repos, run.id
                )
                await db.update_run(run.id, workspace_path=str(workspace))
                workspace_repo_dirs = [
                    dispatch.repo_dir_from_url(url) for url in workspace_repos
                ]
            else:
                # Monorepo/single-repo manage path
                git_origin = project.get("git_origin", "")
                if not git_origin:
                    raise ValueError(f"Project '{project['name']}' has no git_origin")
                workspace = dispatch.prepare_manage_workspace(git_origin, run.id)
                await db.update_run(run.id, workspace_path=str(workspace))
            attachments = []
        elif is_workspace_mode:
            # Multi-repo workspace path
            if run.branch_name is None:  # pragma: no cover
                raise ValueError("branch_name must be set for non-manage mode runs")
            workspace_repos = project.get("workspace_repos") or []
            if not workspace_repos:
                raise ValueError("workspace_repos must be non-empty for workspace mode")
            workspace = dispatch.prepare_workspace_multi(
                workspace_repos, run.id, run.branch_name
            )
            await db.update_run(run.id, workspace_path=str(workspace))
            workspace_repo_dirs = [
                dispatch.repo_dir_from_url(url) for url in workspace_repos
            ]
            # Capture base SHAs for BUILD mode verification — BEFORE stage_attachments
            if mode == DispatchMode.BUILD:
                _verify_repos = [
                    (
                        dispatch.repo_dir_from_url(url),
                        workspace / dispatch.repo_dir_from_url(url),
                        dispatch.get_head_sha(
                            workspace / dispatch.repo_dir_from_url(url)
                        ),
                    )
                    for url in workspace_repos
                ]
            # Stage attachments — item_id guaranteed non-None for non-manage modes
            assert run.item_id is not None  # noqa: S101
            attachments = await dispatch.stage_attachments(
                workspace, run.id, run.item_id, token=run.callback_token
            )
        else:
            # Monorepo path (default — absent/None/empty/unrecognized repo_mode)
            git_origin = project.get("git_origin", "")
            if not git_origin:
                raise ValueError(f"Project '{project['name']}' has no git_origin")
            if (
                mode == DispatchMode.BUILD and run.branch_name is None
            ):  # pragma: no cover
                raise ValueError("branch_name must be set for build-mode runs")
            workspace = dispatch.prepare_workspace(
                git_origin, run.id, run.branch_name or ""
            )
            await db.update_run(run.id, workspace_path=str(workspace))
            # Capture base SHA for BUILD mode verification — BEFORE stage_attachments
            if mode == DispatchMode.BUILD:
                _verify_repos = [
                    (
                        dispatch.repo_name_from_origin(git_origin),
                        workspace,
                        dispatch.get_head_sha(workspace),
                    )
                ]

            # Stage any attachments into {run_id}-attachments/ inside the workspace
            # item_id is guaranteed non-None for non-manage modes (validated at route layer)
            assert run.item_id is not None  # noqa: S101
            attachments = await dispatch.stage_attachments(
                workspace, run.id, run.item_id, token=run.callback_token
            )

        # --- Pre-launch gate install -----------------------------------
        # Deterministic per-repo hook-manager install + verification, run
        # after clone and BEFORE the agent launches. Talos self-gates via
        # gate_command and commits its own output once (installed hooks,
        # especially fixers, would mutate or block that commit — kb-03099),
        # so talos runs skip this entirely. Plan runs never clone a
        # mutable workspace the agent will commit into, so they're exempt
        # too.
        gate_steps: list[gates.GateStep] = []
        _mode_str = mode.value if hasattr(mode, "value") else str(mode)
        if mode not in (DispatchMode.BUILD, DispatchMode.MANAGE):
            logger.info(
                "gate: run_id=%s mode=%s engine=%s decision=skipped reason=%s",
                run.id,
                _mode_str,
                engine_used.name,
                "plan",
            )
        elif is_talos_engine(engine_used.name):
            logger.info(
                "gate: run_id=%s mode=%s engine=%s decision=skipped reason=%s",
                run.id,
                _mode_str,
                engine_used.name,
                "talos",
            )
        elif workspace is None:
            logger.info(
                "gate: run_id=%s mode=%s engine=%s decision=skipped reason=%s",
                run.id,
                _mode_str,
                engine_used.name,
                "no-workspace",
            )
        else:
            if workspace_repo_dirs:
                gate_repos = [(d, workspace / d) for d in workspace_repo_dirs]
            else:
                gate_repos = [
                    (
                        dispatch.repo_name_from_origin(project.get("git_origin", "")),
                        workspace,
                    )
                ]
            gate_steps = gates.detect_gate_steps(gate_repos, run_id=run.id)
            if gate_steps:
                gate_failure = await asyncio.get_event_loop().run_in_executor(
                    dispatch._executor,
                    functools.partial(
                        gates.run_gate_steps,
                        gate_steps,
                        config.GATE_INSTALL_TIMEOUT_SECONDS,
                        run_id=run.id,
                    ),
                )
                if gate_failure is not None:
                    logger.warning(
                        "gate: run_id=%s repo=%s step=%r decision=failed "
                        "reason=%r duration_ms=%d",
                        run.id,
                        gate_failure.repo_label,
                        gate_failure.step,
                        gate_failure.reason,
                        gate_failure.duration_ms,
                    )
                    _gate_fail_now = datetime.now(UTC).isoformat()
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=_gate_fail_now,
                        error=(
                            f"gate install failed: {gate_failure.repo_label}: "
                            f"{gate_failure.step}: {gate_failure.reason}"
                        )[:500],
                    )
                    _publish_run_event(run.id, "failed", _gate_fail_now)
                    if run.item_id is not None:
                        try:
                            await gtd_client.post_comment(
                                run.item_id,
                                gates.format_failure_comment(run.id, gate_failure),
                                created_by=attribution or "agent-gtd-dispatch",
                                token=run.callback_token,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to post gate-failure comment for run %s",
                                run.id,
                            )
                    if mode == DispatchMode.MANAGE and run.rollout_id:
                        _gate_halt_tokens = (
                            [run.callback_token, None] if run.callback_token else [None]
                        )
                        for _gate_tok in _gate_halt_tokens:
                            try:
                                await gtd_client.halt_rollout(
                                    run.rollout_id,
                                    reason=(
                                        f"gate install failed in "
                                        f"{gate_failure.repo_label}: "
                                        f"{gate_failure.step}: "
                                        f"{gate_failure.reason}"
                                    )[:500],
                                    comment=gates.format_failure_comment(
                                        run.id, gate_failure
                                    ),
                                    token=_gate_tok,
                                )
                                break
                            except Exception:
                                logger.exception(
                                    "Failed to halt rollout %s after gate "
                                    "failure (token=%s)",
                                    run.rollout_id,
                                    "callback_token" if _gate_tok else "static",
                                )
                    return

        system_prompt = dispatch.build_system_prompt(
            item,
            project,
            run.branch_name,
            max_turns,
            mode=mode,
            attachments=attachments,
            run_id=run.id,
            rollout_id=run.rollout_id,
            manage_retry_count=manage_retry_count,
            workspace_repo_dirs=workspace_repo_dirs,
            is_recovery=is_recovery,
        )

        item_title = item.get("title", f"rollout:{run.rollout_id}")
        if mode == DispatchMode.MANAGE:
            dispatch_comment = (
                f"Rollout manager dispatched (run `{run.id}`, engine: {engine_used.name}). "
                f"Managing rollout `{run.rollout_id}` in `{project['name']}`."
            )
        else:
            dispatch_comment = (
                f"Agent dispatched (run `{run.id}`, engine: {engine_used.name}). "
                f"Working on branch `{run.branch_name}` in `{project['name']}`."
            )

        if gate_steps:
            dispatch_comment += gates.format_success_lines(gate_steps)

        if run.item_id is not None:
            await gtd_client.post_comment(
                run.item_id,
                dispatch_comment,
                created_by=attribution or "agent-gtd-dispatch",
                token=run.callback_token,
            )

        def _register_subprocess(proc: subprocess.Popen[bytes]) -> None:
            _active_subprocesses[run.id] = proc

        # Defense in depth: talos is build-mode only (no GTD/MCP access, and
        # the worker below commits + pushes its own output — a plan/manage
        # run handed a talos engine would try to build and commit code
        # instead of writing a spec / managing a rollout). The run-creation
        # endpoint already rejects this combination with HTTP 422 when the
        # engine is known at request time; this guard catches any path where
        # the resolved engine only becomes talos afterward (e.g. a relaunch/
        # retry) so such a run can never reach `_run_talos`.
        if is_talos_engine(engine_used.name) and mode != DispatchMode.BUILD:
            _talos_mode_str = mode.value if hasattr(mode, "value") else str(mode)
            _talos_reject_msg = (
                f"Engine '{engine_used.name}' is a talos engine; talos is "
                f"build mode only, refusing to run in {_talos_mode_str} mode."
            )
            logger.error(
                "run %s: rejecting talos engine in non-build mode: %s",
                run.id,
                _talos_reject_msg,
            )
            _talos_reject_now = datetime.now(UTC).isoformat()
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=_talos_reject_now,
                error=_talos_reject_msg,
            )
            _publish_run_event(run.id, "failed", _talos_reject_now)
            if run.item_id is not None:
                try:
                    await gtd_client.post_comment(
                        run.item_id,
                        _talos_reject_msg,
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post talos-reject comment for run %s", run.id
                    )
            return

        # Talos branch: separate execution path that owns git + comment-back
        # inline. Enters INSTEAD of run_agent + verify_pushes because talos has
        # no GTD access (by design) and never runs `git commit` itself — the
        # worker mints commit + push + status on exit 0 only.
        if is_talos_engine(engine_used.name):
            await _run_talos(
                run,
                engine_used,
                workspace,
                item,
                project,
                timeout_seconds,
                attribution=attribution,
                register_cb=_register_subprocess,
                workspace_repo_dirs=workspace_repo_dirs,
            )
            return

        _agent_start_dt = datetime.now(UTC)
        result = await dispatch.run_agent(
            engine_used,
            workspace,
            system_prompt,
            item_title,
            max_turns,
            run.agent_name,
            timeout_seconds,
            mode=mode,
            attribution=attribution,
            popen_callback=_register_subprocess,
            callback_token=run.callback_token,
        )
        _exit_code = result.returncode

        completed = datetime.now(UTC).isoformat()
        if result.returncode == 0:
            # BUILD mode: verify that the agent pushed its work before marking succeeded.
            # Plan and manage modes are exempt (_verify_repos is None for those).
            push_results_list: list[RepoPushStatus] | None = None
            _push_results_json: str | None = None
            _rescued_repos: list[RepoPushStatus] = []
            _gate_cmd: str = ""
            _gate_result: dispatch.GateResult | None = None
            _stashed_names: list[str] = []
            if _verify_repos is not None:
                push_results_list = dispatch.verify_pushes(
                    _verify_repos, run.branch_name or ""
                )
                unpushed = [
                    r for r in push_results_list if r.status == PushStatus.unpushed
                ]

                # Push backstop: the agent exited 0 but left committed work
                # unpushed (e.g. it backgrounded `git push` and its session ended
                # before a slow pre-push hook finished). Attempt one bounded,
                # hooks-enabled push per eligible repo before declaring failure.
                eligible = [r for r in unpushed if r.local_sha is not None]
                if eligible:
                    _repo_paths = {
                        _name: _path for _name, _path, _base in _verify_repos
                    }
                    _attempted_names: set[str] = set()
                    _elapsed = (datetime.now(UTC) - _run_start_dt).total_seconds()
                    _remaining = timeout_seconds - _elapsed
                    if _remaining > config.PUSH_BACKSTOP_MIN_SECONDS:
                        _loop = asyncio.get_event_loop()
                        for _r in eligible:
                            _elapsed = (
                                datetime.now(UTC) - _run_start_dt
                            ).total_seconds()
                            _remaining = timeout_seconds - _elapsed
                            if _remaining <= config.PUSH_BACKSTOP_MIN_SECONDS:
                                break
                            _repo_path = _repo_paths.get(_r.repo_name)
                            if _repo_path is None:
                                continue
                            _attempted_names.add(_r.repo_name)
                            try:
                                await _loop.run_in_executor(
                                    dispatch._executor,
                                    dispatch.push_unpushed_repo,
                                    _repo_path,
                                    run.branch_name or "",
                                    _remaining,
                                )
                            except Exception:
                                logger.exception(
                                    "push backstop: attempt failed for repo %s"
                                    " (run %s)",
                                    _r.repo_name,
                                    run.id,
                                )
                        # Re-classify after the backstop attempt(s).
                        push_results_list = dispatch.verify_pushes(
                            _verify_repos, run.branch_name or ""
                        )
                        _status_by_name = {r.repo_name: r for r in push_results_list}
                        _rescued_repos = [
                            _old
                            for _old in eligible
                            if _old.repo_name in _attempted_names
                            and _status_by_name.get(_old.repo_name) is not None
                            and _status_by_name[_old.repo_name].status
                            != PushStatus.unpushed
                        ]
                        unpushed = [
                            r
                            for r in push_results_list
                            if r.status == PushStatus.unpushed
                        ]

                if unpushed:
                    # Build error string: prefix once, one fragment per unpushed repo
                    fragments = []
                    for r in unpushed:
                        if r.local_sha is not None:
                            fragments.append(
                                f"{r.repo_name}: {r.commits_ahead} unpushed commit(s)"
                                f" on {r.branch}"
                            )
                        else:
                            fragments.append(
                                f"{r.repo_name}: verification error on {r.branch}"
                            )
                    error_str = "push verification failed: " + "; ".join(fragments)
                    _push_results_json = json.dumps(
                        [r.model_dump(mode="json") for r in push_results_list]
                    )
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=completed,
                        exit_code=result.returncode,
                        error=error_str,
                        push_results=_push_results_json,
                    )
                    _publish_run_event(run.id, "failed", completed)
                    should_cleanup = (
                        False  # preserve workspace — commits only exist in clone
                    )
                    # Post per-repo comment
                    if run.item_id is not None:
                        comment_lines = [f"Push verification failed (run `{run.id}`):"]
                        for r in push_results_list:
                            if r.status == PushStatus.pushed:
                                line = (
                                    f"- {r.repo_name}: pushed"
                                    f" ({r.commits_ahead} commit(s),"
                                    f" {(r.local_sha or '')[:8]})"
                                )
                            elif r.status == PushStatus.no_changes:
                                line = f"- {r.repo_name}: no changes"
                            else:
                                # unpushed
                                if r.local_sha is not None:
                                    line = (
                                        f"- {r.repo_name}: UNPUSHED —"
                                        f" {r.commits_ahead} local commit(s) not on origin"
                                    )
                                else:
                                    line = f"- {r.repo_name}: UNPUSHED — verification error"
                            if r.dirty:
                                line += " [dirty working tree]"
                            comment_lines.append(line)
                        if workspace is not None:
                            comment_lines.append(f"Workspace preserved at {workspace}")
                        await gtd_client.post_comment(
                            run.item_id,
                            "\n".join(comment_lines),
                            created_by=attribution or "agent-gtd-dispatch",
                            token=run.callback_token,
                        )
                    return  # exit early — do not mark succeeded

                # Zero-commits guard: all repos idle → may be a silent no-op.
                # Distinguish intentional no-op (agent posted an explanatory
                # comment) from a silent failure (agent exited without doing
                # anything) by counting comments posted since this run started.
                # Rule: ≥2 comments since run start = dispatch comment + ≥1
                # agent comment → intentional no-op → pass.  <2 = silent
                # failure → fail.
                _all_no_changes = (
                    isinstance(push_results_list, list)
                    and bool(push_results_list)
                    and all(
                        r.status == PushStatus.no_changes for r in push_results_list
                    )
                )
                if _all_no_changes:
                    _is_intentional_noop = False
                    if run.item_id is not None:
                        try:
                            _comments = await gtd_client.list_comments(
                                run.item_id, token=run.callback_token
                            )
                            _post_start = [
                                c
                                for c in _comments
                                if datetime.fromisoformat(
                                    c.get("created_at", "1970-01-01T00:00:00+00:00")
                                )
                                >= _run_start_dt
                            ]
                            _is_intentional_noop = len(_post_start) >= 2
                        except Exception:
                            logger.warning(
                                "Zero-commits guard: list_comments failed for run %s"
                                " — treating as silent failure",
                                run.id,
                            )
                    if not _is_intentional_noop:
                        _push_results_json = json.dumps(
                            [r.model_dump(mode="json") for r in push_results_list]
                        )
                        error_str = (
                            "build run produced zero commits across all repos"
                            " and pushed no branch"
                        )
                        await db.update_run(
                            run.id,
                            status=RunStatus.failed,
                            completed_at=completed,
                            exit_code=result.returncode,
                            error=error_str,
                            push_results=_push_results_json,
                        )
                        _publish_run_event(run.id, "failed", completed)
                        if run.item_id is not None:
                            try:
                                await gtd_client.post_comment(
                                    run.item_id,
                                    (
                                        f"Build run produced no commits"
                                        f" (run `{run.id}`). The agent exited"
                                        " cleanly but made no changes — possible"
                                        " silent failure. Check the transcript."
                                    ),
                                    created_by=attribution or "agent-gtd-dispatch",
                                    token=run.callback_token,
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to post zero-commits comment for run %s",
                                    run.id,
                                )
                        return  # exit early — do not mark succeeded

                # Post-run gate: run the project's quality gate (non-talos build
                # runs only) after push verification has succeeded, so a hook
                # bypass (--no-verify, a self-skipping hook) can't slip a
                # gate-failing tree past dispatch as a reported success.
                _raw_gate = project.get("gate_command")
                _gate_cmd = _raw_gate.strip() if isinstance(_raw_gate, str) else ""
                _n_pushed = sum(
                    1 for r in push_results_list if r.status == PushStatus.pushed
                )
                _gate_timeout: int | None = None
                _gate_remaining: float | None = None
                _gate_rc: int | None = None
                _gate_timed_out: bool | None = None
                _gate_duration: float | None = None

                if _n_pushed == 0:
                    decision = "skipped_no_pushed_repo"
                elif not _gate_cmd:
                    decision = "skipped_no_gate_command"
                else:
                    decision = None  # gate runs below

                if decision is None:
                    assert workspace is not None  # noqa: S101
                    _gate_remaining = (
                        timeout_seconds
                        - (datetime.now(UTC) - _run_start_dt).total_seconds()
                    )
                    _gate_timeout = max(
                        int(_gate_remaining), config.POST_RUN_GATE_MIN_SECONDS
                    )
                    _stashed_names = [r.repo_name for r in push_results_list if r.dirty]
                    _dirty_paths = [
                        p for n, p, _b in _verify_repos if n in _stashed_names
                    ]
                    logger.info(
                        "run %s: post-run gate starting (timeout %ds)",
                        run.id,
                        _gate_timeout,
                    )
                    _gate_result = await asyncio.get_event_loop().run_in_executor(
                        dispatch._executor,
                        dispatch.run_gate_command,
                        workspace,
                        _gate_cmd,
                        _gate_timeout,
                        engine_used,
                        _register_subprocess,
                        _dirty_paths or None,
                    )
                    assert _gate_result is not None  # noqa: S101
                    completed = datetime.now(UTC).isoformat()
                    _gate_rc = _gate_result.returncode
                    _gate_timed_out = _gate_result.timed_out
                    _gate_duration = _gate_result.duration_seconds
                    if _gate_result.timed_out:
                        decision = "timed_out"
                    elif _gate_result.returncode is None:
                        decision = "launch_error"
                    elif _gate_result.returncode != 0:
                        decision = "failed"
                    else:
                        decision = "passed"

                logger.info(
                    "post-run gate: run_id=%s decision=%s project=%s"
                    " pushed_repos=%d timeout_s=%s floor_applied=%s"
                    " returncode=%s timed_out=%s duration_s=%s",
                    run.id,
                    decision,
                    project.get("name"),
                    _n_pushed,
                    _gate_timeout or None,
                    (
                        _gate_remaining is not None
                        and _gate_remaining < config.POST_RUN_GATE_MIN_SECONDS
                    )
                    or None,
                    _gate_rc or None,
                    _gate_timed_out or None,
                    (f"{_gate_duration:.1f}" if _gate_duration is not None else None),
                )

                if _gate_result is not None and not _gate_result.passed:
                    if decision == "timed_out":
                        error_str = f"post-run gate timed out after {_gate_timeout}s"
                    elif (
                        decision == "failed" and _gate_rc is not None and _gate_rc >= 0
                    ):
                        error_str = f"post-run gate failed: exit {_gate_rc}"
                    elif decision == "failed":
                        assert _gate_rc is not None  # noqa: S101
                        error_str = (
                            f"post-run gate failed: killed by signal {-_gate_rc}"
                        )
                    else:  # launch_error
                        error_str = "post-run gate failed: launch error"

                    _push_results_json = json.dumps(
                        [r.model_dump(mode="json") for r in push_results_list]
                    )
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=completed,
                        exit_code=result.returncode,
                        error=error_str,
                        push_results=_push_results_json,
                    )
                    _publish_run_event(run.id, "failed", completed)
                    logger.warning(
                        "post-run gate: run_id=%s decision=%s output_tail=%r",
                        run.id,
                        decision,
                        _gate_result.output[-1000:],
                    )
                    if run.item_id is not None:
                        if decision == "timed_out":
                            _gate_first_line = (
                                f"Post-run gate timed out (run `{run.id}`):"
                                f" `{_gate_cmd}` did not finish within"
                                f" {_gate_timeout}s. Branch `{run.branch_name}`"
                                " was pushed but was not gate-verified."
                            )
                        elif (
                            decision == "failed"
                            and _gate_rc is not None
                            and _gate_rc >= 0
                        ):
                            _gate_first_line = (
                                f"Post-run gate failed (run `{run.id}`):"
                                f" `{_gate_cmd}` exited {_gate_rc} after"
                                f" {int(_gate_duration or 0)}s. Branch"
                                f" `{run.branch_name}` was pushed but does not"
                                " pass the project gate."
                            )
                        elif decision == "failed":
                            assert _gate_rc is not None  # noqa: S101
                            _gate_first_line = (
                                f"Post-run gate failed (run `{run.id}`):"
                                f" `{_gate_cmd}` was killed by signal"
                                f" {-_gate_rc} after {int(_gate_duration or 0)}s."
                                f" Branch `{run.branch_name}` was pushed but"
                                " does not pass the project gate."
                            )
                        else:  # launch_error
                            _gate_first_line = (
                                f"Post-run gate failed (run `{run.id}`):"
                                f" `{_gate_cmd}` could not be launched. Branch"
                                f" `{run.branch_name}` was pushed but was not"
                                " gate-verified."
                            )
                        _gate_comment = (
                            _gate_first_line
                            + "\n\nGate output (tail):\n\n````\n"
                            + (_gate_result.output or "(no output)").rstrip("\n")
                            + "\n````"
                        )
                        if decision != "launch_error" and _stashed_names:
                            _gate_comment += (
                                "\n\nUncommitted changes in"
                                f" {', '.join(_stashed_names)} were stashed"
                                " before the gate ran, so the gate checked"
                                " only the committed work."
                            )
                        try:
                            await gtd_client.post_comment(
                                run.item_id,
                                _gate_comment,
                                created_by=attribution or "agent-gtd-dispatch",
                                token=run.callback_token,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to post post-run gate failure comment"
                                " for run %s",
                                run.id,
                            )
                    return  # exit early — do not mark succeeded

            # All pushed (or no BUILD verification needed) — mark succeeded
            if push_results_list is not None:
                _push_results_json = json.dumps(
                    [r.model_dump(mode="json") for r in push_results_list]
                )
            await db.update_run(
                run.id,
                status=RunStatus.succeeded,
                completed_at=completed,
                exit_code=result.returncode,
                push_results=_push_results_json,
            )
            _publish_run_event(run.id, "succeeded", completed)
            if _rescued_repos and run.item_id is not None:
                _rescue_header = (
                    f"Push verification found unpushed work after the agent"
                    f" exited (run `{run.id}`). The dispatch worker completed"
                    " the push before the run would have failed:"
                )
                _rescue_bullets = "\n".join(
                    f"- {r.repo_name}: pushed by worker"
                    f" ({r.commits_ahead} commit(s), {(r.local_sha or '')[:8]})"
                    for r in _rescued_repos
                )
                try:
                    await gtd_client.post_comment(
                        run.item_id,
                        _rescue_header + "\n" + _rescue_bullets,
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post push-backstop rescue comment for run %s",
                        run.id,
                    )
            if (
                _gate_result is not None
                and _gate_result.passed
                and run.item_id is not None
            ):
                _gate_pass_comment = (
                    f"Post-run gate passed (run `{run.id}`): `{_gate_cmd}`"
                    f" exited 0 in {int(_gate_result.duration_seconds)}s."
                )
                if _stashed_names:
                    _gate_pass_comment += (
                        "\n\nUncommitted changes in"
                        f" {', '.join(_stashed_names)} were stashed before the"
                        " gate ran, so the gate checked only the committed work."
                    )
                try:
                    await gtd_client.post_comment(
                        run.item_id,
                        _gate_pass_comment,
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post post-run gate pass comment for run %s",
                        run.id,
                    )
        else:
            # Derive error snippet from transcript (stdout/stderr are always "" with Popen streaming)
            error_msg = None
            if workspace is not None:
                transcript_path = workspace / "transcript.txt"
                if transcript_path.exists():
                    raw = transcript_path.read_bytes()
                    if raw:
                        error_msg = raw[-500:].decode("utf-8", errors="replace")
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=completed,
                exit_code=result.returncode,
                error=error_msg,
            )
            _publish_run_event(run.id, "failed", completed)
            if mode == DispatchMode.MANAGE:
                should_cleanup = False  # preserve workspace for debugging
            if error_msg and run.item_id is not None:
                await gtd_client.post_comment(
                    run.item_id,
                    f"Agent exited with code {result.returncode} (run `{run.id}`)."
                    f"\n\n```\n{error_msg}\n```",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )

    except subprocess.TimeoutExpired:
        # manage runs never take the _linger_success branch below — _verify_repos
        # is None for non-build modes.
        _run_timed_out = True
        _timed_out_at = datetime.now(UTC).isoformat()
        _linger_success = False
        if _verify_repos is not None:
            # BUILD mode: agent process lingered past the timeout but may have
            # already pushed its work.  verify_pushes is fail-closed — any error
            # yields PushStatus.unpushed so it never misclassifies a real timeout.
            push_results_list = dispatch.verify_pushes(
                _verify_repos, run.branch_name or ""
            )
            unpushed = [r for r in push_results_list if r.status == PushStatus.unpushed]
            if not unpushed:
                # Every repo is pushed or has no changes — treat as succeeded.
                _linger_success = True
                _push_results_json = json.dumps(
                    [r.model_dump(mode="json") for r in push_results_list]
                )
                await db.update_run(
                    run.id,
                    status=RunStatus.succeeded,
                    completed_at=_timed_out_at,
                    push_results=_push_results_json,
                )
                _publish_run_event(run.id, "succeeded", _timed_out_at)
                if run.item_id is not None:
                    await gtd_client.post_comment(
                        run.item_id,
                        f"Agent exceeded the {timeout_seconds // 60}-minute wall-clock "
                        f"timeout, but its work was pushed to origin — marking run "
                        f"succeeded (run `{run.id}`).",
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
        if not _linger_success:
            # Genuine timeout: work was not pushed (or plan/manage mode with no
            # push verification).  Preserve the original timed_out behaviour.
            await db.update_run(
                run.id,
                status=RunStatus.timed_out,
                completed_at=_timed_out_at,
                error=f"Timed out after {timeout_seconds}s",
            )
            _publish_run_event(run.id, "timed_out", _timed_out_at)
            if run.item_id is not None:
                await gtd_client.post_comment(
                    run.item_id,
                    f"Agent timed out after {timeout_seconds // 60} minutes (run `{run.id}`). "
                    "The task may need to be broken down into smaller pieces.",
                    created_by=attribution or "agent-gtd-dispatch",
                    token=run.callback_token,
                )
            if mode == DispatchMode.MANAGE:
                should_cleanup = False
    except asyncio.CancelledError:
        _human_cancelled = True
        _cancelled_at = datetime.now(UTC).isoformat()
        await db.update_run(
            run.id,
            status=RunStatus.cancelled,
            completed_at=_cancelled_at,
        )
        _publish_run_event(run.id, "cancelled", _cancelled_at)
    except Exception as exc:
        await db.update_run(
            run.id,
            status=RunStatus.failed,
            completed_at=datetime.now(UTC).isoformat(),
            error=str(exc)[:500],
        )
        if mode == DispatchMode.MANAGE:
            should_cleanup = False
    finally:
        _active_processes.pop(run.id, None)
        _try_start_pending()  # wake up a queued dispatch now that a slot freed
        _active_subprocesses.pop(run.id, None)
        _run_event_queues.pop(run.id, None)
        if run.mode == DispatchMode.MANAGE and run.rollout_id:
            _rollout_to_run.pop(run.rollout_id, None)
            logger.info(
                "manage: exit rollout_id=%s run_id=%s exit_code=%s human_cancelled=%s",
                run.rollout_id,
                run.id,
                _exit_code,
                _human_cancelled,
            )
        if workspace is not None and should_cleanup:
            dispatch.cleanup_workspace(workspace)
        if run.mode == DispatchMode.MANAGE and run.rollout_id and not _human_cancelled:
            await _maybe_relaunch_manage(
                run,
                max_turns,
                engine_used,
                timeout_seconds,
                attribution,
                manager_uptime_seconds=(
                    0.0
                    if _agent_start_dt is None
                    else (datetime.now(UTC) - _agent_start_dt).total_seconds()
                ),
                run_timed_out=_run_timed_out,
            )


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, object]:
    """Return service health and active run count."""
    active = len(_active_processes)
    return {"status": "ok", "active_runs": active}


@app.get("/info", response_model=InfoResponse)
async def info() -> InfoResponse:
    """Return engine identity, version, capacity, and capabilities. No auth required.

    The capacity fields (max_concurrent_runs, active_runs) and capability lists
    (engines, agents) let a multi-host router on the caller side filter and
    rank dispatch targets without a separate round trip to /agents.
    """
    agent_dicts = await run_list_agents_script()
    return InfoResponse(
        engine=ENGINE_NAME,
        version=SERVICE_VERSION,
        max_concurrent_runs=config.MAX_CONCURRENT_RUNS,
        active_runs=len(_active_processes),
        engines=get_available_engine_names(),
        agents=[a["name"] for a in agent_dicts],
    )


@app.get("/agents")
async def list_agents(
    _: str = Depends(_verify_api_key),
) -> dict[str, object]:
    """Return available agents by executing list_agents.sh.

    Always returns 200. Returns an empty list if the script is missing,
    non-executable, exits non-zero, or times out.
    """
    agents = await run_list_agents_script()
    return {"agents": agents}


@app.post("/plan", response_model=RolloutPlan)
async def plan_rollout_endpoint(
    body: PlanRequest,
    _: str = Depends(_verify_api_key),
) -> RolloutPlan:
    """Produce a dependency DAG for a set of items (called by plan_rollout on agent_gtd)."""
    try:
        return await rollout_planner.plan_rollout(body.item_ids)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "detail": str(exc),
                "planner_model": rollout_planner._active_planner_model(),
                "item_count": len(body.item_ids),
            },
        ) from exc


@app.post("/dispatch", response_model=RunResponse)
async def dispatch_item(
    body: DispatchRequest,
    _: str = Depends(_verify_api_key),
) -> RunResponse:
    """Start a new dispatch run for a GTD item."""
    # talos-* is BUILD-only: talos has no GTD/MCP access and the worker
    # commits + pushes its own output, so a plan/manage dispatch handed a
    # talos engine would try to build and commit code instead of writing a
    # spec / managing a rollout. Reject up front (422) rather than silently
    # swapping engines, since the engine is known at request time here — the
    # caller should fix the request (e.g. by dropping the item's build_engine
    # override for plan mode) rather than have it silently run as a different
    # engine. See the `_dispatch_worker` guard immediately before the
    # `is_talos_engine(engine_used.name)` branch for the defense-in-depth
    # backstop covering paths where the engine only becomes talos afterward.
    if body.mode != DispatchMode.BUILD and is_talos_engine(body.engine):
        _mode_str = body.mode.value if hasattr(body.mode, "value") else str(body.mode)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Engine '{body.engine}' is a talos engine; talos is build "
                f"mode only, refusing to dispatch in {_mode_str} mode."
            ),
        )

    # Plan-mode and manage-mode always use Anthropic, regardless of requested engine.
    # The Ollama-routed Claude engines (claude-code-ollama local, claude-code-glm
    # cloud) are BUILD-only; plan/manage dispatches swap to claude-code so the
    # existing planner/manager code path runs untouched (small local/cloud
    # models are unreliable at multi-wave management).
    effective_engine_name = body.engine
    _engine_swap_reason = ""
    if body.mode != DispatchMode.BUILD and body.engine == "claude-code-ollama":
        effective_engine_name = "claude-code"
        _engine_swap_reason = "plan/manage mode does not support ollama"
    elif body.mode != DispatchMode.BUILD and body.engine == "claude-code-glm":
        effective_engine_name = "claude-code"
        _engine_swap_reason = "plan/manage mode does not support ollama-cloud glm"
    engine_swapped = body.engine != effective_engine_name
    try:
        engine = get_engine(effective_engine_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Validate mode-specific requirements
    if body.mode == DispatchMode.MANAGE and not body.rollout_id:
        raise HTTPException(
            status_code=400,
            detail="rollout_id required for mode=manage",
        )
    if body.mode != DispatchMode.MANAGE and not body.item_id:
        raise HTTPException(
            status_code=400,
            detail=f"item_id required for mode={body.mode}",
        )

    if body.mode == DispatchMode.MANAGE:
        # Derive project from rollout — item_id is None for manage-mode runs
        assert body.rollout_id is not None  # noqa: S101 — validated above
        try:
            # Handler-time call: use the sender-supplied per-run callback token
            # so authorization runs as the dispatching user (admin or member).
            # Falls back to the static service key when body.callback_token is None
            # (legacy senders + admin dispatch). See _request fallback in gtd_client.
            rollout = await gtd_client.get_rollout(
                body.rollout_id, token=body.callback_token
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=404, detail="Rollout not found"
                ) from exc
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream error fetching rollout",
                    "upstream_status": exc.response.status_code,
                    "upstream_body_snippet": exc.response.text[:200],
                    "upstream_url": str(exc.request.url),
                },
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "detail": "Upstream unreachable fetching rollout",
                    "upstream_url": str(exc.request.url) if exc.request else None,
                },
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream returned malformed JSON for rollout",
                    "upstream_url": None,
                },
            ) from exc

        project_id = rollout.get("project_id")
        if not project_id:
            raise HTTPException(
                status_code=400, detail="Rollout has no project assigned"
            )

        try:
            # Handler-time call: see token-forwarding rationale at the get_rollout
            # call above.
            project = await gtd_client.get_project(
                project_id, token=body.callback_token
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=404, detail="Project not found"
                ) from exc
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream error fetching project",
                    "upstream_status": exc.response.status_code,
                    "upstream_body_snippet": exc.response.text[:200],
                    "upstream_url": str(exc.request.url),
                },
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "detail": "Upstream unreachable fetching project",
                    "upstream_url": str(exc.request.url) if exc.request else None,
                },
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream returned malformed JSON for project",
                    "upstream_url": None,
                },
            ) from exc

        if (project.get("repo_mode") or "") == "workspace":
            _ws_repos = project.get("workspace_repos") or []
            if not _ws_repos:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Project '{project['name']}' has workspace_repos empty or missing"
                    ),
                )
        else:
            if not project.get("git_origin"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Project '{project['name']}' has no git_origin configured",
                )

        branch_name = None
        item_id_for_run: str | None = None
    else:
        # item_id validated non-empty above
        assert body.item_id is not None  # noqa: S101
        # Fetch item to validate and get project info
        try:
            # THE BUG LOCUS: this synchronous handler-time get_item is the
            # 404 site. agent_gtd scopes item reads to owner-or-member, so a
            # non-admin dispatching against the static admin key gets 404 here.
            # Forwarding body.callback_token authorizes the read as the
            # dispatching user. Falls back to the static service key when None.
            item = await gtd_client.get_item(body.item_id, token=body.callback_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Item not found") from exc
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream error fetching item",
                    "upstream_status": exc.response.status_code,
                    "upstream_body_snippet": exc.response.text[:200],
                    "upstream_url": str(exc.request.url),
                },
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "detail": "Upstream unreachable fetching item",
                    "upstream_url": str(exc.request.url) if exc.request else None,
                },
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream returned malformed JSON for item",
                    "upstream_url": None,
                },
            ) from exc

        project_id = item.get("project_id")
        if not project_id:
            raise HTTPException(status_code=400, detail="Item has no project assigned")

        try:
            # Handler-time call: see token-forwarding rationale at get_item above.
            project = await gtd_client.get_project(
                project_id, token=body.callback_token
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=404, detail="Project not found"
                ) from exc
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream error fetching project",
                    "upstream_status": exc.response.status_code,
                    "upstream_body_snippet": exc.response.text[:200],
                    "upstream_url": str(exc.request.url),
                },
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "detail": "Upstream unreachable fetching project",
                    "upstream_url": str(exc.request.url) if exc.request else None,
                },
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": "Upstream returned malformed JSON for project",
                    "upstream_url": None,
                },
            ) from exc

        if (project.get("repo_mode") or "") == "workspace":
            _ws_repos = project.get("workspace_repos") or []
            if not _ws_repos:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Project '{project['name']}' has workspace_repos empty or missing"
                    ),
                )
        else:
            if not project.get("git_origin"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Project '{project['name']}' has no git_origin configured",
                )

        branch_name = dispatch.branch_name_for_item(body.item_id, item["title"])
        item_id_for_run = body.item_id

    # Talos-only pre-clone rejections. The non-BUILD + talos combination is
    # already rejected with HTTP 422 above, before any of this — so by this
    # point effective_engine_name is only in TALOS_ENGINES for BUILD-mode
    # dispatches. Must happen BEFORE db.insert_run so a rejected dispatch
    # never leaves a run row behind.
    if is_talos_engine(effective_engine_name):
        # Non-empty project.gate_command is a talos-only requirement — the
        # TaskSpec's gate_command is the definition of Done, and talos self-checks
        # it before returning exit 0. Non-talos engines are unaffected.
        _gate = project.get("gate_command")
        if _gate is None or not str(_gate).strip():
            raise HTTPException(
                status_code=400,
                detail="talos engines require a non-empty project gate_command",
            )

    max_turns = body.max_turns
    if body.timeout_minutes:
        timeout_seconds = body.timeout_minutes * 60
    elif body.mode == DispatchMode.MANAGE:
        timeout_seconds = config.MANAGE_TIMEOUT_SECONDS
    else:
        timeout_seconds = config.TIMEOUT_SECONDS

    run = Run(
        item_id=item_id_for_run,
        project_name=project["name"],
        branch_name=branch_name,
        engine=body.engine,
        engine_actual=effective_engine_name,
        agent_name=body.agent_name,
        mode=body.mode,
        rollout_id=body.rollout_id,
        callback_token=body.callback_token,
    )
    await db.insert_run(run)

    if engine_swapped:
        logger.warning(
            "engine_swap run_id=%s requested=%s effective=%s reason=%s",
            run.id,
            body.engine,
            effective_engine_name,
            _engine_swap_reason,
        )

    # Create event queue so the cancel/SSE endpoints can enqueue events for
    # both running AND queued runs (the run.id is known before the task starts).
    _run_event_queues[run.id] = asyncio.Queue()

    # ATOMIC capacity check: no await between this check and task creation /
    # queue append, so concurrent coroutines cannot both pass for the same slot.
    if len(_active_processes) >= config.MAX_CONCURRENT_RUNS:
        # Service is at capacity — queue the run and return 200 immediately.
        # _try_start_pending() will promote it when a slot frees.
        _pending_queue.append(
            _PendingDispatch(
                run=run,
                engine=engine,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                attribution=body.attribution,
            )
        )
        return RunResponse(
            **run.model_dump(),
            engine_swap=EngineSwap(
                from_engine=body.engine,
                to_engine=effective_engine_name,
                reason=_engine_swap_reason,
            )
            if engine_swapped
            else None,
        )

    # Slot available — start the background task immediately.
    task = asyncio.create_task(
        _dispatch_worker(
            run, max_turns, engine, timeout_seconds, attribution=body.attribution
        )
    )
    _active_processes[run.id] = task

    return RunResponse(
        **run.model_dump(),
        engine_swap=EngineSwap(
            from_engine=body.engine,
            to_engine=effective_engine_name,
            reason=_engine_swap_reason,
        )
        if engine_swapped
        else None,
    )


@app.get("/runs", response_model=list[RunResponse])
async def list_runs(
    item_id: str | None = Query(None),
    status: RunStatus | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(_verify_api_key),
) -> list[RunResponse]:
    """List dispatch runs, optionally filtered."""
    runs = await db.list_runs(item_id=item_id, status=status, limit=limit)
    return [RunResponse(**r.model_dump()) for r in runs]


@app.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    _: str = Depends(_verify_api_key),
) -> RunResponse:
    """Get a specific run by ID."""
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run.model_dump())


@app.get("/runs/{run_id}/transcript")
async def get_run_transcript(
    run_id: str,
    lines: int = Query(200, ge=1, le=5000),
    _: str = Depends(_verify_api_key),
) -> dict[str, object]:
    """Return last N lines of the run transcript (streamed during execution)."""
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if not run.workspace_path:
        return {"text": "no transcript yet", "last_modified": None, "total_lines": 0}

    transcript_path = Path(run.workspace_path) / "transcript.txt"
    if not transcript_path.exists():
        return {"text": "no transcript yet", "last_modified": None, "total_lines": 0}

    stat = transcript_path.stat()
    last_modified = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
    content = transcript_path.read_text(errors="replace")
    all_lines = content.splitlines()
    tail_lines = all_lines[-lines:]
    return {
        "text": "\n".join(tail_lines),
        "last_modified": last_modified,
        "total_lines": len(all_lines),
    }


@app.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    _: str = Depends(_verify_api_key),
) -> RunResponse:
    """Cancel a running dispatch.

    Idempotent: returns 200 for already-terminal runs without side effects.
    Sends SIGTERM to the subprocess, waits CANCEL_GRACE_SECONDS, then SIGKILL.
    Posts a comment on the item (if present) and publishes an SSE event.
    """
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Idempotent: terminal state → return 200 as-is with no side effects
    _terminal = {
        RunStatus.succeeded,
        RunStatus.failed,
        RunStatus.timed_out,
        RunStatus.cancelled,
    }
    if run.status in _terminal:
        return RunResponse(**run.model_dump())

    # Cancel the asyncio task (stops the coroutine at its next await)
    task = _active_processes.get(run_id)
    if task is not None:
        task.cancel()

    # Terminate the subprocess: SIGTERM, grace period, then SIGKILL
    proc = _active_subprocesses.get(run_id)
    if proc is not None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        await asyncio.sleep(config.CANCEL_GRACE_SECONDS)
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    # Update DB to cancelled
    completed = datetime.now(UTC).isoformat()
    await db.update_run(run_id, status=RunStatus.cancelled, completed_at=completed)

    # Post comment (best-effort; failures are logged, not raised)
    if run.item_id is not None:
        try:
            await gtd_client.post_comment(
                run.item_id,
                "Run cancelled by lead via agent-gtd",
                created_by="agent-gtd-dispatch",
                token=run.callback_token,
            )
        except Exception:
            logger.warning("Failed to post cancellation comment for run %s", run_id)

    # Publish SSE event
    _publish_run_event(run_id, "cancelled", completed)

    run = await db.get_run(run_id)
    if run is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run.model_dump())
