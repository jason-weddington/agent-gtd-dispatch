"""Dispatch worker API — runs headless coding agents."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    import concurrent.futures
    from collections.abc import AsyncGenerator, Awaitable, Callable
from agent_gtd_dispatch_protocol.branches import make_branch_name

from . import (
    completion,
    config,
    db,
    dispatch,
    gates,
    gtd_client,
    retention,
    run_tmp,
    talos,
)
from .agent_discovery import ENGINE_NAME, SERVICE_VERSION, run_list_agents_script
from .completion import agent_output_summary
from .dispatch import (
    LandGitError,
    LandGitTimeout,
    kill_run_session,
    run_session_has_members,
)
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
from .run_tmp import with_tmp_env

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

_retention_task: asyncio.Task[None] | None = None  # handle for clean shutdown

# rollout_id -> the MANAGE Run whose worker-driven wave loop is live on THIS
# host.  Ownership only: it is the one-loop-per-rollout guard.  Every loop is
# registered here by `_start_rollout_loop` (the single, await-free choke
# point) BEFORE its task is created, so neither the startup re-adoption sweep
# (a background task) nor a second MANAGE `/dispatch` (a lead's resume can
# relaunch a loop) can start a second loop for a rollout already driven here.
# A loop deregisters synchronously the moment it decides to exit (see
# `_drive_rollout`), so a relaunch arriving after that is not refused by a
# loop that is already leaving.  It is NOT relaunch bookkeeping — the resident-manager keep-alive machinery
# that used to share this map (relaunch ladder, staleness watchdog, free-
# relaunch tallies, retry caps) was retired once the worker owned the loop.
_rollout_loops: dict[str, Run] = {}

# Strong references to fire-and-forget bookkeeping tasks (e.g. cancelling a
# refused loop row from a synchronous caller) so they are not GC'd mid-flight.
_background_tasks: set[asyncio.Task[None]] = set()


async def _terminate_run_session(proc: subprocess.Popen[bytes]) -> bool:
    """SIGTERM a run's whole session, wait the grace period, SIGKILL if needed.

    Signals the whole SESSION, not just the sudo wrapper's pid — sudo cannot
    relay signals to the agent user's child, so terminate/kill on proc alone
    would orphan the agent and every command it spawned (kb-03965).  The
    wrapper was launched with ``start_new_session=True``, so ``proc.pid`` is
    the session id.

    Returns:
        True when the SIGKILL escalation was needed.
    """
    kill_run_session(proc, signal.SIGTERM)
    await asyncio.sleep(config.CANCEL_GRACE_SECONDS)
    if proc.poll() is None or run_session_has_members(proc):
        kill_run_session(proc, signal.SIGKILL)
        return True
    return False


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


# The single budget for operator-facing run error strings, shared with the
# git/hook excerpts in dispatch.py so the two truncation layers cannot drift.
ERROR_TEXT_MAX_CHARS: int = dispatch.ERROR_TEXT_MAX_CHARS


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
    global _retention_task
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
    # Nothing is in flight yet, so every rt- dir under RUN_TMP_ROOT is an
    # orphan of a previous process (crash, SIGKILL past the stop timeout).
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(run_tmp.sweep_orphans, frozenset(), 0, context="startup"),
        )
    except Exception:
        logger.exception("run-tmp startup sweep failed — continuing")
    _retention_task = asyncio.create_task(_retention_loop())
    # Re-adoption sweep: the wave loop lives inside THIS service, so a routine
    # deploy restart would otherwise silently stop every running rollout from
    # advancing. Backgrounded so startup never blocks on the GTD API.
    _readoption_task = asyncio.create_task(_readopt_running_rollouts())
    yield
    # Cancel retention, re-adoption and active dispatch tasks on shutdown
    if _retention_task is not None:
        _retention_task.cancel()
    if _readoption_task is not None:
        _readoption_task.cancel()
    # Cancel every in-flight run, then WAIT for their teardown to finish.
    #
    # The wait is load-bearing and its absence was a real hole. Each run's
    # `finally` block captures evidence and then rescues abandoned work —
    # committing and pushing whatever the agent left to the run's own branch — and
    # both of those are `await`s that shell out to git. Cancelling and returning
    # immediately ends the lifespan, the event loop tears down, and those awaits
    # never get to run. So on every service restart the rescue silently did not
    # fire, which is precisely the case it exists for: a deploy is the most common
    # reason a run dies part-way through real work.
    #
    # Bounded, because systemd will SIGKILL us at its stop timeout regardless and
    # a rescue that cannot finish in time must not take the shutdown down with it.
    # `return_exceptions=True` so one run's failed teardown cannot strand the rest.
    _in_flight = []
    for task in list(_active_processes.values()):
        task.cancel()
        # Gather only real futures belonging to THIS loop. In production every
        # value here is an asyncio.Task on the running loop, so this keeps all of
        # them. It guards two things that are not worth failing a shutdown over:
        # a test double, which is not a future at all, and a task left in this
        # module-level dict by a previous event loop, which gather() rejects with
        # "future belongs to a different loop".
        if asyncio.isfuture(task):
            try:
                if task.get_loop() is asyncio.get_running_loop():
                    _in_flight.append(task)
            except RuntimeError:  # no running loop — nothing to wait on
                pass
    if _in_flight:
        logger.info(
            "shutdown: cancelled %d in-flight run(s), waiting up to %ss for"
            " evidence capture and work rescue to finish",
            len(_in_flight),
            config.SHUTDOWN_GRACE_SECONDS,
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*_in_flight, return_exceptions=True),
                timeout=config.SHUTDOWN_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "shutdown: teardown did not finish within %ss — some work may be"
                " unrescued in its workspace",
                config.SHUTDOWN_GRACE_SECONDS,
            )


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
    the rollout loop's decisions and the unasserted-run WARNING were all invisible
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


async def _retention_loop() -> None:
    """Background coroutine: age-prune workspaces and retained run evidence.

    One bad tick must never kill the loop.
    """
    while True:
        await asyncio.sleep(config.RETENTION_INTERVAL_SECONDS)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, retention.prune, frozenset(_active_processes)
            )
            # Snapshot on the event loop; any rt- dir not owned by an
            # in-flight run and older than the min age is a missed teardown.
            live = run_tmp.live_paths()
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    run_tmp.sweep_orphans,
                    live,
                    config.RUN_TMP_ORPHAN_MIN_AGE_SECONDS,
                    context="periodic",
                ),
            )
        except Exception:
            logger.exception("Retention sweep failed — continuing")


def _try_start_pending() -> None:
    """Start as many queued dispatches as there are free slots.

    Called synchronously from _dispatch_worker's finally block after a run
    completes, freeing a slot. No await between slot-count check and task
    creation — stays atomic within the event-loop tick.
    """
    while _pending_queue and len(_active_processes) < config.MAX_CONCURRENT_RUNS:
        pending = _pending_queue.pop(0)
        if pending.run.mode == DispatchMode.MANAGE and pending.run.rollout_id:
            if not _start_rollout_loop(pending.run, attribution=pending.attribution):
                _cancel_refused_loop_run(pending.run)
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
    *,
    tmp_dir: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git commit`` with bounded re-stage retries.

    ``tmp_dir`` is the run's private TMPDIR: the hooked ``git commit`` runs
    the repo's pre-commit hooks, which may write temp files, so both commit
    invocations carry it via ``with_tmp_env``.  ``git status``/``git add``
    stay unwrapped.

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
        dispatch._sudo_wrap(
            with_tmp_env(["git", *git_ident_flags, "commit", "-m", commit_msg], tmp_dir)
        ),
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
            dispatch._sudo_wrap(
                with_tmp_env(
                    ["git", *git_ident_flags, "commit", "-m", commit_msg], tmp_dir
                )
            ),
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
    tmp_dir: Path | None = None,
) -> None:
    """Talos execution branch: subprocess launch, commit/push, comment-back, status set.

    ``tmp_dir`` is the run's private TMPDIR, forwarded to the talos argv and
    to the hooked commits in :func:`_commit_with_retry`.

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

    argv = talos.build_talos_argv(
        workspace,
        item_id,
        attempt=1,
        timeout_seconds=timeout_seconds,
        tmp_dir=tmp_dir,
    )

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
            original_branch = branch_name
            talos_fallbacks: dict[str, str] = {}

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
                    str(repo_path), git_ident_flags, commit_msg, tmp_dir=tmp_dir
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

                _push = dispatch.push_with_fallback(
                    str(repo_path), original_branch, no_verify=True
                )
                push_rc = _push.proc
                _fb = _fallback_branch_of(_push)
                if _fb is not None:
                    talos_fallbacks[repo_dir] = _fb
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

            if talos_fallbacks:
                await _record_pushed_branch(run, talos_fallbacks)
                branch_name = run.branch_name or branch_name

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

        commit_rc = _commit_with_retry(
            str(workspace), git_ident_flags, commit_msg, tmp_dir=tmp_dir
        )
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

        _push = dispatch.push_with_fallback(str(workspace), branch_name, no_verify=True)
        push_rc = _push.proc
        _fb = _fallback_branch_of(_push)
        if _fb is not None:
            await _record_pushed_branch(run, {"repo": _fb})
            branch_name = _fb
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
        "zero_commits_work_abandoned",
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
        "The agent ended its run without producing a single commit, and left the"
        " working tree CLEAN — it wrote nothing at all. The project gate was NOT"
        " run: an unchanged tree passes trivially, which would say the base commit"
        " is green and nothing about this run. Read the agent's comments on the"
        " item before re-dispatching. This is what a deliberate refusal looks like"
        " — an agent that checked its acceptance criteria, found the spec no longer"
        " matched the repo, and correctly declined to guess — and it is also what a"
        " confused run looks like. Only the agent's own account distinguishes them,"
        " which is why this terminal does not try to."
    ),
    "zero_commits_work_abandoned": (
        "The agent ended its run without committing, but left changes in the"
        " working tree — it was part-way through real work. Those changes have been"
        " committed and pushed to the run's own branch by the rescue path, so they"
        " are reviewable rather than lost. This is NOT a refusal: it usually means"
        " the agent ended its turn while something it started was still running,"
        " which kills the process under `claude --print`."
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
        if r.branch and r.branch != branch_name:
            line += f" on `{r.branch}`"
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


def _fallback_branch_of(attempt: object) -> str | None:
    """Return the ``-r<N>`` branch a :class:`dispatch.PushAttempt` fell back to, else None.

    Duck-typed on purpose: the worker tests replace the whole ``dispatch``
    module with a mock, so an ``isinstance`` against ``dispatch.PushAttempt``
    would raise there rather than answer.
    """
    branch = getattr(attempt, "branch", None)
    if getattr(attempt, "fell_back", False) is True and isinstance(branch, str):
        return branch
    return None


async def _record_pushed_branch(run: Run, branch_by_repo: dict[str, str]) -> None:
    """Make the branch a fallback push actually reached the run's recorded branch.

    Everything downstream reads the run's ``branch_name``: the completion and
    gate comments, ``RunResponse.branch_name`` (which the board copies into its
    run row's ``feature_branch`` at terminal) and from there the
    ``branch_name`` the rollout reviewer is handed.  Recording the dispatched
    name after a ``-r<N>`` fallback would point all of them at the older run's
    commits.

    A workspace run falls back per repo, independently, so its repos can end on
    different branches; the run records the first fallback branch and the
    per-repo branches stay visible in ``push_results``.
    """
    if not branch_by_repo:
        return
    chosen = next(iter(branch_by_repo.values()))
    distinct = sorted(set(branch_by_repo.values()))
    if len(distinct) > 1:
        logger.warning(
            "push fallback: run %s repos fell back to different branches %s;"
            " recording %s",
            run.id,
            distinct,
            chosen,
        )
    if run.branch_name == chosen:
        return
    logger.warning(
        "push fallback: run %s recorded branch %s -> %s (%s)",
        run.id,
        run.branch_name,
        chosen,
        ", ".join(f"{k}={v}" for k, v in branch_by_repo.items()),
    )
    run.branch_name = chosen
    try:
        await db.update_run(run.id, branch_name=chosen)
    except Exception:
        logger.exception("could not record fallback branch for run %s", run.id)


def _retained_path(
    run: Run,
    db_run: Run | None,
    repos: list[tuple[str, Path, str | None]],
) -> str:
    """Return where a retained workspace actually is, never ``None``.

    The db row is authoritative (the worker writes ``workspace_path`` as soon
    as the clone exists); the in-memory Run is the next best; failing both,
    the clone directory the repos live in — the repo itself for a monorepo
    run, their common parent for a workspace run.
    """
    for candidate in (db_run.workspace_path if db_run else None, run.workspace_path):
        if candidate:
            return candidate
    paths = [p for _n, p, _b in repos]
    if len(paths) == 1:
        return str(paths[0])
    parents = {str(p.parent) for p in paths}
    if len(parents) == 1:
        return parents.pop()
    return ", ".join(str(p) for p in paths)


async def _reap_leaked_processes(run: Run, workspace: Path) -> None:
    """Kill processes left running from the run's workspace; log and record them.

    Best-effort and never raises (runs in the teardown ``finally``). Recorded by
    appending to the run's ``error`` text, which does not change its status.
    """
    try:
        loop = asyncio.get_running_loop()
        reaped = list(
            await loop.run_in_executor(
                dispatch._executor, dispatch.reap_workspace_processes, workspace
            )
        )
        if not reaped:
            return
        for r in reaped:
            logger.warning(
                "reaped leaked process: run_id=%s pid=%s cmdline=%r (%s)",
                run.id,
                r.pid,
                r.cmdline,
                r.killed_with,
            )
        detail = "; ".join(
            f"pid {r.pid} `{r.cmdline}` ({r.killed_with})" for r in reaped
        )
        _existing = await db.get_run(run.id)
        _prefix = (_existing.error + " | ") if _existing and _existing.error else ""
        await db.update_run(
            run.id,
            error=(
                f"{_prefix}reaped {len(reaped)} leaked process(es) from workspace:"
                f" {detail}"
            )[:ERROR_TEXT_MAX_CHARS],
        )
    except Exception:
        logger.exception("reaping leaked processes failed for run %s", run.id)


async def _rescue_before_teardown(
    run: Run,
    repos: list[tuple[str, Path, str | None]],
    *,
    attribution: str | None,
    branch_by_repo: dict[str, str] | None = None,
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
    _requested: dict[str, str] = {}
    for _name, _path, _base in repos:
        _repo_branch = (branch_by_repo or {}).get(_name) or branch
        if not _repo_branch.startswith("feat/"):
            continue
        _requested[_name] = _repo_branch
        try:
            results.append(
                await loop.run_in_executor(
                    dispatch._executor,
                    dispatch.rescue_abandoned_work,
                    _name,
                    _path,
                    _repo_branch,
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
    # The rescue falls back to ``<branch>-r<N>`` on a non-fast-forward exactly
    # like the success path; record the branch the work actually reached.
    _fallbacks = {
        r.repo_name: r.branch
        for r in _pushed
        if isinstance(r.branch, str)
        and r.branch
        and r.branch != _requested.get(r.repo_name, branch)
    }
    if _fallbacks:
        await _record_pushed_branch(run, _fallbacks)
    _pushed_to = sorted(
        {
            (r.branch if isinstance(r.branch, str) and r.branch else None)
            or _requested.get(r.repo_name, branch)
            for r in _pushed
        }
    )
    _branch_text = (
        ", ".join(f"`{b}`" for b in _pushed_to) if _pushed_to else f"`{branch}`"
    )
    logger.warning(
        "rescue: run_id=%s branch=%s pushed=%s failed=%s",
        run.id,
        _branch_text,
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
                    " workspace, which has been RETAINED at"
                    f" {_retained_path(run, _existing, repos)}"
                    f" — {_detail}"
                )[:ERROR_TEXT_MAX_CHARS],
            )
        except Exception:
            logger.exception("could not record rescue failure for run %s", run.id)

    if run.item_id is not None:
        if _pushed:
            _body = (
                f"Unreviewed partial work from run `{run.id}` was pushed to"
                f" {_branch_text} by the dispatch worker at teardown"
                f" ({', '.join(r.repo_name for r in _pushed)})."
                " The agent left it behind; it is incomplete, hooks were skipped"
                " and it has passed no quality gate. It was pushed only so it"
                " would not be deleted with the workspace — read it before"
                " reusing any of it."
            )
        else:
            _body = (
                f"Run `{run.id}` left unpushed work behind and the dispatch"
                f" worker could NOT rescue it to {_branch_text}. The workspace has"
                " been retained so the work still exists on the dispatch host"
                f" at {_retained_path(run, None, repos)}."
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
# The resident manager's keep-alive machinery (the exit-path relaunch ladder,
# ``_do_manage_recovery``, the staleness watchdog, its in-memory tallies and
# retry caps) was retired once this loop was proven (item 7baf77d0): the loop
# owns liveness, the reviewer is bounded by its own timeout, and a startup
# re-adoption sweep resumes this host's own loops after a restart.  Two
# invariants that machinery encoded still hold here, by construction:
#
# * NEVER HALT WHILE A CHILD BUILD IS IN FLIGHT FOR LACK OF A DRIVER.  The
#   loop's only liveness halt ("rollout stalled") fires only when nothing is in
#   flight; with builds in flight a tick returns ``_TICK_WAIT`` and the loop
#   keeps polling.  A crashed loop fails its own run and does not halt.
# * NEVER WAIT ON A RUN BY BACKGROUNDING IT.  The loop awaits each poll
#   interval and each reviewer in the foreground of its own task; every agent
#   prompt carries the shared turn-discipline block.

# Re-exported for tests (and so the cap has one name, not a literal).
MAX_ITEM_REDISPATCHES: int = config.MAX_ITEM_REDISPATCHES

# rollout_id -> item_id -> number of `re-dispatch` verdicts already honoured.
# In-memory: a dispatch-service restart resets
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
_TICK_PARKED = "parked"  # the loop's host is draining — sleep, then re-read

# Run ids of rollout loops currently SLEEPING after a parked tick.  Membership
# means exactly that: a loop is discarded at the top of every iteration, before
# its tick runs, and added only right before the sleep that follows a parked
# tick.  So any tick that reviews, lands or dispatches runs while the loop
# counts as active.  Parked loops stay in ``_active_processes`` (the host's own
# admission still counts them); only the reported ``active_runs`` leaves them
# out so a draining host can read idle.
_parked_rollout_loops: set[str] = set()

# Substring both the board's pinned-host refusal and HostDrainingError carry
# for a single drained host.  "All dispatch hosts are draining" does not match.
_HOST_DRAINING_MARKER = "' is draining: "


def _counted_active_runs() -> int:
    """Active runs as reported on /health and /info: parked rollout loops excluded."""
    return len(_active_processes) - len(
        _parked_rollout_loops & _active_processes.keys()
    )


def _parked_step(drain: str) -> str:
    """The manager step published while a rollout loop is parked."""
    return (
        f"Parked: this dispatch host is draining ({drain}); "
        "the rollout resumes when the host returns to rotation"
    )


# The worker's land step (see `_land_reviewed_merge`).
# Total `git push` REJECTIONS tolerated per land before it halts; on a
# monorepo this is exactly the number of push invocations.  Fix relaunches do
# not count against it.
MAX_LAND_PUSH_ATTEMPTS: int = 3
# Reviewer FIX MODE relaunches allowed per land after a red land gate.
MAX_LAND_FIX_RELAUNCHES: int = 1
# Heartbeat cadence (``manager_state_updated_at``) while a land waits on
# locks or a gate.  Nothing in the service reads the heartbeat for liveness
# any more; it keeps the rollout monitor's "last update" honest.
LAND_HEARTBEAT_SECONDS: int = 300

# (normalized origin URL, default branch) -> lock.  Serializes lands to one
# remote branch on THIS host, so two items cannot both pass the gate against
# the same old main.  Cross-host races fall back to push rejection + regate.
_land_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Guard against a dispatch that reports success but does not move the item out
# of `next_ready` (the wave linkage is guarded by `AND status = 'ready'`, so a
# `pending` item would be dispatched forever).  Two attempts, then halt loudly.
_MAX_DISPATCH_ATTEMPTS_PER_ITEM = 2

# Recording a reviewer's merge/skip on the board is retried in-tick before it
# is treated as a failure: the merge has already landed by then, so a transient
# board error must not halt the rollout.  4xx responses are not retried.
_BOOKKEEPING_RETRY_DELAYS_SECONDS: tuple[float, ...] = (2.0, 5.0)
_BOOKKEEPING_ATTEMPTS = len(_BOOKKEEPING_RETRY_DELAYS_SECONDS) + 1

# A merge confirmed on origin whose board record keeps failing is retried once
# per poll interval; after this many passes the rollout halts with a recovery
# instruction.
_MAX_UNRECORDED_MERGE_PASSES = 10


class _UnrecordedMerge(NamedTuple):
    """A merge confirmed on origin that the board has not yet recorded."""

    passes: int
    merge_note: str
    next_attempt_at: float


# rollout_id -> item_id -> merged-but-unrecorded entry.  In-memory like
# `_rollout_redispatches`: on a dispatch-service restart the item is simply
# re-reviewed once (accepted).  The only writer that ADDS entries is the
# confirmed-merge branch of `_act_on_verdict`.
_rollout_unrecorded_merges: dict[str, dict[str, _UnrecordedMerge]] = {}


def _monotonic() -> float:
    """Clock seam for the unrecorded-merge retry throttle (patched in tests)."""
    return time.monotonic()


class ReviewVerdict(NamedTuple):
    """A reviewer's bounded answer about ONE completed build."""

    verdict: str  # one of dispatch.REVIEW_VERDICTS
    rationale: str
    merge_note: str
    detail: str  # structured failure detail, or the rejection reason
    commit_type: str = ""  # audit only; "" when missing/invalid
    commit_type_reason: str = ""
    item_commit_type: str = ""  # groomed type on the item; "" when unset/invalid
    branch_name: str = ""  # the feature branch reviewed; the land step deletes it
    # The reviewer's per-repo claim (diagnostic only; the land reads git).
    repo_states: tuple[tuple[str, str], ...] = ()
    # Paths whose squash-merge conflict the reviewer reports resolving (only
    # documentation is allowed; the land step's conflict guard re-checks).
    resolved_conflict_paths: tuple[str, ...] = ()


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
    in-flight builds), ``reviewing`` (published by the reviewer itself, which
    is the agent actually in that phase), ``merging`` (now published by the
    worker's land step — :func:`_land_reviewed_merge` — which owns the fetch,
    rebase, gate and push, and republishes it as a heartbeat while it waits on
    locks or a gate), ``reconciling_ac`` (the reviewer's), ``parked``
    (published by the loop on every tick while its host drains), and
    ``halted``.
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


def _gate_result_summary(
    run_row: dict[str, Any], *, gate_command: str = "", build_engine: str = ""
) -> str:
    """Render the post-run gate outcome for the reviewer's envelope.

    The GTD-side run row has no gate column; the gate's verdict reaches it as
    the ``post-run gate ...`` prefix on ``error_msg`` (and, when it passed, as
    silence).  Say which of those we are looking at rather than handing the
    reviewer a bare string.
    """
    error_msg = str(run_row.get("error_msg") or "").strip()
    if (
        not error_msg
        and gate_command.strip()
        # The board's run status for a finished build is "success"
        # (agent_gtd RunStatus.SUCCESS), not the dispatch-side "succeeded".
        and str(run_row.get("status") or "") == "success"
        and not is_talos_engine(build_engine)
    ):
        # A succeeded non-talos build of a project with a gate_command only
        # reaches ``succeeded`` after the worker's post-run gate exited 0
        # (a failing or timed-out gate fails the run with a ``post-run gate``
        # error_msg), so silence here is a pass, not a skip.
        return dispatch.GATE_PASSED_WORDING + (
            f" (the worker ran `{gate_command.strip()}` and it exited 0)."
        )
    if not error_msg:
        return (
            "no failure recorded on the run — the post-run gate did not fail "
            "(it may have passed or been skipped for a project with no "
            "`gate_command`). Run the merge bar yourself regardless."
        )
    return f"the run recorded a failure: `{error_msg[:1000]}`"


async def _review_branch_name(
    item_id: str, item: dict[str, Any], run_row: dict[str, Any]
) -> str:
    """Return the branch the reviewer must review for the build in *run_row*.

    The build's own dispatch record wins when this host has it: a build whose
    push fell back to ``<branch>-r<N>`` (the item branch already held an older
    run's commits) records that name as its ``branch_name``, and the board's
    ``feature_branch`` only catches up at terminal sync.  Then the board's
    ``feature_branch``, then the deterministic item branch name.
    """
    remote_run_id = str(run_row.get("remote_run_id") or "")
    if remote_run_id:
        try:
            build_run = await db.get_run(remote_run_id)
        except Exception:
            logger.warning(
                "rollout-loop: could not read build run %s for item %s",
                remote_run_id,
                item_id,
            )
            build_run = None
        if build_run is not None and build_run.branch_name:
            return build_run.branch_name
    branch_name = str(run_row.get("feature_branch") or "")
    if branch_name:
        return branch_name
    return make_branch_name(item_id, str(item.get("title") or ""))


async def _launch_reviewer(
    rollout_id: str,
    item_id: str,
    *,
    project: dict[str, Any],
    workspace: dispatch.RolloutWorkspace,
    reviewer_engine: Engine,
    token: str | None,
    attribution: str | None,
    gate_failure_output: str | None = None,
    item: dict[str, Any] | None = None,
    executor: concurrent.futures.ThreadPoolExecutor | None = None,
    slot: int = 0,
) -> ReviewVerdict:
    """Run ONE short-lived reviewer for ONE completed build and read its verdict.

    *executor* is the thread pool the reviewer subprocess wait runs on (None:
    the shared pool).  Loop-started reviews pass ``dispatch._review_executor``;
    the land step's FIX MODE relaunch passes nothing.  *slot* is the review
    slot whose workspace this is, for the logs only.

    The reviewer's process is registered in ``_active_subprocesses`` under its
    run id, so the cancel endpoint can stop it.  Cancelling the awaiting task
    (the rollout loop's review teardown) terminates the reviewer's session and
    records the run ``cancelled`` before re-raising.

    With *gate_failure_output* set, the reviewer is relaunched in FIX MODE by
    the land step: the worker's land gate failed on the rebased squash commit,
    and the reviewer may amend that commit (see ``dispatch.build_review_prompt``).

    The reviewer run carries ``mode=DispatchMode.REVIEW``.  A distinct mode
    buys the env withholding (``_MANAGE_EXECUTOR_ENV_KEYS`` is granted only for
    MANAGE) and the reviewer's own turn budget.  (It was originally chosen to
    keep a reviewer's exit away from the manage relaunch ladder; that ladder
    has since been retired.)

    Returns a :class:`ReviewVerdict`.  Every failure path — launch error,
    non-zero exit, missing or malformed verdict artifact — returns
    ``verdict="halt"``: a reviewer that did not answer does not get the benefit
    of the doubt, because merging on a guess is the one outcome that cannot be
    undone.

    *item* is the board item when the caller already holds it.  The wave tick
    fetches it once, before the first reviewer runs, and the land step's FIX
    MODE relaunch reuses it: a board call that times out AFTER a reviewer has
    run would make the loop re-run the tick from the top and launch a second
    reviewer for the same build.
    """
    if item is None:
        item = await gtd_client.get_item(item_id, token=token)
    raw_item_type = item.get("commit_type")
    item_commit_type = ""
    if raw_item_type in dispatch.ITEM_COMMIT_TYPES:
        item_commit_type = str(raw_item_type)
    elif raw_item_type:
        logger.warning(
            "rollout-loop: commit-type-unknown rollout_id=%s item_id=%s value=%r",
            rollout_id,
            item_id,
            raw_item_type,
        )
    run_row = await _latest_run_for_item(item_id, rollout_id, token=token)
    branch_name = await _review_branch_name(item_id, item, run_row)

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
        gate_result=_gate_result_summary(
            run_row,
            gate_command=str(project.get("gate_command") or ""),
            build_engine=str(
                run_row.get("engine_actual")
                or run_row.get("engine")
                or item.get("build_engine")
                or ""
            ),
        ),
        merge_notes=merge_notes,
        redispatches_used=used,
        redispatch_cap=MAX_ITEM_REDISPATCHES,
        gate_failure_output=gate_failure_output,
    )
    pinned_subject = dispatch.compose_squash_subject(item)
    logger.info(
        "rollout-loop: review-prompt rollout_id=%s item_id=%s "
        "commit_type_mode=%s subject=%r",
        rollout_id,
        item_id,
        "pinned" if pinned_subject is not None else "derived",
        pinned_subject,
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
        "branch=%s max_turns=%d slot=%d",
        rollout_id,
        item_id,
        reviewer_run.id,
        reviewer_engine.name,
        branch_name,
        config.REVIEW_MAX_TURNS,
        slot,
    )

    # Clear any prior verdict so a reviewer that dies before writing one can
    # never be credited with the PREVIOUS item's answer.
    dispatch.clear_review_verdict(workspace.root)

    exit_code: int | None = None
    failure: str = ""
    # Set when this review is cancelled.  run_agent's Popen happens on an
    # executor thread, so a cancel can land after the thread picked up the
    # job but before it registered the process: the cancel handler then finds
    # nothing to stop.  The callback registers first and checks the flag
    # second, the handler sets the flag first and looks second, so one of
    # them always sees the process.
    cancel_requested = threading.Event()

    def _on_popen(proc: subprocess.Popen[bytes]) -> None:
        _active_subprocesses[reviewer_run.id] = proc
        if cancel_requested.is_set():
            _active_subprocesses.pop(reviewer_run.id, None)
            logger.info(
                "rollout-loop: review-cancel-late-popen rollout_id=%s item_id=%s "
                "run_id=%s action=sigkill",
                rollout_id,
                item_id,
                reviewer_run.id,
            )
            kill_run_session(proc, signal.SIGKILL)

    reviewer_tmp: Path | None = None
    try:
        # First: a create failure lands in `except Exception` below and takes
        # the existing no-verdict path.
        reviewer_tmp = run_tmp.create_run_tmp(reviewer_run.id)
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
            disallowed_tools=list(dispatch.REVIEWER_DISALLOWED_TOOLS),
            popen_callback=_on_popen,
            executor=executor,
            tmp_dir=reviewer_tmp,
        )
        exit_code = result.returncode
    except asyncio.CancelledError:
        # Not caught by `except Exception`: the rollout loop tore this review
        # down (a halt, or the loop exiting).  Stop the agent, record the run.
        cancel_requested.set()
        proc = _active_subprocesses.get(reviewer_run.id)
        escalated = False
        if proc is not None:
            escalated = await _terminate_run_session(proc)
        await db.update_run(
            reviewer_run.id,
            status=RunStatus.cancelled,
            completed_at=datetime.now(UTC).isoformat(),
            error="reviewer cancelled by rollout loop",
        )
        logger.info(
            "rollout-loop: review-cancel rollout_id=%s item_id=%s run_id=%s "
            "escalated_sigkill=%d",
            rollout_id,
            item_id,
            reviewer_run.id,
            1 if escalated else 0,
        )
        raise
    except Exception as exc:
        failure = f"reviewer subprocess failed: {type(exc).__name__}: {exc}"
        logger.exception(
            "rollout-loop: reviewer run %s raised for item %s",
            reviewer_run.id,
            item_id,
        )
    finally:
        _active_subprocesses.pop(reviewer_run.id, None)
        if reviewer_tmp is not None:
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    run_tmp.remove_run_tmp, reviewer_tmp, kind="reviewer"
                ),
            )

    verdict_blob, reason = dispatch.read_review_verdict(workspace.root)

    if verdict_blob is None:
        detail = failure or f"verdict artifact {reason} (reviewer exit={exit_code})"
        # Only when run_agent returned: it truncates transcript.txt on launch,
        # so before that the file still holds the PREVIOUS reviewer's output.
        if exit_code is not None:
            agent_said = agent_output_summary(Path(workspace.root) / "transcript.txt")
            if agent_said:
                detail = f"{detail}; agent said: {agent_said}"
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
        return ReviewVerdict(
            "halt",
            "reviewer returned no usable verdict",
            "",
            detail,
            branch_name=branch_name,
        )

    verdict = str(verdict_blob["verdict"])
    rationale = str(verdict_blob.get("rationale") or "")[
        : dispatch.REVIEW_RATIONALE_MAX_CHARS
    ]
    merge_note = str(verdict_blob.get("merge_note") or "")
    raw_type = verdict_blob.get("commit_type")
    commit_type = ""
    if isinstance(raw_type, str) and raw_type in dispatch.COMMIT_TYPES:
        commit_type = raw_type
    elif verdict == "merge":
        # Audit field only: never changes the verdict.
        logger.warning(
            "rollout-loop: reviewer commit_type missing or invalid rollout_id=%s "
            "item_id=%s value=%r",
            rollout_id,
            item_id,
            raw_type,
        )
    commit_type_reason = (
        str(verdict_blob.get("commit_type_reason") or "")[
            : dispatch.REVIEW_RATIONALE_MAX_CHARS
        ]
        if commit_type
        else ""
    )
    fail = verdict_blob.get("failure")
    detail = ""
    if isinstance(fail, dict) and (fail.get("kind") or fail.get("detail")):
        detail = f"{fail.get('kind') or 'unspecified'}: {fail.get('detail') or ''}"
    repo_states = verdict_blob.get("repo_states")
    if isinstance(repo_states, dict) and repo_states:
        states = ", ".join(f"{k}={v}" for k, v in sorted(repo_states.items()))
        detail = f"{detail} [repo states: {states}]" if detail else f"[{states}]"

    raw_resolved = verdict_blob.get("resolved_conflict_paths")
    resolved: tuple[str, ...] = ()
    if isinstance(raw_resolved, list) and all(isinstance(p, str) for p in raw_resolved):
        resolved = tuple(raw_resolved)
    elif raw_resolved is not None and verdict == "merge":
        # A merge whose conflict claim cannot be read is not landed on a guess.
        verdict = "halt"
        rationale = "reviewer returned a malformed resolved_conflict_paths"
        detail = f"malformed resolved_conflict_paths: {raw_resolved!r}"[:200]
    if resolved:
        detail = f"{detail} [resolved doc conflicts: {', '.join(resolved)}]"
    completion: dict[str, Any] = {
        "verdict": verdict,
        "rationale": rationale,
        "detail": detail,
    }
    if resolved:
        completion["resolved_conflict_paths"] = list(resolved)

    await db.update_run(
        reviewer_run.id,
        status=RunStatus.succeeded,
        completed_at=datetime.now(UTC).isoformat(),
        completion=json.dumps(completion),
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
    return ReviewVerdict(
        verdict,
        rationale,
        merge_note,
        detail,
        commit_type,
        commit_type_reason,
        item_commit_type,
        branch_name,
        tuple(
            sorted((str(k), str(v)) for k, v in repo_states.items())
            if isinstance(repo_states, dict)
            else ()
        ),
        resolved,
    )


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


_LANDED_TYPE_RE = re.compile(r"^([a-z]+)(\([^)]*\))?!?:")


async def _verify_landed_commit_type(
    rollout_id: str,
    item_id: str,
    outcome: ReviewVerdict,
    *,
    workspace: dispatch.RolloutWorkspace | None,
    token: str | None,
    attribution: str | None,
) -> None:
    """Compare the landed squash commit's type with the item's groomed type.

    Runs after a ``merge`` verdict has been recorded. Purely observational: a
    mismatch or a missing commit posts ONE loud item comment and logs, but the
    recorded outcome is never changed and the rollout is never halted. Emits
    exactly one ``commit-type-check`` log line per call and never raises.
    """
    expected = outcome.item_commit_type
    landed: dict[str, str] = {}
    result = "error"
    try:
        if not expected:
            result = "skipped_unset"
        elif workspace is None:
            result = "skipped_no_workspace"
        else:
            landed, failed_repos = await asyncio.get_running_loop().run_in_executor(
                None,
                dispatch.find_landed_commit_subjects,
                workspace,
                item_id,
            )
            mismatches: list[tuple[str, str]] = []
            for repo, subject in landed.items():
                match = _LANDED_TYPE_RE.match(subject)
                landed_type = match.group(1) if match else "unparsed"
                if landed_type != expected:
                    mismatches.append((repo, subject))
            if mismatches:
                result = "mismatch"
                listing = "; ".join(f"`{repo}`: `{subj}`" for repo, subj in mismatches)
                await gtd_client.post_comment(
                    item_id,
                    (
                        f"Rollout `{rollout_id}`: commit type mismatch — item is "
                        f"groomed `{expected}` but landed squash commit(s) are "
                        f"{listing}. Reviewer reported "
                        f"`{outcome.commit_type or 'nothing'}`. Outcome unchanged."
                    ),
                    created_by=attribution or "agent-gtd-dispatch",
                    token=token,
                )
            elif not landed and failed_repos:
                result = "error"
            elif not landed:
                result = "not_found"
                await gtd_client.post_comment(
                    item_id,
                    (
                        f"Rollout `{rollout_id}`: could not find a landed commit "
                        f"with trailer `Item: {item_id}` to verify commit type "
                        f"`{expected}`. Outcome unchanged."
                    ),
                    created_by=attribution or "agent-gtd-dispatch",
                    token=token,
                )
            else:
                result = "match"
    except Exception:
        logger.exception(
            "rollout-loop: commit-type check raised rollout_id=%s item_id=%s",
            rollout_id,
            item_id,
        )
        result = "error"

    reviewer_agrees = outcome.commit_type == expected
    level = (
        logging.INFO
        if result == "skipped_unset" or (result == "match" and reviewer_agrees)
        else logging.WARNING
    )
    logger.log(
        level,
        "rollout-loop: commit-type-check rollout_id=%s item_id=%s outcome=%s "
        "expected=%s landed=%s reviewer_reported=%s reviewer_agrees=%s",
        rollout_id,
        item_id,
        result,
        expected,
        landed,
        outcome.commit_type,
        reviewer_agrees,
    )


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status code of an ``httpx.HTTPStatusError``, else None."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


async def _complete_in_rollout_with_retry(
    rollout_id: str,
    item_id: str,
    *,
    outcome: str,
    merge_note: str,
    token: str | None,
) -> tuple[dict[str, Any] | None, Exception | None]:
    """Record a reviewer outcome on the board, retrying transient failures.

    ``complete_in_rollout`` is idempotent by item id (a repeat heals any side
    effect a partial earlier write left undone), so retrying is safe.  A 4xx
    response is a definitive rejection and is returned at once; every other
    exception is retried after each delay in
    ``_BOOKKEEPING_RETRY_DELAYS_SECONDS``.

    Returns:
        ``(result, None)`` on the first success, ``(None, last_exc)`` after
        the final failed attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _BOOKKEEPING_ATTEMPTS + 1):
        try:
            result = await gtd_client.complete_in_rollout(
                rollout_id,
                item_id,
                outcome=outcome,
                merge_actor="reviewer-autonomous",
                decision_rule="agent-judgment",
                merge_note=merge_note,
                token=token,
            )
        except Exception as exc:
            last_exc = exc
            status = _http_status(exc)
            retryable = not (status is not None and 400 <= status <= 499)
            logger.warning(
                "rollout-loop: complete-item-attempt-failed rollout_id=%s "
                "item_id=%s attempt=%d/%d exc=%s status=%s retryable=%s",
                rollout_id,
                item_id,
                attempt,
                _BOOKKEEPING_ATTEMPTS,
                type(exc).__name__,
                status if status is not None else "-",
                "true" if retryable else "false",
            )
            if not retryable or attempt >= _BOOKKEEPING_ATTEMPTS:
                return None, exc
            await asyncio.sleep(_BOOKKEEPING_RETRY_DELAYS_SECONDS[attempt - 1])
            continue
        if attempt > 1:
            logger.warning(
                "rollout-loop: complete-item-recovered rollout_id=%s item_id=%s "
                "attempt=%d/%d already_recorded=%s",
                rollout_id,
                item_id,
                attempt,
                _BOOKKEEPING_ATTEMPTS,
                result.get("already_recorded") if isinstance(result, dict) else None,
            )
        return result, None
    return None, last_exc  # pragma: no cover — loop always returns


async def _confirm_merge_landed(
    workspace: dispatch.RolloutWorkspace | None,
    item_id: str,
    *,
    errors: list[Exception] | None = None,
    rollout_id: str | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Check whether the item's squash commit landed on origin's default branch.

    Args:
        workspace: A rollout slot workspace, or None if never prepared.
        item_id: The item whose ``Item: <id>`` trailer is looked up.
        rollout_id: When set, the commit must also carry the
            ``Rollout: <id>`` trailer (the pre-review already-landed check).
        errors: Optional out-list; on ``lookup-raised`` the exception is
            appended so the caller can name it in its log line.

    Returns:
        ``(confirmation, subjects_by_repo, failed_repos)`` where confirmation
        is one of ``confirmed``, ``no-workspace``, ``lookup-raised`` (subjects
        and failed repos empty) or ``no-trailer-found``.  Never raises.
    """
    if workspace is None:
        return "no-workspace", {}, []
    try:
        subjects, failed = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                dispatch.find_landed_commit_subjects,
                workspace,
                item_id,
                **({"rollout_id": rollout_id} if rollout_id else {}),
            ),
        )
    except Exception as exc:
        if errors is not None:
            errors.append(exc)
        return "lookup-raised", {}, []
    if subjects:
        return "confirmed", dict(subjects), list(failed)
    return "no-trailer-found", {}, list(failed)


def _unblocked_text(result: Any) -> str:
    """The 'Downstream items unblocked' sentence for a completion result."""
    newly_ready = result.get("newly_ready") if isinstance(result, dict) else None
    return (
        f"Downstream items unblocked: {', '.join(str(x) for x in newly_ready)}."
        if newly_ready
        else "No downstream items unblocked."
    )


def _merge_commit_line(outcome: ReviewVerdict) -> str:
    """The ' Commit type: ...' sentence for a merge verdict comment."""
    return (
        f" Commit type: `{outcome.commit_type}` ({outcome.commit_type_reason})."
        if outcome.commit_type
        else " Commit type: not reported by reviewer."
    )


async def _handle_unrecorded_merge(
    rollout_id: str,
    item_id: str,
    outcome: ReviewVerdict,
    exc: Exception,
    *,
    token: str | None,
    attribution: str | None,
    workspace: dispatch.RolloutWorkspace | None,
) -> str:
    """A merge verdict whose board record failed: confirm the merge, then decide.

    A merge confirmed on origin is a success whose record is late — the
    rollout continues and the record is retried each poll interval.  An
    unconfirmed merge halts the rollout as before.
    """
    lookup_errors: list[Exception] = []
    confirmation, subjects, failed_repos = await _confirm_merge_landed(
        workspace, item_id, errors=lookup_errors
    )
    log_args: list[Any] = [
        rollout_id,
        item_id,
        confirmation,
        ",".join(sorted(subjects)) or "-",
        subjects,
        ",".join(failed_repos) or "-",
    ]
    fmt = (
        "rollout-loop: merge-confirmation rollout_id=%s item_id=%s outcome=%s "
        "repos=%s subjects=%r failed_repos=%s"
    )
    if confirmation == "lookup-raised" and lookup_errors:
        fmt += " exc=%s"
        log_args.append(type(lookup_errors[0]).__name__)
    logger.warning(fmt, *log_args)

    if confirmation != "confirmed":
        reason = (
            f"failed to record item {item_id} as completed: "
            f"{type(exc).__name__}: {exc} (merge not confirmed on origin; "
            f"confirmation: {confirmation}"
            + (f"; failed_repos={','.join(failed_repos)}" if failed_repos else "")
            + ")"
        )
        return await _halt_rollout_from_loop(
            rollout_id,
            reason[:ERROR_TEXT_MAX_CHARS],
            item_id=item_id,
            token=token,
            attribution=attribution,
        )

    _rollout_unrecorded_merges.setdefault(rollout_id, {})[item_id] = _UnrecordedMerge(
        1, outcome.merge_note, _monotonic() + config.ROLLOUT_LOOP_POLL_SECONDS
    )
    commit_line = _merge_commit_line(outcome)
    try:
        await gtd_client.post_comment(
            item_id,
            (
                f"Rollout `{rollout_id}`: WARNING — reviewer merged this item "
                f"(landed in: {', '.join(sorted(subjects))}) but recording it on "
                f"the board failed after {_BOOKKEEPING_ATTEMPTS} attempts: "
                f"{type(exc).__name__}: {exc}. Dependents stay blocked until it "
                "is recorded; the rollout continues and retries the record each "
                f"poll interval. Reviewer: {outcome.rationale}{commit_line}"
            ),
            created_by=attribution or "agent-gtd-dispatch",
            token=token,
        )
    except Exception:
        logger.warning(
            "rollout-loop: failed to comment unrecorded merge on %s", item_id
        )
    logger.warning(
        "rollout-loop: bookkeeping-failed-after-merge rollout_id=%s item_id=%s "
        "repos=%s exc=%s",
        rollout_id,
        item_id,
        ",".join(sorted(subjects)),
        type(exc).__name__,
    )
    await _publish_rollout_phase(
        rollout_id,
        "polling",
        item_id=item_id,
        step=f"WARNING: item {item_id} merged but not recorded on the board",
        token=token,
    )
    await _verify_landed_commit_type(
        rollout_id,
        item_id,
        outcome,
        workspace=workspace,
        token=token,
        attribution=attribution,
    )
    return _TICK_CONTINUE


async def _retry_unrecorded_merges(
    rollout_id: str,
    pending: dict[str, _UnrecordedMerge],
    *,
    token: str | None,
    attribution: str | None,
) -> str | None:
    """Retry board records for merged-but-unrecorded items that are due.

    Returns a ``_TICK_*`` directive when the tick should end here (a record
    succeeded, or the pass cap was reached and the rollout halted), else None.
    """
    for item_id in list(pending):
        entry = pending[item_id]
        if _monotonic() < entry.next_attempt_at:
            continue
        result, exc = await _complete_in_rollout_with_retry(
            rollout_id,
            item_id,
            outcome="completed",
            merge_note=entry.merge_note,
            token=token,
        )
        if exc is None:
            del pending[item_id]
            try:
                await gtd_client.post_comment(
                    item_id,
                    (
                        f"Rollout `{rollout_id}`: board record for the merged item "
                        f"succeeded on retry pass {entry.passes + 1}. "
                        f"{_unblocked_text(result)}"
                    ),
                    created_by=attribution or "agent-gtd-dispatch",
                    token=token,
                )
            except Exception:
                logger.warning(
                    "rollout-loop: failed to comment recorded merge on %s", item_id
                )
            logger.info(
                "rollout-loop: unrecorded-merge-retry rollout_id=%s item_id=%s "
                "pass=%d/%d outcome=recorded",
                rollout_id,
                item_id,
                entry.passes + 1,
                _MAX_UNRECORDED_MERGE_PASSES,
            )
            return _TICK_CONTINUE
        n = entry.passes + 1
        logger.warning(
            "rollout-loop: unrecorded-merge-retry rollout_id=%s item_id=%s "
            "pass=%d/%d outcome=failed exc=%s",
            rollout_id,
            item_id,
            n,
            _MAX_UNRECORDED_MERGE_PASSES,
            type(exc).__name__,
        )
        if n >= _MAX_UNRECORDED_MERGE_PASSES:
            return await _halt_rollout_from_loop(
                rollout_id,
                f"item {item_id} merged on origin but recording it on the board "
                f"failed after {n} passes: {type(exc).__name__}: {exc} — record "
                "it with complete_item_in_rollout (outcome=completed) and resume",
                item_id=item_id,
                token=token,
                attribution=attribution,
            )
        pending[item_id] = _UnrecordedMerge(
            n, entry.merge_note, _monotonic() + config.ROLLOUT_LOOP_POLL_SECONDS
        )
    return None


def _with_suffix(reason: str, suffix: str) -> str:
    """*reason* plus *suffix*, clipped to the error budget keeping the suffix."""
    if not suffix:
        return reason[:ERROR_TEXT_MAX_CHARS]
    head = reason[: max(0, ERROR_TEXT_MAX_CHARS - len(suffix))]
    return (head + suffix)[:ERROR_TEXT_MAX_CHARS]


async def _act_on_verdict(
    rollout_id: str,
    item_id: str,
    outcome: ReviewVerdict,
    *,
    token: str | None,
    attribution: str | None,
    workspace: dispatch.RolloutWorkspace | None = None,
    halt_suffix: str = "",
) -> str:
    """Apply a reviewer's verdict. The WORKER acts; the reviewer only decided.

    Including the re-dispatch: a reviewer is never handed the ability to
    dispatch a child run, so ``re-dispatch`` is a request that this function
    executes, capped at ``MAX_ITEM_REDISPATCHES`` per item.  Past the cap the
    rollout halts — which is also the DEFAULT behaviour for a failed child, and
    deliberately unchanged: absent an explicit ``re-dispatch`` verdict, a failed
    child halts the rollout exactly as it does today.

    Bookkeeping classification for ``merge`` / ``skip``:

    - **Must succeed:** ``gtd_client.complete_in_rollout`` — it moves the board
      graph and unblocks dependents.  It is retried
      (``_complete_in_rollout_with_retry``).  If it still fails after a
      ``merge``, the landed commit is looked up on origin: a confirmed merge
      does NOT halt the rollout (the work has landed; only its record is late)
      — it is tracked in ``_rollout_unrecorded_merges`` and retried each poll
      interval by ``_rollout_wave_tick``.  An unconfirmed merge, or a failed
      ``skip``, halts as before.
    - **Best effort, never fatal:** the verdict ``gtd_client.post_comment``
      and ``_verify_landed_commit_type``.
    - For a ``merge``, the push has ALREADY happened by the time this runs:
      :func:`_land_reviewed_merge` fetched, rebased, gated and pushed the
      reviewer's local squash commit (and deleted the feature branch) first.

    *halt_suffix* is appended to the reason of any halt issued here (the
    rollout loop names the queued merge verdicts the halt discards).
    """
    if outcome.verdict == "halt":
        reason = f"reviewer halted item {item_id}: {outcome.rationale}"
        if outcome.detail:
            reason = f"{reason} ({outcome.detail})"
        return await _halt_rollout_from_loop(
            rollout_id,
            _with_suffix(reason, halt_suffix),
            item_id=item_id,
            token=token,
            attribution=attribution,
        )

    if outcome.verdict in ("merge", "skip"):
        recorded = "completed" if outcome.verdict == "merge" else "skipped"
        result, exc = await _complete_in_rollout_with_retry(
            rollout_id,
            item_id,
            outcome=recorded,
            merge_note=outcome.merge_note,
            token=token,
        )
        if exc is not None:
            if outcome.verdict == "skip":
                return await _halt_rollout_from_loop(
                    rollout_id,
                    _with_suffix(
                        f"failed to record item {item_id} as {recorded}: "
                        f"{type(exc).__name__}: {exc}",
                        halt_suffix,
                    ),
                    item_id=item_id,
                    token=token,
                    attribution=attribution,
                )
            return await _handle_unrecorded_merge(
                rollout_id,
                item_id,
                outcome,
                exc,
                token=token,
                attribution=attribution,
                workspace=workspace,
            )
        unblocked = _unblocked_text(result)
        commit_line = _merge_commit_line(outcome) if outcome.verdict == "merge" else ""
        try:
            await gtd_client.post_comment(
                item_id,
                (
                    f"Rollout `{rollout_id}`: reviewer verdict `{outcome.verdict}` — "
                    f"item recorded as {recorded}. {outcome.rationale}"
                    f"{commit_line} {unblocked}"
                ),
                created_by=attribution or "agent-gtd-dispatch",
                token=token,
            )
        except Exception:
            logger.warning("rollout-loop: failed to comment verdict on %s", item_id)
        if outcome.verdict == "merge":
            await _verify_landed_commit_type(
                rollout_id,
                item_id,
                outcome,
                workspace=workspace,
                token=token,
                attribution=attribution,
            )
        if isinstance(result, dict) and result.get("graph_complete") is True:
            logger.info(
                "rollout-loop: rollout_id=%s item_id=%s verdict=%s "
                "graph_complete=True via=completion — loop exits",
                rollout_id,
                item_id,
                outcome.verdict,
            )
            return _TICK_DONE
        return _TICK_CONTINUE

    # re-dispatch
    used = _rollout_redispatches.setdefault(rollout_id, {}).get(item_id, 0)
    if used >= MAX_ITEM_REDISPATCHES:
        return await _halt_rollout_from_loop(
            rollout_id,
            _with_suffix(
                f"re-dispatch cap reached for item {item_id} "
                f"({used}/{MAX_ITEM_REDISPATCHES}): {outcome.rationale}",
                halt_suffix,
            ),
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
            _with_suffix(
                f"re-dispatch of item {item_id} failed: {type(exc).__name__}: {exc}",
                halt_suffix,
            ),
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


class _LandHalt(Exception):  # noqa: N818 — a control-flow signal, not an error
    """Raised inside the land step to stop it and halt the rollout.

    ``head`` is the halt reason; ``tail`` (gate output) is appended last and is
    the part trimmed to fit ``ERROR_TEXT_MAX_CHARS``, keeping its END.
    """

    def __init__(self, outcome: str, head: str, tail: str = "") -> None:
        """Record the land outcome code and the halt reason parts."""
        super().__init__(head)
        self.outcome = outcome
        self.head = head
        self.tail = tail


class _Lander:
    """State for ONE worker-owned land of a reviewer-approved squash commit.

    See :func:`_land_reviewed_merge` for the contract.  Split into a class only
    because the land threads a dozen pieces of per-attempt state (pushed repos,
    gated shas, the gate's last result, the fix budget) through every log line
    and halt reason.
    """

    def __init__(
        self,
        rollout_id: str,
        item_id: str,
        outcome: ReviewVerdict,
        *,
        project: dict[str, Any],
        workspace: dispatch.RolloutWorkspace,
        pre_review_origin: dict[str, str],
        reviewer_engine: Engine,
        loop_run_id: str,
        token: str | None,
        attribution: str | None,
        item: dict[str, Any] | None = None,
    ) -> None:
        """Capture the land's inputs; nothing runs until :meth:`run`."""
        self.item = item
        self.rollout_id = rollout_id
        self.item_id = item_id
        self.outcome = outcome
        self.project = project
        self.workspace = workspace
        self.pre_review_origin = dict(pre_review_origin or {})
        self.reviewer_engine = reviewer_engine
        self.loop_run_id = loop_run_id
        self.token = token
        self.attribution = attribution
        self.repos: list[str] = list(workspace.repo_paths)
        self.branches: dict[str, str] = {
            r: workspace.default_branches.get(r) or "main" for r in self.repos
        }
        raw_gate = project.get("gate_command")
        self.gate_command = raw_gate.strip() if isinstance(raw_gate, str) else ""
        self.landing: list[str] = []
        self.pushed: dict[str, str] = {}
        self.push_round = 0
        self.fix_relaunches = 0
        self.fix_files = 0
        self.fix_rationale = ""
        self.base_after: dict[str, str] = {}
        self.origin_advanced: dict[str, int] = {}
        self.gated: dict[str, str] = {}
        self.gate_state = "not_run"
        self.gate_rc = "-"
        self.gate_secs = 0.0
        self.step = "Land: starting"
        # Conflict guard results per landing repo (see _conflict_guard).
        self.conflict_found: dict[str, list[str]] = {}
        self.conflict_reported: dict[str, list[str]] = {}
        self.conflict_unreported: dict[str, list[str]] = {}
        # Private TMPDIR for the land's gate install and gate run; created in
        # run() and removed in its outer finally.
        self.tmp_dir: Path | None = None

    # -- plumbing ---------------------------------------------------------

    async def _git(
        self, repo: str, fn: Callable[..., Any], *args: Any, **kw: Any
    ) -> Any:
        """Run a sync dispatch land helper in the executor, mapping git errors."""
        try:
            return await asyncio.get_running_loop().run_in_executor(
                dispatch._executor, functools.partial(fn, *args, **kw)
            )
        except LandGitTimeout as exc:
            raise _LandHalt(
                "git_timeout",
                f"land git timed out for item {self.item_id}: "
                f"git {exc.command} in {repo}",
            ) from exc
        except LandGitError as exc:
            raise _LandHalt(
                "bad_local_state",
                f"land bad local state for item {self.item_id}: repo {repo}: {exc}",
            ) from exc

    def _path(self, repo: str) -> Path:
        return self.workspace.repo_paths[repo]

    async def publish(self, step: str) -> None:
        """Publish ``merging`` with *step*; the heartbeat republishes it."""
        self.step = step
        await _publish_rollout_phase(
            self.rollout_id,
            "merging",
            item_id=self.item_id,
            step=step[:200],
            token=self.token,
        )

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        # wait_for on an Event rather than asyncio.sleep: the heartbeat must
        # yield to the loop even where a test has patched sleep away.
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=LAND_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                await _publish_rollout_phase(
                    self.rollout_id,
                    "merging",
                    item_id=self.item_id,
                    step=self.step[:200],
                    token=self.token,
                )

    def _log_land(self, repo: str, outcome: str, pushed_sha: str = "-") -> None:
        logger.log(
            logging.INFO if outcome == "pushed" else logging.WARNING,
            "rollout-loop: land rollout_id=%s item_id=%s push_attempt=%d/%d "
            "fix_relaunches=%d repo=%s base_before=%s base_after=%s "
            "origin_advanced=%d gated_sha=%s pushed_sha=%s gate=%s gate_rc=%s "
            "gate_secs=%.1f outcome=%s",
            self.rollout_id,
            self.item_id,
            self.push_round,
            MAX_LAND_PUSH_ATTEMPTS,
            self.fix_relaunches,
            repo,
            self.pre_review_origin.get(repo) or "-",
            self.base_after.get(repo) or "-",
            self.origin_advanced.get(repo, 0),
            self.gated.get(repo) or "-",
            pushed_sha,
            self.gate_state,
            self.gate_rc,
            self.gate_secs,
            outcome,
        )

    # -- the steps --------------------------------------------------------

    async def _fetch(self, repo: str) -> None:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        branch = self.branches[repo]
        await self._git(repo, dispatch.land_fetch, self._path(repo), branch, timeout)
        base_after = await self._git(
            repo, dispatch.land_rev_parse, self._path(repo), f"origin/{branch}", timeout
        )
        self.base_after[repo] = base_after
        self.origin_advanced[repo] = await self._git(
            repo,
            dispatch.land_count_between,
            self._path(repo),
            self.pre_review_origin.get(repo, ""),
            base_after,
            timeout,
        )

    async def _lock_keys(self) -> list[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for repo in self.repos:
            url = await self._git(
                repo,
                dispatch.land_remote_url,
                self._path(repo),
                config.LAND_GIT_TIMEOUT_SECONDS,
            )
            keys.add((url, self.branches[repo]))
        return sorted(keys)

    async def _reviewer_pushed_guard(self) -> None:
        hits: list[str] = []
        for repo in self.repos:
            base = self.pre_review_origin.get(repo) or ""
            if not base:
                logger.warning(
                    "rollout-loop: land reviewer-pushed guard skipped rollout_id=%s "
                    "item_id=%s repo=%s reason=no-pre-review-origin",
                    self.rollout_id,
                    self.item_id,
                    repo,
                )
                continue
            shas = await self._git(
                repo,
                dispatch.land_reviewer_pushed,
                self._path(repo),
                base,
                self.branches[repo],
                self.rollout_id,
                self.item_id,
                config.LAND_GIT_TIMEOUT_SECONDS,
            )
            if shas:
                hits.append(repo)
        if hits:
            raise _LandHalt(
                "reviewer_pushed",
                f"commit for item {self.item_id} appeared on origin during review, "
                f"before the worker's land: {', '.join(hits)}",
            )

    async def _check_landing_repo(self, repo: str) -> None:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        path = self._path(repo)
        branch = self.branches[repo]
        prefix = f"land bad local state for item {self.item_id}: repo {repo}"
        current = await self._git(repo, dispatch.land_current_branch, path, timeout)
        if current != branch:
            raise _LandHalt(
                "bad_local_state",
                f"{prefix}: HEAD is on {current!r}, not the default branch {branch!r}",
            )
        ahead = await self._git(repo, dispatch.land_ahead_count, path, branch, timeout)
        if ahead != 1:
            raise _LandHalt(
                "bad_local_state",
                f"{prefix}: {ahead} commit(s) ahead of origin/{branch}, "
                "expected exactly 1 squash commit",
            )
        if not await self._git(
            repo, dispatch.land_has_item_trailer, path, branch, self.item_id, timeout
        ):
            raise _LandHalt(
                "bad_local_state",
                f"{prefix}: the squash commit carries no 'Item: {self.item_id}' "
                "trailer",
            )

    def _log_conflict_check(
        self,
        repo: str,
        *,
        base: str,
        base_sha: str,
        tip_ref: str,
        tip_sha: str,
        paths: list[str],
        reported: list[str],
        decision: str,
    ) -> None:
        logger.info(
            "rollout-loop: land-conflict-check rollout_id=%s item_id=%s repo=%s "
            "base=%s base_sha=%s tip_ref=%s tip_sha=%s conflict_paths=%s "
            "reported_resolved=%s decision=%s",
            self.rollout_id,
            self.item_id,
            repo,
            base,
            base_sha or "-",
            tip_ref,
            tip_sha or "-",
            ",".join(paths) or "-",
            ",".join(reported) or "-",
            decision,
        )

    async def _conflict_guard(self) -> None:
        """Fail closed on the reviewer's squash-merge conflict resolution.

        The reviewer may resolve a squash conflict only when every conflicting
        path is documentation.  The worker does not trust that claim: it
        recomputes the conflicts with ``git merge-tree`` against the squash
        commit's parent (what the reviewer merged onto) and the feature
        branch, and halts on a non-documentation conflict or a leftover
        conflict marker.  Runs before anything is pushed.
        """
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        resolved = list(self.outcome.resolved_conflict_paths)
        bad = [p for p in resolved if not dispatch.is_documentation_path(p)]
        if bad:
            raise _LandHalt(
                "non_doc_conflict",
                f"land refused for item {self.item_id}: the reviewer reported "
                f"resolving non-documentation conflict paths: {', '.join(bad)}",
            )
        branch_name = self.outcome.branch_name
        if not branch_name:
            raise _LandHalt(
                "conflict_check_failed",
                f"land conflict check failed for item {self.item_id}: the verdict "
                "names no feature branch",
            )
        for repo in self.landing:
            path = self._path(repo)
            await self._git(
                repo, dispatch.land_fetch_feature_branch, path, branch_name, timeout
            )
            tip_ref = f"refs/remotes/origin/{branch_name}"
            tip = await self._git(
                repo, dispatch.land_resolve_ref, path, tip_ref, timeout
            )
            if not tip:
                tip_ref = f"refs/heads/{branch_name}"
                tip = await self._git(
                    repo, dispatch.land_resolve_ref, path, tip_ref, timeout
                )
            if not tip:
                raise _LandHalt(
                    "conflict_check_failed",
                    f"land conflict check failed for item {self.item_id}: repo "
                    f"{repo}: feature branch {branch_name} not found locally or "
                    "on origin",
                )
            base = f"{self.branches[repo]}~1"
            base_sha = await self._git(
                repo, dispatch.land_resolve_ref, path, base, timeout
            )
            paths = await self._git(
                repo, dispatch.land_conflict_paths, path, base, tip, timeout
            )
            if self.workspace.workspace_mode:
                reported = [
                    p.removeprefix(f"{repo}/")
                    for p in resolved
                    if p.startswith(f"{repo}/")
                ]
            else:
                reported = list(resolved)

            log = functools.partial(
                self._log_conflict_check,
                repo,
                base=base,
                base_sha=base_sha,
                tip_ref=tip_ref,
                tip_sha=tip,
                paths=paths,
                reported=reported,
            )
            non_doc = [p for p in paths if not dispatch.is_documentation_path(p)]
            if non_doc:
                log(decision="non_doc_halt")
                raise _LandHalt(
                    "non_doc_conflict",
                    f"land refused for item {self.item_id}: repo {repo}: the "
                    "squash merge conflicts on non-documentation paths: "
                    f"{', '.join(non_doc)}",
                )
            if paths:
                markers = await self._git(
                    repo,
                    dispatch.land_conflict_markers,
                    path,
                    self.branches[repo],
                    paths,
                    timeout,
                )
                if markers:
                    log(decision="markers_halt")
                    raise _LandHalt(
                        "conflict_markers",
                        f"land refused for item {self.item_id}: repo {repo}: "
                        f"conflict markers remain in: {', '.join(markers)}",
                    )
            unreported = sorted(set(paths) - set(reported))
            if unreported:
                logger.warning(
                    "rollout-loop: land-conflict-unreported rollout_id=%s "
                    "item_id=%s repo=%s unreported=%s",
                    self.rollout_id,
                    self.item_id,
                    repo,
                    ",".join(unreported),
                )
            log(decision="doc_only" if paths else "clean")
            self.conflict_found[repo] = list(paths)
            self.conflict_reported[repo] = reported
            self.conflict_unreported[repo] = unreported

    async def _landing_set(self, *, report_disagreement: bool) -> list[str]:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        claims = dict(self.outcome.repo_states)
        landing: list[str] = []
        for repo in self.repos:
            ahead = await self._git(
                repo,
                dispatch.land_ahead_count,
                self._path(repo),
                self.branches[repo],
                timeout,
            )
            if ahead >= 1:
                landing.append(repo)
            claim = claims.get(repo, "-")
            if report_disagreement and (claim == "committed") != (ahead >= 1):
                logger.warning(
                    "rollout-loop: land-state-disagreement rollout_id=%s item_id=%s "
                    "repo=%s reviewer_claim=%s git_ahead=%d",
                    self.rollout_id,
                    self.item_id,
                    repo,
                    claim,
                    ahead,
                )
        if not landing:
            raise _LandHalt(
                "no_squash_commit",
                f"merge verdict but no local squash commit for item {self.item_id} "
                f"in any repo ({', '.join(self.repos)})",
            )
        for repo in landing:
            await self._check_landing_repo(repo)
        return landing

    async def _install_gate(self) -> None:
        root = self.workspace.root
        if self.workspace.workspace_mode:
            gate_repos = [(d, root / d) for d in self.repos]
        else:
            gate_repos = [
                (
                    dispatch.repo_name_from_origin(
                        str(self.project.get("git_origin", ""))
                    ),
                    root,
                )
            ]
        steps = gates.detect_gate_steps(gate_repos, run_id=self.loop_run_id)
        if not steps:
            return
        failure = await asyncio.get_running_loop().run_in_executor(
            dispatch._executor,
            functools.partial(
                gates.run_gate_steps,
                steps,
                config.GATE_INSTALL_TIMEOUT_SECONDS,
                run_id=self.loop_run_id,
                tmp_dir=self.tmp_dir,
            ),
        )
        if failure is not None:
            raise _LandHalt(
                "gate_install_failed",
                f"land gate install failed for item {self.item_id}: "
                f"{failure.repo_label}: {failure.step}: {failure.reason}",
            )

    async def _reset_non_landing(self) -> None:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        for repo in self.repos:
            if repo in self.landing:
                continue
            path = self._path(repo)
            branch = self.branches[repo]
            await self._fetch(repo)
            ahead = await self._git(
                repo, dispatch.land_ahead_count, path, branch, timeout
            )
            if ahead != 0:
                raise _LandHalt(
                    "bad_local_state",
                    f"land bad local state for item {self.item_id}: repo {repo}: "
                    f"not in the landing set but {ahead} commit(s) ahead of "
                    f"origin/{branch}",
                )
            await self._git(repo, dispatch.land_reset_to_origin, path, branch, timeout)

    async def _run_gate(self) -> dispatch.GateResult:
        await self.publish(f"Land: running the project gate for {self.item_id}")

        def _land_popen_cb(proc: subprocess.Popen[bytes]) -> None:
            _active_subprocesses[self.loop_run_id] = proc

        try:
            result: dispatch.GateResult = (
                await asyncio.get_running_loop().run_in_executor(
                    dispatch._executor,
                    functools.partial(
                        dispatch.run_gate_command,
                        self.workspace.root,
                        self.gate_command,
                        config.LAND_GATE_TIMEOUT_SECONDS,
                        self.reviewer_engine,
                        _land_popen_cb,
                        tmp_dir=self.tmp_dir,
                    ),
                )
            )
        finally:
            _active_subprocesses.pop(self.loop_run_id, None)
        self.gate_rc = str(result.returncode)
        self.gate_secs = result.duration_seconds
        if result.timed_out:
            self.gate_state = "timed_out"
        elif result.returncode is None:
            self.gate_state = "launch_error"
        elif result.returncode != 0:
            self.gate_state = "failed"
        else:
            self.gate_state = "passed"
        return result

    async def _fix_relaunch(self, result: dispatch.GateResult) -> None:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        self.fix_relaunches += 1
        before = dict(self.gated)
        for repo in self.landing:
            self._log_land(repo, "fix_relaunched")
        await self.publish(
            f"Land: gate failed for {self.item_id} — relaunching the reviewer to fix"
        )
        fix = await _launch_reviewer(
            self.rollout_id,
            self.item_id,
            project=self.project,
            workspace=self.workspace,
            reviewer_engine=self.reviewer_engine,
            token=self.token,
            attribution=self.attribution,
            gate_failure_output=result.output,
            item=self.item,
        )
        if fix.verdict != "merge":
            raise _LandHalt(
                "fix_verdict_not_merge",
                f"land gate failed for item {self.item_id} and the fix relaunch "
                f"returned {fix.verdict!r}: {fix.rationale}",
            )
        self.fix_rationale = fix.rationale
        for repo in self.repos:
            await self._fetch(repo)
        landing = await self._landing_set(report_disagreement=False)
        if landing != self.landing:
            raise _LandHalt(
                "bad_local_state",
                f"land bad local state for item {self.item_id}: the fix relaunch "
                f"changed the landing set from {self.landing} to {landing}",
            )
        files = ins = dels = 0
        paths: list[str] = []
        for repo in self.landing:
            after = await self._git(
                repo, dispatch.land_rev_parse, self._path(repo), "HEAD", timeout
            )
            f, i, d, p = await self._git(
                repo,
                dispatch.land_diff_stat,
                self._path(repo),
                before.get(repo, ""),
                after,
                timeout,
            )
            files, ins, dels = files + f, ins + i, dels + d
            paths += [f"{repo}/{x}" if self.workspace.workspace_mode else x for x in p]
        self.fix_files = files
        logger.info(
            "rollout-loop: land-fix rollout_id=%s item_id=%s files_changed=%d "
            "insertions=%d deletions=%d paths=%s",
            self.rollout_id,
            self.item_id,
            files,
            ins,
            dels,
            ",".join(paths) or "-",
        )

    def _gate_halt(self, result: dispatch.GateResult) -> _LandHalt:
        if result.timed_out:
            return _LandHalt(
                "gate_timed_out",
                f"land gate timed out for item {self.item_id} after "
                f"{result.duration_seconds:.0f}s",
            )
        if result.returncode is None:
            return _LandHalt(
                "gate_launch_error",
                f"land gate launch error for item {self.item_id}: {result.output}",
            )
        return _LandHalt(
            "gate_failed",
            f"land gate failed for item {self.item_id}: ",
            tail=result.output,
        )

    async def _push_round(self, remaining: list[str], *, gate_on: bool) -> bool:
        """Push every remaining repo in order. Returns True if one was rejected."""
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        self.push_round += 1
        for repo in remaining:
            path = self._path(repo)
            branch = self.branches[repo]
            await self.publish(
                f"Land: pushing {repo} (push attempt "
                f"{self.push_round}/{MAX_LAND_PUSH_ATTEMPTS})"
            )
            status, excerpt = await self._git(
                repo,
                dispatch.land_push,
                path,
                self.gated[repo],
                branch,
                no_verify=gate_on,
                timeout=config.LAND_GATE_TIMEOUT_SECONDS,
            )
            if status == "non_fast_forward":
                self._log_land(repo, "non_fast_forward")
                if self.push_round >= MAX_LAND_PUSH_ATTEMPTS:
                    raise _LandHalt(
                        "push_attempts_exhausted",
                        f"land push rejected {self.push_round} times for item "
                        f"{self.item_id} (repo {repo}): {excerpt[:500]}",
                    )
                return True
            if status != "pushed":
                raise _LandHalt(
                    "push_failed",
                    f"land push failed for item {self.item_id}: repo {repo}: {excerpt}",
                )
            remote = await self._git(
                repo, dispatch.land_ls_remote, path, branch, timeout
            )
            if remote != self.gated[repo]:
                logger.error(
                    "rollout-loop: land-audit rollout_id=%s item_id=%s repo=%s "
                    "gated_sha=%s remote_sha=%s outcome=mismatch",
                    self.rollout_id,
                    self.item_id,
                    repo,
                    self.gated[repo],
                    remote or "-",
                )
                raise _LandHalt(
                    "audit_mismatch",
                    f"land audit mismatch: remote {remote or '(none)'} != gated "
                    f"{self.gated[repo]} for item {self.item_id} in {repo}",
                )
            logger.info(
                "rollout-loop: land-audit rollout_id=%s item_id=%s repo=%s "
                "gated_sha=%s remote_sha=%s outcome=match",
                self.rollout_id,
                self.item_id,
                repo,
                self.gated[repo],
                remote,
            )
            self.pushed[repo] = self.gated[repo]
            self._log_land(repo, "pushed", pushed_sha=self.gated[repo])
        return False

    async def _land(self) -> ReviewVerdict:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        await self.publish(f"Land: fetching origin for {self.item_id}")
        for repo in self.repos:
            await self._fetch(repo)
        await self._reviewer_pushed_guard()
        self.landing = await self._landing_set(report_disagreement=True)
        await self._conflict_guard()

        gate_on = bool(self.gate_command)
        if gate_on:
            await self._install_gate()
        else:
            self.gate_state = "skipped_no_gate_command"
            logger.info(
                "rollout-loop: land-gate skipped_no_gate_command rollout_id=%s "
                "item_id=%s",
                self.rollout_id,
                self.item_id,
            )

        first = True
        while True:
            remaining = [r for r in self.landing if r not in self.pushed]
            await self.publish(f"Land: rebasing {', '.join(remaining)} onto origin")
            # Rebase EVERY remaining repo before anything is pushed, so a
            # conflict halts with nothing (more) pushed.
            for repo in remaining:
                if not first:
                    await self._fetch(repo)
                ok, paths, excerpt = await self._git(
                    repo,
                    dispatch.land_rebase,
                    self._path(repo),
                    self.branches[repo],
                    timeout,
                )
                if not ok:
                    detail = (
                        f"conflicting paths: {', '.join(paths)}"
                        if paths
                        else f"no conflicting paths reported; git: {excerpt[:500]}"
                    )
                    raise _LandHalt(
                        "rebase_conflict",
                        f"land rebase conflict for item {self.item_id} in repo "
                        f"{repo}: {detail}",
                    )
                await self._check_landing_repo(repo)
            first = False
            if self.workspace.workspace_mode:
                await self._reset_non_landing()
            for repo in remaining:
                self.gated[repo] = await self._git(
                    repo, dispatch.land_rev_parse, self._path(repo), "HEAD", timeout
                )
            if gate_on:
                result = await self._run_gate()
                if not result.passed:
                    if (
                        self.gate_state == "failed"
                        and not self.pushed
                        and self.fix_relaunches < MAX_LAND_FIX_RELAUNCHES
                    ):
                        await self._fix_relaunch(result)
                        continue
                    raise self._gate_halt(result)
                for repo in remaining:
                    head = await self._git(
                        repo, dispatch.land_rev_parse, self._path(repo), "HEAD", timeout
                    )
                    if head != self.gated[repo]:
                        logger.error(
                            "rollout-loop: land-invariant-violation rollout_id=%s "
                            "item_id=%s repo=%s gated_sha=%s head_after_gate=%s",
                            self.rollout_id,
                            self.item_id,
                            repo,
                            self.gated[repo],
                            head,
                        )
                        raise _LandHalt(
                            "bad_local_state",
                            f"land bad local state for item {self.item_id}: repo "
                            f"{repo}: HEAD moved during the gate "
                            f"({self.gated[repo]} -> {head})",
                        )
            if not await self._push_round(remaining, gate_on=gate_on):
                break
        return await self._finish(gate_on=gate_on)

    async def _finish(self, *, gate_on: bool) -> ReviewVerdict:
        timeout = config.LAND_GIT_TIMEOUT_SECONDS
        branch_name = self.outcome.branch_name
        if branch_name:
            for repo in self.landing:
                notes = await self._git(
                    repo,
                    dispatch.land_delete_feature_branch,
                    self._path(repo),
                    branch_name,
                    timeout,
                )
                for note in notes:
                    logger.warning(
                        "rollout-loop: land branch-cleanup-failed rollout_id=%s "
                        "item_id=%s repo=%s branch=%s detail=%s",
                        self.rollout_id,
                        self.item_id,
                        repo,
                        branch_name,
                        note,
                    )
        gate_text = (
            f"passed in {self.gate_secs:.1f}s"
            if gate_on
            else "SKIPPED — project has no gate_command"
        )
        fix_text = (
            f"used, amended {self.fix_files} file(s)" if self.fix_relaunches else "none"
        )
        lines = [
            f"Landed {self.pushed[repo][:12]} on {repo}/{self.branches[repo]} "
            f"(rebased over {self.origin_advanced.get(repo, 0)} new origin "
            f"commit(s); gate: {gate_text}; push attempts: {self.push_round}; "
            f"fix relaunch: {fix_text})"
            for repo in self.landing
        ]
        for repo in self.landing:
            found = self.conflict_found.get(repo) or []
            if not found:
                continue
            reported = self.conflict_reported.get(repo) or []
            line = (
                f"Conflict check ({repo}): worker found documentation conflicts in "
                f"{', '.join(found)}; reviewer reported resolving "
                f"{', '.join(reported) if reported else 'none'}"
            )
            unreported = self.conflict_unreported.get(repo) or []
            if unreported:
                line += f" (mismatch: unreported {', '.join(unreported)})"
            lines.append(line)
        if self.outcome.resolved_conflict_paths:
            lines.append(
                "Reviewer resolved documentation-only merge conflicts in: "
                + ", ".join(self.outcome.resolved_conflict_paths)
            )
        try:
            await gtd_client.post_comment(
                self.item_id,
                "\n".join(lines),
                created_by=self.attribution or "agent-gtd-dispatch",
                token=self.token,
            )
        except Exception:
            logger.warning(
                "rollout-loop: failed to post land comment on item %s", self.item_id
            )
        if self.fix_relaunches:
            return self.outcome._replace(
                rationale=f"{self.outcome.rationale} (land fix: {self.fix_rationale})"
            )
        return self.outcome

    async def _halt(self, halt: _LandHalt) -> str:
        for repo in self.landing or self.repos:
            if repo not in self.pushed:
                self._log_land(repo, halt.outcome)
        status = ""
        if self.pushed:
            status = (
                "("
                + ", ".join(
                    f"{r}: {'pushed' if r in self.pushed else 'not pushed'}"
                    for r in self.landing
                )
                + ")"
            )
        if halt.tail:
            head = f"{halt.head}{status + ' ' if status else ''}"
            budget = max(ERROR_TEXT_MAX_CHARS - len(head), 0)
            reason = head + (halt.tail[-budget:] if budget else "")
        else:
            reason = f"{halt.head} {status}".rstrip()
        return await _halt_rollout_from_loop(
            self.rollout_id,
            reason[:ERROR_TEXT_MAX_CHARS],
            item_id=self.item_id,
            token=self.token,
            attribution=self.attribution,
        )

    async def run(self) -> ReviewVerdict | str:
        """Acquire the locks, land, release. Returns the verdict or a halt."""
        await self.publish(f"Land: starting for {self.item_id}")
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(stop))
        held: list[asyncio.Lock] = []
        keys: list[tuple[str, str]] = []
        acquired_at = 0.0
        try:
            try:
                try:
                    self.tmp_dir = run_tmp.create_run_tmp(
                        f"land-{self.loop_run_id}-{self.item_id}"
                    )
                except Exception as exc:
                    raise _LandHalt(
                        "run_tmp_failed",
                        "land could not create private TMPDIR for item "
                        f"{self.item_id}: {exc}",
                    ) from exc
                keys = await self._lock_keys()
                wait_start = time.monotonic()
                for key in keys:
                    lock = _land_locks.setdefault(key, asyncio.Lock())
                    await lock.acquire()
                    held.append(lock)
                acquired_at = time.monotonic()
                logger.info(
                    "rollout-loop: land-lock rollout_id=%s item_id=%s keys=%s "
                    "waited_secs=%.1f",
                    self.rollout_id,
                    self.item_id,
                    keys,
                    acquired_at - wait_start,
                )
                await self.publish(f"Land: lock acquired for {self.item_id}")
                return await self._land()
            except _LandHalt as halt:
                return await self._halt(halt)
        finally:
            for lock in reversed(held):
                lock.release()
            if held:
                logger.info(
                    "rollout-loop: land-lock-release rollout_id=%s item_id=%s "
                    "keys=%s held_secs=%.1f",
                    self.rollout_id,
                    self.item_id,
                    keys,
                    time.monotonic() - acquired_at,
                )
            # A cancelled land may leave its gate running on an executor
            # thread; stop it before its TMPDIR is removed under it.
            leftover = _active_subprocesses.get(self.loop_run_id)
            if leftover is not None:
                await _terminate_run_session(leftover)
            if self.tmp_dir is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(
                        run_tmp.remove_run_tmp, self.tmp_dir, kind="land"
                    ),
                )
                self.tmp_dir = None
            stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


async def _land_reviewed_merge(
    rollout_id: str,
    item_id: str,
    outcome: ReviewVerdict,
    *,
    project: dict[str, Any],
    workspace: dispatch.RolloutWorkspace,
    pre_review_origin: dict[str, str],
    reviewer_engine: Engine,
    loop_run_id: str,
    token: str | None,
    attribution: str | None,
    item: dict[str, Any] | None = None,
) -> ReviewVerdict | str:
    """Land a reviewer-approved squash commit: fetch, rebase, gate, push.

    The reviewer approves a build by creating its squash commit LOCALLY and
    never pushes.  Items that were gate-green on their own clones can still
    break ``main`` together when one carries a contract another changes (a test
    asserting state the other item moves), which file-overlap planning cannot
    see.  So, immediately before the push, the worker rebases the squash onto
    freshly fetched ``origin/<default>``, runs the project's ``gate_command``
    on that exact tree, and pushes exactly the gated sha — only on green.

    Contract, in order:

    - Per-remote locks (``_land_locks``) for every repo in the workspace,
      acquired in sorted key order and held until the final push or halt, so
      two lands on this host never gate against the same old main.
    - A time-scoped reviewer-pushed guard: a commit for this item that reached
      origin after ``pre_review_origin`` halts with nothing pushed.
    - The landing set is read from git (repos exactly one trailer-bearing
      commit ahead of origin, HEAD on the default branch), not from the
      reviewer's ``repo_states``.
    - Rebase every not-yet-pushed landing repo, reset non-landing repos to
      origin (workspace mode), gate once from the workspace root, push in repo
      order with ``--no-verify`` (the land gate just judged that exact sha; a
      project with no ``gate_command`` pushes WITH hooks instead).
    - A red gate before any push gets ONE reviewer FIX MODE relaunch; a
      non-fast-forward push re-fetches, re-rebases and re-gates, bounded by
      ``MAX_LAND_PUSH_ATTEMPTS``; every other failure halts.
    - Each pushed sha is audited with ``ls-remote``; then the feature branch
      is deleted (best effort) and one comment is posted on the item.

    Returns:
        The original verdict (with any fix rationale appended) on success, or
        the ``_TICK_HALTED`` directive after halting the rollout.
    """
    return await _Lander(
        rollout_id,
        item_id,
        outcome,
        project=project,
        workspace=workspace,
        pre_review_origin=pre_review_origin,
        reviewer_engine=reviewer_engine,
        loop_run_id=loop_run_id,
        token=token,
        attribution=attribution,
        item=item,
    ).run()


_TRANSIENT_BOARD_STATUSES = frozenset({502, 503, 504})


def _is_transient_board_error(exc: BaseException) -> bool:
    """Whether *exc* is a board blip the wave loop should ride out.

    Exactly: any ``httpx.TransportError`` (ReadTimeout, ConnectTimeout,
    ConnectError, RemoteProtocolError, ...) or an ``httpx.HTTPStatusError``
    whose status is 502, 503 or 504.  Everything else is a real answer.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    return _http_status(exc) in _TRANSIENT_BOARD_STATUSES


class _BoardOutage:
    """Tracks one rollout loop's current streak of transient board errors.

    The streak is bounded by WALL-CLOCK time, not a count:
    ``config.ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS`` measured from the first error
    of the streak.  A call that completes without a transient error ends it.
    """

    def __init__(self, rollout_id: str) -> None:
        """Start with no outage in progress."""
        self.rollout_id = rollout_id
        self.count = 0
        self.started_at: float | None = None

    def reset(self) -> None:
        """A clean call: the streak is over."""
        if self.count:
            logger.info(
                "rollout-loop: board-outage-recovered rollout_id=%s failures=%d",
                self.rollout_id,
                self.count,
            )
        self.count = 0
        self.started_at = None

    def absorb(self, exc: BaseException) -> bool:
        """Record a transient error. False once the outage window is exceeded."""
        now = _monotonic()
        if self.started_at is None:
            self.started_at = now
        self.count += 1
        elapsed = now - self.started_at
        if elapsed > config.ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS:
            logger.error(
                "rollout-loop: board-outage-exceeded rollout_id=%s exc=%s "
                "consecutive=%d elapsed_secs=%.0f window_secs=%d",
                self.rollout_id,
                type(exc).__name__,
                self.count,
                elapsed,
                config.ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS,
            )
            return False
        logger.warning(
            "rollout-loop: board-unavailable rollout_id=%s exc=%s consecutive=%d "
            "elapsed_secs=%.0f — will retry in %ds",
            self.rollout_id,
            type(exc).__name__,
            self.count,
            elapsed,
            config.ROLLOUT_LOOP_POLL_SECONDS,
        )
        return True


async def _ride_out_board_outage[T](
    outage: _BoardOutage, call: Callable[[], Awaitable[T]]
) -> T:
    """Await ``call()``, retrying it while the board is transiently unavailable.

    A transient error (see :func:`_is_transient_board_error`) sleeps
    ``config.ROLLOUT_LOOP_POLL_SECONDS`` and calls again, until the outage
    window is exceeded — then the last error is re-raised.  Every other
    exception propagates on its first occurrence.
    """
    while True:
        try:
            result = await call()
        except Exception as exc:
            if not _is_transient_board_error(exc) or not outage.absorb(exc):
                raise
            await asyncio.sleep(config.ROLLOUT_LOOP_POLL_SECONDS)
            continue
        outage.reset()
        return result


async def _park_or_halt_on_dispatch_refusal(
    rollout_id: str,
    item_id: str,
    exc: BaseException,
    *,
    halt_reason: str,
    token: str | None,
    attribution: str | None,
) -> str:
    """Decide whether a NON-transient dispatch refusal parks the loop or halts.

    Re-reads the rollout: a drain on the loop's host parks (the refusal was the
    drain racing this tick), otherwise the rollout halts with ``halt_reason``.
    A transient error on the re-read re-raises so ``_ride_out_board_outage``
    re-runs the tick; that is safe because a refused dispatch did not move the
    item out of ``next_ready``.
    """
    status_code = _http_status(exc)
    try:
        reread = await gtd_client.get_rollout(rollout_id, token=token)
    except Exception as reread_exc:
        if _is_transient_board_error(reread_exc):
            raise
        logger.info(
            "rollout-loop: dispatch-failure-drain-check rollout_id=%s item_id=%s "
            "status_code=%s outcome=%s drain_reason=%r",
            rollout_id,
            item_id,
            status_code,
            "halt_reread_failed",
            None,
        )
        return await _halt_rollout_from_loop(
            rollout_id,
            halt_reason + f" (drain re-check failed: {type(reread_exc).__name__})",
            item_id=item_id,
            token=token,
            attribution=attribution,
        )
    drain = str(reread.get("loop_host_drain_reason") or "").strip()
    if drain:
        logger.info(
            "rollout-loop: dispatch-failure-drain-check rollout_id=%s item_id=%s "
            "status_code=%s outcome=%s drain_reason=%r",
            rollout_id,
            item_id,
            status_code,
            "parked",
            drain,
        )
        await _publish_rollout_phase(
            rollout_id, "parked", step=_parked_step(drain), token=token
        )
        return _TICK_PARKED
    logger.info(
        "rollout-loop: dispatch-failure-drain-check rollout_id=%s item_id=%s "
        "status_code=%s outcome=%s drain_reason=%r",
        rollout_id,
        item_id,
        status_code,
        "halt_no_drain",
        None,
    )
    return await _halt_rollout_from_loop(
        rollout_id,
        halt_reason,
        item_id=item_id,
        token=token,
        attribution=attribution,
    )


# Loop-started reviewer tasks in flight across EVERY rollout loop in this
# process, capped by config.ROLLOUT_MAX_CONCURRENT_REVIEWS.  Incremented right
# after a review task's create_task and decremented when that task finishes;
# no lock is needed because the event loop is single-threaded.  Never exceeds
# the size of dispatch._review_executor, so a loop-started reviewer never
# queues inside that pool.
_host_reviews_in_flight: int = 0


@dataclass
class _ReviewEntry:
    """One reviewer task in flight for one completed build."""

    item_id: str
    slot: int
    task: asyncio.Task[ReviewVerdict]
    item: dict[str, Any]
    pre_review_origin: dict[str, str]
    started_at: float


@dataclass
class _QueuedLand:
    """A ``merge`` verdict waiting for this rollout's single land step."""

    item_id: str
    slot: int
    verdict: ReviewVerdict
    item: dict[str, Any]
    pre_review_origin: dict[str, str]
    queued_at: float


@dataclass
class _ActiveLand:
    """The one land task (land, then act on the verdict) running for a rollout."""

    item_id: str
    slot: int
    task: asyncio.Task[ReviewVerdict | str]
    started_at: float


@dataclass
class _RolloutLoopState:
    """In-memory state one rollout loop carries across ticks. Never persisted.

    A restart starts a fresh, empty state: every item whose review or land was
    in flight is still ``dispatched`` with a terminal build, so it is simply
    re-reviewed (after the rollout-scoped already-landed precheck).
    """

    slots: dict[int, dispatch.RolloutWorkspace] = field(default_factory=dict)
    reviews: dict[str, _ReviewEntry] = field(default_factory=dict)
    land_queue: list[_QueuedLand] = field(default_factory=list)
    active_land: _ActiveLand | None = None
    pending_directive: str | None = None
    # Observability counters (logged as `concurrency-summary` at loop exit).
    reviews_started: int = 0
    peak_reviews_in_flight: int = 0
    dispatches_while_reviewing: int = 0
    lands: int = 0
    peak_land_queue: int = 0
    max_land_queued_secs: float = 0.0
    halt_cancelled_reviews: int = 0
    # Quiets the drain-finishing INFO line after the first tick that logs it.
    drain_finishing_logged: bool = False


def _held_slots(loop_state: _RolloutLoopState) -> list[int]:
    """Every slot index held by a review, a queued land or the active land."""
    held = [e.slot for e in loop_state.reviews.values()]
    held += [q.slot for q in loop_state.land_queue]
    if loop_state.active_land is not None:
        held.append(loop_state.active_land.slot)
    return held


def _free_slot(loop_state: _RolloutLoopState) -> int | None:
    """The lowest slot index no review or land holds, else None."""
    held = set(_held_slots(loop_state))
    for slot in range(config.ROLLOUT_MAX_CONCURRENT_REVIEWS):
        if slot not in held:
            return slot
    return None


def _tracked_item_ids(rollout_id: str, loop_state: _RolloutLoopState) -> list[str]:
    """Item ids the loop is tracking, with duplicates kept (for the audit).

    The active land's own item is not counted again when it is also an
    unrecorded merge: the land task registers that entry from inside
    ``_handle_unrecorded_merge`` and keeps running (comment, phase, commit-type
    check), so for that window both legitimately name the same item.
    """
    ids = list(loop_state.reviews)
    ids += [q.item_id for q in loop_state.land_queue]
    active_item: str | None = None
    if loop_state.active_land is not None:
        active_item = loop_state.active_land.item_id
        ids.append(active_item)
    ids += [
        i for i in _rollout_unrecorded_merges.get(rollout_id, {}) if i != active_item
    ]
    return ids


def _busy(loop_state: _RolloutLoopState) -> bool:
    """Whether any review, queued land or active land exists."""
    return bool(
        loop_state.reviews
        or loop_state.land_queue
        or loop_state.active_land is not None
    )


def _audit_loop_state(
    rollout_id: str,
    loop_state: _RolloutLoopState,
    *,
    dispatching: str | None = None,
    land_start: bool = False,
) -> str | None:
    """Check the loop's concurrency invariants. Returns the violation kind or None.

    Called at review start, at land start (``land_start=True``), before each
    ``next_ready`` dispatch (``dispatching=<item id>``) and at the end of each
    tick.  A breach is logged at ERROR; the caller halts the rollout with
    ``bad_local_state: <kind>`` instead of proceeding.
    """
    kind: str | None = None
    item_id = dispatching or "-"
    detail: Any = None
    tracked = _tracked_item_ids(rollout_id, loop_state)
    held = _held_slots(loop_state)
    if len(loop_state.reviews) > config.ROLLOUT_MAX_CONCURRENT_REVIEWS:
        kind = "reviews-over-limit"
        detail = sorted(loop_state.reviews)
    elif len(held) != len(set(held)):
        kind = "slot-shared"
        detail = held
    elif len(tracked) != len(set(tracked)):
        kind = "item-double-tracked"
        dupes = sorted({i for i in tracked if tracked.count(i) > 1})
        item_id = ",".join(dupes)
        detail = tracked
    elif land_start and loop_state.active_land is not None:
        kind = "second-land"
        item_id = loop_state.active_land.item_id
        detail = [q.item_id for q in loop_state.land_queue]
    elif dispatching is not None and dispatching in tracked:
        kind = "dispatch-while-tracked"
        detail = tracked
    if kind is not None:
        logger.error(
            "rollout-loop: concurrency-invariant-violation rollout_id=%s kind=%s "
            "item_id=%s detail=%r",
            rollout_id,
            kind,
            item_id,
            detail,
        )
    return kind


async def _teardown_reviews(
    loop_state: _RolloutLoopState, *, rollout_id: str = ""
) -> list[str]:
    """Cancel and await every review task, clear ``reviews`` (freeing slots).

    Cancelling a task stops its reviewer: ``_launch_reviewer`` terminates the
    reviewer's session and records the run ``cancelled``.  Returns the ids of
    the reviews that were still running when torn down.
    """
    entries = list(loop_state.reviews.values())
    loop_state.reviews.clear()
    if not entries:
        return []
    running = [e.item_id for e in entries if not e.task.done()]
    for entry in entries:
        entry.task.cancel()
    await asyncio.gather(*(e.task for e in entries), return_exceptions=True)
    loop_state.halt_cancelled_reviews += len(running)
    if running:
        logger.info(
            "rollout-loop: reviews-torn-down rollout_id=%s item_ids=%s",
            rollout_id,
            ",".join(running),
        )
    return running


def _queued_land_suffix(loop_state: _RolloutLoopState) -> str:
    """The halt-reason suffix naming queued merge verdicts a halt discards."""
    if not loop_state.land_queue:
        return ""
    ids = ", ".join(q.item_id for q in loop_state.land_queue)
    return (
        f"; discarded queued merge verdict(s) for {ids}: "
        "they will be re-reviewed on resume"
    )


async def _halt_teardown(
    rollout_id: str,
    loop_state: _RolloutLoopState,
    *,
    cause: str,
    cause_item: str | None,
    directive: str = _TICK_HALTED,
) -> str:
    """The halt path: no further reviews, lands or dispatches.

    Tears down every review, drops the land queue WITHOUT landing, and never
    cancels the active land: while one runs, *directive* is parked in
    ``pending_directive`` and the tick waits for that land to finish.
    """
    cancelled = await _teardown_reviews(loop_state, rollout_id=rollout_id)
    dropped = [q.item_id for q in loop_state.land_queue]
    loop_state.land_queue.clear()
    active = loop_state.active_land
    logger.info(
        "rollout-loop: halt-teardown rollout_id=%s cause=%s cause_item=%s "
        "reviews_cancelled=%s land_queue_dropped=%s active_land=%s",
        rollout_id,
        cause,
        cause_item or "-",
        ",".join(cancelled) or "-",
        ",".join(dropped) or "-",
        active.item_id if active is not None else "-",
    )
    if active is not None and not active.task.done():
        loop_state.pending_directive = directive
        return _TICK_WAIT
    return directive


async def _loop_halt(
    rollout_id: str,
    loop_state: _RolloutLoopState,
    reason: str,
    *,
    item_id: str | None,
    cause: str,
    token: str | None,
    attribution: str | None,
) -> str:
    """Halt the rollout from the loop (naming discarded merges), then tear down."""
    await _halt_rollout_from_loop(
        rollout_id,
        _with_suffix(reason, _queued_land_suffix(loop_state)),
        item_id=item_id,
        token=token,
        attribution=attribution,
    )
    return await _halt_teardown(rollout_id, loop_state, cause=cause, cause_item=item_id)


async def _exit_tick(
    rollout_id: str, loop_state: _RolloutLoopState, directive: str
) -> str:
    """An early DONE/HALTED exit at the tick top, deferred behind an active land."""
    land = loop_state.active_land
    if (
        loop_state.reviews
        or loop_state.land_queue
        or (land is not None and not land.task.done())
    ):
        return await _halt_teardown(
            rollout_id,
            loop_state,
            cause="board-status",
            cause_item=None,
            directive=directive,
        )
    return directive


async def _slot_workspace(
    rollout_id: str,
    loop_state: _RolloutLoopState,
    slot: int,
    *,
    project: dict[str, Any],
    item_id: str,
    token: str | None,
) -> dispatch.RolloutWorkspace:
    """Return *slot*'s workspace, preparing it the first time it is needed.

    A cached slot whose root has vanished (pruned by retention) is dropped and
    prepared again rather than crashed on.
    """
    workspace = loop_state.slots.get(slot)
    if workspace is not None and not workspace.root.is_dir():
        logger.warning(
            "rollout-loop: slot-workspace-missing rollout_id=%s slot=%d root=%s "
            "action=re-prepare",
            rollout_id,
            slot,
            workspace.root,
        )
        del loop_state.slots[slot]
        workspace = None
    if workspace is None:
        await _publish_rollout_phase(
            rollout_id,
            "warm_up",
            item_id=item_id,
            step=f"Preparing rollout review workspace slot {slot}",
            token=token,
        )
        workspace = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                dispatch.prepare_rollout_workspace,
                rollout_id,
                git_origin=str(project.get("git_origin") or ""),
                repo_urls=list(project.get("workspace_repos") or []),
                slot=slot,
            ),
        )
        loop_state.slots[slot] = workspace
    return workspace


def _release_host_review(_task: asyncio.Task[Any]) -> None:
    """Done callback of a loop-started review task: free its host-wide slot.

    A done callback rather than a ``finally`` in the coroutine, because a task
    cancelled before its first step never enters its coroutine body.
    """
    global _host_reviews_in_flight
    _host_reviews_in_flight = max(0, _host_reviews_in_flight - 1)


async def _run_land_task(
    rollout_id: str,
    queued: _QueuedLand,
    *,
    project: dict[str, Any],
    workspace: dispatch.RolloutWorkspace,
    reviewer_engine: Engine,
    loop_run_id: str,
    token: str | None,
    attribution: str | None,
) -> ReviewVerdict | str:
    """Land one queued merge in its slot, then act on the returned verdict."""
    landed = await _land_reviewed_merge(
        rollout_id,
        queued.item_id,
        queued.verdict,
        project=project,
        workspace=workspace,
        pre_review_origin=queued.pre_review_origin,
        reviewer_engine=reviewer_engine,
        loop_run_id=loop_run_id,
        token=token,
        attribution=attribution,
        item=queued.item,
    )
    if isinstance(landed, str):
        return landed
    return await _act_on_verdict(
        rollout_id,
        queued.item_id,
        landed,
        token=token,
        attribution=attribution,
        workspace=workspace,
    )


def _start_land(
    rollout_id: str,
    loop_state: _RolloutLoopState,
    *,
    project: dict[str, Any],
    reviewer_engine: Engine,
    loop_run_id: str,
    token: str | None,
    attribution: str | None,
) -> None:
    """Pop the head of the land queue and start its (single) land task."""
    queued = loop_state.land_queue.pop(0)
    workspace = loop_state.slots[queued.slot]
    task: asyncio.Task[ReviewVerdict | str] = asyncio.create_task(
        _run_land_task(
            rollout_id,
            queued,
            project=project,
            workspace=workspace,
            reviewer_engine=reviewer_engine,
            loop_run_id=loop_run_id,
            token=token,
            attribution=attribution,
        )
    )
    now = _monotonic()
    loop_state.active_land = _ActiveLand(queued.item_id, queued.slot, task, now)
    queued_secs = now - queued.queued_at
    loop_state.max_land_queued_secs = max(loop_state.max_land_queued_secs, queued_secs)
    logger.info(
        "rollout-loop: land-start rollout_id=%s item_id=%s slot=%d "
        "queued_secs=%.1f land_queue_remaining=%d",
        rollout_id,
        queued.item_id,
        queued.slot,
        queued_secs,
        len(loop_state.land_queue),
    )


async def _rollout_wave_tick(
    rollout_id: str,
    *,
    project: dict[str, Any],
    loop_state: _RolloutLoopState,
    reviewer_engine: Engine,
    dispatch_attempts: dict[str, int],
    token: str | None,
    attribution: str | None,
    loop_run_id: str,
    was_parked: bool = False,
) -> str:
    """One pass of the wave loop. Returns a ``_TICK_*`` directive.

    Reviews run as background tasks, several at once (capped per rollout by
    the slot count and host-wide by ``_host_reviews_in_flight``), each in its
    own slot workspace.  A tick never waits on a reviewer; it HARVESTS the
    finished ones.  After the board reads at the top, in this order:

    (a) harvest finished reviews: a ``merge`` verdict joins ``land_queue``;
        every other verdict is acted on at once; (b) harvest the finished
        land; (c) the drain check; (d) start ONE land if none is active;
        (e) start reviews into free slots; (f) dispatch ``next_ready``;
        (g) publish the phase and return.

    Land is strictly serialized per rollout: at most one land task exists
    (``active_land``), lands start in harvest order, and ``_Lander`` rebases
    each onto the LIVE origin tip and gates that exact tree — so a review done
    against an older base is still gated on the post-merge tip.  No halt ever
    cancels a land: the DONE/HALTED exits wait for it (``pending_directive``).

    Drain parking.  When the board reports the loop's host draining
    (``loop_host_drain_reason`` on ``get_rollout``), the tick starts no new
    review and dispatches nothing; in-flight reviews and queued lands finish
    first (``_TICK_WAIT``), and only then does it return ``_TICK_PARKED``.
    ``was_parked`` only quiets the park-enter log.

    Dependent ordering (why dispatching on every tick is safe).
    ``advance_rollout`` lists an item in ``next_ready`` only when every
    predecessor's rollout item is terminal (completed, halted or skipped).  The
    worker records ``completed`` for a merge only inside ``_act_on_verdict``,
    AFTER ``_land_reviewed_merge`` has pushed and audited the gated sha.  So a
    dependent is dispatched only once every predecessor is terminal, and for a
    merged predecessor that means its gated sha is on origin's tip — no matter
    how many unrelated reviews are in flight.

    Re-run safety.  ``_drive_rollout`` re-runs a tick FROM THE TOP when a
    transient board error escapes it, so every side effect taken before a
    board call that can raise must be safe to reach again:

    - Phase publishes, item comments, ``halt_rollout``, merge-note and run-list
      reads: best effort, they swallow every error and never raise.
    - Slot workspaces, review tasks, the land queue and the active land live in
      ``loop_state``, which outlives the tick, so a re-run reuses them.
    - Starting a review: every board call that can raise (``get_item``, and
      ``advance_rollout``/``get_rollout`` above) happens BEFORE that item's
      ``create_task``, and an item already in ``reviews`` is never started
      again.
    - Harvesting: each finished review is popped from ``reviews`` BEFORE it is
      acted on, so a re-run never acts on the same verdict twice.
    - The land (push): runs in its own task, followed only by best-effort
      board calls.
    - Recording an outcome: ``_complete_in_rollout_with_retry`` returns its
      error instead of raising, and ``complete_in_rollout`` is idempotent by
      item id; a merge whose record still fails is tracked in
      ``_rollout_unrecorded_merges`` and never re-reviewed.
    - Re-dispatch (reset + dispatch): any error halts, so it never escapes.
    - Dispatching a ready item: a transient error escapes and the tick is
      re-run.  A dispatch that landed moved the item out of ``next_ready``, so
      it is not dispatched again.
    """
    global _host_reviews_in_flight
    try:
        advance = await gtd_client.advance_rollout(rollout_id, token=token)
    except httpx.HTTPStatusError as exc:
        if _http_status(exc) != 422:
            raise
        # The board answers 422 once the rollout is no longer running; re-read
        # once to tell a terminal rollout from a genuine 422.
        reread = await gtd_client.get_rollout(rollout_id, token=token)
        reread_status = str(reread.get("status") or "")
        if reread_status == "running":
            logger.warning(
                "rollout-loop: advance-422-reread rollout_id=%s "
                "reread_status=running action=reraise",
                rollout_id,
            )
            raise
        action = "halted" if reread_status == "halted" else "done"
        logger.warning(
            "rollout-loop: advance-422-reread rollout_id=%s reread_status=%s action=%s",
            rollout_id,
            reread_status,
            action,
        )
        return await _exit_tick(
            rollout_id,
            loop_state,
            _TICK_HALTED if reread_status == "halted" else _TICK_DONE,
        )
    rollout = await gtd_client.get_rollout(rollout_id, token=token)

    status = str(rollout.get("status") or "")
    if status != "running":
        logger.info(
            "rollout-loop: rollout_id=%s left running (status=%s) — loop exits",
            rollout_id,
            status,
        )
        return await _exit_tick(
            rollout_id, loop_state, _TICK_HALTED if status == "halted" else _TICK_DONE
        )

    # Wave completion is READ, not computed. `complete_item_in_rollout` already
    # closes the rollout and signals `graph_complete`, and `advance_rollout`
    # computes the same thing independently. The worker consumes that field and
    # does NOT invent a third notion of done.
    if advance.get("graph_complete"):
        logger.info(
            "rollout-loop: rollout_id=%s graph_complete — loop exits", rollout_id
        )
        return await _exit_tick(rollout_id, loop_state, _TICK_DONE)

    in_flight = _in_flight_build_runs(rollout)
    completed = _completed_build_item_ids(advance, in_flight)

    # Merged-but-unrecorded items: drop any that left `in_progress` (recorded
    # by someone else, or reset), retry the due ones, and never re-review them.
    pending = _rollout_unrecorded_merges.get(rollout_id, {})
    in_progress_ids = {str(i) for i in (advance.get("in_progress") or [])}
    for stale_id in [k for k in pending if k not in in_progress_ids]:
        logger.warning(
            "rollout-loop: unrecorded-merge-dropped rollout_id=%s item_id=%s "
            "passes=%d reason=left-in-progress",
            rollout_id,
            stale_id,
            pending[stale_id].passes,
        )
        del pending[stale_id]
    if pending:
        directive = await _retry_unrecorded_merges(
            rollout_id, pending, token=token, attribution=attribution
        )
        if directive == _TICK_HALTED:
            return await _halt_teardown(
                rollout_id, loop_state, cause="verdict", cause_item=None
            )
        if directive is not None:
            return directive

    acted_this_tick: set[str] = set()
    harvested = False
    started = False
    dispatched = False
    done_seen = False

    # (a) HARVEST finished reviews.
    for item_id, entry in list(loop_state.reviews.items()):
        if not entry.task.done():
            continue
        loop_state.reviews.pop(item_id)
        dispatch_attempts.pop(item_id, None)
        acted_this_tick.add(item_id)
        harvested = True
        if entry.task.cancelled():
            continue
        task_exc = entry.task.exception()
        if task_exc is not None:
            logger.exception(
                "rollout-loop: review-task-raised rollout_id=%s item_id=%s slot=%d",
                rollout_id,
                item_id,
                entry.slot,
                exc_info=task_exc,
            )
            outcome = ReviewVerdict(
                "halt",
                "review task raised",
                "",
                f"{type(task_exc).__name__}: {task_exc}",
            )
        else:
            outcome = entry.task.result()
        review_secs = _monotonic() - entry.started_at
        if outcome.verdict == "merge":
            loop_state.land_queue.append(
                _QueuedLand(
                    item_id,
                    entry.slot,
                    outcome,
                    entry.item,
                    entry.pre_review_origin,
                    _monotonic(),
                )
            )
            loop_state.peak_land_queue = max(
                loop_state.peak_land_queue, len(loop_state.land_queue)
            )
            logger.info(
                "rollout-loop: review-harvest rollout_id=%s item_id=%s slot=%d "
                "verdict=%s review_secs=%.1f action=land-queued",
                rollout_id,
                item_id,
                entry.slot,
                outcome.verdict,
                review_secs,
            )
            logger.info(
                "rollout-loop: land-enqueue rollout_id=%s item_id=%s slot=%d "
                "queue_pos=%d",
                rollout_id,
                item_id,
                entry.slot,
                len(loop_state.land_queue),
            )
            continue
        logger.info(
            "rollout-loop: review-harvest rollout_id=%s item_id=%s slot=%d "
            "verdict=%s review_secs=%.1f action=%s",
            rollout_id,
            item_id,
            entry.slot,
            outcome.verdict,
            review_secs,
            "halt-raised" if outcome.verdict == "halt" else "acted",
        )
        # The entry is already popped, so its slot is free whatever happens
        # here; a transient error re-raises and the tick is re-run.
        directive = await _act_on_verdict(
            rollout_id,
            item_id,
            outcome,
            token=token,
            attribution=attribution,
            workspace=loop_state.slots.get(entry.slot),
            halt_suffix=_queued_land_suffix(loop_state),
        )
        if directive == _TICK_HALTED:
            return await _halt_teardown(
                rollout_id, loop_state, cause="verdict", cause_item=item_id
            )
        if directive == _TICK_DONE:
            done_seen = True

    # (b) HARVEST the land.
    land = loop_state.active_land
    if land is not None and land.task.done():
        loop_state.active_land = None
        acted_this_tick.add(land.item_id)
        harvested = True
        land_secs = _monotonic() - land.started_at
        land_exc: BaseException | None = (
            asyncio.CancelledError("land task cancelled")
            if land.task.cancelled()
            else land.task.exception()
        )
        if land_exc is None:
            result = land.task.result()
            loop_state.lands += 1
            label = result if isinstance(result, str) else result.verdict
            logger.info(
                "rollout-loop: land-done rollout_id=%s item_id=%s slot=%d "
                "directive=%s land_secs=%.1f",
                rollout_id,
                land.item_id,
                land.slot,
                label,
                land_secs,
            )
            if result == _TICK_HALTED:
                return await _halt_teardown(
                    rollout_id, loop_state, cause="land", cause_item=land.item_id
                )
            if result == _TICK_DONE:
                done_seen = True
        elif _is_transient_board_error(land_exc):
            # The item stays dispatched with a terminal build; the pre-review
            # already-landed check records it if the push reached origin.
            logger.warning(
                "rollout-loop: land-task-transient rollout_id=%s item_id=%s",
                rollout_id,
                land.item_id,
            )
        else:
            # The slot's tree stays on disk as evidence; the item is never
            # re-queued.
            return await _loop_halt(
                rollout_id,
                loop_state,
                f"land task for item {land.item_id} raised: "
                f"{type(land_exc).__name__}: {land_exc}",
                item_id=land.item_id,
                cause="land",
                token=token,
                attribution=attribution,
            )
    if loop_state.pending_directive is not None:
        if loop_state.active_land is not None:
            return _TICK_WAIT
        directive = loop_state.pending_directive
        loop_state.pending_directive = None
        return directive

    # (c) DRAIN check.
    drain = str(rollout.get("loop_host_drain_reason") or "").strip()
    if not drain:
        loop_state.drain_finishing_logged = False
    else:
        if loop_state.active_land is None and loop_state.land_queue:
            _start_land(
                rollout_id,
                loop_state,
                project=project,
                reviewer_engine=reviewer_engine,
                loop_run_id=loop_run_id,
                token=token,
                attribution=attribution,
            )
        if pending:
            await _publish_rollout_phase(
                rollout_id,
                "polling",
                step=(
                    "Recording merged item(s) on the board before parking: "
                    f"{', '.join(pending)}"
                ),
                token=token,
            )
            return _TICK_WAIT
        if _busy(loop_state):
            n_reviews = len(loop_state.reviews)
            n_lands = len(loop_state.land_queue) + (
                1 if loop_state.active_land is not None else 0
            )
            if loop_state.active_land is None:
                await _publish_rollout_phase(
                    rollout_id,
                    "polling",
                    step=(
                        f"Host draining: finishing {n_reviews} review(s) and "
                        f"{n_lands} land(s) before parking"
                    ),
                    token=token,
                )
            log = logger.debug if loop_state.drain_finishing_logged else logger.info
            log(
                "rollout-loop: drain-finishing rollout_id=%s run_id=%s reviews=%d "
                "lands=%d",
                rollout_id,
                loop_run_id,
                n_reviews,
                n_lands,
            )
            loop_state.drain_finishing_logged = True
            return _TICK_WAIT
        await _publish_rollout_phase(
            rollout_id, "parked", step=_parked_step(drain), token=token
        )
        if not was_parked:
            logger.info(
                "rollout-loop: park-enter rollout_id=%s run_id=%s reason=%r",
                rollout_id,
                loop_run_id,
                drain,
            )
        else:
            logger.debug(
                "rollout-loop: park-still rollout_id=%s run_id=%s reason=%r",
                rollout_id,
                loop_run_id,
                drain,
            )
        return _TICK_PARKED

    # (d) Start ONE land.
    if loop_state.active_land is None and loop_state.land_queue:
        kind = _audit_loop_state(rollout_id, loop_state, land_start=True)
        if kind is not None:
            return await _loop_halt(
                rollout_id,
                loop_state,
                f"bad_local_state: {kind}",
                item_id=loop_state.land_queue[0].item_id,
                cause="verdict",
                token=token,
                attribution=attribution,
            )
        _start_land(
            rollout_id,
            loop_state,
            project=project,
            reviewer_engine=reviewer_engine,
            loop_run_id=loop_run_id,
            token=token,
            attribution=attribution,
        )
        started = True

    # (e) START reviews.
    active_item = (
        loop_state.active_land.item_id if loop_state.active_land is not None else None
    )
    queued_ids = {q.item_id for q in loop_state.land_queue}
    reviewable = [
        i
        for i in completed
        if i not in loop_state.reviews
        and i != active_item
        and i not in queued_ids
        and i not in pending
        and i not in acted_this_tick
    ]
    parked = False
    for item_id in reviewable:
        # A child build the board failed because its pinned host was draining
        # never ran: re-dispatch it (the host is back, or this tick would have
        # stopped at the drain check) instead of sending it to the reviewer.
        latest_run = await _latest_run_for_item(item_id, rollout_id, token=token)
        if str(latest_run.get("status") or "") == "failed" and (
            _HOST_DRAINING_MARKER in str(latest_run.get("error_msg") or "")
        ):
            try:
                await gtd_client.reset_rollout_item(rollout_id, item_id, token=token)
                await gtd_client.dispatch_item(
                    item_id, rollout_id=rollout_id, token=token
                )
            except Exception as exc:
                if _is_transient_board_error(exc):
                    raise
                refusal = await _park_or_halt_on_dispatch_refusal(
                    rollout_id,
                    item_id,
                    exc,
                    halt_reason=_with_suffix(
                        f"re-dispatch of item {item_id} after a drain refusal "
                        f"failed: {type(exc).__name__}: {exc}",
                        _queued_land_suffix(loop_state),
                    ),
                    token=token,
                    attribution=attribution,
                )
                if refusal == _TICK_HALTED:
                    return await _halt_teardown(
                        rollout_id, loop_state, cause="verdict", cause_item=item_id
                    )
                parked = True
                break
            logger.info(
                "rollout-loop: redispatch-after-drain-refusal rollout_id=%s item_id=%s",
                rollout_id,
                item_id,
            )
            acted_this_tick.add(item_id)
            dispatched = True
            continue
        slot = _free_slot(loop_state)
        if (
            slot is None
            or _host_reviews_in_flight >= config.ROLLOUT_MAX_CONCURRENT_REVIEWS
        ):
            continue
        workspace = await _slot_workspace(
            rollout_id,
            loop_state,
            slot,
            project=project,
            item_id=item_id,
            token=token,
        )
        # Fetched here, before the reviewer starts, and threaded through to the
        # land step's fix relaunch: nothing after create_task may raise a board
        # error out of this tick for this item.
        review_item = await gtd_client.get_item(item_id, token=token)
        await asyncio.get_running_loop().run_in_executor(
            None, dispatch.refresh_rollout_workspace, workspace
        )
        # Origin's tip per repo as the reviewer starts: the reviewer-pushed
        # guard in the land step only looks at commits that landed AFTER this.
        pre_review_origin: dict[
            str, str
        ] = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                dispatch.land_origin_shas,
                dict(workspace.repo_paths),
                dict(workspace.default_branches),
                config.LAND_GIT_TIMEOUT_SECONDS,
            ),
        )
        precheck = await _pre_review_landed_check(
            rollout_id,
            item_id,
            slot,
            workspace,
            loop_state,
            token=token,
            attribution=attribution,
        )
        if precheck is not None:
            acted_this_tick.add(item_id)
            harvested = True
            # Any halt path has already torn down; a halt behind an active
            # land comes back as WAIT with HALTED parked in pending_directive.
            # Either way nothing more may start or dispatch this tick.
            if precheck not in (_TICK_CONTINUE, _TICK_DONE):
                return precheck
            if precheck == _TICK_DONE:
                done_seen = True
            continue
        kind = _audit_loop_state(rollout_id, loop_state)
        if kind is not None:
            return await _loop_halt(
                rollout_id,
                loop_state,
                f"bad_local_state: {kind}",
                item_id=item_id,
                cause="verdict",
                token=token,
                attribution=attribution,
            )
        task: asyncio.Task[ReviewVerdict] = asyncio.create_task(
            _launch_reviewer(
                rollout_id,
                item_id,
                project=project,
                workspace=workspace,
                reviewer_engine=reviewer_engine,
                token=token,
                attribution=attribution,
                item=review_item,
                executor=dispatch._review_executor,
                slot=slot,
            )
        )
        _host_reviews_in_flight += 1
        task.add_done_callback(_release_host_review)
        loop_state.reviews[item_id] = _ReviewEntry(
            item_id, slot, task, review_item, pre_review_origin, _monotonic()
        )
        loop_state.reviews_started += 1
        loop_state.peak_reviews_in_flight = max(
            loop_state.peak_reviews_in_flight, len(loop_state.reviews)
        )
        started = True
        logger.info(
            "rollout-loop: review-start rollout_id=%s item_id=%s slot=%d "
            "workspace=%s reviews_in_flight=%d host_reviews_in_flight=%d/%d "
            "land_queue=%d active_land=%s",
            rollout_id,
            item_id,
            slot,
            workspace.root,
            len(loop_state.reviews),
            _host_reviews_in_flight,
            config.ROLLOUT_MAX_CONCURRENT_REVIEWS,
            len(loop_state.land_queue),
            loop_state.active_land.item_id
            if loop_state.active_land is not None
            else "-",
        )

    # (f) DISPATCH next_ready, on every tick regardless of reviews or lands.
    next_ready = [str(i) for i in (advance.get("next_ready") or [])]
    tracked = set(_tracked_item_ids(rollout_id, loop_state))
    for item_id in [] if parked else next_ready:
        if item_id in tracked or item_id in acted_this_tick:
            continue
        attempts = dispatch_attempts.get(item_id, 0)
        if attempts >= _MAX_DISPATCH_ATTEMPTS_PER_ITEM:
            return await _loop_halt(
                rollout_id,
                loop_state,
                f"item {item_id} is still reported ready after {attempts} "
                "successful dispatch calls — the rollout item never "
                "transitioned to 'dispatched'",
                item_id=item_id,
                cause="verdict",
                token=token,
                attribution=attribution,
            )
        kind = _audit_loop_state(rollout_id, loop_state, dispatching=item_id)
        if kind is not None:
            return await _loop_halt(
                rollout_id,
                loop_state,
                f"bad_local_state: {kind}",
                item_id=item_id,
                cause="verdict",
                token=token,
                attribution=attribution,
            )
        if loop_state.active_land is None:
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
            # A transient board error is ridden out by _drive_rollout, not
            # turned into a halt.  Re-running the tick cannot double-dispatch:
            # a dispatch that DID land moved the item to `dispatched`, so
            # advance_rollout no longer reports it in next_ready (and the
            # board's wave linkage is guarded by `AND status = 'ready'`).
            if _is_transient_board_error(exc):
                raise
            refusal = await _park_or_halt_on_dispatch_refusal(
                rollout_id,
                item_id,
                exc,
                halt_reason=_with_suffix(
                    f"dispatch of item {item_id} failed: {type(exc).__name__}: {exc}",
                    _queued_land_suffix(loop_state),
                ),
                token=token,
                attribution=attribution,
            )
            if refusal == _TICK_HALTED:
                return await _halt_teardown(
                    rollout_id, loop_state, cause="verdict", cause_item=item_id
                )
            parked = True
            break
        dispatch_attempts[item_id] = attempts + 1
        dispatched = True
        if loop_state.reviews:
            loop_state.dispatches_while_reviewing += 1
        logger.info(
            "rollout-loop: dispatched rollout_id=%s item_id=%s run_id=%s "
            "reviews_in_flight=%d lands_in_flight=%d",
            rollout_id,
            item_id,
            (child or {}).get("id"),
            len(loop_state.reviews),
            1 if loop_state.active_land is not None else 0,
        )

    # (g) PUBLISH and return.
    kind = _audit_loop_state(rollout_id, loop_state)
    if kind is not None:
        return await _loop_halt(
            rollout_id,
            loop_state,
            f"bad_local_state: {kind}",
            item_id=None,
            cause="verdict",
            token=token,
            attribution=attribution,
        )
    if parked:
        return _TICK_WAIT if _busy(loop_state) else _TICK_PARKED
    if loop_state.active_land is None:
        if loop_state.reviews:
            newest = max(loop_state.reviews.values(), key=lambda e: e.started_at)
            ids = ", ".join(loop_state.reviews)
            await _publish_rollout_phase(
                rollout_id,
                "reviewing",
                item_id=newest.item_id,
                step=(
                    f"Reviewing {len(loop_state.reviews)} completed build(s): "
                    f"{ids}; {len(loop_state.land_queue)} queued to land"
                )[:200],
                token=token,
            )
        elif not (harvested or started or dispatched):
            if in_flight:
                await _publish_rollout_phase(
                    rollout_id,
                    "polling",
                    step=f"Waiting on {len(in_flight)} in-flight build run(s)",
                    token=token,
                )
            elif pending:
                await _publish_rollout_phase(
                    rollout_id,
                    "polling",
                    step=(
                        "Waiting to record merged item(s) on the board: "
                        f"{', '.join(pending)}"
                    ),
                    token=token,
                )
    if done_seen and not _busy(loop_state):
        return _TICK_DONE
    if harvested or started or dispatched:
        return _TICK_CONTINUE
    if _busy(loop_state) or in_flight:
        return _TICK_WAIT
    if pending:
        logger.warning(
            "rollout-loop: waiting-on-unrecorded-merges rollout_id=%s pending=%s",
            rollout_id,
            ",".join(f"{k}:{v.passes}" for k, v in pending.items()),
        )
        return _TICK_WAIT

    return await _halt_rollout_from_loop(
        rollout_id,
        "rollout stalled: nothing ready, nothing in flight, graph not complete "
        f"(blocked={list(advance.get('blocked') or [])})",
        token=token,
        attribution=attribution,
    )


async def _pre_review_landed_check(
    rollout_id: str,
    item_id: str,
    slot: int,
    workspace: dispatch.RolloutWorkspace,
    loop_state: _RolloutLoopState,
    *,
    token: str | None,
    attribution: str | None,
) -> str | None:
    """Record, instead of re-reviewing, an item whose squash already landed.

    After a restart (or a land task that died on a board blip after its push),
    an item can still be ``dispatched`` with its squash commit already on
    origin.  The lookup is scoped to THIS rollout (``Rollout:`` trailer).

    A hit in some repos and none in the others counts as landed (a workspace
    item lands only in the repos it touched); a repo whose lookup failed halts.

    Returns None to proceed to review, else the directive of what was done
    (recorded, routed to the unrecorded-merge path, or halted).  Every halt
    path has already run the halt teardown, so a halt behind an active land
    comes back as ``_TICK_WAIT`` with ``pending_directive`` set.
    """
    confirmation, subjects, failed_repos = await _confirm_merge_landed(
        workspace, item_id, rollout_id=rollout_id
    )
    action = "review"
    directive: str | None = None
    if confirmation == "confirmed":
        # A workspace item lands only in the repos it touched, and the lookup
        # omits repos with no matching commit, so a repo with no hit is not
        # evidence of a partial land.  Only a lookup that FAILED leaves the
        # answer unknown, and that halts.
        if failed_repos:
            action = "halted"
            directive = await _loop_halt(
                rollout_id,
                loop_state,
                f"item {item_id}'s squash commit is on origin in "
                f"{', '.join(sorted(subjects))}, but the landed-commit lookup "
                f"failed in {', '.join(sorted(failed_repos))}",
                item_id=item_id,
                cause="verdict",
                token=token,
                attribution=attribution,
            )
        else:
            action = "recorded"
            merge_note = (
                f"Recorded after restart: the squash commit for {item_id} was "
                "already on origin ("
                + "; ".join(
                    f"{repo}: {subj}" for repo, subj in sorted(subjects.items())
                )
                + ")."
            )
            result, exc = await _complete_in_rollout_with_retry(
                rollout_id,
                item_id,
                outcome="completed",
                merge_note=merge_note,
                token=token,
            )
            if exc is not None:
                directive = await _handle_unrecorded_merge(
                    rollout_id,
                    item_id,
                    ReviewVerdict("merge", "already landed on origin", merge_note, ""),
                    exc,
                    token=token,
                    attribution=attribution,
                    workspace=workspace,
                )
                if directive == _TICK_HALTED:
                    directive = await _halt_teardown(
                        rollout_id, loop_state, cause="verdict", cause_item=item_id
                    )
            elif isinstance(result, dict) and result.get("graph_complete") is True:
                directive = _TICK_DONE
            else:
                directive = _TICK_CONTINUE
    log = logger.warning if confirmation == "confirmed" else logger.debug
    log(
        "rollout-loop: pre-review-landed-check rollout_id=%s item_id=%s slot=%d "
        "result=%s action=%s",
        rollout_id,
        item_id,
        slot,
        confirmation,
        action,
    )
    return directive


async def _drive_rollout(
    run: Run,
    *,
    attribution: str | None = None,
) -> str:
    """Drive one rollout's wave loop to completion. Returns the final directive.

    Determine what is ready, dispatch it, wait for completion, review each
    completed build (several at once), land merges one at a time, act on the
    verdicts, advance, repeat until the rollout is complete or halted.
    """
    rollout_id = run.rollout_id
    assert rollout_id is not None  # noqa: S101 — caller guarantees this
    token = run.callback_token
    outage = _BoardOutage(rollout_id)

    rollout = await _ride_out_board_outage(
        outage, lambda: gtd_client.get_rollout(rollout_id, token=token)
    )
    project = await _ride_out_board_outage(
        outage,
        lambda: gtd_client.get_project(
            str(rollout.get("project_id") or ""), token=token
        ),
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

    loop_state = _RolloutLoopState()
    dispatch_attempts: dict[str, int] = {}
    directive = _TICK_CONTINUE
    parked_since: float | None = None
    parked_ticks = 0
    last_park_warn_at: float | None = None
    shutdown = False
    try:
        while True:
            # Every tick runs while the loop counts as active: membership in
            # the parked set means "sleeping after a parked tick", nothing else.
            _parked_rollout_loops.discard(run.id)
            was_parked = parked_since is not None
            # A transient board error re-runs the whole tick from the top.
            # That is safe because every side effect in the tick is either
            # re-derived from the board on the next read, held in loop_state,
            # or followed only by board calls that never raise (see
            # _rollout_wave_tick).
            directive = await _ride_out_board_outage(
                outage,
                functools.partial(
                    _rollout_wave_tick,
                    rollout_id,
                    project=project,
                    loop_state=loop_state,
                    reviewer_engine=reviewer_engine,
                    dispatch_attempts=dispatch_attempts,
                    token=token,
                    attribution=attribution,
                    loop_run_id=run.id,
                    was_parked=was_parked,
                ),
            )
            if directive == _TICK_PARKED:
                now = _monotonic()
                if parked_since is None:
                    parked_since = now
                    last_park_warn_at = now
                parked_ticks += 1
                assert last_park_warn_at is not None  # noqa: S101
                if now - last_park_warn_at >= config.ROLLOUT_PARK_WARN_SECONDS:
                    logger.warning(
                        "rollout-loop: park-long rollout_id=%s run_id=%s "
                        "parked_seconds=%d",
                        rollout_id,
                        run.id,
                        int(now - parked_since),
                    )
                    last_park_warn_at = now
                _parked_rollout_loops.add(run.id)
                await asyncio.sleep(config.ROLLOUT_LOOP_POLL_SECONDS)
                continue
            if parked_since is not None:
                logger.info(
                    "rollout-loop: park-exit rollout_id=%s run_id=%s "
                    "parked_seconds=%d parked_ticks=%d next_directive=%s",
                    rollout_id,
                    run.id,
                    int(_monotonic() - parked_since),
                    parked_ticks,
                    directive,
                )
                parked_since = None
                parked_ticks = 0
                last_park_warn_at = None
            if directive in (_TICK_DONE, _TICK_HALTED):
                # Deregister NOW, synchronously, before any further await
                # (workspace cleanup, the worker's db.update_run): a lead's
                # resume that relaunches the loop after this tick read
                # 'halted' must start a fresh loop, not be refused by this
                # one, which is already leaving.
                _deregister_rollout_loop(run)
                return directive
            if directive == _TICK_WAIT:
                # Wake as soon as a review or the land finishes, or after the
                # poll interval (builds run elsewhere and push nothing).
                tasks: set[asyncio.Task[Any]] = {
                    e.task for e in loop_state.reviews.values()
                }
                if loop_state.active_land is not None:
                    tasks.add(loop_state.active_land.task)
                if tasks:
                    await asyncio.wait(
                        tasks,
                        timeout=config.ROLLOUT_LOOP_POLL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(config.ROLLOUT_LOOP_POLL_SECONDS)
    except asyncio.CancelledError:
        shutdown = True
        raise
    finally:
        _parked_rollout_loops.discard(run.id)
        if parked_since is not None:
            logger.info(
                "rollout-loop: park-abandoned rollout_id=%s run_id=%s "
                "parked_seconds=%d",
                rollout_id,
                run.id,
                int(_monotonic() - parked_since),
            )
        await _teardown_reviews(loop_state, rollout_id=rollout_id)
        land = loop_state.active_land
        if land is not None:
            # A land is never abandoned mid-push: it is awaited.  Only service
            # shutdown cancels it; the pre-review already-landed check then
            # recovers a push that had already reached origin.
            if shutdown:
                land.task.cancel()
            await asyncio.gather(land.task, return_exceptions=True)
        _rollout_redispatches.pop(rollout_id, None)
        _rollout_unrecorded_merges.pop(rollout_id, None)
        logger.info(
            "rollout-loop: concurrency-summary rollout_id=%s run_id=%s limit=%d "
            "reviews_started=%d peak_reviews_in_flight=%d "
            "dispatches_while_reviewing=%d lands=%d peak_land_queue=%d "
            "max_land_queued_secs=%.1f halt_cancelled_reviews=%d",
            rollout_id,
            run.id,
            config.ROLLOUT_MAX_CONCURRENT_REVIEWS,
            loop_state.reviews_started,
            loop_state.peak_reviews_in_flight,
            loop_state.dispatches_while_reviewing,
            loop_state.lands,
            loop_state.peak_land_queue,
            loop_state.max_land_queued_secs,
            loop_state.halt_cancelled_reviews,
        )
        # Slot workspaces are torn down only when the rollout completed. A
        # halted rollout keeps every slot: the trees are the evidence a human
        # needs.
        if directive == _TICK_DONE:
            for ws in loop_state.slots.values():
                with contextlib.suppress(Exception):
                    for _rp in dispatch.reap_workspace_processes(ws.root):
                        logger.warning(
                            "reaped leaked process from rollout workspace: "
                            "pid=%s %s (%s)",
                            _rp.pid,
                            _rp.cmdline,
                            _rp.killed_with,
                        )
                with contextlib.suppress(Exception):
                    dispatch.cleanup_workspace(ws.root)


async def _rollout_loop_worker(run: Run, *, attribution: str | None = None) -> None:
    """Background task wrapper around :func:`_drive_rollout` with run bookkeeping.

    Owns the same slot/queue accounting ``_dispatch_worker`` does, because it
    occupies a run slot for as long as the rollout runs.
    """
    assert run.rollout_id is not None  # noqa: S101
    started = datetime.now(UTC).isoformat()
    try:
        await db.update_run(run.id, status=RunStatus.running, started_at=started)
        _publish_run_event(run.id, "running", None)
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
        _parked_rollout_loops.discard(run.id)
        _active_processes.pop(run.id, None)
        _try_start_pending()
        _run_event_queues.pop(run.id, None)
        _deregister_rollout_loop(run)
        logger.info(
            "rollout-loop: exit rollout_id=%s run_id=%s", run.rollout_id, run.id
        )


def _deregister_rollout_loop(run: Run) -> None:
    """Drop ``run``'s rollout from ``_rollout_loops`` if it still maps to ``run``.

    Only the owning run may remove the entry: once a loop has deregistered
    itself at its exit decision, a relaunched loop may already hold the slot.
    """
    if run.rollout_id and _rollout_loops.get(run.rollout_id) is run:
        del _rollout_loops[run.rollout_id]


def _live_rollout_driver(rollout_id: str) -> str | None:
    """Return the id of the MANAGE run already driving ``rollout_id`` here, if any.

    Covers both a live loop (``_rollout_loops``) and a MANAGE dispatch still
    waiting for a slot in ``_pending_queue``.
    """
    live = _rollout_loops.get(rollout_id)
    if live is not None:
        return live.id
    for pending in _pending_queue:
        if (
            pending.run.mode == DispatchMode.MANAGE
            and pending.run.rollout_id == rollout_id
        ):
            return pending.run.id
    return None


def _start_rollout_loop(run: Run, *, attribution: str | None = None) -> bool:
    """Register and start the background task that drives one rollout.

    THE single choke point for the one-loop-per-rollout guard: every caller
    (``/dispatch``, ``_try_start_pending``, ``_readopt_running_rollouts``)
    goes through here.  There is no await between the check and the
    registration, so two callers in the same event loop cannot both pass.
    The map entry is set BEFORE the task is created.  Returns False (and
    starts nothing) when the rollout already has a live loop on this host.
    """
    assert run.rollout_id is not None  # noqa: S101 — MANAGE runs carry one
    live = _rollout_loops.get(run.rollout_id)
    if live is not None and live is not run:
        logger.warning(
            "rollout-loop: refusing second loop for rollout_id=%s run_id=%s"
            " — already driven by run_id=%s",
            run.rollout_id,
            run.id,
            live.id,
        )
        return False
    _rollout_loops[run.rollout_id] = run
    task = asyncio.create_task(_rollout_loop_worker(run, attribution=attribution))
    _active_processes[run.id] = task
    return True


def _cancel_refused_loop_run(run: Run) -> None:
    """Mark a local MANAGE row ``cancelled`` after the choke point refused it.

    Synchronous callers (``_try_start_pending``) cannot await, so the DB write
    is scheduled as a task.
    """
    live = _rollout_loops.get(run.rollout_id or "")
    error = (
        f"rollout {run.rollout_id} is already driven by live loop run "
        f"{live.id if live else '?'} on this host"
    )
    _run_event_queues.pop(run.id, None)

    async def _write() -> None:
        try:
            await db.update_run(
                run.id,
                status=RunStatus.cancelled,
                completed_at=datetime.now(UTC).isoformat(),
                error=error,
            )
        except Exception:
            logger.exception("rollout-loop: failed to cancel refused run %s", run.id)

    task = asyncio.create_task(_write())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _readopt_running_rollouts() -> None:
    """Startup sweep: resume every running rollout from its persisted frontier.

    REQUIRED, not a follow-up.  The wave loop now lives INSIDE the dispatch
    service, whose own ``deploy.sh`` restarts it — so a routine deploy used to
    mean "the monitor lies" and would now mean "the wave silently stops
    advancing" without this.  Largely a relocation of the retired watchdog's
    scan shape: list running rollouts, and for each one that no live loop owns,
    start one.  The loop itself resumes from the persisted frontier, because
    every tick re-reads ``advance_rollout`` plus the in-flight query rather than
    trusting any in-process state.

    Double-dispatch safety comes from the GTD side: the wave linkage in
    ``dispatch_item`` is guarded by ``AND status = 'ready'``, so an item whose
    build is already in flight (status ``dispatched``) cannot be dispatched
    again — and such items are reported by ``advance_rollout`` as
    ``in_progress``, not ``next_ready``, so the resumed loop does not even try.

    THIS HOST'S ROLLOUTS ONLY.  ``list_running_rollouts`` is a FLEET-WIDE query:
    it returns every running rollout the lead can see, on any host.  The only
    thing that ever stopped this loop adopting another host's rollout was
    ``_rollout_loops``, which is in-process memory and therefore empty in
    exactly the situation this sweep runs in — process start.  So any host
    coming up would adopt every running rollout in the fleet, and since a
    manager owns the squash-merge and the ``git push origin main``, two managers
    on one rollout is two processes merging the same wave.  A routine
    ``deploy.sh`` restarts every host at once, so this fired fleet-wide on every
    deploy; it was caught when a brand-new fourth host came up and immediately
    adopted another project's in-flight rollout.

    The local ``runs`` table is the ownership record: it is SQLite on this
    host's disk, it survives a restart, and a row exists for a rollout only if
    THIS host was told to drive it (``dispatch_rollout`` writes one) or already
    was.  That is precisely the resume-after-my-own-restart case this sweep
    exists for, and it cannot express "steal someone else's".  A host whose
    database is wiped declines to resume rather than guessing, which is the
    correct direction to fail.
    """
    try:
        rollouts = await gtd_client.list_running_rollouts()
    except Exception:
        logger.exception("re-adoption sweep: failed to list running rollouts")
        return

    logger.info("re-adoption sweep: %d running rollout(s) fleet-wide", len(rollouts))
    for rollout in rollouts:
        rollout_id = str(rollout.get("id") or "")
        # Only a local MANAGE row proves this host drove the loop. Child build
        # runs carry the same rollout_id, so counting them let a host that had
        # merely built one item resume a loop still live on another host:
        # two loops on one rollout, one of which halted it (d79f2243).
        if rollout_id and not any(
            r.mode == DispatchMode.MANAGE
            for r in await db.list_runs_by_rollout(rollout_id)
        ):
            logger.info(
                "re-adoption sweep: skipping rollout_id=%s — no local loop run,"
                " so this host never drove it",
                rollout_id,
            )
            continue
        if not rollout_id or rollout_id in _rollout_loops:
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
        # A /dispatch may have started a loop for this rollout while the sweep
        # was awaiting insert_run; the choke point refuses the duplicate.
        if not _start_rollout_loop(run):
            _cancel_refused_loop_run(run)


async def _dispatch_worker(
    run: Run,
    max_turns: int,
    engine: Engine,
    timeout_seconds: int,
    *,
    attribution: str | None = None,
) -> None:
    """Background task that executes a dispatch run."""
    _run_start_dt: datetime = datetime.now(UTC)
    now = _run_start_dt.isoformat()
    await db.update_run(run.id, status=RunStatus.running, started_at=now)
    _publish_run_event(run.id, "running", None)

    if run.mode == DispatchMode.MANAGE and run.rollout_id:
        logger.info(
            "manage: spawn rollout_id=%s run_id=%s engine=%s",
            run.rollout_id,
            run.id,
            engine.name,
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
    # Per-repo remote branch a worker push fell back to (``<branch>-r<N>``)
    # because the item branch already held an older run's commits. Repos absent
    # here pushed (or will be rescued) to the dispatched branch name.
    _dispatched_branch = run.branch_name or ""
    _branch_by_repo: dict[str, str] = {}
    # For manage mode: preserve workspace on failure for debugging.
    # For build/plan mode: always clean up.
    should_cleanup = True
    _human_cancelled = False
    _exit_code: int | None = None
    # The run's private TMPDIR (TMPDIR/TMP/TEMP for every process it
    # launches).  Removed in the teardown `finally` on every path, regardless
    # of workspace retention.
    run_tmp_dir: Path | None = None

    try:
        # First, before anything that launches a process.  A create failure
        # fails the run through the generic handler — never fall back to /tmp.
        run_tmp_dir = run_tmp.create_run_tmp(run.id)
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
                run.workspace_path = str(workspace)
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
                run.workspace_path = str(workspace)
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
            run.workspace_path = str(workspace)
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
            run.workspace_path = str(workspace)
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
                        tmp_dir=run_tmp_dir,
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
            workspace_repo_dirs=workspace_repo_dirs,
            workspace=workspace,
            merge_notes=merge_notes,
            engine_name=engine_used.name,
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
                tmp_dir=run_tmp_dir,
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
            callback_token=run.callback_token,
            run_id=run.id,
            tmp_dir=run_tmp_dir,
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
                                _attempt = await _loop.run_in_executor(
                                    dispatch._executor,
                                    dispatch.push_unpushed_repo,
                                    _repo_path,
                                    _dispatched_branch,
                                    _remaining,
                                )
                                _fb = _fallback_branch_of(_attempt)
                                if _fb is not None:
                                    _branch_by_repo[_r.repo_name] = _fb
                            except Exception:
                                logger.exception(
                                    "push backstop: attempt failed for repo %s"
                                    " (run %s)",
                                    _r.repo_name,
                                    run.id,
                                )
                        # Re-classify after the backstop attempt(s), against
                        # the branch each repo was actually pushed to.
                        push_results_list = dispatch.verify_pushes(
                            _verify_repos,
                            _dispatched_branch,
                            _branch_by_repo or None,
                        )
                        if _branch_by_repo:
                            await _record_pushed_branch(run, _branch_by_repo)
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
                    #
                    # Split by TREE STATE, because zero-commits covers two
                    # opposite situations and they need opposite responses from
                    # the lead. A DIRTY tree means the agent was part-way through
                    # real work and stopped — the rescue path has its changes on a
                    # branch, go and look. A CLEAN tree means it wrote nothing,
                    # which is what a correct refusal looks like when an agent
                    # checks its acceptance criteria, finds the spec no longer
                    # matches the repo and declines to guess. Counting those
                    # together makes the fleet look less reliable than it is while
                    # hiding the real defect, which is a stale spec.
                    #
                    # This is a MECHANICAL split — `dirty` is observed, not
                    # inferred. Nothing here decides WHY the tree was clean; only
                    # the agent's own comments can say that, and the terminal text
                    # sends the reader to them rather than guessing.
                    _failure_prefix = (
                        "zero_commits_work_abandoned"
                        if any(r.dirty for r in push_results_list)
                        else "zero_commits"
                    )

                if _failure_prefix is not None:
                    error_str = f"{_failure_prefix}: " + (
                        "the agent ended its run without committing, leaving"
                        " changes in the working tree; the rescue path pushed them"
                        " to the run's branch"
                        if _failure_prefix == "zero_commits_work_abandoned"
                        else "the agent ended its run having produced no commits"
                        " and no working-tree changes — read its comments"
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
                        functools.partial(
                            dispatch.run_gate_command,
                            workspace,
                            _gate_cmd,
                            _gate_timeout,
                            engine_used,
                            _register_subprocess,
                            tmp_dir=run_tmp_dir,
                        ),
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
                    + (
                        f" to `{_branch_by_repo[r.repo_name]}` — `{_dispatched_branch}`"
                        " already held an older run's commits, which were left"
                        " untouched"
                        if r.repo_name in _branch_by_repo
                        else ""
                    )
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
        _timed_out_at = datetime.now(UTC).isoformat()
        _linger_success = False
        if _verify_repos is not None:
            # BUILD mode: agent process lingered past the timeout but may have
            # already pushed its work.  verify_pushes is fail-closed — any error
            # yields PushStatus.unpushed so it never misclassifies a real timeout.
            push_results_list = dispatch.verify_pushes(
                _verify_repos, _dispatched_branch, _branch_by_repo or None
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
            run,
            _evidence_repos,
            attribution=attribution,
            branch_by_repo={
                _n: _branch_by_repo.get(_n, _dispatched_branch)
                for _n, _p, _b in _evidence_repos
            },
        )
        if _rescued is not None and not _rescued.ok:
            # Rescue could not get the work to origin. The workspace is the only
            # copy left, so keep it and say so where an operator will see it.
            should_cleanup = False

        # Reap daemons the agent left running from this workspace (detached,
        # own session, so killing the agent tree missed them). After the rescue,
        # before deletion, and even when the workspace is kept.
        if workspace is not None:
            await _reap_leaked_processes(run, workspace)

        # Independent of should_cleanup: a retained workspace keeps the work,
        # never the run's temp files.
        if run_tmp_dir is not None:
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(run_tmp.remove_run_tmp, run_tmp_dir, kind="build"),
            )

        if workspace is not None and should_cleanup:
            dispatch.cleanup_workspace(workspace)


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, object]:
    """Return service health and active run count (parked rollout loops excluded)."""
    active = _counted_active_runs()
    return {"status": "ok", "active_runs": active}


@app.get("/info", response_model=InfoResponse)
async def info() -> InfoResponse:
    """Return engine identity, version, capacity, and capabilities. No auth required.

    The capacity fields (max_concurrent_runs, active_runs) and capability lists
    (engines, agents) let a multi-host router on the caller side filter and
    rank dispatch targets without a separate round trip to /agents.
    ``active_runs`` leaves out rollout loops parked behind a host drain; they
    are reported in ``parked_rollout_loops``.
    """
    agent_dicts = await run_list_agents_script()
    return InfoResponse(
        engine=ENGINE_NAME,
        version=SERVICE_VERSION,
        max_concurrent_runs=config.MAX_CONCURRENT_RUNS,
        active_runs=_counted_active_runs(),
        engines=get_available_engine_names(),
        agents=[a["name"] for a in agent_dicts],
        parked_rollout_loops=len(_parked_rollout_loops & _active_processes.keys()),
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

    # One loop per rollout per host: refuse a MANAGE dispatch for a rollout
    # this host is already driving (live loop or queued entry).  Checked
    # before db.insert_run so a refusal leaves no row; the choke point
    # (_start_rollout_loop) covers the insert_run await gap.
    if body.mode == DispatchMode.MANAGE and body.rollout_id:
        _driver = _live_rollout_driver(body.rollout_id)
        if _driver is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"rollout {body.rollout_id} is already driven by live loop "
                    f"run {_driver} on this host"
                ),
            )

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
        # completed build.  Nothing relaunches a manager any more — the loop
        # owns liveness and the re-adoption sweep resumes it after a restart.
        if not _start_rollout_loop(run, attribution=body.attribution):
            _run_event_queues.pop(run.id, None)
            _live = _rollout_loops.get(run.rollout_id)
            _msg = (
                f"rollout {run.rollout_id} is already driven by live loop run "
                f"{_live.id if _live else '?'} on this host"
            )
            await db.update_run(
                run.id,
                status=RunStatus.cancelled,
                completed_at=datetime.now(UTC).isoformat(),
                error=_msg,
            )
            raise HTTPException(status_code=409, detail=_msg)
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

    # Terminate the subprocess: SIGTERM, grace period, then SIGKILL, on the
    # whole session (see _terminate_run_session).
    proc = _active_subprocesses.get(run_id)
    if proc is not None:
        await _terminate_run_session(proc)

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
