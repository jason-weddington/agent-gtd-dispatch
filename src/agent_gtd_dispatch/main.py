"""Dispatch worker API — runs headless coding agents."""

from __future__ import annotations

import asyncio
import contextlib
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

from . import config, db, dispatch, gtd_client, rollout_planner, talos
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
        "manage-recovery: entry rollout_id=%s run_id=%s source=%s run_killed=%s",
        rollout_id,
        run_id,
        source,
        run_killed,
    )

    # manage-recovery deliberately uses the static service key: it can fire from
    # the watchdog (no owning user/run) or from the post-exit relaunch path, and
    # we want recovery to succeed even when the original Run's callback_token
    # has expired. Do NOT thread a per-run token here.
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
            logger.exception("Failed to halt rollout %s after cap exceeded", rollout_id)
        return

    logger.info(
        "Relaunching manage agent for rollout %s (retry %d/%d) after %ds",
        rollout_id,
        retry_count,
        MAX_MANAGE_RETRIES,
        MANAGE_RETRY_BACKOFF_SECONDS,
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
    task = asyncio.create_task(
        _dispatch_worker(
            new_run,
            max_turns,
            engine,
            timeout_seconds,
            attribution=attribution,
            manage_retry_count=retry_count,
        )
    )
    _active_processes[new_run.id] = task


async def _maybe_relaunch_manage(
    run: Run,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    attribution: str | None,
) -> None:
    """Check rollout status on manage exit and relaunch or halt as appropriate.

    Called from _dispatch_worker's finally block (skipped when human-cancelled).
    - If rollout is in a clean terminal state: do nothing.
    - If rollout is still running/pending (unexpected exit): delegate to
      _do_manage_recovery which increments retry count and either relaunches
      or halts.
    """
    assert run.rollout_id is not None  # noqa: S101 — caller guarantees this
    # manage-recovery probe: deliberately on the static key (callback_token may
    # have expired by the time we relaunch). See _do_manage_recovery comment.
    try:
        rollout = await gtd_client.get_rollout(run.rollout_id)
    except Exception:
        logger.exception(
            "Failed to fetch rollout %s for relaunch check — skipping recovery",
            run.rollout_id,
        )
        return

    if rollout["status"] in _CLEAN_EXIT_STATUSES:
        logger.info(
            "manage-recovery: clean-exit rollout_id=%s run_id=%s rollout_status=%s",
            run.rollout_id,
            run.id,
            rollout["status"],
        )
        return  # clean exit — nothing to do

    logger.info(
        "manage-recovery: unexpected-exit rollout_id=%s run_id=%s rollout_status=%s",
        run.rollout_id,
        run.id,
        rollout["status"],
    )
    # Unexpected exit: rollout still running or pending
    await _do_manage_recovery(
        run.rollout_id,
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
                commit_rc = subprocess.run(
                    dispatch._sudo_wrap(
                        ["git", *git_ident_flags, "commit", "-m", commit_msg]
                    ),
                    cwd=str(repo_path),
                    check=False,
                    capture_output=True,
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
                    dispatch._sudo_wrap(["git", "push", "-u", "origin", branch_name]),
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

        commit_rc = subprocess.run(
            dispatch._sudo_wrap(["git", *git_ident_flags, "commit", "-m", commit_msg]),
            cwd=str(workspace),
            check=False,
            capture_output=True,
        )
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
            dispatch._sudo_wrap(["git", "push", "-u", "origin", branch_name]),
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
        await db.update_run(
            run.id,
            status=status,
            completed_at=now,
            exit_code=exit_code,
            error=comment_header,
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
) -> None:
    """Background task that executes a dispatch run."""
    now = datetime.now(UTC).isoformat()
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

    try:
        # Fetch item and project.
        # manage-mode runs have item_id=None — derive project from the rollout instead.
        item: dict[str, Any] = {}
        if run.item_id is not None:
            item = await gtd_client.get_item(run.item_id, token=run.callback_token)
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

        if run.item_id is not None:
            await gtd_client.post_comment(
                run.item_id,
                dispatch_comment,
                created_by=attribution or "agent-gtd-dispatch",
                token=run.callback_token,
            )

        def _register_subprocess(proc: subprocess.Popen[bytes]) -> None:
            _active_subprocesses[run.id] = proc

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
        )
        _exit_code = result.returncode

        completed = datetime.now(UTC).isoformat()
        if result.returncode == 0:
            # BUILD mode: verify that the agent pushed its work before marking succeeded.
            # Plan and manage modes are exempt (_verify_repos is None for those).
            push_results_list: list[RepoPushStatus] | None = None
            _push_results_json: str | None = None
            if _verify_repos is not None:
                push_results_list = dispatch.verify_pushes(
                    _verify_repos, run.branch_name or ""
                )
                unpushed = [
                    r for r in push_results_list if r.status == PushStatus.unpushed
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
                run, max_turns, engine_used, timeout_seconds, attribution
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
    # Plan-mode and manage-mode always use Anthropic, regardless of requested engine.
    # The Ollama-routed Claude engines (claude-code-ollama local, claude-code-glm
    # cloud) and the talos-* family are all BUILD-only; plan/manage dispatches swap
    # to claude-code so the existing planner/manager code path runs untouched (small
    # local/cloud models are unreliable at multi-wave management; talos itself does
    # not implement plan/manage modes).
    effective_engine_name = body.engine
    _engine_swap_reason = ""
    if body.mode != DispatchMode.BUILD and body.engine == "claude-code-ollama":
        effective_engine_name = "claude-code"
        _engine_swap_reason = "plan/manage mode does not support ollama"
    elif body.mode != DispatchMode.BUILD and body.engine == "claude-code-glm":
        effective_engine_name = "claude-code"
        _engine_swap_reason = "plan/manage mode does not support ollama-cloud glm"
    elif body.mode != DispatchMode.BUILD and is_talos_engine(body.engine):
        effective_engine_name = "claude-code"
        _engine_swap_reason = "plan/manage mode does not support talos"
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

    # Talos-only pre-clone rejections. Runs post-swap: after the plan/manage
    # engine swap above, effective_engine_name is only in TALOS_ENGINES for
    # BUILD-mode dispatches — so these two guards do NOT fire against a
    # non-BUILD dispatch that started life as a talos-* request. Both must
    # happen BEFORE db.insert_run so a rejected dispatch never leaves a run
    # row behind.
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
