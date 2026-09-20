"""Dispatch worker API — runs headless coding agents."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

from agent_gtd_dispatch_protocol.branches import make_branch_name

from . import (
    completion,
    config,
    db,
    dispatch,
    gates,
    gtd_client,
    retention,
    talos,
)
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
    PushStatus,
    RepoPushStatus,
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
_retention_task: asyncio.Task[None] | None = None  # handle for clean shutdown
# One-shot disposition-classifier reachability probe (see lifespan). Held only
# so shutdown can cancel it — nothing awaits its result.
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

# The single budget for operator-facing run error strings, shared with the
# git/hook excerpts in dispatch.py so the two truncation layers cannot drift.
ERROR_TEXT_MAX_CHARS: int = dispatch.ERROR_TEXT_MAX_CHARS

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


async def _fresh_in_flight_build_runs(
    rollout_id: str, fallback: list[dict[str, Any]] | None
) -> list[Any]:
    """Re-read the rollout and return its in-flight build runs RIGHT NOW.

    The cap-exceeded halt decision must never be made from a copy of the
    rollout fetched before the relaunch call: minutes of backoff, HTTP and
    subprocess teardown can pass in between, and a build that was in flight
    then may be terminal now (or vice versa). Halting on a stale read is
    exactly how running builds get stranded.

    On a fetch failure we fall back to ``fallback`` (the caller's in-hand
    copy, or None on the watchdog path). That biases towards DEFERRING the
    halt when we last saw a build running — the watchdog resolves a deferred
    halt later, whereas a wrongly-issued halt strands the build permanently.
    """
    try:
        rollout = await gtd_client.get_rollout(rollout_id)
    except Exception:
        logger.exception(
            "Failed to re-read rollout %s for the in-flight check — falling back "
            "to the in-hand copy",
            rollout_id,
        )
        return list(fallback or [])
    return _in_flight_build_runs(rollout)


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
    global _watchdog_task, _retention_task
    global _readoption_task
    config.load()
    # Before config.load() there is no LOG_LEVEL to honour, and after this line
    # every logger.info in the package reaches the journal.
    configure_logging()
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
    _retention_task = asyncio.create_task(_retention_loop())
    # Re-adoption sweep: the wave loop lives inside THIS service, so a routine
    # deploy restart would otherwise silently stop every running rollout from
    # advancing. Backgrounded so startup never blocks on the GTD API.
    _readoption_task = asyncio.create_task(_readopt_running_rollouts())
    yield
    # Cancel watchdog, retention and active dispatch tasks on shutdown
    if _watchdog_task is not None:
        _watchdog_task.cancel()
    if _retention_task is not None:
        _retention_task.cancel()
    if _readoption_task is not None:
        _readoption_task.cancel()
    for task in _active_processes.values():
        task.cancel()


app = FastAPI(title="Agent GTD Dispatch", lifespan=lifespan)


# Marks the handler this function owns, so repeated calls replace it instead of
# stacking duplicates (lifespan runs per-app; tests construct several).
_LOG_HANDLER_NAME = "agent-gtd-dispatch"


def configure_logging(level: str | None = None) -> None:
    """Attach a stdout handler to THIS package's logger at ``level``.

    `uvicorn.run()` applies its own dictConfig, which names only the `uvicorn*`
    loggers and leaves the root logger untouched.  The practical effect on a
    systemd host was that the journal showed uvicorn access lines and nothing
    else: every `logger.info(...)` in this package was dropped on the floor, and
    warnings reached stderr only via logging's lastResort fallback, unformatted
    and without the logger name.  Gate-install decisions, post-run gate results,
    the manage-recovery ladder and the unasserted-run WARNING were all invisible
    in production — the exact signals needed to debug a misbehaving dispatch.

    Configures the package logger rather than the root logger so that uvicorn's
    own configuration is left alone.

    Propagation is deliberately left ON.  Silencing it would be marginally
    tidier in production (the root logger has no handler there, so propagating
    records go nowhere), but pytest's `caplog` captures by attaching a handler
    to the ROOT logger and relies on propagation to see anything — turning it
    off broke 52 existing tests.  Observability that costs us the test suite's
    ability to assert on log output is a bad trade.
    """
    resolved = (level or config.LOG_LEVEL or "INFO").strip().upper()
    pkg_logger = logging.getLogger(__package__ or "agent_gtd_dispatch")

    for existing in list(pkg_logger.handlers):
        if getattr(existing, "name", None) == _LOG_HANDLER_NAME:
            pkg_logger.removeHandler(existing)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.name = _LOG_HANDLER_NAME
    handler.setFormatter(
        logging.Formatter("%(levelname)s [%(name)s] %(message)s"),
    )
    pkg_logger.addHandler(handler)
    # An unknown level string must not silence the service: fall back to INFO.
    pkg_logger.setLevel(getattr(logging, resolved, logging.INFO))


def start() -> None:  # pragma: no cover
    """Entry point for the `agent-gtd-dispatch` console script.

    Runs uvicorn against the module-level FastAPI app. Bound to 0.0.0.0
    because the systemd unit expects the service to accept LAN traffic.
    """
    uvicorn.run(app, host="0.0.0.0", port=8100)


# --- Background dispatch worker ---


async def _comment_on_in_flight_items(
    rollout_id: str,
    in_flight: list[dict[str, Any]] | None,
    render: Callable[[dict[str, Any]], str],
    *,
    log_label: str,
) -> None:
    """Post one comment per in-flight build run, on that run's own item.

    Fans out across every entry rather than ``in_flight[0]``: in a multi-item
    wave, commenting only on the first item leaves the other items' reviewers
    with no record that their build was left running without a manager.
    A failure to comment on one item must not suppress the others.
    """
    for entry in in_flight or []:
        item_id = entry.get("itemId")
        if not item_id:
            continue
        try:
            await gtd_client.post_comment(
                str(item_id), render(entry), created_by="agent-gtd-dispatch"
            )
        except Exception:
            logger.exception(
                "Failed to post %s comment for rollout %s item %s",
                log_label,
                rollout_id,
                item_id,
            )


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
    resume_context: list[dict[str, Any]] | None = None,
) -> None:
    """Shared manage-recovery: kill stale subprocess (if any), increment retry, relaunch or halt.

    When the incremented retry count exceeds ``MAX_MANAGE_RETRIES`` the halt is
    DEFERRED (not cancelled) if a freshly-read child build run is still in
    flight — halting there would strand a running build with no manager to
    review or merge it. The deferred halt is resolved by
    ``_watchdog_evaluate_rollout`` once those builds reach terminal. Deferring
    never launches a replacement manager: the cap really is exhausted.

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
        resume_context: The rollout's in-flight build runs
            (``{runId, itemId, status}``) as known at decision time. Threaded
            into the replacement manager's prompt so it does not have to
            rediscover the wave, and used to make a cap-exceeded halt legible
            by commenting on every stranded item. None on the watchdog path.
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
            # Never halt on top of a running build. Stranding an in-flight
            # build is the worst outcome this system can produce: the build
            # finishes, pushes its branch, and then sits there with no manager
            # to review, gate or merge it. The cap decision is DEFERRED (not
            # cancelled) until those builds reach terminal — at which point
            # _watchdog_evaluate_rollout issues the same halt.
            #
            # Deferring is not a retry: no replacement manager is launched
            # here either. The run simply ends, leaving the rollout `running`
            # with its builds finishing under no manager.
            live_in_flight = await _fresh_in_flight_build_runs(
                rollout_id, resume_context
            )
            if live_in_flight:
                live_run_ids = (
                    ",".join(str(r.get("runId")) for r in live_in_flight) or "none"
                )
                logger.warning(
                    "manage-recovery: cap-exceeded halt DEFERRED rollout_id=%s "
                    "retry_count=%d cap=%d in_flight_builds=%d "
                    "in_flight_build_run_ids=%s decision=deferred-halt-build-in-flight",
                    rollout_id,
                    retry_count,
                    MAX_MANAGE_RETRIES,
                    len(live_in_flight),
                    live_run_ids,
                )
                await _comment_on_in_flight_items(
                    rollout_id,
                    live_in_flight,
                    lambda r: (
                        "⏸️ Rollout manager relaunch cap exhausted "
                        f"(manage_retry_count={retry_count} > cap "
                        f"{MAX_MANAGE_RETRIES}) — the halt has been DEFERRED "
                        f"because build run `{r.get('runId')}` for item "
                        f"`{r.get('itemId')}` is still executing (status "
                        f"`{r.get('status')}`). The rollout has been left "
                        "`running` so the build can finish rather than being "
                        "stranded mid-flight, and NO replacement manager will "
                        "be started. Once every build in this wave reaches a "
                        "terminal state the watchdog halts the rollout with "
                        "`manage_relaunch_cap_exceeded`. A human must review "
                        "and merge the result."
                    ),
                    log_label="cap-exceeded halt deferred",
                )
                return

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
            # Make the consequence legible: any build that was in flight when
            # the ladder decided (and has since gone terminal — the fresh read
            # above proves nothing is still running) is now orphaned, with no
            # manager to review or merge it. Comment on EVERY such item, not
            # just the first.
            await _comment_on_in_flight_items(
                rollout_id,
                resume_context,
                lambda r: (
                    f"\U0001f6d1 Rollout halted — `{halt_reason}` "
                    f"(manage_retry_count={retry_count} > cap {MAX_MANAGE_RETRIES}). "
                    f"Build run `{r.get('runId')}` for item `{r.get('itemId')}` was "
                    "in flight when the manager relaunch cap was exhausted and has "
                    "since reached a terminal state with NO rollout manager attached "
                    "to it. Nobody reviewed, gated or merged its branch — this item "
                    "needs manual review and merge by a lead."
                ),
                log_label="cap-exceeded halt",
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
    # The ladder's "relaunch" is now a loop RESUMPTION: the replacement drives
    # the same worker-owned wave loop, which reads its frontier from
    # `advance_rollout` plus the in-flight query and therefore needs no
    # resume_context of its own. The ladder, its tallies and its caps are
    # untouched — this is only what they relaunch INTO.
    _start_rollout_loop(new_run, attribution=attribution)


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

        _current_retry_count = int(rollout.get("manage_retry_count", 0))
        await _comment_on_in_flight_items(
            rollout_id,
            in_flight,
            lambda r: (
                "manage-recovery: free relaunch — the rollout manager exited while "
                f"build run `{r.get('runId')}` is still in flight. "
                f"`manage_retry_count` is unchanged at {_current_retry_count}; "
                f"free relaunch {new_free_relaunches}/"
                f"{config.MAX_MANAGE_FREE_RELAUNCHES}; "
                f"manager uptime {manager_uptime_seconds:.0f}s."
            ),
            log_label="free-relaunch",
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
            known_retry_count=_current_retry_count,
            resume_context=in_flight,
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
        resume_context=in_flight,
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

    # Resolve a DEFERRED cap-exceeded halt (see _do_manage_recovery). A rollout
    # whose manage_retry_count is already past the cap has exhausted its manager
    # budget and will never get another manager; the exit path deliberately left
    # it `running` rather than stranding a build that was still executing. Halt
    # it here, but only once every child build has reached terminal.
    #
    # Deliberately placed AFTER the polling/in-flight short-circuit above (so
    # `decision=skipped-build-in-flight` still wins for a polling manager with a
    # live build) and after the idempotency guard (so a concurrent tick cannot
    # double-act). Rollouts already in a terminal status returned at the top of
    # this function, so a halted rollout is never halted twice.
    if int(rollout.get("manage_retry_count") or 0) > MAX_MANAGE_RETRIES:
        cap_in_flight = _in_flight_build_runs(rollout)
        if cap_in_flight:
            logger.info(
                "watchdog: rollout_id=%s manager_phase=%s age_seconds=%.0f "
                "in_flight_builds=%d decision=deferred-halt-build-in-flight",
                rollout_id,
                manager_phase,
                age_seconds,
                len(cap_in_flight),
            )
            return  # still waiting for the stranded-build window to close
        logger.warning(
            "watchdog: rollout_id=%s manager_phase=%s age_seconds=%.0f "
            "retry_count=%s cap=%d decision=deferred-halt-resolved",
            rollout_id,
            manager_phase,
            age_seconds,
            rollout.get("manage_retry_count"),
            MAX_MANAGE_RETRIES,
        )
        # Mark acted-on BEFORE awaiting the halt, same as the recovery path.
        _watchdog_acted[rollout_id] = time.monotonic()
        try:
            await gtd_client.halt_rollout(
                rollout_id, reason="manage_relaunch_cap_exceeded"
            )
        except Exception:
            logger.exception(
                "Failed to halt rollout %s when resolving the deferred "
                "cap-exceeded halt",
                rollout_id,
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


async def _retention_loop() -> None:
    """Background coroutine: age-prune workspaces and retained run evidence.

    One bad tick must never kill the loop — same shape as ``_manage_watchdog``.
    """
    while True:
        await asyncio.sleep(config.RETENTION_INTERVAL_SECONDS)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, retention.prune, frozenset(_active_processes)
            )
        except Exception:
            logger.exception("Retention sweep failed — continuing")


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
        if pending.run.mode == DispatchMode.MANAGE and pending.run.rollout_id:
            _start_rollout_loop(pending.run, attribution=pending.attribution)
            continue
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


# Classification of a non-zero `git commit` in the talos paths. A failed commit
# is NOT sufficient on its own to fail the run: lefthook/pre-commit report the
# same surface symptom ("nothing to commit, working tree clean") for a run that
# already committed everything and for a run that did nothing at all.
_COMMIT_DIRTY = "dirty"  # real hook rejection / conflict — fail, unchanged
_COMMIT_ALREADY_COMMITTED = "already_committed"  # redundant commit — success
_COMMIT_NO_CHANGES = "no_changes"  # the agent produced nothing — fail, loudly


def _classify_failed_commit(repo_path: Path) -> str:
    """Disambiguate a non-zero ``git commit`` in one repo.

    Decision table:

    - tree DIRTY (or ``git status`` itself failed) -> ``_COMMIT_DIRTY``. The
      caller keeps today's behaviour exactly: a genuine hook rejection or
      conflict still fails the run.
    - tree CLEAN and the branch has commits AHEAD of its base ->
      ``_COMMIT_ALREADY_COMMITTED``. The agent committed its work and then
      attempted a redundant final commit; git exits non-zero but the run is
      fine and the caller continues to the normal push path.
    - tree CLEAN and NO commits ahead of base -> ``_COMMIT_NO_CHANGES``. The
      agent produced nothing. The run must still fail, and say so plainly.

    The commits-ahead clause is the whole point: without it, "nothing to
    commit" would read as success and a do-nothing run would report green.
    """
    porcelain = subprocess.run(
        dispatch._sudo_wrap(["git", "status", "--porcelain"]),
        cwd=str(repo_path),
        check=False,
        capture_output=True,
    )
    stdout = porcelain.stdout if isinstance(porcelain.stdout, bytes) else b""
    if porcelain.returncode != 0 or stdout.decode("utf-8", errors="replace").strip():
        return _COMMIT_DIRTY
    if dispatch.commits_ahead_of_base(repo_path) > 0:
        return _COMMIT_ALREADY_COMMITTED
    return _COMMIT_NO_CHANGES


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
    - Exit 10/20/1/40 (or unpushed after exit 0): no commit, no push, no status set.
    - Exit 30 (AlreadySatisfied): no commit, no push, but item status IS PATCHed
      to ``review`` (best-effort) via :func:`_route_already_satisfied_item` — the
      SAME routing the claude-code already_satisfied path (48617eb) uses. If the
      run carries a ``rollout_id``, :func:`_complete_rollout_item_skipped` also
      records the rollout item as skipped so the wave advances (talos has no
      GTD/MCP access to do this itself, unlike a claude-code manage-mode run).
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
                    _err = dispatch.git_output_excerpt(add_rc)
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
                    _err = dispatch.git_output_excerpt(diff_rc)
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
                    # A non-zero commit is not, on its own, a failed run.
                    # Workspace semantics are PER REPO: an already-committed
                    # repo continues to push, and a repo that produced nothing
                    # is merely skipped — a sibling repo's work still counts.
                    verdict = _classify_failed_commit(repo_path)
                    if verdict == _COMMIT_NO_CHANGES:
                        logger.info(
                            "talos run %s: repo %s has a clean tree and no commits "
                            "ahead of base — recording it as unchanged",
                            run.id,
                            repo_dir,
                        )
                        skipped.append(repo_dir)
                        continue
                    if verdict == _COMMIT_ALREADY_COMMITTED:
                        logger.info(
                            "talos run %s: redundant empty commit in repo %s — the "
                            "branch already has commits ahead of base; pushing",
                            run.id,
                            repo_dir,
                        )
                    else:
                        _err = dispatch.git_output_excerpt(commit_rc)
                        # Dirty tree — a genuine hook rejection or conflict.
                        # Unchanged behaviour: the whole run fails.
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
                    _err = dispatch.git_output_excerpt(push_rc)
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
            _err = dispatch.git_output_excerpt(add_rc)
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
            # A non-zero commit is not, on its own, a failed run: the agent may
            # have committed everything and then attempted a redundant final
            # commit. Disambiguate on commits-ahead-of-base.
            verdict = _classify_failed_commit(workspace)
            if verdict != _COMMIT_ALREADY_COMMITTED:
                if verdict == _COMMIT_NO_CHANGES:
                    # The loud case: nothing to commit AND nothing committed.
                    error_text = (
                        "talos reported Done but the agent produced no changes: "
                        "nothing to commit and no commits ahead of the base branch"
                    )
                    comment_text = (
                        f"talos reported Done but produced no changes — nothing to "
                        f"commit and the branch has no commits ahead of its base "
                        f"(run `{run.id}`, branch `{branch_name}`)."
                    )
                else:
                    # Dirty tree — a genuine hook rejection or conflict.
                    _err = dispatch.git_output_excerpt(commit_rc)
                    error_text = f"git commit failed: {_err}"
                    comment_text = f"talos completed but `git commit` failed: {_err}"
                await db.update_run(
                    run.id,
                    status=RunStatus.failed,
                    completed_at=now,
                    exit_code=exit_code,
                    error=error_text,
                )
                _publish_run_event(run.id, "failed", now)
                try:
                    await gtd_client.post_comment(
                        item_id,
                        comment_text,
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post git-commit failure comment for %s", run.id
                    )
                return
            logger.info(
                "talos run %s: redundant empty commit — the branch already has "
                "commits ahead of base; continuing to push",
                run.id,
            )

        push_rc = subprocess.run(
            dispatch._sudo_wrap(
                ["git", "push", "--no-verify", "-u", "origin", branch_name]
            ),
            cwd=str(workspace),
            check=False,
            capture_output=True,
        )
        if push_rc.returncode != 0:
            _err = dispatch.git_output_excerpt(push_rc)
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

    if status == RunStatus.already_satisfied:
        # Exit 30 (AlreadySatisfied) — identical routing to the claude-code
        # already_satisfied path shipped in 48617eb: item -> review
        # (best-effort). Returns early (skipping the generic build_comment_body
        # tail below) so exactly one set of comments fires for this terminal —
        # mirrors the workspace success path's early-return precedent above.
        await _route_already_satisfied_item(
            item_id,
            run.id,
            comment_header,
            callback_token=run.callback_token,
            attribution=attribution,
        )
        if run.rollout_id is not None:
            await _complete_rollout_item_skipped(
                run.rollout_id,
                item_id,
                run.id,
                callback_token=run.callback_token,
                attribution=attribution,
            )
        return

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


# --- BUILD-mode completion classification -----------------------------------

# Triage classes for a failed build run.  These are `error`-string PREFIXES, not
# protocol statuses: the dispatch boundary stays binary and the distinctions live
# in the message.
BUILD_FAILURE_PREFIXES: frozenset[str] = frozenset(
    {
        "no_result_envelope",
        "result_is_error",
        "max_turns_exhausted",
        "zero_commits",
        "invariant_zero_commit_success",
    }
)

_FAILURE_PREFIX_DETAIL: dict[str, str] = {
    "no_result_envelope": (
        "The agent CLI produced no parseable result envelope, so there is no"
        " evidence the run reached a conclusion."
    ),
    "result_is_error": "The agent CLI reported its run ended in error.",
    "max_turns_exhausted": (
        "The agent ran out of turns; commits and gate result (if any) are"
        " recorded — review before re-dispatching."
    ),
    "zero_commits": (
        "The agent ended its run without producing a single commit on any repo,"
        " so there is nothing to review. The project gate was NOT run: an"
        " unchanged tree passes trivially, which would say the base commit is"
        " green and nothing at all about this run."
    ),
    "invariant_zero_commit_success": (
        "A zero-commit build run was about to be recorded as a success — the"
        " invariant guard coerced it to failed."
    ),
}


def build_failure_comment(prefix: str, run_id: str, detail: str = "") -> str:
    """Return the GTD comment body for a failed build run of triage class ``prefix``.

    The wording is derived from the triage class rather than fixed prose, so a
    max-turns run is never described as a possible silent failure.
    """
    body = f"Build run failed ({prefix}) — run `{run_id}`."
    canned = _FAILURE_PREFIX_DETAIL.get(prefix)
    if canned:
        body += f" {canned}"
    if detail:
        body += f"\n\n{detail}"
    return body


def build_completion_comment(
    run_id: str,
    branch_name: str | None,
    push_results: list[RepoPushStatus] | None,
    gate_decision: str | None,
) -> str:
    """Return the GTD comment body for a SUCCESSFUL build terminal.

    Composed ENTIRELY from facts the worker observed — branch, per-repo commit counts, push outcomes, gate decision. It used to be enriched with the agent's own account of its run, read from a file the agent wrote; nothing here reads anything the agent produced any more. The agent's own comments appear on the item separately, posted by the agent under its own attribution, which is where a reader can weigh them as a claim rather than as a finding.

    Failure terminals and the talos ``already_satisfied`` terminal compose their own comments (:func:`build_failure_comment`, :func:`_route_already_satisfied_item`) and do not use this one.
    """
    results = push_results or []
    total_commits = sum(r.commits_ahead for r in results)
    lines = [
        f"Build run `{run_id}` finished on branch `{branch_name or '(unknown)'}` —"
        f" {total_commits} commit(s) across {len(results)} repo(s)."
    ]
    for r in results:
        line = (
            f"- {r.repo_name}: {r.status.value}"
            f" ({r.commits_ahead} commit(s), {(r.local_sha or '')[:8]})"
        )
        if r.dirty:
            line += " [dirty working tree]"
        lines.append(line)
    if gate_decision:
        lines.append(f"\nQuality gate: `{gate_decision}`.")
    return "\n".join(lines)


def build_completion_blob(
    *,
    envelope: completion.ResultEnvelope | None,
    envelope_verdict: str,
    zero_commits: bool,
    gate_decision: str | None,
    evidence_dir: str,
) -> str:
    """Serialize the mechanical evidence persisted on every build terminal.

    This is the only durable carrier of the CLI envelope on a run whose `error` is NULL, and the only way session_id / num_turns / total_cost_usd survive workspace teardown.

    Every field here is something the WORKER observed. The blob used to also carry the agent's self-reported disposition, an `artifact` presence flag, a rejection reason, and five `classifier_*` fields recording which model had been asked to guess what the agent meant. All of that is gone with the artifact contract: a run's record should say what happened, not what something claimed about it.
    """
    return json.dumps(
        {
            "envelope_verdict": envelope_verdict,
            "envelope_subtype": envelope.subtype if envelope else None,
            "is_error": envelope.is_error if envelope else None,
            "num_turns": envelope.num_turns if envelope else None,
            "stop_reason": envelope.stop_reason if envelope else None,
            "session_id": envelope.session_id if envelope else None,
            "total_cost_usd": envelope.total_cost_usd if envelope else None,
            "zero_commits": zero_commits,
            "gate_decision": gate_decision,
            "evidence_dir": evidence_dir,
        }
    )


@dataclass(frozen=True, slots=True)
class _RescueOutcome:
    """Aggregate of a run's pre-teardown rescue across every repo."""

    results: list[dispatch.RescueResult]

    @property
    def attempted(self) -> list[dispatch.RescueResult]:
        """Only the repos that actually had work to rescue."""
        return [r for r in self.results if r.attempted]

    @property
    def ok(self) -> bool:
        """True when every repo that had work got it onto origin."""
        return all(r.pushed for r in self.attempted)


async def _rescue_before_teardown(
    run: Run,
    repos: list[tuple[str, Path, str | None]],
    *,
    attribution: str | None,
) -> _RescueOutcome | None:
    """Push anything the agent left behind, then report it on the item.

    Returns None when there was nothing to inspect (no repos recorded, e.g. a run that failed before its workspace was prepared, or a plan/manage run).

    Everything here is best-effort and nothing raises: this runs inside the teardown ``finally``, where an exception would skip the workspace cleanup that follows it. What it must NOT do is fail silently — a rescue that could not push is the one case where the workspace has to be kept, so that outcome is returned to the caller AND written to the run's error text.
    """
    if not repos:
        return None
    branch = run.branch_name or ""
    if not branch.startswith("feat/"):
        # Hard guard on the blast radius: this pushes with hooks disabled, so it
        # may only ever touch the dispatch worker's own feature branch — never a
        # default branch, never anything it did not create.
        return None

    loop = asyncio.get_event_loop()
    results: list[dispatch.RescueResult] = []
    for _name, _path, _base in repos:
        try:
            results.append(
                await loop.run_in_executor(
                    dispatch._executor,
                    dispatch.rescue_abandoned_work,
                    _name,
                    _path,
                    branch,
                    run.id,
                )
            )
        except Exception:
            logger.exception(
                "rescue raised for repo %s (run %s) — continuing", _name, run.id
            )
    outcome = _RescueOutcome(results)
    if not outcome.attempted:
        return outcome

    _pushed = [r for r in outcome.attempted if r.pushed]
    _failed = [r for r in outcome.attempted if not r.pushed]
    logger.warning(
        "rescue: run_id=%s branch=%s pushed=%s failed=%s",
        run.id,
        branch,
        ",".join(r.repo_name for r in _pushed) or None,
        ",".join(r.repo_name for r in _failed) or None,
    )

    if _failed:
        _detail = "; ".join(r.error or f"{r.repo_name}: push failed" for r in _failed)
        try:
            _existing = await db.get_run(run.id)
            _prefix = (_existing.error + " | ") if _existing and _existing.error else ""
            await db.update_run(
                run.id,
                error=(
                    f"{_prefix}rescue_push_failed: unpushed work remains in the"
                    f" workspace, which has been RETAINED at {run.workspace_path}"
                    f" — {_detail}"
                )[:ERROR_TEXT_MAX_CHARS],
            )
        except Exception:
            logger.exception("could not record rescue failure for run %s", run.id)

    if run.item_id is not None:
        if _pushed:
            _body = (
                f"Unreviewed partial work from run `{run.id}` was pushed to"
                f" `{branch}` by the dispatch worker at teardown"
                f" ({', '.join(r.repo_name for r in _pushed)})."
                " The agent left it behind; it is incomplete, hooks were skipped"
                " and it has passed no quality gate. It was pushed only so it"
                " would not be deleted with the workspace — read it before"
                " reusing any of it."
            )
        else:
            _body = (
                f"Run `{run.id}` left unpushed work behind and the dispatch"
                f" worker could NOT rescue it to `{branch}`. The workspace has"
                " been retained so the work still exists on the dispatch host."
                f" Details: {_detail}"
            )
        try:
            await gtd_client.post_comment(
                run.item_id,
                _body,
                created_by=attribution or "agent-gtd-dispatch",
                token=run.callback_token,
            )
        except Exception:
            logger.warning("Failed to post rescue comment for run %s", run.id)
    return outcome


async def _record_build_terminal(
    run_id: str,
    *,
    status: RunStatus,
    push_results_list: list[RepoPushStatus] | None,
    envelope_verdict: str | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    push_results: str | None = None,
    completion_blob: str | None = None,
) -> RunStatus:
    """Persist a BUILD-mode terminal status through the single invariant choke point.

    The rule this enforces — a zero-commit build run is never a success — already
    regressed once when an agent re-decided it during a port, and the regression
    was invisible for weeks.  Construction-time discipline is not enough, so the
    check runs at runtime immediately before every terminal write and COERCES a
    violating status rather than trusting the caller.
    """
    if (
        status == RunStatus.succeeded
        and push_results_list is not None
        and dispatch.is_zero_commits_run(push_results_list)
    ):
        logger.error(
            "INVARIANT VIOLATION: zero-commit build run about to be recorded"
            " succeeded run_id=%s envelope_verdict=%s",
            run_id,
            envelope_verdict,
        )
        status = RunStatus.failed
        error = "invariant_zero_commit_success: " + (
            error or "zero-commit build run reached the success path"
        )
    await db.update_run(
        run_id,
        status=status,
        completed_at=completed_at,
        exit_code=exit_code,
        error=error,
        push_results=push_results,
        completion=completion_blob,
    )
    return status


async def _best_effort_set_item_status(
    item_id: str,
    status: str,
    run_id: str,
    *,
    callback_token: str | None,
    terminal: str,
) -> None:
    """PATCH an item's status, tolerating failure.

    SHARED by every worker path that nudges an item after a terminal write.  A
    PATCH failure must NEVER flip the run's already-recorded terminal — the run
    row is the source of truth about what happened and a flaky GTD call is not
    evidence that the build failed.
    """
    try:
        await gtd_client.set_item_status(item_id, status, token=callback_token)
    except Exception:
        logger.warning(
            "Failed to set item %s status=%s (run %s) — run stays %s",
            item_id,
            status,
            run_id,
            terminal,
        )


async def _nudge_item_to_review(
    item_id: str,
    run_id: str,
    *,
    callback_token: str | None,
    terminal: str,
) -> None:
    """Move an item to ``review`` unless it is already there (or ``done``).

    The worker — not the agent — materializes the item transition that follows
    from a build terminal.  The mapping, stated once:

    * a SUCCESSFUL build run (commits pushed, gate green or absent) -> ``review``
      (this function)
    * a talos ``already_satisfied`` run -> ``review``, via
      :func:`_route_already_satisfied_item`
    * every FAILED run -> NOT moved; those runs exit on the failure path and must
      never present themselves as ready for review.

    Reading the item first is the guard: never regress one a human already moved
    on.  A read failure leaves the item untouched, and a PATCH failure never
    flips the run's already-recorded terminal.
    """
    try:
        current = await gtd_client.get_item(item_id, token=callback_token)
        current_status = str(current.get("status") or "")
    except Exception:
        logger.warning(
            "Failed to read item %s status (run %s) — leaving it untouched",
            item_id,
            run_id,
        )
        return
    if current_status in {"review", "done"}:
        return
    await _best_effort_set_item_status(
        item_id,
        "review",
        run_id,
        callback_token=callback_token,
        terminal=terminal,
    )


async def _route_already_satisfied_item(
    item_id: str,
    run_id: str,
    reason: str,
    *,
    callback_token: str | None,
    attribution: str | None,
) -> None:
    """Route an ``already_satisfied`` BUILD terminal to GTD: item -> review + comment.

    TALOS-ONLY. This used to be shared with a claude-code path that reached the same terminal from an agent-written file claiming the work was already done; that path is gone, because a claim about a run cannot be evidence about a run. Talos keeps the terminal because talos exit 30 is emitted only AFTER talos has run the project's checks itself — the no-op is verified, not asserted.

    Status-set is deliberately tolerant: a PATCH failure does NOT flip the run status away from ``already_satisfied`` — mirrors every other status-set in this module.
    """
    await _best_effort_set_item_status(
        item_id,
        "review",
        run_id,
        callback_token=callback_token,
        terminal="already_satisfied",
    )
    body = (
        f"Build run `{run_id}` made no changes: the build engine reported the"
        " acceptance criteria are already satisfied, and its own checks passed"
        f" on the unchanged tree.\n\nReason: {reason}\n\n"
        "The item is moved to review for a human — it was NOT completed."
    )
    try:
        await gtd_client.post_comment(
            item_id,
            body,
            created_by=attribution or "agent-gtd-dispatch",
            token=callback_token,
        )
    except Exception:
        logger.warning("Failed to post already-satisfied comment for run %s", run_id)


async def _complete_rollout_item_skipped(
    rollout_id: str,
    item_id: str,
    run_id: str,
    *,
    callback_token: str | None,
    attribution: str | None,
) -> None:
    """Complete a rollout item with outcome=skipped and name what it unblocked.

    Talos-only today: talos has no GTD/MCP access by design, so the dispatch
    worker performs the rollout skip-and-advance step directly instead of
    relying on the manage-mode LLM to notice via polling (which is how the
    claude-code already_satisfied path reaches this same outcome — see the
    manage-prompt zero-commit rule in dispatch.py, which now recognizes both
    engines' already_satisfied terminal identically). Best-effort throughout:
    a failure here must never affect the run's already-recorded terminal
    status.
    """
    try:
        result = await gtd_client.complete_in_rollout(
            rollout_id,
            item_id,
            outcome="skipped",
            merge_actor="dispatch-worker",
            decision_rule="already-satisfied",
            token=callback_token,
        )
    except Exception:
        logger.warning(
            "Failed to complete rollout item %s in rollout %s as skipped (run %s)",
            item_id,
            rollout_id,
            run_id,
        )
        return
    newly_ready = result.get("newly_ready") if isinstance(result, dict) else None
    if newly_ready:
        detail = (
            f"Downstream items unblocked: {', '.join(str(x) for x in newly_ready)}."
        )
    else:
        detail = "No downstream items unblocked."
    try:
        await gtd_client.post_comment(
            item_id,
            (
                f"Rollout `{rollout_id}`: item recorded as skipped (already"
                f" satisfied) — the wave advances. {detail}"
            ),
            created_by=attribution or "agent-gtd-dispatch",
            token=callback_token,
        )
    except Exception:
        logger.warning(
            "Failed to post rollout skip-and-advance comment for run %s", run_id
        )


# ---------------------------------------------------------------------------
# Worker-driven rollout wave loop
# ---------------------------------------------------------------------------
#
# The worker owns the loop: determine what is ready, dispatch it, wait for
# completion, launch a short-lived REVIEWER for each completed build, act on
# that reviewer's verdict, advance, repeat.  No long-lived agent process is
# involved in any of it.
#
# Of roughly twenty steps in the old resident-manager prompt, exactly four were
# irreducibly model work — AC reconciliation, the unrelated-manifest scope
# judgment, the inline-fix small-or-not decision, and sensitive-area discretion
# — and all four sit inside the review-and-merge window for a SINGLE completed
# build.  Everything else was deterministic, and one step (the
# ``already_satisfied`` skip-and-advance) had already been ported to worker code
# precisely to stop relying on the LLM for it.  This generalises that precedent.
#
# The relaunch ladder, ``_do_manage_recovery``, the watchdog and the in-memory
# tallies above are deliberately left in place: they are torn down by a
# follow-up item once this loop is proven.

# Re-exported for tests (and so the cap has one name, not a literal).
MAX_ITEM_REDISPATCHES: int = config.MAX_ITEM_REDISPATCHES

# rollout_id -> item_id -> number of `re-dispatch` verdicts already honoured.
# In-memory like the manage tallies above: a dispatch-service restart resets
# the count, which errs toward giving an item one more chance rather than
# halting a rollout that a restart happened to interrupt.
_rollout_redispatches: dict[str, dict[str, int]] = {}

# Handle for the startup re-adoption sweep, held only so shutdown can cancel it.
_readoption_task: asyncio.Task[None] | None = None

# Loop tick directives.
_TICK_CONTINUE = "continue"  # state changed — re-read immediately
_TICK_WAIT = "wait"  # builds in flight — sleep, then re-read
_TICK_DONE = "done"  # graph_complete, or the rollout left `running`
_TICK_HALTED = "halted"  # the loop halted the rollout (or it was halted)

# Guard against a dispatch that reports success but does not move the item out
# of `next_ready` (the wave linkage is guarded by `AND status = 'ready'`, so a
# `pending` item would be dispatched forever).  Two attempts, then halt loudly.
_MAX_DISPATCH_ATTEMPTS_PER_ITEM = 2


class ReviewVerdict(NamedTuple):
    """A reviewer's bounded answer about ONE completed build."""

    verdict: str  # one of dispatch.REVIEW_VERDICTS
    rationale: str
    merge_note: str
    detail: str  # structured failure detail, or the rejection reason


def _completed_build_item_ids(
    advance: dict[str, Any], in_flight: list[Any]
) -> list[str]:
    """Items whose build has FINISHED — the advance/in-flight combination.

    THIS IS THE TRAP THE WHOLE LOOP TURNS ON.  ``advance_rollout``'s
    ``in_progress`` is "rollout_items rows in ``dispatched`` status" and is NOT
    joined to ``claude_runs.status``.  An item stays ``dispatched`` until
    something completes it in the rollout — which, in this design, is the
    worker acting on a reviewer's verdict.  So ``in_progress`` reports finished
    items as in-flight FOREVER, and a loop driven by ``advance_rollout`` alone
    never advances: it waits for a transition that only it can cause.

    The rollout's ``inFlightBuildRuns`` is the missing half — a JOIN against
    ``claude_runs.status`` filtered to non-terminal runs.  Subtracting it from
    ``in_progress`` yields exactly the items whose build run has gone terminal
    and which therefore need reviewing.  Using either source alone is a hang.

    Args:
        advance: The ``advance_rollout`` response.
        in_flight: The rollout's ``inFlightBuildRuns`` list.

    Returns:
        Item ids, in ``in_progress`` order, whose build run is no longer
        in flight.
    """
    in_flight_items = {
        str(entry.get("itemId"))
        for entry in in_flight or []
        if isinstance(entry, dict) and entry.get("itemId")
    }
    return [
        str(item_id)
        for item_id in (advance.get("in_progress") or [])
        if str(item_id) not in in_flight_items
    ]


async def _publish_rollout_phase(
    rollout_id: str,
    phase: str,
    *,
    item_id: str | None = None,
    step: str | None = None,
    token: str | None = None,
) -> None:
    """Write the rollout's manager_* state fields. Best effort, never fatal.

    ``manager_phase`` / ``manager_current_item_id`` / ``manager_current_step`` /
    ``manager_state_updated_at`` feed the Rollout Detail banner over SSE and
    have no other writer once the resident manager is gone.  Nothing branches
    on them for control flow — they are pure observability — so a failure to
    publish must never stop the wave.

    The stage -> ``ManagerPhase`` mapping is 1:1 with the existing enum, which
    is why no UI change is needed: ``warm_up`` (adopting the rollout and
    preparing the shared workspace), ``dispatching``, ``polling`` (waiting on
    in-flight builds), ``reviewing`` and ``merging`` (published by the reviewer
    itself, which is the agent actually in those phases), ``reconciling_ac``
    (also the reviewer's), and ``halted``.
    """
    try:
        await gtd_client.update_rollout_state(
            rollout_id, phase, current_item_id=item_id, current_step=step, token=token
        )
    except Exception:
        logger.warning(
            "rollout-loop: failed to publish phase=%s for rollout %s",
            phase,
            rollout_id,
        )


async def _latest_run_for_item(
    item_id: str, rollout_id: str, *, token: str | None
) -> dict[str, Any]:
    """Return the most recent child build run for *item_id* in *rollout_id*.

    Resolved by QUERY, never from memory: the startup re-adoption sweep picks
    up rollouts this process never dispatched, so an in-memory item->run map
    would be empty exactly when it is needed most.
    """
    try:
        runs = await gtd_client.list_runs_for_item(item_id, token=token)
    except Exception:
        logger.warning("rollout-loop: failed to list runs for item %s", item_id)
        return {}
    matching = [r for r in runs if str(r.get("rollout_id") or "") == rollout_id]
    if not matching:
        return {}
    return max(matching, key=lambda r: str(r.get("created_at") or ""))


def _gate_result_summary(run_row: dict[str, Any]) -> str:
    """Render the post-run gate outcome for the reviewer's envelope.

    The GTD-side run row has no gate column; the gate's verdict reaches it as
    the ``post-run gate ...`` prefix on ``error_msg`` (and, when it passed, as
    silence).  Say which of those we are looking at rather than handing the
    reviewer a bare string.
    """
    error_msg = str(run_row.get("error_msg") or "").strip()
    if not error_msg:
        return (
            "no failure recorded on the run — the post-run gate did not fail "
            "(it may have passed or been skipped for a project with no "
            "`gate_command`). Run the merge bar yourself regardless."
        )
    return f"the run recorded a failure: `{error_msg[:1000]}`"


async def _launch_reviewer(
    rollout_id: str,
    item_id: str,
    *,
    project: dict[str, Any],
    workspace: dispatch.RolloutWorkspace,
    reviewer_engine: Engine,
    token: str | None,
    attribution: str | None,
) -> ReviewVerdict:
    """Run ONE short-lived reviewer for ONE completed build and read its verdict.

    The reviewer run carries ``mode=DispatchMode.REVIEW``.  That is the chosen
    mechanism for keeping a reviewer's exit away from the manage relaunch
    ladder: the ladder fires on ``run.mode == MANAGE and run.rollout_id`` and
    exists to resurrect a RESIDENT manager, whereas a reviewer exiting is normal
    completion.  A distinct mode makes a reviewer structurally ineligible
    without disabling the ladder for anything else — the resident-manager path
    still exists until the follow-up item removes it — and it simultaneously
    buys the env withholding (``_MANAGE_EXECUTOR_ENV_KEYS`` is granted only for
    MANAGE) and the reviewer's own turn budget.  An explicit marker on a
    manage-mode run would have bought none of those and would have left the
    ladder one ``if`` away from firing on a reviewer.

    Returns a :class:`ReviewVerdict`.  Every failure path — launch error,
    non-zero exit, missing or malformed verdict artifact — returns
    ``verdict="halt"``: a reviewer that did not answer does not get the benefit
    of the doubt, because merging on a guess is the one outcome that cannot be
    undone.
    """
    item = await gtd_client.get_item(item_id, token=token)
    run_row = await _latest_run_for_item(item_id, rollout_id, token=token)
    branch_name = str(run_row.get("feature_branch") or "")
    if not branch_name:
        branch_name = make_branch_name(item_id, str(item.get("title") or ""))

    try:
        merge_notes = await gtd_client.get_rollout_merge_notes(
            rollout_id, limit=dispatch.MERGE_NOTE_CONTEXT_LIMIT, token=token
        )
    except Exception:
        logger.warning(
            "rollout-loop: failed to fetch merge notes for rollout %s", rollout_id
        )
        merge_notes = []

    used = _rollout_redispatches.get(rollout_id, {}).get(item_id, 0)
    repo_dirs = list(workspace.repo_paths)
    prompt = dispatch.build_review_prompt(
        item,
        project,
        rollout_id,
        branch_name,
        config.REVIEW_MAX_TURNS,
        workspace.root,
        repo_dirs=repo_dirs,
        default_branches=workspace.default_branches,
        workspace_mode=workspace.workspace_mode,
        run_status=str(run_row.get("status") or "unknown"),
        gate_result=_gate_result_summary(run_row),
        merge_notes=merge_notes,
        redispatches_used=used,
        redispatch_cap=MAX_ITEM_REDISPATCHES,
    )

    reviewer_run = Run(
        item_id=item_id,
        project_name=str(project.get("name") or ""),
        branch_name=branch_name,
        engine=reviewer_engine.name,
        engine_actual=reviewer_engine.name,
        mode=DispatchMode.REVIEW,
        rollout_id=rollout_id,
        workspace_path=str(workspace.root),
        callback_token=token,
        status=RunStatus.running,
        started_at=datetime.now(UTC),
    )
    await db.insert_run(reviewer_run)
    logger.info(
        "rollout-loop: reviewer spawn rollout_id=%s item_id=%s run_id=%s engine=%s "
        "branch=%s max_turns=%d",
        rollout_id,
        item_id,
        reviewer_run.id,
        reviewer_engine.name,
        branch_name,
        config.REVIEW_MAX_TURNS,
    )

    # Clear any prior verdict so a reviewer that dies before writing one can
    # never be credited with the PREVIOUS item's answer.
    dispatch.clear_review_verdict(workspace.root)

    exit_code: int | None = None
    failure: str = ""
    try:
        result = await dispatch.run_agent(
            reviewer_engine,
            workspace.root,
            prompt,
            f"Review and merge `{branch_name}` for item {item_id}",
            config.REVIEW_MAX_TURNS,
            timeout_seconds=config.REVIEW_TIMEOUT_SECONDS,
            mode=DispatchMode.REVIEW,
            attribution=attribution,
            callback_token=token,
            run_id=reviewer_run.id,
        )
        exit_code = result.returncode
    except Exception as exc:
        failure = f"reviewer subprocess failed: {type(exc).__name__}: {exc}"
        logger.exception(
            "rollout-loop: reviewer run %s raised for item %s",
            reviewer_run.id,
            item_id,
        )

    verdict_blob, reason = dispatch.read_review_verdict(workspace.root)

    if verdict_blob is None:
        detail = failure or f"verdict artifact {reason} (reviewer exit={exit_code})"
        await db.update_run(
            reviewer_run.id,
            status=RunStatus.failed,
            completed_at=datetime.now(UTC).isoformat(),
            error=f"review_no_verdict: {detail}"[:ERROR_TEXT_MAX_CHARS],
        )
        logger.warning(
            "rollout-loop: reviewer run %s produced no usable verdict (%s) — "
            "treating as halt",
            reviewer_run.id,
            reason,
        )
        return ReviewVerdict("halt", "reviewer returned no usable verdict", "", detail)

    verdict = str(verdict_blob["verdict"])
    rationale = str(verdict_blob.get("rationale") or "")[
        : dispatch.REVIEW_RATIONALE_MAX_CHARS
    ]
    merge_note = str(verdict_blob.get("merge_note") or "")
    fail = verdict_blob.get("failure")
    detail = ""
    if isinstance(fail, dict) and (fail.get("kind") or fail.get("detail")):
        detail = f"{fail.get('kind') or 'unspecified'}: {fail.get('detail') or ''}"
    repo_states = verdict_blob.get("repo_states")
    if isinstance(repo_states, dict) and repo_states:
        states = ", ".join(f"{k}={v}" for k, v in sorted(repo_states.items()))
        detail = f"{detail} [repo states: {states}]" if detail else f"[{states}]"

    await db.update_run(
        reviewer_run.id,
        status=RunStatus.succeeded,
        completed_at=datetime.now(UTC).isoformat(),
        completion=json.dumps(
            {"verdict": verdict, "rationale": rationale, "detail": detail}
        ),
    )
    logger.info(
        "rollout-loop: reviewer verdict rollout_id=%s item_id=%s run_id=%s "
        "verdict=%s exit_code=%s",
        rollout_id,
        item_id,
        reviewer_run.id,
        verdict,
        exit_code,
    )
    return ReviewVerdict(verdict, rationale, merge_note, detail)


async def _halt_rollout_from_loop(
    rollout_id: str,
    reason: str,
    *,
    item_id: str | None = None,
    token: str | None = None,
    attribution: str | None = None,
) -> str:
    """Halt the rollout, publish the halted phase, and comment. Returns _TICK_HALTED."""
    await _publish_rollout_phase(
        rollout_id, "halted", item_id=item_id, step=reason[:200], token=token
    )
    if item_id:
        try:
            await gtd_client.post_comment(
                item_id,
                f"Rollout `{rollout_id}` halted: {reason}",
                created_by=attribution or "agent-gtd-dispatch",
                token=token,
            )
        except Exception:
            logger.warning("rollout-loop: failed to comment halt on item %s", item_id)
    try:
        await gtd_client.halt_rollout(rollout_id, reason=reason, token=token)
    except Exception:
        logger.exception("rollout-loop: failed to halt rollout %s", rollout_id)
    logger.warning("rollout-loop: halted rollout_id=%s reason=%s", rollout_id, reason)
    return _TICK_HALTED


async def _act_on_verdict(
    rollout_id: str,
    item_id: str,
    outcome: ReviewVerdict,
    *,
    token: str | None,
    attribution: str | None,
) -> str:
    """Apply a reviewer's verdict. The WORKER acts; the reviewer only decided.

    Including the re-dispatch: a reviewer is never handed the ability to
    dispatch a child run, so ``re-dispatch`` is a request that this function
    executes, capped at ``MAX_ITEM_REDISPATCHES`` per item.  Past the cap the
    rollout halts — which is also the DEFAULT behaviour for a failed child, and
    deliberately unchanged: absent an explicit ``re-dispatch`` verdict, a failed
    child halts the rollout exactly as it does today.
    """
    if outcome.verdict == "halt":
        reason = f"reviewer halted item {item_id}: {outcome.rationale}"
        if outcome.detail:
            reason = f"{reason} ({outcome.detail})"
        return await _halt_rollout_from_loop(
            rollout_id,
            reason[:ERROR_TEXT_MAX_CHARS],
            item_id=item_id,
            token=token,
            attribution=attribution,
        )

    if outcome.verdict in ("merge", "skip"):
        recorded = "completed" if outcome.verdict == "merge" else "skipped"
        try:
            result = await gtd_client.complete_in_rollout(
                rollout_id,
                item_id,
                outcome=recorded,
                merge_actor="reviewer-autonomous",
                decision_rule="agent-judgment",
                merge_note=outcome.merge_note,
                token=token,
            )
        except Exception as exc:
            return await _halt_rollout_from_loop(
                rollout_id,
                f"failed to record item {item_id} as {recorded}: "
                f"{type(exc).__name__}: {exc}",
                item_id=item_id,
                token=token,
                attribution=attribution,
            )
        newly_ready = result.get("newly_ready") if isinstance(result, dict) else None
        unblocked = (
            f"Downstream items unblocked: {', '.join(str(x) for x in newly_ready)}."
            if newly_ready
            else "No downstream items unblocked."
        )
        try:
            await gtd_client.post_comment(
                item_id,
                (
                    f"Rollout `{rollout_id}`: reviewer verdict `{outcome.verdict}` — "
                    f"item recorded as {recorded}. {outcome.rationale} {unblocked}"
                ),
                created_by=attribution or "agent-gtd-dispatch",
                token=token,
            )
        except Exception:
            logger.warning("rollout-loop: failed to comment verdict on %s", item_id)
        return _TICK_CONTINUE

    # re-dispatch
    used = _rollout_redispatches.setdefault(rollout_id, {}).get(item_id, 0)
    if used >= MAX_ITEM_REDISPATCHES:
        return await _halt_rollout_from_loop(
            rollout_id,
            f"re-dispatch cap reached for item {item_id} "
            f"({used}/{MAX_ITEM_REDISPATCHES}): {outcome.rationale}",
            item_id=item_id,
            token=token,
            attribution=attribution,
        )
    _rollout_redispatches[rollout_id][item_id] = used + 1
    try:
        await gtd_client.reset_rollout_item(rollout_id, item_id, token=token)
        await gtd_client.dispatch_item(item_id, rollout_id=rollout_id, token=token)
    except Exception as exc:
        return await _halt_rollout_from_loop(
            rollout_id,
            f"re-dispatch of item {item_id} failed: {type(exc).__name__}: {exc}",
            item_id=item_id,
            token=token,
            attribution=attribution,
        )
    logger.info(
        "rollout-loop: re-dispatched rollout_id=%s item_id=%s attempt=%d cap=%d",
        rollout_id,
        item_id,
        used + 1,
        MAX_ITEM_REDISPATCHES,
    )
    try:
        await gtd_client.post_comment(
            item_id,
            (
                f"Rollout `{rollout_id}`: reviewer verdict `re-dispatch` "
                f"({used + 1}/{MAX_ITEM_REDISPATCHES}) — {outcome.rationale}"
            ),
            created_by=attribution or "agent-gtd-dispatch",
            token=token,
        )
    except Exception:
        logger.warning("rollout-loop: failed to comment re-dispatch on %s", item_id)
    return _TICK_CONTINUE


async def _rollout_wave_tick(
    rollout_id: str,
    *,
    project: dict[str, Any],
    workspace_ref: list[dispatch.RolloutWorkspace | None],
    reviewer_engine: Engine,
    dispatch_attempts: dict[str, int],
    token: str | None,
    attribution: str | None,
) -> str:
    """One pass of the wave loop. Returns a ``_TICK_*`` directive.

    Order is load-bearing: completed builds are reviewed BEFORE newly-ready
    items are dispatched, so a dependent item is never dispatched against a
    base its predecessor has not yet been merged into.
    """
    advance = await gtd_client.advance_rollout(rollout_id, token=token)
    rollout = await gtd_client.get_rollout(rollout_id, token=token)

    status = str(rollout.get("status") or "")
    if status != "running":
        logger.info(
            "rollout-loop: rollout_id=%s left running (status=%s) — loop exits",
            rollout_id,
            status,
        )
        return _TICK_HALTED if status == "halted" else _TICK_DONE

    # Wave completion is READ, not computed. `complete_item_in_rollout` already
    # closes the rollout and signals `graph_complete`, and `advance_rollout`
    # computes the same thing independently. The worker consumes that field and
    # does NOT invent a third notion of done.
    if advance.get("graph_complete"):
        logger.info(
            "rollout-loop: rollout_id=%s graph_complete — loop exits", rollout_id
        )
        return _TICK_DONE

    in_flight = _in_flight_build_runs(rollout)
    completed = _completed_build_item_ids(advance, in_flight)

    if completed:
        item_id = completed[0]
        if workspace_ref[0] is None:
            await _publish_rollout_phase(
                rollout_id,
                "warm_up",
                item_id=item_id,
                step="Preparing the shared rollout workspace",
                token=token,
            )
            workspace_ref[0] = await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    dispatch.prepare_rollout_workspace,
                    rollout_id,
                    git_origin=str(project.get("git_origin") or ""),
                    repo_urls=list(project.get("workspace_repos") or []),
                ),
            )
        workspace = workspace_ref[0]
        assert workspace is not None  # noqa: S101 — just assigned above
        await asyncio.get_running_loop().run_in_executor(
            None, dispatch.refresh_rollout_workspace, workspace
        )
        await _publish_rollout_phase(
            rollout_id,
            "reviewing",
            item_id=item_id,
            step=f"Reviewing the completed build for {item_id}",
            token=token,
        )
        outcome = await _launch_reviewer(
            rollout_id,
            item_id,
            project=project,
            workspace=workspace,
            reviewer_engine=reviewer_engine,
            token=token,
            attribution=attribution,
        )
        dispatch_attempts.pop(item_id, None)
        return await _act_on_verdict(
            rollout_id, item_id, outcome, token=token, attribution=attribution
        )

    next_ready = [str(i) for i in (advance.get("next_ready") or [])]
    if next_ready:
        for item_id in next_ready:
            attempts = dispatch_attempts.get(item_id, 0)
            if attempts >= _MAX_DISPATCH_ATTEMPTS_PER_ITEM:
                return await _halt_rollout_from_loop(
                    rollout_id,
                    f"item {item_id} is still reported ready after {attempts} "
                    "successful dispatch calls — the rollout item never "
                    "transitioned to 'dispatched'",
                    item_id=item_id,
                    token=token,
                    attribution=attribution,
                )
            await _publish_rollout_phase(
                rollout_id,
                "dispatching",
                item_id=item_id,
                step=f"Dispatching {item_id}",
                token=token,
            )
            try:
                child = await gtd_client.dispatch_item(
                    item_id, rollout_id=rollout_id, token=token
                )
            except Exception as exc:
                return await _halt_rollout_from_loop(
                    rollout_id,
                    f"dispatch of item {item_id} failed: {type(exc).__name__}: {exc}",
                    item_id=item_id,
                    token=token,
                    attribution=attribution,
                )
            dispatch_attempts[item_id] = attempts + 1
            logger.info(
                "rollout-loop: dispatched rollout_id=%s item_id=%s run_id=%s",
                rollout_id,
                item_id,
                (child or {}).get("id"),
            )
        return _TICK_CONTINUE

    if in_flight:
        await _publish_rollout_phase(
            rollout_id,
            "polling",
            step=f"Waiting on {len(in_flight)} in-flight build run(s)",
            token=token,
        )
        return _TICK_WAIT

    return await _halt_rollout_from_loop(
        rollout_id,
        "rollout stalled: nothing ready, nothing in flight, graph not complete "
        f"(blocked={list(advance.get('blocked') or [])})",
        token=token,
        attribution=attribution,
    )


async def _drive_rollout(
    run: Run,
    *,
    attribution: str | None = None,
) -> str:
    """Drive one rollout's wave loop to completion. Returns the final directive.

    This is the whole of AC1: determine what is ready, dispatch it, wait for
    completion, review each completed build, act on the verdict, advance,
    repeat until the rollout is complete or halted.
    """
    rollout_id = run.rollout_id
    assert rollout_id is not None  # noqa: S101 — caller guarantees this
    token = run.callback_token

    rollout = await gtd_client.get_rollout(rollout_id, token=token)
    project = await gtd_client.get_project(
        str(rollout.get("project_id") or ""), token=token
    )
    run.project_name = str(project.get("name") or run.project_name)

    reviewer_engine_name = (
        str(rollout.get("reviewer_engine") or "").strip() or config.REVIEWER_ENGINE
    )
    try:
        reviewer_engine = get_engine(reviewer_engine_name)
    except ValueError:
        logger.warning(
            "rollout-loop: unknown reviewer engine %r for rollout %s — using %s",
            reviewer_engine_name,
            rollout_id,
            config.REVIEWER_ENGINE,
        )
        reviewer_engine = get_engine(config.REVIEWER_ENGINE)

    logger.info(
        "rollout-loop: start rollout_id=%s run_id=%s project=%s reviewer_engine=%s",
        rollout_id,
        run.id,
        project.get("name"),
        reviewer_engine.name,
    )
    await _publish_rollout_phase(
        rollout_id, "warm_up", step="Wave loop adopted the rollout", token=token
    )

    workspace_ref: list[dispatch.RolloutWorkspace | None] = [None]
    dispatch_attempts: dict[str, int] = {}
    directive = _TICK_CONTINUE
    try:
        while True:
            directive = await _rollout_wave_tick(
                rollout_id,
                project=project,
                workspace_ref=workspace_ref,
                reviewer_engine=reviewer_engine,
                dispatch_attempts=dispatch_attempts,
                token=token,
                attribution=attribution,
            )
            if directive in (_TICK_DONE, _TICK_HALTED):
                return directive
            if directive == _TICK_WAIT:
                await asyncio.sleep(config.ROLLOUT_LOOP_POLL_SECONDS)
    finally:
        _rollout_redispatches.pop(rollout_id, None)
        ws = workspace_ref[0]
        # The shared workspace is torn down only when the loop itself ends. A
        # halted rollout keeps it: the tree is the evidence a human needs.
        if ws is not None and directive == _TICK_DONE:
            with contextlib.suppress(Exception):
                dispatch.cleanup_workspace(ws.root)


async def _rollout_loop_worker(run: Run, *, attribution: str | None = None) -> None:
    """Background task wrapper around :func:`_drive_rollout` with run bookkeeping.

    Owns the same slot/queue accounting ``_dispatch_worker`` does, because it
    occupies a run slot for as long as the rollout runs.
    """
    assert run.rollout_id is not None  # noqa: S101
    started = datetime.now(UTC).isoformat()
    await db.update_run(run.id, status=RunStatus.running, started_at=started)
    _publish_run_event(run.id, "running", None)
    _rollout_to_run[run.rollout_id] = run
    try:
        directive = await _drive_rollout(run, attribution=attribution)
        finished = datetime.now(UTC).isoformat()
        status = RunStatus.succeeded if directive == _TICK_DONE else RunStatus.failed
        await db.update_run(
            run.id,
            status=status,
            completed_at=finished,
            error="" if directive == _TICK_DONE else f"rollout {directive}",
        )
        _publish_run_event(run.id, status.value, finished)
    except asyncio.CancelledError:
        cancelled = datetime.now(UTC).isoformat()
        await db.update_run(run.id, status=RunStatus.cancelled, completed_at=cancelled)
        _publish_run_event(run.id, "cancelled", cancelled)
        raise
    except Exception as exc:
        logger.exception("rollout-loop: rollout %s loop crashed", run.rollout_id)
        failed = datetime.now(UTC).isoformat()
        await db.update_run(
            run.id,
            status=RunStatus.failed,
            completed_at=failed,
            error=f"rollout_loop_error: {type(exc).__name__}: {exc}"[
                :ERROR_TEXT_MAX_CHARS
            ],
        )
        _publish_run_event(run.id, "failed", failed)
    finally:
        _active_processes.pop(run.id, None)
        _try_start_pending()
        _run_event_queues.pop(run.id, None)
        _rollout_to_run.pop(run.rollout_id, None)
        logger.info(
            "rollout-loop: exit rollout_id=%s run_id=%s", run.rollout_id, run.id
        )


def _start_rollout_loop(run: Run, *, attribution: str | None = None) -> None:
    """Create and register the background task that drives one rollout."""
    task = asyncio.create_task(_rollout_loop_worker(run, attribution=attribution))
    _active_processes[run.id] = task


async def _readopt_running_rollouts() -> None:
    """Startup sweep: resume every running rollout from its persisted frontier.

    REQUIRED, not a follow-up.  The wave loop now lives INSIDE the dispatch
    service, whose own ``deploy.sh`` restarts it — so a routine deploy used to
    mean "the monitor lies" and would now mean "the wave silently stops
    advancing" without this.  Largely a relocation of the watchdog's existing
    scan shape: list running rollouts, and for each one that no live loop owns,
    start one.  The loop itself resumes from the persisted frontier, because
    every tick re-reads ``advance_rollout`` plus the in-flight query rather than
    trusting any in-process state.

    Double-dispatch safety comes from the GTD side: the wave linkage in
    ``dispatch_item`` is guarded by ``AND status = 'ready'``, so an item whose
    build is already in flight (status ``dispatched``) cannot be dispatched
    again — and such items are reported by ``advance_rollout`` as
    ``in_progress``, not ``next_ready``, so the resumed loop does not even try.
    """
    try:
        rollouts = await gtd_client.list_running_rollouts()
    except Exception:
        logger.exception("re-adoption sweep: failed to list running rollouts")
        return

    logger.info("re-adoption sweep: %d running rollout(s)", len(rollouts))
    for rollout in rollouts:
        rollout_id = str(rollout.get("id") or "")
        if not rollout_id or rollout_id in _rollout_to_run:
            continue
        run = Run(
            project_name=str(rollout.get("project_name") or ""),
            mode=DispatchMode.MANAGE,
            rollout_id=rollout_id,
            engine=str(rollout.get("reviewer_engine") or config.REVIEWER_ENGINE),
        )
        try:
            await db.insert_run(run)
        except Exception:
            logger.exception(
                "re-adoption sweep: failed to record loop run for rollout %s",
                rollout_id,
            )
            continue
        logger.info(
            "re-adoption sweep: resuming rollout_id=%s with run_id=%s",
            rollout_id,
            run.id,
        )
        _start_rollout_loop(run)


async def _dispatch_worker(
    run: Run,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    *,
    attribution: str | None = None,
    manage_retry_count: int = 0,
    is_recovery: bool = False,
    resume_context: list[dict[str, Any]] | None = None,
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
    # For BUILD mode: list of (repo_name, repo_path, base_sha) passed to
    # verify_pushes after the agent exits.  None means skip verification
    # (manage/plan mode or exceptions during workspace prep).  Declared before
    # the try so the teardown `finally` can capture evidence on every path,
    # including ones that raise before workspace prep finishes.
    _verify_repos: list[tuple[str, Path, str]] | None = None
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

        # Durable cross-item context for manage runs: the last N merge notes.
        # Best effort — a manager with no notes is exactly today's behaviour, so a
        # failed fetch must never block the dispatch.
        merge_notes: list[dict[str, Any]] = []
        if mode == DispatchMode.MANAGE and run.rollout_id:
            try:
                merge_notes = await gtd_client.get_rollout_merge_notes(
                    run.rollout_id,
                    limit=dispatch.MERGE_NOTE_CONTEXT_LIMIT,
                    token=run.callback_token,
                )
            except Exception:
                logger.warning(
                    "Failed to fetch merge notes for rollout %s — the manage "
                    "prompt will render without them",
                    run.rollout_id,
                )

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
            workspace=workspace,
            resume_context=resume_context,
            merge_notes=merge_notes,
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
            run_id=run.id,
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
            _envelope: completion.ResultEnvelope | None = None
            _verdict = "no_result_envelope"
            _evidence_dir = str(retention.evidence_dir(run.id))
            if _verify_repos is not None:
                # The CLI's own result envelope, from the merged-stream
                # transcript.  Read BEFORE any terminal so every build path can
                # persist it.
                if workspace is not None:
                    _envelope = completion.parse_result_envelope(
                        workspace / "transcript.txt"
                    )
                _verdict = completion.envelope_verdict(_envelope)

                def _build_completion(
                    outcome: str,
                    gate_decision: str | None = None,
                ) -> str:
                    """Log the one structured decision line and return the blob.

                    Called immediately before every BUILD terminal write so the
                    branch taken and the evidence that drove it are both
                    greppable in the journal and durable on the run row.
                    """
                    _results = push_results_list or []
                    _zero = dispatch.is_zero_commits_run(_results)
                    _pushed = sum(
                        1 for _r in _results if _r.status == PushStatus.pushed
                    )
                    blob = build_completion_blob(
                        envelope=_envelope,
                        envelope_verdict=_verdict,
                        zero_commits=_zero,
                        gate_decision=gate_decision,
                        evidence_dir=_evidence_dir,
                    )
                    logger.info(
                        "build completion: run_id=%s outcome=%s envelope_verdict=%s"
                        " envelope_subtype=%s is_error=%s num_turns=%s"
                        " stop_reason=%s session_id=%s total_cost_usd=%s"
                        " zero_commits=%s pushed_repos=%d gate_decision=%s engine=%s",
                        run.id,
                        outcome,
                        _verdict,
                        _envelope.subtype if _envelope else None,
                        _envelope.is_error if _envelope else None,
                        _envelope.num_turns if _envelope else None,
                        _envelope.stop_reason if _envelope else None,
                        _envelope.session_id if _envelope else None,
                        _envelope.total_cost_usd if _envelope else None,
                        _zero,
                        _pushed,
                        gate_decision,
                        engine_used.name,
                    )
                    return blob

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
                        completion=_build_completion("failed"),
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

                # --- BUILD terminal classification -------------------------
                # MECHANICAL EVIDENCE ONLY. Every input to this decision is
                # something the WORKER observed: the CLI's own result envelope,
                # the commits that reached origin, and the project gate's exit
                # code. Nothing the agent originated is read here.
                #
                # NO CODE IN THIS REGION MAY BRANCH ON WHETHER THE AGENT SAID
                # ANYTHING — not on a comment, not on a file it wrote, not on an
                # MCP call it made. That exact mistake has now been made twice at
                # two different addresses. First it was `len(comments) >= 2`,
                # counting GTD comments as proof of work. The completion artifact
                # was introduced to fix it and reproduced it one layer down: a
                # JSON file the agent wrote, believed on its own say-so, with a
                # three-tier cascade bolted on to guess what the agent meant when
                # the file was missing. The file was in fact unreadable on every
                # host from the day it shipped (`cat` was never in the sudoers
                # NOPASSWD list), so the cascade ran on every single run and the
                # "agents are 0% compliant" signal that justified building it was
                # an artefact of a permissions bug.
                #
                # The rule that replaces all of it: an agent-originated signal is
                # a REQUEST, never a guarantee. It may inform a human. It may not
                # move a run's status.
                _zero_commits = dispatch.is_zero_commits_run(push_results_list)
                _failure_prefix: str | None = None

                if _verdict != "ok":
                    # The envelope is the CLI's own statement about how the
                    # process ended — harness-emitted, not agent-authored.
                    _failure_prefix = _verdict
                elif _zero_commits:
                    # Zero commits across every repo is terminal, decided HERE,
                    # before the gate runs.
                    #
                    # Running the gate on a commit-less tree is worse than
                    # useless: the tree IS the base commit, so a green result
                    # says the base is green and says nothing whatever about the
                    # run. Treating that green as evidence of a completed no-op
                    # is precisely how four agents' worth of real work was
                    # discarded as "already satisfied" in a single night. It also
                    # costs ~6 minutes of gate time per run to learn nothing.
                    _failure_prefix = "zero_commits"

                if _failure_prefix is not None:
                    error_str = f"{_failure_prefix}: " + (
                        "the agent ended its run without producing any commits"
                        " on any repo"
                        if _failure_prefix == "zero_commits"
                        else "build run did not reach a usable conclusion"
                    )
                    _push_results_json = json.dumps(
                        [r.model_dump(mode="json") for r in push_results_list]
                    )
                    await db.update_run(
                        run.id,
                        status=RunStatus.failed,
                        completed_at=completed,
                        exit_code=result.returncode,
                        error=error_str[:ERROR_TEXT_MAX_CHARS],
                        push_results=_push_results_json,
                        completion=_build_completion("failed"),
                    )
                    _publish_run_event(run.id, "failed", completed)
                    if run.item_id is not None:
                        try:
                            await gtd_client.post_comment(
                                run.item_id,
                                build_failure_comment(_failure_prefix, run.id),
                                created_by=attribution or "agent-gtd-dispatch",
                                token=run.callback_token,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to post build-failure comment for run %s",
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

                # Reaching here means commits were pushed — the zero-commit case
                # already returned above without running the gate. The
                # `_n_pushed == 0` branch survives only for the case where every
                # repo reports `no_changes` yet `is_zero_commits_run` did not
                # fire (an empty repo list), and it short-circuits rather than
                # gating a tree nothing touched.
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
                        completion=_build_completion("failed", decision),
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

            if push_results_list is not None:
                _push_results_json = json.dumps(
                    [r.model_dump(mode="json") for r in push_results_list]
                )

            # NOTE: there is no `already_satisfied` branch here. That terminal is
            # reachable from talos exit 30 ONLY (see `_run_talos`), where talos
            # ran its own checks before emitting the verdict. A claude-code build
            # cannot produce it: the only evidence that ever distinguished a
            # deliberate no-op from a silent failure was the agent's own claim,
            # and a claim is not evidence. Zero commits now fails above.

            # All pushed (or no BUILD verification needed) — mark succeeded
            if _verify_repos is not None:
                _final_status = await _record_build_terminal(
                    run.id,
                    status=RunStatus.succeeded,
                    push_results_list=push_results_list,
                    envelope_verdict=_verdict,
                    completed_at=completed,
                    exit_code=result.returncode,
                    push_results=_push_results_json,
                    completion_blob=_build_completion("succeeded", decision),
                )
                _publish_run_event(run.id, _final_status.value, completed)
                if _final_status is not RunStatus.succeeded:
                    if run.item_id is not None:
                        try:
                            await gtd_client.post_comment(
                                run.item_id,
                                build_failure_comment(
                                    "invariant_zero_commit_success", run.id
                                ),
                                created_by=attribution or "agent-gtd-dispatch",
                                token=run.callback_token,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to post invariant-violation comment for run %s",
                                run.id,
                            )
                    return
            else:
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
            if _verify_repos is not None and run.item_id is not None:
                # ALWAYS: the worker's own account of a successful build run,
                # composed entirely from what the worker observed — branch,
                # per-repo commit counts, push outcomes, gate decision. The agent
                # does not report the mechanical half and does not set the item
                # status; both are materialized here.
                try:
                    await gtd_client.post_comment(
                        run.item_id,
                        build_completion_comment(
                            run.id,
                            run.branch_name,
                            push_results_list,
                            decision,
                        ),
                        created_by=attribution or "agent-gtd-dispatch",
                        token=run.callback_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to post build completion comment for run %s",
                        run.id,
                    )
                # A successful build run moves its item to `review`. There is no
                # other outcome that reaches here: failures returned early above.
                await _nudge_item_to_review(
                    run.item_id,
                    run.id,
                    callback_token=run.callback_token,
                    terminal="succeeded",
                )
        else:
            # Derive error snippet from the transcript (stdout/stderr are always
            # "" with Popen streaming).  With --output-format json the raw tail is
            # a truncated mid-object JSON fragment, so prefer the parsed result
            # envelope and fall back to the raw tail only when there isn't one.
            error_msg = None
            if workspace is not None:
                transcript_path = workspace / "transcript.txt"
                _exit_envelope = completion.parse_result_envelope(transcript_path)
                if _exit_envelope is not None:
                    error_msg = (
                        f"{_exit_envelope.subtype}: {_exit_envelope.result or ''}"
                    )[:500]
                elif transcript_path.exists():
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
                _linger_status = await _record_build_terminal(
                    run.id,
                    status=RunStatus.succeeded,
                    push_results_list=push_results_list,
                    completed_at=_timed_out_at,
                    push_results=_push_results_json,
                )
                _publish_run_event(run.id, _linger_status.value, _timed_out_at)
                if run.item_id is not None and _linger_status is RunStatus.succeeded:
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
        # Evidence capture is verdict-free and runs on EVERY terminal path —
        # success, gate failure, push-verification failure, zero-commit, agent
        # non-zero exit, timeout, cancellation and the generic exception — and
        # always BEFORE the workspace is torn down.
        _evidence_repos: list[tuple[str, Path, str | None]] = [
            (_n, _p, _b) for _n, _p, _b in (_verify_repos or [])
        ]
        try:
            retention.capture_evidence(run.id, workspace, _evidence_repos)
        except Exception:
            logger.exception("evidence capture raised for run %s — continuing", run.id)

        # Rescue: commit and push anything the agent left behind, BEFORE the
        # clone is deleted. Teardown is irreversible, so this is the last moment
        # the work exists anywhere. Runs regardless of the recorded terminal —
        # a failed run is exactly the case where unpushed work is most likely.
        _rescued = await _rescue_before_teardown(
            run, _evidence_repos, attribution=attribution
        )
        if _rescued is not None and not _rescued.ok:
            # Rescue could not get the work to origin. The workspace is the only
            # copy left, so keep it and say so where an operator will see it.
            should_cleanup = False

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
    if body.mode == DispatchMode.REVIEW:
        # REVIEW runs are worker-internal: the wave loop launches one per
        # completed build, with an envelope (branch, gate result, merge notes,
        # re-dispatch tally) that no external caller can supply. Accepting one
        # over HTTP would build a reviewer prompt with an empty envelope and
        # let it merge on no evidence.
        raise HTTPException(
            status_code=400,
            detail=(
                "mode=review is not externally dispatchable — reviewers are "
                "launched by the dispatch worker's rollout wave loop"
            ),
        )
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
        # Authoritative when present: for manage runs this carries the GTD-side
        # `dispatch.manager_default_timeout_minutes` setting.
        timeout_seconds = body.timeout_minutes * 60
    else:
        timeout_seconds = config.timeout_seconds_for_mode(body.mode)

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
    if run.mode == DispatchMode.MANAGE and run.rollout_id:
        # A manage dispatch no longer spawns a resident manager agent: the
        # WORKER drives the wave loop and launches a short-lived reviewer per
        # completed build. The resident-manager prompt, the relaunch ladder,
        # `_do_manage_recovery` and the watchdog all remain in place — a
        # follow-up item tears them down once this loop is proven.
        _start_rollout_loop(run, attribution=body.attribution)
    else:
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
        RunStatus.already_satisfied,
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
