"""Platform seam for run-lifecycle safety: procfs vs the portable (psutil) path.

Selection is by CAPABILITY, never by ``uname``: a host whose ``/proc`` exposes
``environ`` runs the original /proc code (Linux, byte-identical); every other
host runs :mod:`agent_gtd_dispatch.portable_procs`.  Callers read
``platform_support.USE_PROCFS`` as a module attribute at CALL time so tests can
monkeypatch it.
"""

from __future__ import annotations

import functools
import logging
import shutil
import sys
from pathlib import Path
from typing import Literal

from . import config

logger = logging.getLogger(__name__)

SessionBackend = Literal["pkill", "portable"]

TWO_USER_UNSUPPORTED_MESSAGE: str = (
    "two-user mode (DISPATCH_AGENT_SUBPROCESS_USER) requires Linux /proc: the "
    "sudo-run process scan and kill scripts read /proc and would silently reap "
    "nothing on this host. Run in single-user mode under a dedicated account "
    "instead (unset DISPATCH_AGENT_SUBPROCESS_USER); see docs/install.md "
    "(Running on macOS)."
)


def procfs_usable() -> bool:
    """True iff this is Linux and ``/proc/self/environ`` exists."""
    return sys.platform.startswith("linux") and Path("/proc/self/environ").exists()


USE_PROCFS: bool = procfs_usable()


def session_backend() -> SessionBackend:
    """``'pkill'`` on procfs hosts (today's behaviour), else ``'portable'``."""
    return "pkill" if USE_PROCFS else "portable"


@functools.cache
def gate_timeout_tool() -> str | None:
    """The gate's timeout binary: ``timeout``, else ``gtimeout``, else None.

    None means the gate runs the Python-side deadline instead.  Cached: chosen
    once at startup and logged by the lifespan.
    """
    if shutil.which("timeout"):
        return "timeout"
    if shutil.which("gtimeout"):
        return "gtimeout"
    return None


def check_two_user_supported() -> None:
    """Refuse two-user mode off procfs (fail closed); never returns otherwise.

    Raises:
        RuntimeError: when ``config.AGENT_SUBPROCESS_USER`` is set and
            ``USE_PROCFS`` is False.
    """
    if config.AGENT_SUBPROCESS_USER and not USE_PROCFS:
        logger.error(TWO_USER_UNSUPPORTED_MESSAGE)
        raise RuntimeError(TWO_USER_UNSUPPORTED_MESSAGE)


def _psutil_version() -> str:
    try:
        from . import portable_procs

        return str(portable_procs._psutil().__version__)
    except Exception:
        return "missing"


def platform_info() -> dict[str, str]:
    """The keys of the startup ``platform:`` line (also served under /info)."""
    return {
        "sys_platform": sys.platform,
        "procfs": "true" if USE_PROCFS else "false",
        "session_backend": session_backend(),
        "gate_timeout": gate_timeout_tool() or "python",
        "psutil": _psutil_version(),
        "two_user": "true" if config.AGENT_SUBPROCESS_USER else "false",
    }


def log_platform_line() -> None:
    """Log the one ``platform:`` line (WARNING on Linux without procfs)."""
    info = platform_info()
    level = (
        logging.WARNING
        if info["sys_platform"].startswith("linux") and info["procfs"] == "false"
        else logging.INFO
    )
    logger.log(level, "platform: %s", " ".join(f"{k}={v}" for k, v in info.items()))


def log_gate_timeout_line() -> None:
    """Log the startup choice of gate timeout tool (WARNING for the Python path)."""
    tool = gate_timeout_tool()
    if tool is None:
        logger.warning("gate-timeout: tool=python")
    else:
        logger.info("gate-timeout: tool=%s", tool)
