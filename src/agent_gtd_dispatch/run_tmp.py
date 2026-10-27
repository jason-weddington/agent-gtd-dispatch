"""Private per-run TMPDIR: creation, teardown removal and orphan sweeping.

Every process the worker launches for a run (build agent, post-run gate,
rollout reviewer, land gate, plan runs) gets ``TMPDIR``/``TMP``/``TEMP``
pointing at ``<RUN_TMP_ROOT>/rt-<key>``, created at run start and removed at
teardown on every path that reaches teardown.  Before this, nothing set
TMPDIR, so every tool fell back to the host's ``/tmp`` and nothing removed
what a run left there (a 16G tmpfs filled to 0 bytes on 2026-10-27).

The values reach the child THROUGH THE ARGV (``/usr/bin/env TMPDIR=...``),
placed INSIDE the sudo wrap: sudo-rs strips TMPDIR even with
``--preserve-env=TMPDIR``, so neither the subprocess env dict nor sudoers
``env_keep`` can carry it.

This module must NOT import ``.dispatch`` or ``.retention`` at module level:
dispatch imports this module, and retention imports dispatch, so a
module-level import would be circular.  The helpers needed are imported
inside the function bodies instead.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Literal

from . import config

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TMP_ENV_KEYS: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")
RUN_TMP_KEY_RE = re.compile(r"^(?:[0-9a-f]{12}|land-[0-9a-f]{12}-[0-9A-Za-z-]{1,64})$")
RUN_TMP_NAME_RE = re.compile(
    r"^rt-(?:[0-9a-f]{12}|land-[0-9a-f]{12}-[0-9A-Za-z-]{1,64})$"
)

RunTmpKind = Literal["build", "reviewer", "land", "orphan"]

# Registry of run tmp dirs that belong to a run still in flight.  The orphan
# sweep never touches these.  Every add, discard and snapshot is under _LOCK
# because creation/removal run on executor threads.
_LIVE: set[Path] = set()
_LOCK = threading.Lock()


def with_tmp_env(cmd: list[str], tmp_dir: Path | None) -> list[str]:
    """Prefix *cmd* with ``/usr/bin/env TMPDIR=.. TMP=.. TEMP=..``.

    ``tmp_dir=None`` returns a copy of *cmd* unchanged.  Callers wrap the
    result in ``_sudo_wrap`` so the env prefix runs AFTER sudo's env filter.
    """
    if tmp_dir is None:
        return list(cmd)
    return [
        "/usr/bin/env",
        *(f"{key}={tmp_dir}" for key in TMP_ENV_KEYS),
        *cmd,
    ]


def live_paths() -> frozenset[Path]:
    """Snapshot of the run tmp dirs currently owned by in-flight runs."""
    with _LOCK:
        return frozenset(_LIVE)


def create_run_tmp(key: str) -> Path:
    """Create ``<RUN_TMP_ROOT>/rt-<key>`` and register it as live.

    Raises:
        ValueError: *key* is not a run id or land key.
        Exception: whatever the creation raised; the path is unregistered.
    """
    from .dispatch import _sudo_wrap

    if not RUN_TMP_KEY_RE.fullmatch(key):
        msg = f"invalid run tmp key {key!r}"
        raise ValueError(msg)
    path = config.RUN_TMP_ROOT / f"rt-{key}"
    # Register BEFORE creating so a concurrent orphan sweep can never see the
    # fresh directory as unowned.
    with _LOCK:
        _LIVE.add(path)
    two_user = bool(config.AGENT_SUBPROCESS_USER)
    try:
        if two_user:
            subprocess.run(_sudo_wrap(["mkdir", "-p", str(path)]), check=True)  # noqa: S603
        else:
            path.mkdir(parents=True, exist_ok=True)
    except BaseException:
        with _LOCK:
            _LIVE.discard(path)
        raise
    logger.info(
        "run-tmp: created key=%s path=%s mode=%s",
        key,
        path,
        "two-user" if two_user else "single-user",
    )
    return path


def _is_removable(path: Path) -> bool:
    root = config.RUN_TMP_ROOT
    return (
        root in path.parents
        and path.parent == root
        and RUN_TMP_NAME_RE.fullmatch(path.name) is not None
    )


def remove_run_tmp(path: Path, *, kind: RunTmpKind) -> None:
    """Remove a run tmp dir and unregister it.  Never raises."""
    try:
        _remove_run_tmp(path, kind=kind)
    except Exception:
        logger.warning("run-tmp: removal of %s raised", path, exc_info=True)
    finally:
        with _LOCK:
            _LIVE.discard(path)


def _remove_run_tmp(path: Path, *, kind: RunTmpKind) -> None:
    from .dispatch import _sudo_wrap
    from .retention import _dir_size

    if not _is_removable(path):
        logger.warning(
            "run-tmp: refusing to remove %s (not an rt- child of %s)",
            path,
            config.RUN_TMP_ROOT,
        )
        return
    key = path.name.removeprefix("rt-")
    size = _dir_size(path)
    try:
        age_s = int(time.time() - path.stat().st_mtime)
    except OSError:
        age_s = 0
    rm_rc: int | None = None
    if config.AGENT_SUBPROCESS_USER:
        proc = subprocess.run(_sudo_wrap(["rm", "-rf", str(path)]), check=False)  # noqa: S603
        rm_rc = proc.returncode
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    else:
        shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        logger.error(
            "run-tmp: removal FAILED key=%s rm_rc=%s residual_bytes=%d",
            key,
            rm_rc,
            _dir_size(path),
        )
        return
    logger.info(
        "run-tmp: removed key=%s kind=%s bytes=%d age_s=%d", key, kind, size, age_s
    )


def sweep_orphans(
    live: frozenset[Path],
    min_age_seconds: int,
    *,
    context: Literal["startup", "periodic"],
) -> int:
    """Remove ``rt-`` dirs under RUN_TMP_ROOT that no in-flight run owns.

    Only names matching :data:`RUN_TMP_NAME_RE` are ever touched; dirs in
    *live* and dirs younger than *min_age_seconds* are kept.

    Returns:
        Number of orphan directories removed.
    """
    root = config.RUN_TMP_ROOT
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    for child in sorted(root.iterdir()):
        try:
            if not child.is_dir():
                continue
            if not RUN_TMP_NAME_RE.fullmatch(child.name):
                logger.debug("run-tmp: sweep skipping non-rt entry %s", child)
                continue
            if child in live:
                continue
            age_s = int(now - child.stat().st_mtime)
        except OSError:
            continue
        if age_s < min_age_seconds:
            continue
        remove_run_tmp(child, kind="orphan")
        removed += 1
        if context == "periodic":
            logger.warning(
                "run-tmp: orphan swept (teardown missed it) key=%s age_s=%d",
                child.name.removeprefix("rt-"),
                age_s,
            )
    if context == "periodic":
        logger.info("run-tmp: tick orphans_swept=%d live=%d", removed, len(live))
    else:
        logger.info("run-tmp: startup sweep removed=%d", removed)
    return removed
