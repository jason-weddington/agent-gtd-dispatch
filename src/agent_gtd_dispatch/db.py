"""SQLite storage for dispatch runs."""

from __future__ import annotations

from datetime import UTC

import aiosqlite

from . import config
from .models import Run, RunStatus

_DB_PATH = "dispatch.db"


def db_path() -> str:
    """Return the absolute path to the SQLite database."""
    return str(config.WORKSPACE_ROOT / _DB_PATH)


async def init_db() -> None:
    """Create the runs table if it doesn't exist."""
    async with aiosqlite.connect(db_path()) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                project_name TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                exit_code INTEGER,
                error TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()
        await _migrate_db(db)


async def _migrate_db(db: aiosqlite.Connection) -> None:
    """Add columns introduced after v1.1.0."""
    cursor = await db.execute("PRAGMA table_info(runs)")
    existing = {row[1] for row in await cursor.fetchall()}

    if "engine" not in existing:
        await db.execute(
            "ALTER TABLE runs ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'"
        )
    if "agent_name" not in existing:
        await db.execute("ALTER TABLE runs ADD COLUMN agent_name TEXT")
    if "mode" not in existing:
        await db.execute(
            "ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'build'"
        )
    await db.commit()


async def reconcile_orphans() -> int:
    """Mark any runs stuck in pending/running as failed (service restart).

    Returns the number of rows updated.
    """
    async with aiosqlite.connect(db_path()) as db:
        cursor = await db.execute(
            "UPDATE runs SET status = 'failed',"
            " error = 'Service restarted while run was active'"
            " WHERE status IN ('pending', 'running')"
        )
        await db.commit()
        return cursor.rowcount


async def insert_run(run: Run) -> None:
    """Insert a new run into the database."""
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(
            """INSERT INTO runs
               (id, item_id, project_name, branch_name, engine, agent_name,
                mode, status, started_at, completed_at, exit_code, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.id,
                run.item_id,
                run.project_name,
                run.branch_name,
                run.engine,
                run.agent_name,
                run.mode,
                run.status.value,
                run.started_at.isoformat() if run.started_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.exit_code,
                run.error,
                run.created_at.isoformat(),
            ),
        )
        await db.commit()


async def update_run(
    run_id: str,
    *,
    status: RunStatus | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
) -> None:
    """Update fields on an existing run."""
    parts: list[str] = []
    values: list[object] = []
    if status is not None:
        parts.append("status = ?")
        values.append(status.value)
    if started_at is not None:
        parts.append("started_at = ?")
        values.append(started_at)
    if completed_at is not None:
        parts.append("completed_at = ?")
        values.append(completed_at)
    if exit_code is not None:
        parts.append("exit_code = ?")
        values.append(exit_code)
    if error is not None:
        parts.append("error = ?")
        values.append(error)

    if not parts:
        return

    values.append(run_id)
    sql = f"UPDATE runs SET {', '.join(parts)} WHERE id = ?"  # noqa: S608
    async with aiosqlite.connect(db_path()) as db:
        await db.execute(sql, values)
        await db.commit()


async def get_run(run_id: str) -> Run | None:
    """Fetch a single run by ID, or None if not found."""
    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_run(row)


async def list_runs(
    item_id: str | None = None,
    status: RunStatus | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs with optional filtering."""
    clauses: list[str] = []
    values: list[object] = []
    if item_id:
        clauses.append("item_id = ?")
        values.append(item_id)
    if status:
        clauses.append("status = ?")
        values.append(status.value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ?"  # noqa: S608
    values.append(limit)

    async with aiosqlite.connect(db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, values) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_run(r) for r in rows]


def _row_to_run(row: aiosqlite.Row) -> Run:
    from datetime import datetime

    def _parse_dt(val: str | None) -> datetime | None:
        if val is None:
            return None
        return datetime.fromisoformat(val).replace(tzinfo=UTC)

    return Run(
        id=row["id"],
        item_id=row["item_id"],
        project_name=row["project_name"],
        branch_name=row["branch_name"],
        engine=row["engine"],
        agent_name=row["agent_name"],
        status=RunStatus(row["status"]),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        exit_code=row["exit_code"],
        error=row["error"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
    )
