"""Dispatch worker API — runs headless Claude Code agents."""

from __future__ import annotations

import asyncio
import signal
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config, db, dispatch, gtd_client
from .models import DispatchRequest, Run, RunResponse, RunStatus

# Track running subprocesses for cancellation
_active_processes: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

security = HTTPBearer()


def _verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    if credentials.credentials != config.DISPATCH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    config.load()
    await db.init_db()
    yield
    # Cancel any active dispatch tasks on shutdown
    for task in _active_processes.values():
        task.cancel()


app = FastAPI(title="Agent GTD Dispatch", lifespan=lifespan)


# --- Background dispatch worker ---


async def _dispatch_worker(run: Run, max_turns: int) -> None:
    """Background task that executes a dispatch run."""
    now = datetime.now(timezone.utc).isoformat()
    await db.update_run(run.id, status=RunStatus.running, started_at=now)

    try:
        # Fetch item and project
        item = await gtd_client.get_item(run.item_id)
        project_id = item.get("project_id")
        if not project_id:
            raise ValueError("Item has no project assigned")

        project = await gtd_client.get_project(project_id)
        git_origin = project.get("git_origin", "")
        if not git_origin:
            raise ValueError(f"Project '{project['name']}' has no git_origin")

        # Prepare workspace
        workspace = dispatch.prepare_workspace(git_origin, run.item_id)

        # Build prompt and run
        system_prompt = dispatch.build_system_prompt(
            item, project, run.branch_name, max_turns
        )

        await gtd_client.post_comment(
            run.item_id,
            f"Agent dispatched (run `{run.id}`). "
            f"Working on branch `{run.branch_name}` in `{project['name']}`.",
        )

        result = await dispatch.run_claude(
            workspace, system_prompt, item["title"], max_turns
        )

        completed = datetime.now(timezone.utc).isoformat()
        if result.returncode == 0:
            await db.update_run(
                run.id,
                status=RunStatus.succeeded,
                completed_at=completed,
                exit_code=result.returncode,
            )
        else:
            # Capture stderr, falling back to stdout tail for diagnostics
            error_msg = result.stderr[-500:] if result.stderr else None
            if not error_msg and result.stdout:
                error_msg = result.stdout[-500:]
            await db.update_run(
                run.id,
                status=RunStatus.failed,
                completed_at=completed,
                exit_code=result.returncode,
                error=error_msg,
            )
            if error_msg:
                await gtd_client.post_comment(
                    run.item_id,
                    f"Agent exited with code {result.returncode} (run `{run.id}`)."
                    f"\n\n```\n{error_msg}\n```",
                )

    except subprocess.TimeoutExpired:
        await db.update_run(
            run.id,
            status=RunStatus.timed_out,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=f"Timed out after {config.TIMEOUT_SECONDS}s",
        )
        await gtd_client.post_comment(
            run.item_id,
            f"Agent timed out after {config.TIMEOUT_SECONDS // 60} minutes (run `{run.id}`). "
            "The task may need to be broken down into smaller pieces.",
        )
    except asyncio.CancelledError:
        await db.update_run(
            run.id,
            status=RunStatus.cancelled,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        await db.update_run(
            run.id,
            status=RunStatus.failed,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc)[:500],
        )
    finally:
        _active_processes.pop(run.id, None)


# --- Endpoints ---


@app.get("/health")
async def health() -> dict:
    active = len(_active_processes)
    return {"status": "ok", "active_runs": active}


@app.post("/dispatch", response_model=RunResponse)
async def dispatch_item(
    body: DispatchRequest,
    _: str = Depends(_verify_api_key),
) -> RunResponse:
    """Start a new dispatch run for a GTD item."""
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
    max_turns = body.max_turns or config.MAX_TURNS

    run = Run(
        item_id=body.item_id,
        project_name=project["name"],
        branch_name=branch_name,
    )
    await db.insert_run(run)

    # Start background task
    task = asyncio.create_task(_dispatch_worker(run, max_turns))
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
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    run = await db.get_run(run_id)
    assert run is not None
    return RunResponse(**run.model_dump())
