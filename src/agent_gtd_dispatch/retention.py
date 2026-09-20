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
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime
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


def _throwaway_index_path(repo_path: Path) -> Path:
    """A unique, never-reused path for a scratch git index inside ``repo_path``.

    Lives under ``.git/`` so it is owned by whichever user owns the clone (the
    same user every ``_git_with_index`` call below runs as), and is removed by
    the caller once done. It is a NEW file, never ``.git/index`` — the real
    index is never touched.
    """
    return repo_path / ".git" / f"evidence-index-{secrets.token_hex(8)}"


def _git_with_index(
    repo_path: Path, index_file: Path, args: list[str]
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git -C repo_path <args>`` against a throwaway ``GIT_INDEX_FILE``.

    The index path is given as an argument to a ``bash -c`` wrapper rather than
    via ``subprocess.run(env=...)`` because when cross-user (``_sudo_wrap``)
    sudo resets the environment and ``GIT_INDEX_FILE`` is not in this project's
    sudoers ``env_keep`` allowlist — and adding it there would need a host
    redeploy this fix cannot assume. ``bash`` is already NOPASSWD-authorised
    for the agent user, so setting the var *inside* the sudo'd process needs no
    sudoers change.
    """
    script = 'export GIT_INDEX_FILE="$1"; shift; exec "$@"'
    inner = ["git", "-C", str(repo_path), *args]
    cmd = ["bash", "-c", script, "bash", str(index_file), *inner]
    return subprocess.run(_sudo_wrap(cmd), capture_output=True, check=False)  # noqa: S603


def repo_diff(repo_path: Path, base_sha: str) -> str:
    """Return the full diff of a repo against ``base_sha``, cross-user.

    Covers everything ``base_sha`` does not have: committed changes up to
    ``HEAD``, uncommitted modifications to tracked files, AND newly created
    untracked files. A plain ``git diff <base>..HEAD`` only sees the commit
    graph, which is empty by design for engines (talos) that never commit —
    exactly the runs whose evidence matters most.

    Achieved via a throwaway index (``GIT_INDEX_FILE`` pointed at a scratch
    file, never ``.git/index``): ``git add -A`` stages the CURRENT working
    tree — tracked, modified and untracked files alike — into that scratch
    index, then ``git diff --cached base_sha`` compares it to the base tree.
    The repo's real index is never read or written.

    Raises ``RuntimeError`` when git refuses or the repo is unreadable; callers
    treat that as "no diff captured" and continue.
    """
    index_file = _throwaway_index_path(repo_path)
    try:
        add_result = _git_with_index(repo_path, index_file, ["add", "-A"])
        if add_result.returncode != 0:
            tail = add_result.stderr.decode("utf-8", errors="replace")[-200:]
            msg = f"git add exited {add_result.returncode}: {tail}"
            raise RuntimeError(msg)

        diff_result = _git_with_index(
            repo_path, index_file, ["diff", "--cached", base_sha]
        )
        if diff_result.returncode != 0:
            tail = diff_result.stderr.decode("utf-8", errors="replace")[-200:]
            msg = f"git diff exited {diff_result.returncode}: {tail}"
            raise RuntimeError(msg)
        return diff_result.stdout.decode("utf-8", errors="replace")
    finally:
        subprocess.run(  # noqa: S603
            _sudo_wrap(["rm", "-f", str(index_file)]), capture_output=True, check=False
        )


def _patch_header(run_id: str, agent_gtd_run_id: str | None) -> str:
    """Build the self-identifying header prepended to every captured patch.

    Stamps both run identifiers — the dispatch run id (always known here) and
    the agent_gtd run id (known only when a future caller plumbs it through;
    the dispatch service has no field for it today and none is added by this
    change) — plus the capture time in ISO-8601 UTC, since evidence directory
    mtimes are host-local while run records are UTC and the skew makes a
    correct candidate look wrong.
    """
    captured_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    agent_gtd_line = (
        agent_gtd_run_id
        if agent_gtd_run_id
        else "unknown (not available to the dispatch worker at capture time)"
    )
    return (
        f"# dispatch_run_id: {run_id}\n"
        f"# agent_gtd_run_id: {agent_gtd_line}\n"
        f"# captured_at: {captured_at}\n"
    )


def capture_evidence(
    run_id: str,
    workspace: Path | None,
    repos: list[tuple[str, Path, str | None]],
    agent_gtd_run_id: str | None = None,
) -> Path:
    """Copy a run's durable evidence out of the workspace before teardown.

    ``repos`` is a list of ``(repo_name, repo_path, base_sha)``.  Callers that run
    before the base SHAs exist (timeout, cancellation, clone failure, generic
    exception) supply an empty list.

    ``agent_gtd_run_id`` is stamped into the patch header when the caller has
    it; today no caller does (the dispatch service has no plumbing back to the
    agent_gtd-side run id and none is added here — see the header docstring),
    so it defaults to ``None`` and the header says so explicitly.

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
        chunks: list[str] = [_patch_header(run_id, agent_gtd_run_id)]
        for repo_name, repo_path, base_sha in repos:
            chunks.append(f"# repo: {repo_name}\n")
            if base_sha is None:
                chunks.append("# no diff captured: no base sha recorded\n")
                continue
            try:
                diff_text = repo_diff(repo_path, base_sha)
                chunks.append(diff_text)
                diff_bytes = len(diff_text.encode("utf-8"))
                if not diff_text.strip():
                    logger.warning(
                        "evidence capture: essentially empty patch "
                        "run_id=%s repo=%s bytes=%d",
                        run_id,
                        repo_name,
                        diff_bytes,
                    )
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
