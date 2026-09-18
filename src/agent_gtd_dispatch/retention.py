"""Evidence capture and age-based pruning.

This module is deliberately VERDICT-FREE: nothing here reads, accepts or branches
on a run's outcome, its gate result or its push result.  Evidence is captured for
every terminal run without exception, because the runs whose evidence matters most
are exactly the ones whose outcome was recorded wrongly.

Two decay rates, two roots:

Evidence (``EVIDENCE_ROOT``) is small — a transcript, a JSON artifact and a diff —
and stays useful for months.

The workspace TREE (``WORKSPACE_ROOT``) is hundreds of megabytes to gigabytes of
clones plus build output, and its value decays within a day or two.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from . import config
from .dispatch import _sudo_wrap

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TRANSCRIPT_NAME: str = "transcript.txt"
ARTIFACT_NAME: str = "completion.json"
PATCH_NAME: str = "patch.diff"


def evidence_dir(run_id: str) -> Path:
    """Return the evidence directory for a run (not created)."""
    return config.EVIDENCE_ROOT / run_id


def _locate_artifact(workspace: Path) -> Path | None:
    """Find the agent's completion artifact under a workspace, if any."""
    primary = workspace / ".dispatch" / ARTIFACT_NAME
    if primary.exists():
        return primary
    try:
        hits = sorted(workspace.glob(f"*/.dispatch/{ARTIFACT_NAME}"))
    except OSError:
        return None
    if len(hits) == 1:
        return hits[0]
    return None


def _read_cross_user(path: Path) -> bytes | None:
    """Read a file that may be owned by the agent user."""
    try:
        result = subprocess.run(  # noqa: S603
            _sudo_wrap(["cat", str(path)]),
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def repo_diff(repo_path: Path, base_sha: str) -> str:
    """Return ``git diff <base_sha>..HEAD`` for a repo, cross-user.

    Raises ``RuntimeError`` when git refuses or the repo is unreadable; callers
    treat that as "no diff captured" and continue.
    """
    result = subprocess.run(  # noqa: S603
        _sudo_wrap(["git", "-C", str(repo_path), "diff", f"{base_sha}..HEAD"]),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace")[-200:]
        msg = f"git diff exited {result.returncode}: {tail}"
        raise RuntimeError(msg)
    return result.stdout.decode("utf-8", errors="replace")


def capture_evidence(
    run_id: str,
    workspace: Path | None,
    repos: list[tuple[str, Path, str | None]],
) -> Path:
    """Copy a run's durable evidence out of the workspace before teardown.

    ``repos`` is a list of ``(repo_name, repo_path, base_sha)``.  Callers that run
    before the base SHAs exist (timeout, cancellation, clone failure, generic
    exception) supply an empty list.

    Every step is individually best-effort: this function is called from teardown
    and must NEVER raise, because an exception escaping here would abort the
    caller's terminal database write.
    """
    target = evidence_dir(run_id)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("evidence capture FAILED: run_id=%s dir=%s", run_id, target)
        return target

    written: list[str] = []

    if workspace is not None:
        try:
            src = workspace / TRANSCRIPT_NAME
            if src.exists():
                shutil.copyfile(src, target / TRANSCRIPT_NAME)
                written.append(TRANSCRIPT_NAME)
        except Exception:
            logger.warning("evidence capture: transcript copy failed run_id=%s", run_id)

        try:
            artifact = _locate_artifact(workspace)
            if artifact is not None:
                raw = _read_cross_user(artifact)
                if raw is not None:
                    (target / ARTIFACT_NAME).write_bytes(raw)
                    written.append(ARTIFACT_NAME)
        except Exception:
            logger.warning("evidence capture: artifact copy failed run_id=%s", run_id)

    try:
        chunks: list[str] = []
        for repo_name, repo_path, base_sha in repos:
            chunks.append(f"# repo: {repo_name}\n")
            if base_sha is None:
                chunks.append("# no diff captured: no base sha recorded\n")
                continue
            try:
                chunks.append(repo_diff(repo_path, base_sha))
            except Exception as exc:  # best effort — record and keep going
                chunks.append(f"# no diff captured: {exc}\n")
        (target / PATCH_NAME).write_text("".join(chunks))
        written.append(PATCH_NAME)
    except Exception:
        logger.warning("evidence capture: diff capture failed run_id=%s", run_id)

    try:
        total = sum(p.stat().st_size for p in target.iterdir() if p.is_file())
    except Exception:
        total = 0
    logger.info(
        "evidence captured: run_id=%s dir=%s bytes=%d files=%s",
        run_id,
        target,
        total,
        ",".join(written),
    )
    return target


def _dir_size(path: Path) -> int:
    """Total size in bytes of every regular file under ``path``."""
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _remove_tree(path: Path) -> None:
    """Delete a directory tree, cross-user when an agent user is configured."""
    if config.AGENT_SUBPROCESS_USER:
        subprocess.run(  # noqa: S603
            _sudo_wrap(["rm", "-rf", str(path)]), check=False
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _age_seconds(path: Path, now: float) -> float:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def prune(active_run_ids: frozenset[str]) -> None:
    """Delete aged workspace trees and evidence directories.

    Pruning is AGE-BASED ONLY — never free-space-based, never outcome-based.

    A workspace directory is protected when its name ends with ``-<run_id>`` for a
    live run: workspace names come in three shapes (``{repo}-{run_id}``,
    ``ws-{run_id}`` and ``repos-{run_id}``), so a prefix or equality match would
    protect nothing.  Evidence directories are named for the run id exactly.
    """
    now = time.time()
    workspace_max_age = config.WORKSPACE_RETENTION_HOURS * 3600
    evidence_max_age = config.EVIDENCE_RETENTION_DAYS * 86400

    workspaces_pruned = 0
    evidence_pruned = 0
    evidence_kept = 0
    bytes_freed = 0

    root = config.WORKSPACE_ROOT
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if _age_seconds(child, now) <= workspace_max_age:
                continue
            live = next(
                (rid for rid in active_run_ids if child.name.endswith(f"-{rid}")),
                None,
            )
            if live is not None:
                logger.warning(
                    "retention: skipped active-run path %s (run_id=%s)", child, live
                )
                continue
            size = _dir_size(child)
            logger.info("retention: deleting workspace %s", child)
            _remove_tree(child)
            workspaces_pruned += 1
            bytes_freed += size

    eroot = config.EVIDENCE_ROOT
    if eroot.is_dir():
        for child in sorted(eroot.iterdir()):
            if not child.is_dir():
                continue
            if _age_seconds(child, now) <= evidence_max_age:
                evidence_kept += 1
                continue
            if child.name in active_run_ids:
                logger.warning(
                    "retention: skipped active-run path %s (run_id=%s)",
                    child,
                    child.name,
                )
                evidence_kept += 1
                continue
            size = _dir_size(child)
            logger.info("retention: deleting evidence %s", child)
            _remove_tree(child)
            evidence_pruned += 1
            bytes_freed += size

    logger.info(
        "retention: tick evidence_pruned=%d evidence_kept=%d"
        " workspaces_pruned=%d bytes_freed=%d",
        evidence_pruned,
        evidence_kept,
        workspaces_pruned,
        bytes_freed,
    )
