"""Dispatch worker API — runs headless coding agents."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from . import config, db, dispatch, gtd_client, rollout_planner
from .agent_discovery import ENGINE_NAME, SERVICE_VERSION, run_list_agents_script
from .dispatch import _MANAGE_ALLOWED_TOOLS
from .engines import Engine, get_engine
from .models import (
    DispatchRequest,
    PlanRequest,
    RolloutPlan,
    Run,
    RunResponse,
    RunStatus,
)

logger = logging.getLogger(__name__)

# Track running subprocesses for cancellation
_active_processes: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

# Manage subprocess auto-recovery settings
MAX_MANAGE_RETRIES = 2
MANAGE_RETRY_BACKOFF_SECONDS = 30

# Frozenset of rollout statuses that indicate a clean/terminal manage exit
_CLEAN_EXIT_STATUSES: frozenset[str] = frozenset(
    {"completed", "halted", "cancelled", "crashed"}
)

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
    config.load()
    await db.init_db()
    orphan_count = await db.reconcile_orphans()
    if orphan_count > 0:
        logger.warning("Reconciled %d orphaned run(s) on startup", orphan_count)
    else:
        logger.info("No orphaned runs found on startup")
    yield
    # Cancel any active dispatch tasks on shutdown
    for task in _active_processes.values():
        task.cancel()


app = FastAPI(title="Agent GTD Dispatch", lifespan=lifespan)


# --- Background dispatch worker ---


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
    - If rollout is still running/pending (unexpected exit): increment retry count.
      - If count <= MAX_MANAGE_RETRIES: sleep and spawn a fresh _dispatch_worker.
      - If count > MAX_MANAGE_RETRIES: halt the rollout with reason
        'manage_relaunch_cap_exceeded'.
    """
    assert run.rollout_id is not None  # noqa: S101 — caller guarantees this
    try:
        rollout = await gtd_client.get_rollout(run.rollout_id)
    except Exception:
        logger.exception(
            "Failed to fetch rollout %s for relaunch check — skipping recovery",
            run.rollout_id,
        )
        return

    if rollout["status"] in _CLEAN_EXIT_STATUSES:
        return  # clean exit — nothing to do

    # Unexpected exit: rollout still running or pending
    try:
        updated = await gtd_client.relaunch_manage_rollout(run.rollout_id)
    except Exception:
        logger.exception(
            "Failed to increment manage_retry_count for rollout %s — skipping recovery",
            run.rollout_id,
        )
        return

    retry_count = int(updated["manage_retry_count"])

    if retry_count > MAX_MANAGE_RETRIES:
        logger.warning(
            "Manage retry cap exceeded for rollout %s (count=%d) — halting",
            run.rollout_id,
            retry_count,
        )
        try:
            await gtd_client.halt_rollout(
                run.rollout_id, reason="manage_relaunch_cap_exceeded"
            )
        except Exception:
            logger.exception(
                "Failed to halt rollout %s after cap exceeded", run.rollout_id
            )
        return

    logger.info(
        "Relaunching manage agent for rollout %s (retry %d/%d) after %ds",
        run.rollout_id,
        retry_count,
        MAX_MANAGE_RETRIES,
        MANAGE_RETRY_BACKOFF_SECONDS,
    )
    await asyncio.sleep(MANAGE_RETRY_BACKOFF_SECONDS)

    new_run = Run(
        item_id=run.item_id,
        project_name=run.project_name,
        mode="manage",
        rollout_id=run.rollout_id,
        engine=run.engine,
        agent_name=run.agent_name,
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

    mode = run.mode or "build"
    workspace = None
    # For manage mode: preserve workspace on failure for debugging.
    # For build/plan mode: always clean up.
    should_cleanup = True
    _human_cancelled = False

    try:
        # Fetch item and project.
        # manage-mode runs have item_id=None — derive project from the rollout instead.
        item: dict[str, Any] = {}
        if run.item_id is not None:
            item = await gtd_client.get_item(run.item_id)
            project_id = item.get("project_id")
            if not project_id:
                raise ValueError("Item has no project assigned")
            project = await gtd_client.get_project(project_id)
        else:
            assert run.rollout_id is not None  # noqa: S101 — guaranteed by route handler
            rollout_info = await gtd_client.get_rollout(run.rollout_id)
            project_id = rollout_info.get("project_id")
            if not project_id:
                raise ValueError("Rollout has no project assigned")
            project = await gtd_client.get_project(project_id)

        # Build workspace
        git_origin = project.get("git_origin", "")
        if not git_origin:
            raise ValueError(f"Project '{project['name']}' has no git_origin")

        if mode == "manage":
            # Clone the project's default branch for quality gates + git operations
            workspace = dispatch.prepare_manage_workspace(git_origin, run.id)
            await db.update_run(run.id, workspace_path=str(workspace))
            attachments = []
        else:
            # Prepare workspace (fresh clone on feature branch)
            if run.branch_name is None:  # pragma: no cover
                raise ValueError("branch_name must be set for non-manage mode runs")
            workspace = dispatch.prepare_workspace(git_origin, run.id, run.branch_name)
            await db.update_run(run.id, workspace_path=str(workspace))

            # Stage any attachments into {run_id}-attachments/ inside the workspace
            # item_id is guaranteed non-None for non-manage modes (validated at route layer)
            assert run.item_id is not None  # noqa: S101
            attachments = await dispatch.stage_attachments(
                workspace, run.id, run.item_id
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
        )

        item_title = item.get("title", f"rollout:{run.rollout_id}")
        if mode == "manage":
            dispatch_comment = (
                f"Rollout manager dispatched (run `{run.id}`, engine: {engine.name}). "
                f"Managing rollout `{run.rollout_id}` in `{project['name']}`."
            )
        else:
            dispatch_comment = (
                f"Agent dispatched (run `{run.id}`, engine: {engine.name}). "
                f"Working on branch `{run.branch_name}` in `{project['name']}`."
            )

        if run.item_id is not None:
            await gtd_client.post_comment(
                run.item_id,
                dispatch_comment,
                created_by=f"{engine.name}-dispatch",
            )

        agent_allowed_tools = list(_MANAGE_ALLOWED_TOOLS) if mode == "manage" else None
        result = await dispatch.run_agent(
            engine,
            workspace,
            system_prompt,
            item_title,
            max_turns,
            run.agent_name,
            timeout_seconds,
            allowed_tools=agent_allowed_tools,
            mode=mode,
            attribution=attribution,
        )

        completed = datetime.now(UTC).isoformat()
        if result.returncode == 0:
            await db.update_run(
                run.id,
                status=RunStatus.succeeded,
                completed_at=completed,
                exit_code=result.returncode,
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
            if mode == "manage":
                should_cleanup = False  # preserve workspace for debugging
            if error_msg and run.item_id is not None:
                await gtd_client.post_comment(
                    run.item_id,
                    f"Agent exited with code {result.returncode} (run `{run.id}`)."
                    f"\n\n```\n{error_msg}\n```",
                    created_by=f"{engine.name}-dispatch",
                )

    except subprocess.TimeoutExpired:
        await db.update_run(
            run.id,
            status=RunStatus.timed_out,
            completed_at=datetime.now(UTC).isoformat(),
            error=f"Timed out after {timeout_seconds}s",
        )
        if run.item_id is not None:
            await gtd_client.post_comment(
                run.item_id,
                f"Agent timed out after {timeout_seconds // 60} minutes (run `{run.id}`). "
                "The task may need to be broken down into smaller pieces.",
                created_by=f"{engine.name}-dispatch",
            )
        if mode == "manage":
            should_cleanup = False
    except asyncio.CancelledError:
        _human_cancelled = True
        await db.update_run(
            run.id,
            status=RunStatus.cancelled,
            completed_at=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        await db.update_run(
            run.id,
            status=RunStatus.failed,
            completed_at=datetime.now(UTC).isoformat(),
            error=str(exc)[:500],
        )
        if mode == "manage":
            should_cleanup = False
    finally:
        _active_processes.pop(run.id, None)
        if workspace is not None and should_cleanup:
            dispatch.cleanup_workspace(workspace)
        if run.mode == "manage" and run.rollout_id and not _human_cancelled:
            await _maybe_relaunch_manage(
                run, max_turns, engine, timeout_seconds, attribution
            )


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, object]:
    """Return service health and active run count."""
    active = len(_active_processes)
    return {"status": "ok", "active_runs": active}


@app.get("/info")
async def info() -> dict[str, str]:
    """Return engine identity and service version. No auth required."""
    return {"engine": ENGINE_NAME, "version": SERVICE_VERSION}


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
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/dispatch", response_model=RunResponse)
async def dispatch_item(
    body: DispatchRequest,
    _: str = Depends(_verify_api_key),
) -> RunResponse:
    """Start a new dispatch run for a GTD item."""
    try:
        engine = get_engine(body.engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Validate mode-specific requirements
    if body.mode == "manage" and not body.rollout_id:
        raise HTTPException(
            status_code=400,
            detail="rollout_id required for mode=manage",
        )
    if body.mode != "manage" and not body.item_id:
        raise HTTPException(
            status_code=400,
            detail=f"item_id required for mode={body.mode}",
        )

    if body.mode == "manage":
        # Derive project from rollout — item_id is None for manage-mode runs
        assert body.rollout_id is not None  # noqa: S101 — validated above
        try:
            rollout = await gtd_client.get_rollout(body.rollout_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Rollout not found") from None

        project_id = rollout.get("project_id")
        if not project_id:
            raise HTTPException(
                status_code=400, detail="Rollout has no project assigned"
            )

        try:
            project = await gtd_client.get_project(project_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Project not found") from None

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
            item = await gtd_client.get_item(body.item_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Item not found") from None

        project_id = item.get("project_id")
        if not project_id:
            raise HTTPException(status_code=400, detail="Item has no project assigned")

        try:
            project = await gtd_client.get_project(project_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Project not found") from None

        if not project.get("git_origin"):
            raise HTTPException(
                status_code=400,
                detail=f"Project '{project['name']}' has no git_origin configured",
            )

        branch_name = dispatch.branch_name_for_item(body.item_id, item["title"])
        item_id_for_run = body.item_id

    max_turns = body.max_turns
    if body.timeout_minutes:
        timeout_seconds = body.timeout_minutes * 60
    elif body.mode == "manage":
        timeout_seconds = config.MANAGE_TIMEOUT_SECONDS
    else:
        timeout_seconds = config.TIMEOUT_SECONDS

    run = Run(
        item_id=item_id_for_run,
        project_name=project["name"],
        branch_name=branch_name,
        engine=body.engine,
        agent_name=body.agent_name,
        mode=body.mode,
        rollout_id=body.rollout_id,
    )
    await db.insert_run(run)

    # Start background task
    task = asyncio.create_task(
        _dispatch_worker(
            run, max_turns, engine, timeout_seconds, attribution=body.attribution
        )
    )
    _active_processes[run.id] = task

    return RunResponse(**run.model_dump())


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
    """Cancel a running dispatch."""
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.running and run.status != RunStatus.pending:
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel run in status '{run.status.value}'"
        )

    task = _active_processes.get(run_id)
    if task:
        task.cancel()

    await db.update_run(
        run_id,
        status=RunStatus.cancelled,
        completed_at=datetime.now(UTC).isoformat(),
    )

    run = await db.get_run(run_id)
    if run is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run.model_dump())
