"""Portable (psutil) process API for hosts without a usable Linux ``/proc``.

The reaper and session-probe code in :mod:`agent_gtd_dispatch.dispatch` runs
its original /proc implementation when ``platform_support.USE_PROCFS`` is
True, and delegates here otherwise (macOS).  psutil is imported LAZILY through
:func:`_psutil` so a missing psutil can never break the dispatch import.

Error contract, two scopes:

* POINT-LOOKUP helpers (:func:`cmdline`, :func:`is_live`,
  :func:`ancestor_pids`, :func:`real_uid`, :func:`run_marker`, :func:`ppid`)
  never raise: any per-process psutil error, ``OSError`` or a psutil import
  failure yields the documented fallback.
* SCAN helpers (:func:`scan_workspace_pids`, :func:`scan_marker`) skip a
  process whose lookups fail, but let WHOLE-SCAN failures (psutil import,
  ``process_iter`` itself raising) propagate, so dispatch's ``except
  Exception`` turns them into ``scan=error`` rather than a silent empty scan.

Never-signal guarantees (another run's process, a non-agent-uid process) are
enforced by the callers in dispatch.py exactly as on the /proc branch; these
helpers only report.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import ModuleType

logger = logging.getLogger(__name__)

RUN_MARKER_ENV = "AGENT_GTD_RUN_KEY"
# Mirrors dispatch._REAP_CMDLINE_MAX_CHARS (asserted equal by the tests);
# importing dispatch here would be circular.
CMDLINE_MAX_CHARS = 200


def _psutil() -> ModuleType:
    """Import psutil on first use (a missing psutil must not break imports)."""
    import psutil

    return psutil


class ScanResult(NamedTuple):
    """One portable scan: matches plus how many same-uid processes it could read."""

    pids: dict[int, str]
    examined: int
    unreadable: int


def cmdline(pid: int) -> str:
    """Space-joined command line of ``pid`` (capped), or '' when unreadable."""
    try:
        ps = _psutil()
        parts = ps.Process(pid).cmdline()
        text = " ".join(str(p) for p in parts).replace("\n", " ").strip()
    except Exception:
        return ""
    return text[:CMDLINE_MAX_CHARS]


def _status_live(ps: Any, p: Any) -> bool:
    return p.status() not in (ps.STATUS_ZOMBIE, ps.STATUS_DEAD)


def is_live(pid: int) -> bool:
    """True when ``pid`` exists and is neither a zombie nor dead."""
    try:
        ps = _psutil()
        return bool(_status_live(ps, ps.Process(pid)))
    except Exception:
        return False


def ppid(pid: int) -> int:
    """Parent pid of ``pid``; -1 on any error."""
    try:
        ps = _psutil()
        return int(ps.Process(pid).ppid())
    except Exception:
        return -1


def ancestor_pids() -> set[int]:
    """This process plus its ancestor chain (stops at pid<=1 or a repeat)."""
    pids = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        pid = ppid(pid)
    return pids


def real_uid(pid: int) -> int | None:
    """Real uid of ``pid``; None when unreadable."""
    try:
        ps = _psutil()
        return int(ps.Process(pid).uids().real)
    except Exception:
        return None


def run_marker(pid: int) -> str:
    """``pid``'s ``AGENT_GTD_RUN_KEY`` environ value; '' when absent or unreadable."""
    try:
        ps = _psutil()
        env = ps.Process(pid).environ()
        return str(env.get(RUN_MARKER_ENV, ""))
    except Exception:
        return ""


def _read(ps: Any, fn: Callable[[], Any]) -> tuple[str, Any]:
    """Call ``fn``: ('ok', value) | ('gone', None) | ('unreadable', None)."""
    try:
        return "ok", fn()
    except ps.ZombieProcess:
        return "unreadable", None
    except ps.NoSuchProcess:
        return "gone", None
    except (ps.AccessDenied, OSError):
        return "unreadable", None
    except ps.Error:
        return "unreadable", None


def scan_workspace_pids(workspace: Path) -> ScanResult:
    """Processes whose resolved exe or cwd lies under ``workspace``.

    The workspace is resolved first (macOS ``/tmp`` and ``/var`` are symlinks
    into ``/private``), then matched by PATH COMPONENT (``is_relative_to``),
    never by string prefix: ``/ws/repos-a`` does not match ``/ws/repos-ab``.
    ``examined``/``unreadable`` count same-uid processes only.
    """
    ps = _psutil()
    root = Path(os.path.realpath(workspace))
    uid = os.getuid()
    found: dict[int, str] = {}
    examined = 0
    unreadable = 0
    for p in ps.process_iter():
        state, uids = _read(ps, p.uids)
        if state != "ok":
            continue
        same_uid = uids.real == uid
        hit = False
        failed = False
        gone = False
        for getter in (p.exe, p.cwd):
            state, target = _read(ps, getter)
            if state == "gone":
                gone = True
                break
            if state == "unreadable":
                failed = True
                continue
            if target and Path(os.path.realpath(target)).is_relative_to(root):
                hit = True
                break
        if gone:
            continue
        if same_uid:
            examined += 1
            if failed and not hit:
                unreadable += 1
        if hit:
            found[int(p.pid)] = cmdline(int(p.pid))
    return ScanResult(found, examined, unreadable)


def scan_marker(marker: str, uid: int) -> ScanResult:
    """Processes of ``uid`` whose ``AGENT_GTD_RUN_KEY`` is EXACTLY ``marker``.

    ``'rt-abc'`` never matches ``'rt-abcd'`` or ``'rt-ab'``.  A process whose
    environ is unreadable is never matched; it counts toward ``unreadable``.
    """
    ps = _psutil()
    found: dict[int, str] = {}
    examined = 0
    unreadable = 0
    for p in ps.process_iter():
        state, uids = _read(ps, p.uids)
        if state != "ok" or uids.real != uid:
            continue
        state, env = _read(ps, p.environ)
        if state == "gone":
            continue
        examined += 1
        if state == "unreadable":
            unreadable += 1
            continue
        if env.get(RUN_MARKER_ENV) == marker:
            found[int(p.pid)] = cmdline(int(p.pid))
    return ScanResult(found, examined, unreadable)


# ---------------------------------------------------------------------------
# Session snapshots (the portable replacement for ``pkill -s <sid>``)
# ---------------------------------------------------------------------------


@dataclass
class SessionSnapshot:
    """Members of one run session, captured on the first probe or signal.

    Keyed by the leader's pid in :data:`_SESSIONS`.  psutil.Process objects
    remember their create_time, so a reused pid reads as gone rather than as a
    member.
    """

    owner: int
    leader_pid: int
    members: dict[int, Any] = field(default_factory=dict)
    descendants: int = 0
    dropped_foreign_uid: int = 0


_SESSIONS: dict[int, SessionSnapshot] = {}
_SESSIONS_LOCK = threading.Lock()


def member_live(p: Any) -> bool:
    """True when ``p`` is still the same process and not a zombie."""
    try:
        ps = _psutil()
        return bool(p.is_running()) and bool(_status_live(ps, p))
    except Exception:
        return False


def _add_members(ps: Any, snap: SessionSnapshot, procs: Iterable[Any]) -> None:
    uid = os.getuid()
    for c in procs:
        existing = snap.members.get(int(c.pid))
        if existing is not None and member_live(existing):
            continue
        if not member_live(c):
            continue
        state, uids = _read(ps, c.uids)
        if state == "gone":
            continue
        if state != "ok" or uids.real != uid:
            snap.dropped_foreign_uid += 1
            continue
        snap.members[int(c.pid)] = c


def _capture(
    ps: Any, leader_pid: int, owner: int, leader_reaped: bool
) -> SessionSnapshot:
    snap = SessionSnapshot(owner=owner, leader_pid=leader_pid)
    if leader_reaped:
        # The pid may already belong to someone else: the member set is empty
        # and the caller's marker sweep is the net.
        return snap
    try:
        leader = ps.Process(leader_pid)
    except ps.NoSuchProcess:
        return snap
    if member_live(leader):
        snap.members[leader_pid] = leader
    try:
        children = leader.children(recursive=True)
    except ps.NoSuchProcess:
        # Includes ZombieProcess: an exited leader's children were reparented.
        children = []
    _add_members(ps, snap, children)
    snap.descendants = len([p for p in snap.members if p != leader_pid])
    return snap


def session_snapshot(
    leader_pid: int, *, owner: int, leader_reaped: bool
) -> SessionSnapshot:
    """Return the session's snapshot, capturing it on first use, refreshed.

    A refresh adds newly found descendants of still-live members.  Raises on
    psutil errors other than NoSuchProcess on the leader (the caller maps that
    to a probe error and sends no signal).
    """
    ps = _psutil()
    with _SESSIONS_LOCK:
        snap = _SESSIONS.get(leader_pid)
        if snap is not None and snap.owner == owner:
            fresh = False
        else:
            snap = _capture(ps, leader_pid, owner, leader_reaped)
            _SESSIONS[leader_pid] = snap
            fresh = True
    if not fresh:
        for m in list(snap.members.values()):
            if not member_live(m):
                continue
            state, kids = _read(ps, functools.partial(m.children, recursive=True))
            if state == "ok":
                _add_members(ps, snap, kids)
    return snap


def live_members(snap: SessionSnapshot) -> list[Any]:
    """The snapshot's members that are still running."""
    return [p for p in snap.members.values() if member_live(p)]


def signal_members(snap: SessionSnapshot, sig: int) -> int:
    """Signal live descendants, then the leader; returns how many were signalled.

    Uses ``Process.send_signal`` (which refuses a reused pid), never a raw
    ``os.kill`` by number.  Errors are logged and swallowed.
    """
    members = live_members(snap)
    ordered = [p for p in members if int(p.pid) != snap.leader_pid] + [
        p for p in members if int(p.pid) == snap.leader_pid
    ]
    sent = 0
    for p in ordered:
        try:
            p.send_signal(sig)
            sent += 1
        except (ProcessLookupError, PermissionError) as exc:
            logger.info("session-signal: pid=%s sig=%s skipped: %s", p.pid, sig, exc)
        except Exception as exc:
            logger.info(
                "session-signal: pid=%s sig=%s skipped: %s",
                p.pid,
                sig,
                type(exc).__name__,
            )
    return sent


def drop_session(leader_pid: int) -> None:
    """Forget the session snapshot for ``leader_pid`` (no-op when absent)."""
    with _SESSIONS_LOCK:
        _SESSIONS.pop(leader_pid, None)
