"""Configuration from environment variables."""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        msg = f"Required environment variable {name} is not set"
        raise RuntimeError(msg)
    return val


# Dispatch API auth
DISPATCH_API_KEY: str = ""

# Agent GTD API
AGENT_GTD_URL: str = ""
AGENT_GTD_API_KEY: str = ""

# Workspace
WORKSPACE_ROOT: Path = Path.home() / "workspace"
AGENT_SUBPROCESS_USER: str = ""

# Retention — the decay-rate split.
# Evidence (transcript and patch) is small and stays useful for
# months; the workspace TREE is hundreds of MB to GB (full clones plus target/,
# node_modules/, .venv) and its value decays in a day or two.
EVIDENCE_ROOT: Path = Path.home() / "run-evidence"
EVIDENCE_RETENTION_DAYS: int = 30
WORKSPACE_RETENTION_HOURS: int = 48
RETENTION_INTERVAL_SECONDS: int = 3600

# Private per-run TMPDIR root (see run_tmp.py).  Every build/plan run, reviewer
# and land gets TMPDIR/TMP/TEMP at <RUN_TMP_ROOT>/rt-<key>, removed at teardown;
# leftovers older than RUN_TMP_ORPHAN_MIN_AGE_SECONDS are swept at startup and
# every RETENTION_INTERVAL_SECONDS.
RUN_TMP_ROOT: Path = Path.home() / "run-tmp"
RUN_TMP_ORPHAN_MIN_AGE_SECONDS: int = 600

# Agent limits
MAX_TURNS: int = 100
TIMEOUT_SECONDS: int = 30 * 60  # 30 minutes
# Manage-mode wall-clock backstop — DERIVED, not authoritative.
#
# The single source of truth for the manage timeout is the GTD-side app setting
# ``dispatch.manager_default_timeout_minutes`` (seeded from
# ``agent_gtd.dispatch_constants.MANAGER_DEFAULT_TIMEOUT_MINUTES``).  The
# dispatch worker resolves it and sends it on the wire as
# ``DispatchRequest.timeout_minutes``, which wins whenever it is present.
#
# This value only applies to a manage dispatch that arrives with NO explicit
# ``timeout_minutes`` at all, and must stay numerically equal to the
# authoritative one.  Both route through ``timeout_seconds_for_mode`` below.
#
# SIZED FOR A SHORT-LIVED REVIEWER, not a resident manager: 45 min, equal to
# ``REVIEW_TIMEOUT_SECONDS``.  It used to be 4 hours because a resident
# manager babysat a whole wave; nothing that long-lived exists any more.
MANAGE_TIMEOUT_SECONDS: int = 45 * 60  # == MANAGER_DEFAULT_TIMEOUT_MINUTES (45)

# THE RESIDENT-MANAGER ERA IS OVER (item 7baf77d0).  The worker drives the wave
# loop itself and launches one short-lived reviewer per completed build, so the
# keep-alive machinery that existed only to resurrect a resident manager agent
# is gone, and so are its knobs: MAX_MANAGE_RETRIES, MAX_MANAGE_FREE_RELAUNCHES,
# MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS, MANAGE_STALE_THRESHOLD_SECONDS,
# WATCHDOG_INTERVAL_SECONDS and MANAGE_RETRY_BACKOFF_SECONDS.  Do not
# reintroduce a relaunch cap, a staleness watchdog or a heartbeat threshold by
# analogy: the loop owns liveness, the reviewer is bounded by
# REVIEW_TIMEOUT_SECONDS, and a restart is covered by the re-adoption sweep,
# which resumes ONLY rollouts this host holds a MANAGE run row for.  A
# fleet-wide staleness scan relaunching rollouts it never owned is exactly how
# rollout fe1e828d ended up with two loops landing the same item.
MAX_CONCURRENT_RUNS: int = 32  # thread-pool ceiling for run_in_executor
CANCEL_GRACE_SECONDS: int = 5  # seconds between SIGTERM and SIGKILL on cancel

# How long shutdown waits for cancelled runs to finish their teardown — evidence
# capture and the rescue of abandoned work, both of which shell out to git.
# Must stay comfortably under systemd's TimeoutStopSec (90s default), because
# systemd SIGKILLs us regardless and a rescue killed mid-push is worse than one
# that never started.
SHUTDOWN_GRACE_SECONDS: int = 45

# Floor on remaining run budget below which the push backstop (worker completes an
# agent's unfinished `git push` after a successful build-mode exit) is not attempted
# at all — not enough time left to plausibly succeed (e.g. a slow pre-push hook).
PUSH_BACKSTOP_MIN_SECONDS: int = 10

# Dedicated post-run gate budget, independent of build elapsed time.
POST_RUN_GATE_TIMEOUT_SECONDS: int = 3600

# Lower bound of the post-run gate budget: the effective budget is never below
# this, whatever the other settings say.
POST_RUN_GATE_MIN_SECONDS: int = 600

# Per-subprocess timeout for the pre-launch gate install (hook-manager install,
# hook-dir lookup, or `.agent-gtd/setup`). See gates.py.
GATE_INSTALL_TIMEOUT_SECONDS: int = 300

# --- Worker-driven rollout wave loop + short-lived merge reviewer -----------
#
# The worker owns the wave loop; a REVIEW-mode agent is launched per completed
# build, reviews it, and merges it. Everything below tunes that loop.

# Default engine for a reviewer when the rollout row carries none. The
# AUTHORITATIVE resolution lives on the GTD side
# (``dispatch_worker.resolve_reviewer_engine``: rollout -> project ->
# deployment default) and arrives on the rollout dict as ``reviewer_engine``.
# This is only the floor for a rollout that predates that column.
REVIEWER_ENGINE: str = "claude-code-sonnet"

# Recording label for a MANAGE run with a rollout_id (the worker-driven wave
# loop, no agent session).  Never a routable engine.  Must equal the board's
# ROLLOUT_LOOP_ENGINE in agent_gtd dispatch_constants.py.
ROLLOUT_LOOP_ENGINE = "rollout-loop"

# Turn budget for one reviewer. Mirrors
# ``agent_gtd.dispatch_worker.REVIEW_MAX_TURNS`` — a reviewer decides ONE build:
# read the diff, reconcile the item's ACs, run the gate, merge a handful of
# repos, write the verdict artifact. Keep the two numerically equal.
REVIEW_MAX_TURNS: int = 40

# Wall-clock ceiling for one reviewer. Generous enough for a cold gate run on
# the Pi (the project gate command is the expensive part, not the model), and
# far below the 4-hour resident-manager backstop it replaced: a reviewer that
# has not finished one build in this long is wedged, not slow.
REVIEW_TIMEOUT_SECONDS: int = 45 * 60

# Bounds for a project's ``review_timeout_seconds`` override.  Must equal the
# board's MIN/MAX_REVIEW_TIMEOUT_SECONDS in agent_gtd/dispatch_constants.py.
MIN_REVIEW_TIMEOUT_OVERRIDE_SECONDS: int = 600
MAX_REVIEW_TIMEOUT_OVERRIDE_SECONDS: int = 14400


def resolve_review_timeout_seconds(project_value: object) -> tuple[int, str]:
    """Resolve the effective reviewer timeout for a project.

    Args:
        project_value: The project record's ``review_timeout_seconds`` (any
            JSON value; None when unset or when the board predates the field).

    Returns:
        ``(seconds, source)``.  ``source`` is ``project`` for a valid override,
        ``env_default`` when unset, or ``invalid_project_value`` when the value
        is not an int in range (fail closed to the env default).
    """
    if project_value is None:
        return REVIEW_TIMEOUT_SECONDS, "env_default"
    if (
        isinstance(project_value, int)
        and not isinstance(project_value, bool)
        and MIN_REVIEW_TIMEOUT_OVERRIDE_SECONDS
        <= project_value
        <= MAX_REVIEW_TIMEOUT_OVERRIDE_SECONDS
    ):
        return project_value, "project"
    return REVIEW_TIMEOUT_SECONDS, "invalid_project_value"


# The worker's land step (fetch, rebase onto current origin, gate the exact
# tree, push).  The gate timeout also bounds the push, because a push may run
# the repo's own pre-push hook when the project has no gate_command.
LAND_GATE_TIMEOUT_SECONDS: int = 1800
# Per-invocation ceiling for every other git call in the land step.
LAND_GIT_TIMEOUT_SECONDS: int = 120

# Seconds the wave loop sleeps between ticks while builds are in flight.
# The loop is a poller by necessity — child build runs are executed by a
# different process (possibly on a different host) and there is no push
# notification to wait on.
ROLLOUT_LOOP_POLL_SECONDS: int = 30

# Host-wide cap on rollout-loop reviewer runs in flight at once, across every
# rollout loop in this process.  Each review runs in its own per-slot rollout
# checkout on a dedicated reviewer thread pool of this size; the land step
# stays serialized per rollout.  Clamped to at least 1.
ROLLOUT_MAX_CONCURRENT_REVIEWS: int = 3

# Seconds a rollout loop may stay parked behind a host drain before it logs a
# ``park-long`` WARNING; repeated every further interval while still parked.
ROLLOUT_PARK_WARN_SECONDS: int = 3600

# Wall-clock seconds the wave loop rides out a TRANSIENT board outage (a
# transport error, or a 502/503/504) before giving up.  Measured from the first
# error of the current consecutive streak; a clean tick resets it.  Past the
# window the last error is re-raised and the loop run fails — and because
# dispatch_rollout only accepts a pending rollout, the rollout then needs
# manual recovery.
ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS: int = 3600

# Per-ITEM cap on `re-dispatch` verdicts within one rollout.
#
# There is no retry concept for a failed child build anywhere today: one failed
# child halts the whole rollout. A reviewer's `re-dispatch` verdict introduces
# one, and a cap is what keeps it from becoming an unbounded build loop that
# burns a token budget on an item that will never go green. 1 means: a given
# item gets exactly one second chance per rollout; the next `re-dispatch`
# verdict for that item halts instead. Default behaviour is unchanged — absent
# an explicit `re-dispatch` verdict, a failed child still halts.
MAX_ITEM_REDISPATCHES: int = 1

# Anthropic API key.
#
# This used to be a hard `_require` at startup, but ONLY because the LLM
# rollout planner (rollout_planner.py / POST /plan) called the Anthropic API
# in-process. That planner is gone: plan_rollout derives its DAG on the
# agent_gtd side from item blockers plus file-overlap serialisation, with no
# model in the loop. Do NOT re-add the `_require` on the assumption it is
# load-bearing for planning — it is not.
#
# The key IS still read, but only as an OPTIONAL engine credential: talos.py
# puts it in the per-engine env overlay for talos-haiku/sonnet/opus, and
# engines.py gates those engines' availability on it (an unset key simply
# means those lanes are not advertised via /info). It is deliberately never
# exposed to claude-code subprocesses — see kb-01512.
ANTHROPIC_API_KEY: str = ""

# Personal-KB access handed to talos runs only (claude-code engines reach the KB
# via MCP). Both must be set for the pair to be injected; see talos_env_overlay.
PERSONAL_KB_URL: str = ""
TALOS_PERSONAL_KB_API_KEY: str = ""

# Ollama local inference backend.
# OLLAMA_BASE_URL is the Ollama root URL, e.g. "http://10.0.0.5:11434".
# Do NOT include /v1 or any path suffix — Ollama exposes the Anthropic
# Messages API at the root. Empty = engine disabled.
OLLAMA_BASE_URL: str = ""
OLLAMA_API_KEY: str = "ollama"  # dummy value; Ollama ignores auth
OLLAMA_DEFAULT_MODEL: str = "qwen3.6:35b"
OLLAMA_TIMEOUT_MULTIPLIER: float = 2.0

# Ollama Cloud API key (https://ollama.com) — distinct from the LOCAL OLLAMA_API_KEY
# above, which points at the operator's own Ollama server (dummy 'ollama' on
# unauthenticated setups). Consumed by the talos-glm engine (native /api/chat wire)
# AND the claude-code-glm engine (Claude Code driving the Anthropic-compatible
# /v1/messages endpoint ollama.com also exposes). There is intentionally NO fallback
# to OLLAMA_API_KEY: mixing the two would silently ship the operator's local-server
# key to the cloud.
OLLAMA_CLOUD_API_KEY: str = ""

# Ollama Cloud model for the claude-code-glm engine. Claude Code reaches it via the
# Anthropic-compatible /v1/messages route ollama.com serves. claude-code-glm is the
# HARNESS twin of talos-glm (same cloud model + key, the Claude Code loop instead of
# talos), so this default must match talos-glm's pinned literal in talos.py. The env
# override steers claude-code-glm ONLY — talos-glm never reads it.
OLLAMA_CLOUD_BASE_URL: str = "https://ollama.com"
OLLAMA_CLOUD_MODEL: str = "glm-5.3:cloud"
# claude-code-deepseek pins deepseek-v4.1-flash:cloud as a literal and never reads OLLAMA_CLOUD_MODEL.  # noqa: E501

# Per-engine concurrency caps declared by THIS host (DISPATCH_ENGINE_SLOTS, e.g.
# "talos-qwen=1").  Empty means no per-engine caps.  A cap of 0 hides the engine.
ENGINE_SLOTS: dict[str, int] = {}

# Optional allowlist of engines THIS host serves (DISPATCH_ENGINES_ONLY).  None
# means no allowlist (today's behaviour).  It only ever removes engines.
ENGINES_ONLY: frozenset[str] | None = None

# Host-local talos-qwen endpoint config.  TALOS_QWEN_BASE_URL empty means
# "unset" (talos-qwen is then not advertised).  There is NO fallback to
# OLLAMA_BASE_URL.  The endpoint is never probed.
TALOS_QWEN_BASE_URL: str = ""
TALOS_QWEN_MODEL: str = "qwen3.8:27b"
TALOS_QWEN_NUM_CTX: int = 262144
# Empty means unset; otherwise passed through verbatim to talos as
# TALOS_OLLAMA_KEEP_ALIVE (talos-qwen only).
TALOS_QWEN_KEEP_ALIVE: str = ""
_KEEP_ALIVE_SECONDS_RE = re.compile(r"-?[0-9]+", re.ASCII)
_KEEP_ALIVE_DURATION_RE = re.compile(r"(?:[0-9]+(?:ns|us|ms|s|m|h))+", re.ASCII)


def talos_qwen_base_url() -> str:
    """Return the host's talos-qwen Ollama base URL ("" when unset)."""
    return TALOS_QWEN_BASE_URL


def _parse_engine_slots(raw: str) -> dict[str, int]:
    """Parse ``DISPATCH_ENGINE_SLOTS`` (``<engine>=<int>,...``) or raise ValueError."""
    from agent_gtd_dispatch_protocol.models import BuildEngine

    valid = {e.value for e in BuildEngine}
    slots: dict[str, int] = {}
    for segment in raw.split(","):
        segment = segment.strip()
        if not segment:
            continue
        name, sep, value = segment.partition("=")
        name = name.strip()
        value = value.strip()
        problem = ""
        if not sep:
            problem = "expected <engine>=<int>"
        elif name not in valid:
            problem = f"unknown engine {name!r}"
        elif not (value.isascii() and value.isdigit()):
            problem = f"cap {value!r} is not a non-negative integer"
        elif name in slots:
            problem = f"engine {name!r} appears twice"
        if problem:
            msg = f"Invalid DISPATCH_ENGINE_SLOTS={raw!r}: {problem}"
            raise ValueError(msg)
        slots[name] = int(value)
    return slots


def _parse_engines_only(raw: str) -> frozenset[str] | None:
    """Parse ``DISPATCH_ENGINES_ONLY`` (comma-separated engine names) or raise."""
    if not raw.strip():
        return None
    from .engines import ENGINES

    seen: list[str] = []
    for segment in raw.split(","):
        name = segment.strip()
        if not name:
            continue
        if name not in ENGINES:
            msg = (
                f"Invalid DISPATCH_ENGINES_ONLY={raw!r}: unknown engine {name!r}; "
                f"known engines: {sorted(ENGINES)}"
            )
            raise ValueError(msg)
        if name in seen:
            msg = (
                f"Invalid DISPATCH_ENGINES_ONLY={raw!r}: engine {name!r} appears twice"
            )
            raise ValueError(msg)
        seen.append(name)
    if not seen:
        msg = f"Invalid DISPATCH_ENGINES_ONLY={raw!r}: no engine names given"
        raise ValueError(msg)
    return frozenset(seen)


# talos binary discovery: default 'talos', PATH-resolved by the subprocess machinery
# (mirrors how the 'claude' binary is resolved for claude-code engines). Override via
# TALOS_BIN env var when the binary lives at a non-default path on the host.
TALOS_BIN: str = "talos"
# Talos gate-command timeout. Talos' own default is 300 s; 900 s is chosen to
# survive a COLD fmt+clippy+nextest gate run on a Pi-class host.
TALOS_GATE_TIMEOUT_SECS: int = 900
# Fleet-wide talos KB memory mode, passed as `talos run --memory <mode>` when not 'off'.
TALOS_MEMORY_MODES = frozenset({"off", "annotate", "deny-once"})
TALOS_MEMORY: str = "off"

# Root log level for THIS package's loggers.  `uvicorn.run()` configures only
# uvicorn's own loggers and leaves the root logger alone, so before 2026-09-19
# every `logger.info(...)` in this package went nowhere: the journal carried
# uvicorn access lines and nothing else.  That made the gate-install decisions,
# the post-run gate, the rollout loop's decisions and the rescue WARNING
# invisible in production — all of them precisely the things you need when a
# dispatch misbehaves.  See `main.configure_logging`.
LOG_LEVEL: str = "INFO"


def _post_run_gate_budget() -> tuple[int, str]:
    """Pick the post-run gate budget and the term that supplied it.

    Ties break in the order post_run > land > min.
    """
    terms = (
        ("post_run", POST_RUN_GATE_TIMEOUT_SECONDS),
        ("land", LAND_GATE_TIMEOUT_SECONDS),
        ("min", POST_RUN_GATE_MIN_SECONDS),
    )
    source, seconds = terms[0]
    for name, value in terms[1:]:
        if value > seconds:
            source, seconds = name, value
    return seconds, source


def post_run_gate_timeout_seconds() -> int:
    """Effective post-run gate budget in seconds."""
    return _post_run_gate_budget()[0]


def post_run_gate_timeout_source() -> str:
    """Which term (post_run, land, min) determines the gate budget."""
    return _post_run_gate_budget()[1]


def timeout_seconds_for_mode(mode: str) -> int:
    """Return the default run timeout for *mode* when none was supplied.

    The ONE place the dispatch service turns "no explicit timeout" into a
    number.  Both the HTTP dispatch entrypoint and ``dispatch.run_agent`` call
    this instead of re-deriving the mode branch themselves, so the two can no
    longer drift apart.

    Note this is only the backstop: an explicit ``timeout_minutes`` on the
    dispatch request always wins, and for manage mode the GTD-side
    ``dispatch.manager_default_timeout_minutes`` setting is authoritative (see
    the ``MANAGE_TIMEOUT_SECONDS`` comment above).

    Args:
        mode: The dispatch mode — ``"build"``, ``"plan"``, ``"manage"`` or
            ``"review"``. ``DispatchMode`` is a ``StrEnum``, so members may be
            passed directly.

    Returns:
        Default timeout in seconds for that mode.
    """
    if mode == "manage":
        return MANAGE_TIMEOUT_SECONDS
    if mode == "review":
        return REVIEW_TIMEOUT_SECONDS
    return TIMEOUT_SECONDS


def _validate_run_tmp_root(root: Path, min_age_seconds: int) -> None:
    """Refuse a RUN_TMP_ROOT whose orphan sweep could touch anything else.

    Raises:
        ValueError: the root is a shared temp dir, overlaps the workspace or
            evidence roots, is the service home or one of its ancestors, or
            the orphan min age is negative.
    """
    problem = ""
    home = Path.home()
    if root in (Path("/"), Path("/tmp"), Path("/var/tmp")):  # noqa: S108
        problem = "is a shared system temp directory"
    for name, other in (("workspace", WORKSPACE_ROOT), ("evidence", EVIDENCE_ROOT)):
        if root == other or other in root.parents or root in other.parents:
            problem = f"overlaps the {name} root {other}"
    if root == home or root in home.parents:
        problem = f"is the service home {home} or one of its ancestors"
    if problem:
        msg = f"Invalid DISPATCH_RUN_TMP_ROOT={str(root)!r}: {problem}"
        raise ValueError(msg)
    if min_age_seconds < 0:
        msg = (
            "Invalid DISPATCH_RUN_TMP_ORPHAN_MIN_AGE_SECONDS="
            f"{min_age_seconds}: must be >= 0"
        )
        raise ValueError(msg)


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS, MANAGE_TIMEOUT_SECONDS
    global ANTHROPIC_API_KEY, MAX_CONCURRENT_RUNS
    global PERSONAL_KB_URL, TALOS_PERSONAL_KB_API_KEY
    global OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_DEFAULT_MODEL
    global OLLAMA_TIMEOUT_MULTIPLIER, CANCEL_GRACE_SECONDS, PUSH_BACKSTOP_MIN_SECONDS
    global GATE_INSTALL_TIMEOUT_SECONDS, POST_RUN_GATE_MIN_SECONDS
    global POST_RUN_GATE_TIMEOUT_SECONDS
    global OLLAMA_CLOUD_API_KEY, OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_MODEL
    global TALOS_BIN, TALOS_GATE_TIMEOUT_SECS, TALOS_MEMORY
    global AGENT_SUBPROCESS_USER
    global ENGINES_ONLY
    global ENGINE_SLOTS, TALOS_QWEN_BASE_URL, TALOS_QWEN_MODEL, TALOS_QWEN_NUM_CTX
    global TALOS_QWEN_KEEP_ALIVE
    global REVIEWER_ENGINE, REVIEW_MAX_TURNS, REVIEW_TIMEOUT_SECONDS
    global LAND_GATE_TIMEOUT_SECONDS, LAND_GIT_TIMEOUT_SECONDS
    global ROLLOUT_LOOP_POLL_SECONDS, ROLLOUT_PARK_WARN_SECONDS, MAX_ITEM_REDISPATCHES
    global ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS, ROLLOUT_MAX_CONCURRENT_REVIEWS
    global EVIDENCE_ROOT, EVIDENCE_RETENTION_DAYS, WORKSPACE_RETENTION_HOURS
    global RETENTION_INTERVAL_SECONDS
    global RUN_TMP_ROOT, RUN_TMP_ORPHAN_MIN_AGE_SECONDS
    global LOG_LEVEL

    LOG_LEVEL = os.environ.get("DISPATCH_LOG_LEVEL", "INFO").strip().upper() or "INFO"

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")

    # Optional: gates the talos anthropic engines only (see the declaration
    # above). Absent = those engines are simply not advertised.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    PERSONAL_KB_URL = os.environ.get("PERSONAL_KB_URL", "").strip()
    TALOS_PERSONAL_KB_API_KEY = os.environ.get("TALOS_PERSONAL_KB_API_KEY", "").strip()

    AGENT_SUBPROCESS_USER = os.environ.get("DISPATCH_AGENT_SUBPROCESS_USER", "")
    _workspace_env = os.environ.get("DISPATCH_WORKSPACE_ROOT", "")
    if _workspace_env:
        WORKSPACE_ROOT = Path(_workspace_env)
    elif AGENT_SUBPROCESS_USER:
        WORKSPACE_ROOT = Path.home().parent / AGENT_SUBPROCESS_USER / "workspace"
    else:
        WORKSPACE_ROOT = Path.home() / "workspace"
    _evidence_env = os.environ.get("DISPATCH_EVIDENCE_ROOT", "")
    if _evidence_env:
        EVIDENCE_ROOT = Path(_evidence_env)
    else:
        EVIDENCE_ROOT = WORKSPACE_ROOT.parent / "run-evidence"
    _run_tmp_env = os.environ.get("DISPATCH_RUN_TMP_ROOT", "")
    if _run_tmp_env:
        RUN_TMP_ROOT = Path(_run_tmp_env)
    else:
        RUN_TMP_ROOT = WORKSPACE_ROOT.parent / "run-tmp"
    RUN_TMP_ORPHAN_MIN_AGE_SECONDS = int(
        os.environ.get("DISPATCH_RUN_TMP_ORPHAN_MIN_AGE_SECONDS", "600")
    )
    _validate_run_tmp_root(RUN_TMP_ROOT, RUN_TMP_ORPHAN_MIN_AGE_SECONDS)
    EVIDENCE_RETENTION_DAYS = int(
        os.environ.get("DISPATCH_EVIDENCE_RETENTION_DAYS", "30")
    )
    WORKSPACE_RETENTION_HOURS = int(
        os.environ.get("DISPATCH_WORKSPACE_RETENTION_HOURS", "48")
    )
    RETENTION_INTERVAL_SECONDS = int(
        os.environ.get("DISPATCH_RETENTION_INTERVAL_SECONDS", "3600")
    )
    MAX_TURNS = int(os.environ.get("DISPATCH_MAX_TURNS", "100"))
    TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "1800"))
    MANAGE_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_TIMEOUT_SECONDS", "2700")
    )
    MAX_CONCURRENT_RUNS = int(os.environ.get("DISPATCH_MAX_CONCURRENT_RUNS", "32"))
    OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
    if OLLAMA_BASE_URL:
        _parsed = urllib.parse.urlparse(OLLAMA_BASE_URL)
        if _parsed.scheme not in ("http", "https") or not _parsed.netloc:
            msg = (
                f"Invalid OLLAMA_BASE_URL={OLLAMA_BASE_URL!r}: must start with "
                f"http:// or https:// and include a hostname. "
                f"Expected format: http://host:port — got {OLLAMA_BASE_URL!r}"
            )
            raise ValueError(msg)
    OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
    TALOS_QWEN_BASE_URL = os.environ.get("TALOS_QWEN_BASE_URL", "").strip()
    if TALOS_QWEN_BASE_URL:
        _qparsed = urllib.parse.urlparse(TALOS_QWEN_BASE_URL)
        if _qparsed.scheme not in ("http", "https") or not _qparsed.netloc:
            msg = (
                f"Invalid TALOS_QWEN_BASE_URL={TALOS_QWEN_BASE_URL!r}: must start "
                f"with http:// or https:// and include a hostname. "
                f"Expected format: http://host:port"
            )
            raise ValueError(msg)
    TALOS_QWEN_MODEL = os.environ.get("TALOS_QWEN_MODEL", "").strip() or "qwen3.8:27b"
    _qctx = os.environ.get("TALOS_QWEN_NUM_CTX", "").strip()
    if _qctx:
        if not (_qctx.isascii() and _qctx.isdigit()) or int(_qctx) < 1:
            msg = f"Invalid TALOS_QWEN_NUM_CTX={_qctx!r}: must be an integer >= 1"
            raise ValueError(msg)
        TALOS_QWEN_NUM_CTX = int(_qctx)
    else:
        TALOS_QWEN_NUM_CTX = 262144
    _ka = os.environ.get("TALOS_QWEN_KEEP_ALIVE", "").strip()
    if _ka:
        _ka_ok = bool(
            _KEEP_ALIVE_SECONDS_RE.fullmatch(_ka)
            and -9223372036854775808 <= int(_ka) <= 9223372036854775807
        ) or bool(_KEEP_ALIVE_DURATION_RE.fullmatch(_ka))
        if not _ka_ok:
            msg = (
                f"Invalid TALOS_QWEN_KEEP_ALIVE={_ka!r}: must be integer seconds "
                f"(e.g. -1, 0, 300) or a duration such as 10m or 1h30m"
            )
            raise ValueError(msg)
        TALOS_QWEN_KEEP_ALIVE = _ka
    else:
        TALOS_QWEN_KEEP_ALIVE = ""
    ENGINE_SLOTS = _parse_engine_slots(os.environ.get("DISPATCH_ENGINE_SLOTS", ""))
    logger.info(
        "engine-slots config slots=%s talos_qwen_endpoint_source=%s "
        "talos_qwen_keep_alive=%s",
        ENGINE_SLOTS,
        "TALOS_QWEN_BASE_URL" if TALOS_QWEN_BASE_URL else "unset",
        TALOS_QWEN_KEEP_ALIVE or "unset",
    )
    if TALOS_QWEN_KEEP_ALIVE and (
        not TALOS_QWEN_BASE_URL or ENGINE_SLOTS.get("talos-qwen", 0) < 1
    ):
        logger.warning(
            "TALOS_QWEN_KEEP_ALIVE=%s set but talos-qwen is not runnable on this "
            "host; keep-alive is inert",
            TALOS_QWEN_KEEP_ALIVE,
        )
    from .engines import SLOT_REQUIRED_ENGINES

    for _hidden in sorted(SLOT_REQUIRED_ENGINES):
        if (
            _hidden == "talos-qwen"
            and TALOS_QWEN_BASE_URL
            and ENGINE_SLOTS.get(_hidden, 0) < 1
        ):
            logger.warning(
                "engine %s hidden: requires DISPATCH_ENGINE_SLOTS cap >= 1",
                _hidden,
            )
    ENGINES_ONLY = _parse_engines_only(os.environ.get("DISPATCH_ENGINES_ONLY", ""))
    logger.info(
        "engines-only config allowlist=%s",
        sorted(ENGINES_ONLY) if ENGINES_ONLY is not None else "unset",
    )
    if ENGINES_ONLY is not None:
        for _slot_engine in sorted(ENGINE_SLOTS):
            if _slot_engine not in ENGINES_ONLY:
                logger.warning(
                    "engine-slots config names engine %s outside "
                    "DISPATCH_ENGINES_ONLY; its cap is inert",
                    _slot_engine,
                )
    # NO fallback to OLLAMA_API_KEY — cloud vs. local Ollama servers use distinct
    # credentials and mixing them ships the local key to the cloud.
    OLLAMA_CLOUD_API_KEY = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    OLLAMA_CLOUD_BASE_URL = os.environ.get(
        "OLLAMA_CLOUD_BASE_URL", "https://ollama.com"
    )
    OLLAMA_CLOUD_MODEL = os.environ.get("OLLAMA_CLOUD_MODEL", "glm-5.3:cloud")
    TALOS_BIN = os.environ.get("TALOS_BIN", "talos")
    TALOS_GATE_TIMEOUT_SECS = int(os.environ.get("TALOS_GATE_TIMEOUT_SECS", "900"))
    _memory = os.environ.get("TALOS_MEMORY", "off").strip().lower()
    if _memory not in TALOS_MEMORY_MODES:
        logger.warning("Ignoring invalid TALOS_MEMORY=%r; using 'off'", _memory)
        _memory = "off"
    TALOS_MEMORY = _memory
    OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen3.6:35b")
    OLLAMA_TIMEOUT_MULTIPLIER = float(
        os.environ.get("OLLAMA_TIMEOUT_MULTIPLIER", "2.0")
    )
    CANCEL_GRACE_SECONDS = int(os.environ.get("DISPATCH_CANCEL_GRACE_SECONDS", "5"))
    PUSH_BACKSTOP_MIN_SECONDS = int(
        os.environ.get("DISPATCH_PUSH_BACKSTOP_MIN_SECONDS", "10")
    )
    GATE_INSTALL_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_GATE_INSTALL_TIMEOUT_SECONDS", "300")
    )
    POST_RUN_GATE_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_POST_RUN_GATE_TIMEOUT_SECONDS", "3600")
    )
    POST_RUN_GATE_MIN_SECONDS = int(
        os.environ.get("DISPATCH_POST_RUN_GATE_MIN_SECONDS", "600")
    )
    REVIEWER_ENGINE = (
        os.environ.get("DISPATCH_REVIEWER_ENGINE", "").strip() or "claude-code-sonnet"
    )
    REVIEW_MAX_TURNS = int(os.environ.get("DISPATCH_REVIEW_MAX_TURNS", "40"))
    REVIEW_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_REVIEW_TIMEOUT_SECONDS", "2700")
    )
    LAND_GATE_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_LAND_GATE_TIMEOUT_SECONDS", "1800")
    )
    LAND_GIT_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_LAND_GIT_TIMEOUT_SECONDS", "120")
    )
    ROLLOUT_LOOP_POLL_SECONDS = int(
        os.environ.get("DISPATCH_ROLLOUT_LOOP_POLL_SECONDS", "30")
    )
    ROLLOUT_MAX_CONCURRENT_REVIEWS = max(
        1, int(os.environ.get("DISPATCH_ROLLOUT_MAX_CONCURRENT_REVIEWS", "3"))
    )
    ROLLOUT_PARK_WARN_SECONDS = int(
        os.environ.get("DISPATCH_ROLLOUT_PARK_WARN_SECONDS", "3600")
    )
    ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS = int(
        os.environ.get("DISPATCH_ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS", "3600")
    )
    MAX_ITEM_REDISPATCHES = int(os.environ.get("DISPATCH_MAX_ITEM_REDISPATCHES", "1"))
