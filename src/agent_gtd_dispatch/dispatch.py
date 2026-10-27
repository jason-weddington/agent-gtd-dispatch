"""Core dispatch logic — workspace prep, prompt building, agent invocation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from agent_gtd_dispatch_protocol.branches import make_branch_name
from agent_gtd_dispatch_protocol.models import BuildEngine, DispatchMode

from . import config, gtd_client
from .engines import (
    COMMON_ENV_KEYS,
    FORBIDDEN_ENV_KEYS,
    Engine,
    build_env,
    is_talos_engine,
)
from .models import PushStatus, RepoPushStatus
from .run_tmp import with_tmp_env

logger = logging.getLogger(__name__)

_executor: concurrent.futures.ThreadPoolExecutor | None = None
# Dedicated pool for rollout-loop reviewer runs, sized to
# config.ROLLOUT_MAX_CONCURRENT_REVIEWS, so concurrent reviewers never starve
# builds, land gates or reaping on ``_executor`` (and vice versa).
_review_executor: concurrent.futures.ThreadPoolExecutor | None = None

_DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master")

# Post-run gate: how many chars of combined gate output to keep for the
# failure comment (tail only — the most recent output is the most useful).
GATE_OUTPUT_TAIL_CHARS: int = 3000

# Env vars passed to the post-run gate subprocess — COMMON_ENV_KEYS minus the
# secrets an arbitrary project-authored gate_command has no business touching
# (AGENT_GTD_URL / AGENT_GTD_API_KEY, plus FORBIDDEN_ENV_KEYS as a backstop).
GATE_ENV_KEYS: frozenset[str] = (
    COMMON_ENV_KEYS
    - {
        "AGENT_GTD_URL",
        "AGENT_GTD_API_KEY",
    }
    - FORBIDDEN_ENV_KEYS
)

# Shim run via `/bin/bash -c` (the binary the sudoers NOPASSWD list already
# authorizes): GNU `timeout` bounds the gate and signals its whole process
# group, then `/bin/sh -c` runs the gate_command string itself.
_GATE_SHIM: str = 'exec timeout --kill-after="$1" "$2" /bin/sh -c "$3"'

# ONE budget for every operator-facing run error string.  Two independent
# truncation layers used to clip these — a 300-char stderr TAIL here and a
# 500-char clip when agent_gtd mirrors the remote run — and the pair silently
# discarded the failing pre-commit hook (only `(no files to check) Skipped`
# lines survived).  Both layers now quote this one constant so they cannot
# drift apart again.
ERROR_TEXT_MAX_CHARS: int = 2000

# How that budget is split when excerpting git/hook output.  The HEAD is the
# end that matters: the FIRST failing hook and git's own message appear there,
# while the tail is usually per-hook "Skipped" noise.
GIT_EXCERPT_HEAD_CHARS: int = 1500
GIT_EXCERPT_TAIL_CHARS: int = ERROR_TEXT_MAX_CHARS - GIT_EXCERPT_HEAD_CHARS


# ANSI escape sequences emitted by hook runners (lefthook colours its summary
# box, pre-commit colours PASS/FAIL).  They make a stored ``error_msg``
# unreadable in a terminal and actively hostile inside JSON, and they carry no
# information the operator needs.  Matches CSI sequences (``\x1b[38;2;0;0;0m``),
# OSC sequences (terminated by BEL or ST), the nF charset escapes some runners
# pair with SGR resets (``\x1b(B``) and the remaining two-character escapes.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[ -/]+[0-~]"
    r"|[@-Z\\-_])"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences, leaving the human-readable text intact."""
    return _ANSI_ESCAPE_RE.sub("", text)


def git_output_excerpt(
    proc: subprocess.CompletedProcess[bytes],
    *,
    head: int = GIT_EXCERPT_HEAD_CHARS,
    tail: int = GIT_EXCERPT_TAIL_CHARS,
) -> str:
    """Excerpt a failed git invocation's output for an operator-facing error string.

    Combines the captured stdout AND stderr — stdout first, because git forwards
    hook stdout on its own stream and the previous stderr-only excerpt threw that
    away entirely — then keeps the HEAD of the result.

    ANSI escape sequences are stripped from each stream BEFORE the length budget
    is applied, so colour codes neither pollute the stored text nor consume the
    excerpt budget.  Stripping happens here, in the shared helper, so every
    caller benefits.

    When the combined output does not fit in ``head + tail`` characters the middle
    is dropped and replaced by a marker naming how many characters went missing,
    so the operator can tell an excerpt from a complete message.  Keeping the head
    is the whole point: the first failing pre-commit hook and git's own message
    are at the top, and a tail-only excerpt of a long hook run shows nothing but
    ``(no files to check) Skipped`` lines.
    """
    parts: list[str] = []
    for stream in (proc.stdout, proc.stderr):
        # Non-bytes (an unset attribute on a test double, None from a call made
        # without capture_output) contributes nothing rather than its repr.
        if not isinstance(stream, bytes) or not stream:
            continue
        text = strip_ansi(stream.decode("utf-8", errors="replace"))
        if text.strip():
            parts.append(text)
    combined = "\n".join(parts).strip()
    budget = head + tail
    if len(combined) <= budget:
        return combined
    dropped = len(combined) - budget
    tail_text = combined[len(combined) - tail :] if tail > 0 else ""
    return f"{combined[:head]}\n[... {dropped} characters elided ...]\n{tail_text}"


# Shim run via ``/bin/bash -c`` (already in the sudoers NOPASSWD list) so that
# every agent-user command is created with umask 0002 regardless of the host's
# sudo implementation (sudo-rs leaves 0022).  Files the agent creates are then
# group-writable for the service user without relying on pam_umask or a
# hand-set default ACL on the workspace.  ``exec`` keeps the real command as
# the process, so signals and exit codes are unchanged.
_UMASK_SHIM: str = 'umask 0002; exec "$@"'


def _sudo_wrap(cmd: list[str]) -> list[str]:
    """Prepend sudo -u <user> -H (with an explicit umask 0002) when AGENT_SUBPROCESS_USER is set."""
    if config.AGENT_SUBPROCESS_USER:
        return [
            "sudo",
            "-u",
            config.AGENT_SUBPROCESS_USER,
            "-H",
            "/bin/bash",
            "-c",
            _UMASK_SHIM,
            "agent-gtd-umask",
            *cmd,
        ]
    return cmd


def _pkill_session_argv(proc: subprocess.Popen[bytes], args: list[str]) -> list[str]:
    """Build the pkill argv that targets proc's session, crossing the sudo boundary.

    Every agent/gate subprocess is launched with ``start_new_session=True`` and
    (in the two-user split) under ``sudo -u <agent user> -H``, so the sudo
    wrapper leads the session and the agent user owns every process in it.
    sudo cannot relay signals to its child, so the session must be signalled
    AS the agent user — hence ``sudo -n -u <user> pkill`` when
    ``AGENT_SUBPROCESS_USER`` is set, plain ``pkill`` in single-user mode.
    """
    pkill = ["pkill", *args, "-s", str(proc.pid)]
    if config.AGENT_SUBPROCESS_USER:
        return ["sudo", "-n", "-u", config.AGENT_SUBPROCESS_USER, *pkill]
    return pkill


def _run_pkill_quietly(argv: list[str]) -> int:
    """Run a pkill argv, returning its exit code; failures are logged, not raised."""
    try:
        result = subprocess.run(argv, check=True, timeout=10, capture_output=True)
        return result.returncode
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            # pkill exit 1 = no process matched — the session is already gone,
            # which is the normal case for an exited run.  Not an error.
            return 1
        logger.warning(
            "pkill of run session failed (rc=%s): %s",
            exc.returncode,
            (exc.stderr or b"").decode("utf-8", errors="replace").strip(),
        )
        return exc.returncode
    except subprocess.TimeoutExpired:
        logger.warning("pkill of run session timed out after 10s: %s", argv)
    except OSError as exc:
        logger.warning("could not run pkill for run session: %s", exc)
    return -1


def kill_run_session(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal every process in the run's session, then the wrapper itself.

    Killing only ``proc`` (the sudo wrapper's pid) orphans the agent and every
    command it spawned: SIGKILL cannot be relayed by sudo, so the children are
    reparented to init and keep running inside a clone that is then deleted
    (kb-03965 — one such tree ran 19 hours on one host).  talos
    >= 0.15.0-11 puts each of its own commands in a separate process group, so
    a process-group kill is not enough either; this kills by SESSION id, which
    reaches the whole tree.  Requires the Popen to have used
    ``start_new_session=True`` so the wrapper's pid IS the session id.
    """
    # pkill wants the signal name WITHOUT the SIG prefix (-KILL, not -SIGKILL).
    signame = signal.Signals(sig).name.removeprefix("SIG")
    _run_pkill_quietly(_pkill_session_argv(proc, [f"-{signame}"]))
    # The wrapper itself may have escaped the pkill (e.g. it already exited its
    # session, or pkill could not see it); signal it directly as well.
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


def run_session_has_members(proc: subprocess.Popen[bytes]) -> bool:
    """True if any process still runs in the session led by ``proc.pid``.

    Used by the cancel path to decide whether a SIGKILL escalation is needed
    even after the wrapper itself has exited (``pkill -0`` probes without
    signalling).
    """
    return _run_pkill_quietly(_pkill_session_argv(proc, ["-0"])) == 0


def init_executor() -> None:
    """Create (or recreate) the module-level ThreadPoolExecutor.

    Must be called after config.load() so that config.MAX_CONCURRENT_RUNS is set.
    Also (re)creates ``_review_executor``, sized to
    ``config.ROLLOUT_MAX_CONCURRENT_REVIEWS``.
    Shuts down the previous executor without waiting for running tasks to finish.
    """
    global _executor, _review_executor
    if _executor is not None:
        _executor.shutdown(wait=False)
    _executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.MAX_CONCURRENT_RUNS
    )
    if _review_executor is not None:
        _review_executor.shutdown(wait=False)
    _review_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.ROLLOUT_MAX_CONCURRENT_REVIEWS
    )


def repo_name_from_origin(origin: str) -> str:
    """Extract a clean repo name from a git origin URL.

    Handles SSH (git@host:org/repo.git), SCP-style (git@host:repos/name),
    and HTTPS URLs.
    """
    # SSH/SCP style: git@host:path/repo.git or git@host:repos/name
    match = re.search(r"[/:]([^/:]+/[^/:]+?)(?:\.git)?$", origin)
    if match:
        return match.group(1).replace("/", "-")
    # Fallback: last path component
    parsed = urlparse(origin)
    return Path(parsed.path).stem or "unknown"


branch_name_for_item = make_branch_name


def repo_dir_from_url(url: str) -> str:
    """Extract a directory name from a git clone URL.

    Takes the segment after the last '/' or ':' (whichever appears later),
    strips a trailing '.git', and returns the result.  Raises ValueError if
    the result is empty.

    Examples::

        repo_dir_from_url('git@host:org/repo.git')          → 'repo'
        repo_dir_from_url('https://host/org/repo.git')      → 'repo'
        repo_dir_from_url('ssh://git@git.example.com/~/repos/agent_gtd') → 'agent_gtd'
        repo_dir_from_url('git@host:repo.git')              → 'repo'  (SCP, no slash)
    """
    # Tolerate a single trailing slash (e.g. from user copy-paste)
    url = url.rstrip("/")
    last_slash = url.rfind("/")
    last_colon = url.rfind(":")
    sep_pos = max(last_slash, last_colon)
    segment = url[sep_pos + 1 :] if sep_pos >= 0 else url
    if segment.endswith(".git"):
        segment = segment[:-4]
    if not segment:
        raise ValueError(f"Cannot determine repo directory from URL: {url!r}")
    return segment


def prepare_workspace(origin: str, run_id: str, branch_name: str) -> Path:
    """Clone the repo and check out a feature branch for this run.

    Launch-time check-and-clean: if a leftover workspace from a prior crashed
    run exists at the expected path, it is removed before cloning so that the
    build branch is always created from the current default-branch tip.
    Exit-time cleanup_workspace remains best-effort; this call is the
    crash-safe guarantee.
    """
    name = repo_name_from_origin(origin)
    workspace = config.WORKSPACE_ROOT / f"{name}-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    # Remove any stale workspace from a prior crashed run before cloning fresh.
    # Pre-clean: teardown owns reaping, so no process scan here.
    cleanup_workspace(workspace)
    subprocess.run(
        _sudo_wrap(["git", "clone", origin, str(workspace)]),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _sudo_wrap(["git", "checkout", "-b", branch_name]),
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    return workspace


def prepare_workspace_multi(
    repo_urls: list[str], run_id: str, branch_name: str
) -> Path:
    """Clone multiple repos and create a feature branch in each for this run.

    Workspace root is ``config.WORKSPACE_ROOT / f'ws-{run_id}'``.

    - Python-mkdirs only ``config.WORKSPACE_ROOT`` (not the workspace root).
    - Creates the workspace root via a sudo-wrapped ``mkdir -p`` so that under
      the two-user split (``AGENT_SUBPROCESS_USER`` set) the agent user owns it
      and the subsequent clones can write into it.
    - Clones each URL **in order** into ``<root>/<repo_dir_from_url(url)>``.
    - Checks out ``branch_name`` in every repo (service-side branch creation).
    - Returns the workspace root ``Path``.

    Raises ``ValueError`` before any subprocess if *repo_urls* is empty, if two
    URLs map to the same directory name, or if any URL produces an empty
    basename.  Raises ``RuntimeError`` on clone or checkout failure.
    """
    if not repo_urls:
        raise ValueError("workspace_repos must not be empty")

    # Validate / resolve directory names before touching the filesystem
    dir_names: list[str] = []
    for url in repo_urls:
        dir_names.append(repo_dir_from_url(url))  # raises ValueError on empty basename

    seen: set[str] = set()
    for name in dir_names:
        if name in seen:
            raise ValueError(f"Duplicate workspace repo directory: '{name}'")
        seen.add(name)

    # Python-mkdirs ONLY config.WORKSPACE_ROOT (mirrors prepare_workspace)
    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    root = config.WORKSPACE_ROOT / f"ws-{run_id}"

    # Launch-time check-and-clean: remove any stale workspace root from a prior
    # crashed run before cloning fresh (mirrors prepare_workspace guarantee).
    # Pre-clean of a stale dir: reaping is left to teardown (the owning run reaps
    # its own workspace), so no process scan here.
    cleanup_workspace(root)

    # Create workspace root via subprocess so the agent user owns it
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root)]),
        check=True,
        capture_output=True,
    )

    for url, dir_name in zip(repo_urls, dir_names, strict=False):
        dest = root / dir_name

        result = subprocess.run(
            _sudo_wrap(["git", "clone", url, str(dest)]),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace clone failed for {url}: {stderr_tail}")

        result = subprocess.run(
            _sudo_wrap(["git", "checkout", "-b", branch_name]),
            cwd=dest,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace checkout failed for {url}: {stderr_tail}")

    return root


def get_head_sha(repo_path: Path) -> str:
    """Return the current HEAD SHA in repo_path (stripped)."""
    result = subprocess.run(
        _sudo_wrap(["git", "rev-parse", "HEAD"]),
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_pushes(
    repos: list[tuple[str, Path, str]],
    branch_name: str,
    branch_by_repo: dict[str, str] | None = None,
) -> list[RepoPushStatus]:
    """Verify that each repo has pushed its branch to origin.

    Args:
        repos: List of (repo_name, repo_path, base_sha) tuples.
        branch_name: The feature branch to check.
        branch_by_repo: Per-repo override of *branch_name* — the ``-r<N>``
            branch a repo's push fell back to (see :func:`push_with_fallback`).

    Returns:
        Per-repo RepoPushStatus in the same order as *repos*.

    Classification order (fail-closed):
    1. Any git subprocess failure → unpushed (local_sha=None, remote_sha=None,
       commits_ahead=0, dirty=False)
    2. commits_ahead == 0 → no_changes
    3. remote_sha == local_sha → pushed
    4. else → unpushed
    """
    results: list[RepoPushStatus] = []
    for repo_name, repo_path, base_sha in repos:
        repo_branch = (branch_by_repo or {}).get(repo_name) or branch_name
        try:
            # local HEAD SHA
            local_proc = subprocess.run(
                _sudo_wrap(["git", "rev-parse", "HEAD"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if local_proc.returncode != 0:
                raise RuntimeError(f"git rev-parse HEAD failed: {local_proc.stderr}")
            local_sha = local_proc.stdout.strip()

            # commits ahead of base_sha
            ahead_proc = subprocess.run(
                _sudo_wrap(["git", "rev-list", f"{base_sha}..HEAD", "--count"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if ahead_proc.returncode != 0:
                raise RuntimeError(f"git rev-list failed: {ahead_proc.stderr}")
            commits_ahead = int(ahead_proc.stdout.strip())

            # remote SHA for the branch (empty output → branch not on remote)
            remote_proc = subprocess.run(
                _sudo_wrap(["git", "ls-remote", "origin", f"refs/heads/{repo_branch}"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if remote_proc.returncode != 0:
                raise RuntimeError(f"git ls-remote failed: {remote_proc.stderr}")
            remote_sha: str | None = None
            ls_output = remote_proc.stdout.strip()
            if ls_output:
                # output format: "<sha>\trefs/heads/<branch>"
                remote_sha = ls_output.split()[0]

            # dirty check — only tracked-file modifications (untracked files ignored)
            dirty_proc = subprocess.run(
                _sudo_wrap(["git", "status", "--porcelain", "--untracked-files=no"]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if dirty_proc.returncode != 0:
                raise RuntimeError(f"git status failed: {dirty_proc.stderr}")
            dirty = bool(dirty_proc.stdout.strip())

            # Classification
            if commits_ahead == 0:
                status = PushStatus.no_changes
            elif remote_sha == local_sha:
                status = PushStatus.pushed
            else:
                status = PushStatus.unpushed

        except Exception:
            logger.exception("verify_pushes: git command failed for repo %s", repo_name)
            results.append(
                RepoPushStatus(
                    repo_name=repo_name,
                    branch=repo_branch,
                    status=PushStatus.unpushed,
                    local_sha=None,
                    remote_sha=None,
                    commits_ahead=0,
                    dirty=False,
                )
            )
            continue

        results.append(
            RepoPushStatus(
                repo_name=repo_name,
                branch=repo_branch,
                status=status,
                local_sha=local_sha,
                remote_sha=remote_sha,
                commits_ahead=commits_ahead,
                dirty=dirty,
            )
        )

    return results


def is_zero_commits_run(push_results: list[RepoPushStatus]) -> bool:
    """Return True when all repos have no_changes status (zero commits across the run).

    The invariant this function exists to serve: a zero-commit build run is never success.

    There is no longer any attempt to tell a "deliberate no-op" apart from a silent failure for a claude-code build, because nothing the worker can observe distinguishes them. An unchanged tree passes a quality gate trivially — the gate is judging the base commit — so a green gate on zero commits is evidence about the base, not about the run. The worker used to accept an agent-written file claiming "already done" as the tiebreaker; that file is gone, and with it the only input that ever separated the two cases. Zero commits is now simply a failure.

    ``already_satisfied`` survives as a terminal for ONE engine only: talos exit code 30, where talos ran its own checks before emitting the verdict. See :func:`main._run_talos`.
    """
    return bool(push_results) and all(
        r.status == PushStatus.no_changes for r in push_results
    )


#: Highest ``-r<N>`` suffix :func:`push_with_fallback` will probe before giving up.
#: A branch that has been rebuilt this many times needs a human, not another suffix.
FALLBACK_BRANCH_MAX_SUFFIX = 50


@dataclass(frozen=True, slots=True)
class PushAttempt:
    """What a worker-side push did to one repo.

    ``branch`` is the remote branch the FINAL attempt targeted — the requested
    branch, or ``<branch>-r<N>`` when the requested one already held unrelated
    commits (``fell_back``).  ``proc`` is that final attempt's process, so a
    caller that only checks ``proc.returncode`` keeps today's semantics.
    """

    proc: subprocess.CompletedProcess[bytes]
    branch: str
    fell_back: bool = False


def _git_push_argv(branch: str, *, no_verify: bool) -> list[str]:
    """Argv pushing the clone's HEAD to ``refs/heads/<branch>`` on origin.

    ``HEAD:refs/heads/<branch>`` rather than a bare ``<branch>`` because a
    fallback (``-r<N>``) branch never exists locally: the clone's branch keeps
    its original name, and HEAD is exactly what push verification compares.
    Never ``--force`` and never ``--force-with-lease``.
    """
    argv = ["git", "push"]
    if no_verify:
        argv.append("--no-verify")
    argv += ["-u", "origin", f"HEAD:refs/heads/{branch}"]
    return argv


def is_non_fast_forward(
    repo_path: Path | str, branch_name: str, timeout_seconds: float | None = None
) -> bool:
    """Return True when origin's ``branch_name`` exists and is NOT an ancestor of HEAD.

    That is the one rejection the ``-r<N>`` fallback exists for: the branch
    already holds an older run's commits that this clone does not contain (a
    re-dispatch onto a fresh base reusing the deterministic item branch name).

    Detection is mechanical — a fetch of that one ref, then ``git merge-base
    --is-ancestor`` — and deliberately never parses git's English stderr. Any
    failure to establish the fact (the fetch fails because the branch is
    absent, auth or the network is down) returns False, so every other push
    rejection keeps its existing behaviour.
    """
    try:
        fetch = subprocess.run(
            _sudo_wrap(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
                ]
            ),
            cwd=repo_path,
            timeout=timeout_seconds,
            capture_output=True,
            check=False,
        )
        if fetch.returncode != 0:
            return False
        ancestor = subprocess.run(
            _sudo_wrap(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    f"refs/remotes/origin/{branch_name}",
                    "HEAD",
                ]
            ),
            cwd=repo_path,
            timeout=timeout_seconds,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("non-fast-forward detection failed in %s", repo_path)
        return False
    return ancestor.returncode != 0


def next_free_fallback_branch(
    repo_path: Path | str, branch_name: str, timeout_seconds: float | None = None
) -> str | None:
    """Return the first ``<branch_name>-r<N>`` (N >= 2) that does not exist on origin.

    Probed with ``git ls-remote origin refs/heads/<candidate>`` — an empty
    answer means free.  Returns None when origin cannot be queried or every
    candidate up to :data:`FALLBACK_BRANCH_MAX_SUFFIX` is taken.
    """
    for n in range(2, FALLBACK_BRANCH_MAX_SUFFIX + 1):
        candidate = f"{branch_name}-r{n}"
        try:
            probe = subprocess.run(
                _sudo_wrap(["git", "ls-remote", "origin", f"refs/heads/{candidate}"]),
                cwd=repo_path,
                timeout=timeout_seconds,
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("ls-remote probe failed in %s", repo_path)
            return None
        if probe.returncode != 0:
            return None
        if not probe.stdout.strip():
            return candidate
    return None


def push_with_fallback(
    repo_path: Path | str,
    branch_name: str,
    *,
    no_verify: bool,
    timeout_seconds: float | None = None,
) -> PushAttempt:
    """Push HEAD to ``branch_name``; on a non-fast-forward, push it to ``-r<N>``.

    A re-dispatched item reuses its deterministic branch name, and when the
    earlier run's branch was never deleted the rebuild's push is rejected — as
    is the rescue's — and gate-green work survives only in the clone.  So when
    the push fails AND :func:`is_non_fast_forward` confirms origin's branch is
    not an ancestor of HEAD, the same HEAD goes to the smallest free
    ``<branch_name>-r<N>``.  The existing branch is never force-pushed,
    deleted or rewritten.  Any other rejection (auth, hook, network) returns
    the original failed attempt unchanged.
    """
    proc = subprocess.run(
        _sudo_wrap(_git_push_argv(branch_name, no_verify=no_verify)),
        cwd=repo_path,
        timeout=timeout_seconds,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        return PushAttempt(proc, branch_name)
    if not is_non_fast_forward(repo_path, branch_name, timeout_seconds):
        return PushAttempt(proc, branch_name)
    fallback = next_free_fallback_branch(repo_path, branch_name, timeout_seconds)
    if fallback is None:
        logger.warning(
            "push fallback: %s in %s is non-fast-forward but no free -rN branch"
            " could be found",
            branch_name,
            repo_path,
        )
        return PushAttempt(proc, branch_name)
    logger.warning(
        "push fallback: %s on origin holds commits not in HEAD (%s); pushing to %s",
        branch_name,
        repo_path,
        fallback,
    )
    retry = subprocess.run(
        _sudo_wrap(_git_push_argv(fallback, no_verify=no_verify)),
        cwd=repo_path,
        timeout=timeout_seconds,
        capture_output=True,
        check=False,
    )
    if retry.returncode != 0:
        # Report the fallback's failure, but keep the requested name: nothing
        # reached the fallback branch, so it is not the run's branch.
        return PushAttempt(retry, branch_name)
    return PushAttempt(retry, fallback, fell_back=True)


def push_unpushed_repo(
    repo_path: Path, branch_name: str, timeout_seconds: float
) -> PushAttempt:
    """Backstop push: complete a push the agent started but never finished.

    Runs synchronously and may block for the full pre-push hook duration (e.g. a
    coverage-gated test suite) — callers MUST invoke this via
    ``loop.run_in_executor(_executor, ...)``, never inline on the event loop.

    Hooks stay ENABLED (never ``--no-verify``) — this is the same push the agent
    would have run, just completed by the worker after the agent exited.  A
    non-fast-forward rejection falls back to ``<branch>-r<N>`` (see
    :func:`push_with_fallback`); the returned :class:`PushAttempt` names the
    branch that was actually pushed.
    """
    return push_with_fallback(
        repo_path, branch_name, no_verify=False, timeout_seconds=timeout_seconds
    )


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of a single post-run quality-gate invocation."""

    returncode: int | None
    timed_out: bool
    output: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        """True when the gate exited 0 and did not time out."""
        return self.returncode == 0 and not self.timed_out


def _classify_gate_timeout(
    returncode: int, duration_seconds: float, timeout_seconds: int
) -> bool:
    """Classify whether *returncode* represents a gate timeout rather than a real failure.

    124 is GNU ``timeout``'s own expiry status. 137 and -9 are what a gate that
    ignores SIGTERM gets once ``--kill-after`` escalates to SIGKILL. The
    duration check guards against a gate that legitimately produces one of
    these exit codes well before the deadline being misclassified as a
    timeout.
    """
    return returncode in (124, 137, -9) and duration_seconds >= timeout_seconds - 1


def run_gate_command(
    workspace: Path,
    gate_command: str,
    timeout_seconds: int,
    engine: Engine,
    popen_callback: Callable[[subprocess.Popen[bytes]], None] | None = None,
    *,
    tmp_dir: Path | None = None,
) -> GateResult:
    """Run the project's post-run quality gate and capture its outcome.

    Runs synchronously and may block for the gate's full duration (a cold
    fmt+lint+test+coverage run) — callers MUST invoke this via
    ``loop.run_in_executor(_executor, ...)``, never inline on the event loop,
    mirroring :func:`push_unpushed_repo`.

    Never raises ``subprocess.TimeoutExpired`` or ``OSError`` — both are
    caught and folded into the returned :class:`GateResult` so a gate failure
    or a launch error can never be misclassified by the caller as an agent
    timeout.

    The gate runs against the working tree EXACTLY as the agent left it. This function used to ``git stash push`` any uncommitted changes first, on the theory that a gate should only judge committed work. That theory destroyed four runs' worth of real work in one night: agents that had implemented an item but not yet committed it had their entire tree stashed away, at which point the gate passed trivially against the pristine base commit and the worker concluded the work was already done. Stashing is a MUTATION performed on evidence during the act of judging it, and there is no version of that which is safe. A dirty tree is now handled where it belongs — by the rescue path at teardown, which commits and pushes it to the run's own branch rather than hiding it.
    """
    # ``tmp_dir`` (the run's private TMPDIR) rides in the argv via
    # /usr/bin/env, inside the sudo wrap: sudo strips TMPDIR from the env.
    argv = _sudo_wrap(
        with_tmp_env(
            [
                "/bin/bash",
                "-c",
                _GATE_SHIM,
                "agent-gtd-gate",
                f"{config.CANCEL_GRACE_SECONDS}s",
                f"{timeout_seconds}s",
                gate_command,
            ],
            tmp_dir,
        )
    )
    env = {
        k: v
        for k, v in build_env(engine, mode=DispatchMode.BUILD).items()
        if k in GATE_ENV_KEYS
    }

    with tempfile.TemporaryFile() as stdout_file:
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=subprocess.STDOUT,
                # The sudo wrapper must lead its own session so that on
                # timeout/cancel the WHOLE tree (agent + its commands, each in
                # its own process group since talos 0.15.0-11) can be killed
                # by session id — see kill_run_session (kb-03965).
                start_new_session=True,
            )
        except OSError as exc:
            return GateResult(
                returncode=None,
                timed_out=False,
                output=f"gate launch failed: {exc}",
                duration_seconds=time.monotonic() - start,
            )

        if popen_callback is not None:
            popen_callback(proc)

        def _tail() -> str:
            size = stdout_file.tell()
            stdout_file.seek(max(0, size - 4 * GATE_OUTPUT_TAIL_CHARS))
            data = stdout_file.read()
            return data.decode("utf-8", errors="replace")[-GATE_OUTPUT_TAIL_CHARS:]

        try:
            proc.wait(timeout=timeout_seconds + 2 * config.CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Kill the whole session, not just the sudo wrapper: sudo cannot
            # relay SIGKILL, so proc.kill() alone would orphan the gate's
            # command tree (kb-03965).
            kill_run_session(proc, signal.SIGKILL)
            proc.wait()
            return GateResult(
                returncode=None,
                timed_out=True,
                output=_tail(),
                duration_seconds=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        rc = proc.returncode
        return GateResult(
            returncode=rc,
            timed_out=_classify_gate_timeout(rc, duration, timeout_seconds),
            output=_tail(),
            duration_seconds=duration,
        )


def _detect_default_branch(repo_path: Path) -> str:
    """Detect the default branch for a cloned repo (detection only, no checkout).

    Steps:
    1. git remote set-head origin --auto  (non-fatal if it fails)
    2. git symbolic-ref --short refs/remotes/origin/HEAD  → extract branch name
    3. If step 2 fails, probe remote branches via git branch -r against
       _DEFAULT_BRANCH_CANDIDATES (defaults to _DEFAULT_BRANCH_CANDIDATES[0])

    Returns the detected default branch name (e.g. 'main' or 'master').
    """
    subprocess.run(
        _sudo_wrap(["git", "remote", "set-head", "origin", "--auto"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    result = subprocess.run(
        _sudo_wrap(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        branches_result = subprocess.run(
            _sudo_wrap(["git", "branch", "-r", "--format=%(refname:short)"]),
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        remote_branches = branches_result.stdout.splitlines()
        default_branch = _DEFAULT_BRANCH_CANDIDATES[0]
        for candidate in _DEFAULT_BRANCH_CANDIDATES:
            if f"origin/{candidate}" in remote_branches:
                default_branch = candidate
                break
    else:
        default_branch = result.stdout.strip().removeprefix("origin/")
    return default_branch


def commits_ahead_of_base(repo_path: Path, *, base_branch: str | None = None) -> int:
    """Count commits on HEAD that the repo's BASE branch does not have.

    The base is DETECTED per repo via :func:`_detect_default_branch` (never a
    hardcoded ``main``), and the count is taken against ``origin/<base>`` first,
    falling back to a local ``<base>`` ref when the remote-tracking ref is
    absent.

    This is the disambiguator for a ``git commit`` that failed on a CLEAN tree:
    ahead of base means the agent had already committed its work and merely
    attempted a redundant final commit; not ahead means the agent produced
    nothing at all.

    Returns 0 when the count cannot be determined, so an undeterminable base
    can never promote a do-nothing run to success.
    """
    base = base_branch or _detect_default_branch(repo_path)
    for ref in (f"origin/{base}", base):
        result = subprocess.run(
            _sudo_wrap(["git", "rev-list", "--count", f"{ref}..HEAD"]),
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        try:
            return int(result.stdout.strip())
        except (AttributeError, ValueError):
            continue
    return 0


def prepare_manage_workspace(git_origin: str, run_id: str) -> Path:
    """Clone the repo for manage mode and detect the default branch.

    Steps:
    1. git clone --depth=50 {git_origin} {workspace}
    2. git remote set-head origin --auto  (populate HEAD ref; non-fatal if it fails)
    3. git symbolic-ref --short refs/remotes/origin/HEAD  → detect default branch
    4. If step 3 fails, probe remote branches via git branch -r and pick the first
       match from _DEFAULT_BRANCH_CANDIDATES (defaults to _DEFAULT_BRANCH_CANDIDATES[0])
    5. git checkout {default_branch}  (explicit, stays on default branch)

    Returns the workspace path.
    """
    workspace = config.WORKSPACE_ROOT / f"repos-{run_id}"

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _sudo_wrap(["git", "clone", "--depth=50", git_origin, str(workspace)]),
        check=True,
        capture_output=True,
    )
    default_branch = _detect_default_branch(workspace)
    subprocess.run(
        _sudo_wrap(["git", "checkout", default_branch]),
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    return workspace


def prepare_manage_workspace_multi(repo_urls: list[str], run_id: str) -> Path:
    """Clone multiple repos for manage mode, each checked out on its default branch.

    Workspace root is ``config.WORKSPACE_ROOT / f'repos-{run_id}'``.

    - Python-mkdirs only ``config.WORKSPACE_ROOT`` (not the workspace root).
    - Creates the workspace root via a sudo-wrapped ``mkdir -p`` so that under
      the two-user split (``AGENT_SUBPROCESS_USER`` set) the agent user owns it.
    - Clones each URL **in order** with ``--depth=50`` into
      ``<root>/<repo_dir_from_url(url)>``.
    - Detects and checks out each repo's default branch (no feature branch).
    - Returns the workspace root ``Path``.

    Raises ``ValueError`` before any subprocess if *repo_urls* is empty, if two
    URLs map to the same directory name, or if any URL produces an empty
    basename.  Raises ``RuntimeError`` on clone or checkout failure.
    """
    if not repo_urls:
        raise ValueError("workspace_repos must not be empty")

    # Validate / resolve directory names before touching the filesystem
    dir_names: list[str] = []
    for url in repo_urls:
        dir_names.append(repo_dir_from_url(url))  # raises ValueError on empty basename

    seen: set[str] = set()
    for name in dir_names:
        if name in seen:
            raise ValueError(f"Duplicate workspace repo directory: '{name}'")
        seen.add(name)

    # Python-mkdirs ONLY config.WORKSPACE_ROOT (mirrors prepare_workspace_multi)
    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    root = config.WORKSPACE_ROOT / f"repos-{run_id}"

    # Create workspace root via subprocess so the agent user owns it
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root)]),
        check=True,
        capture_output=True,
    )

    for url, dir_name in zip(repo_urls, dir_names, strict=False):
        dest = root / dir_name

        result = subprocess.run(
            _sudo_wrap(["git", "clone", "--depth=50", url, str(dest)]),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace clone failed for {url}: {stderr_tail}")

        default_branch = _detect_default_branch(dest)

        result = subprocess.run(
            _sudo_wrap(["git", "checkout", default_branch]),
            cwd=dest,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"workspace checkout failed for {url}: {stderr_tail}")

    return root


@dataclass(frozen=True, slots=True)
class RescueResult:
    """What the pre-teardown rescue did to one repo."""

    repo_name: str
    #: True when this repo had unpushed commits or a dirty tree to rescue.
    attempted: bool
    #: True when the rescued branch reached origin.
    pushed: bool
    #: True when uncommitted working-tree changes were committed by the rescue.
    committed: bool
    #: Operator-facing failure text; empty when nothing went wrong.
    error: str = ""
    #: The remote branch the rescue pushed to — the run's branch, or its
    #: ``-r<N>`` fallback when that branch held an older run's commits.
    branch: str = ""


def _repo_has_unrescued_work(repo_path: Path, branch_name: str) -> tuple[bool, bool]:
    """Return ``(has_work, tree_is_dirty)`` for one repo on ``branch_name``.

    ``has_work`` is true when the repo holds anything that would be destroyed by teardown: uncommitted changes in the working tree (tracked, modified OR untracked — the sweep-style ``--untracked-files=no`` used for push verification is deliberately NOT used here, because a run whose entire output is new files would look clean), or commits on the branch that are not on the remote.

    The remote comparison is against the LIVE remote ref via ``git ls-remote``, never against the clone's own ``origin/main``. A dispatch clone's remote-tracking refs are frozen at clone time, so comparing against them reports work as unpushed forever — a permanent false positive that would make every teardown rescue-push something already safely on origin.
    """
    porcelain = subprocess.run(
        _sudo_wrap(["git", "status", "--porcelain"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    dirty = bool(
        porcelain.returncode == 0
        and porcelain.stdout.decode("utf-8", errors="replace").strip()
    )

    head = subprocess.run(
        _sudo_wrap(["git", "rev-parse", "HEAD"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    if head.returncode != 0:
        return dirty, dirty
    local_sha = head.stdout.decode("utf-8", errors="replace").strip()

    remote = subprocess.run(
        _sudo_wrap(["git", "ls-remote", "origin", f"refs/heads/{branch_name}"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    if remote.returncode != 0:
        # Cannot establish what origin has. Fail LOUD, not quiet: assume there
        # is work to rescue. A redundant push is refused harmlessly by git; a
        # skipped one deletes the work.
        return True, dirty
    remote_line = remote.stdout.decode("utf-8", errors="replace").strip()
    remote_sha = remote_line.split("\t")[0] if remote_line else ""
    if remote_sha:
        return (dirty or remote_sha != local_sha), dirty
    # The run's branch is absent on origin. HEAD alone proves nothing: an
    # untouched repo sits on the base commit, which is already on a remote.
    # Only commits reachable from no remote-tracking ref are real work.
    count = subprocess.run(
        _sudo_wrap(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"]),
        cwd=repo_path,
        check=False,
        capture_output=True,
    )
    if count.returncode != 0:
        return True, dirty
    try:
        unpushed = int(count.stdout.decode("utf-8", errors="replace").strip() or "0")
    except ValueError:
        return True, dirty
    return (dirty or unpushed > 0), dirty


def rescue_abandoned_work(
    repo_name: str, repo_path: Path, branch_name: str, run_id: str
) -> RescueResult:
    """Commit and push work an agent left behind, immediately before teardown.

    This is the LAST line of defence and it exists because teardown is irreversible. A dispatch clone is deleted at run exit, so anything the agent finished but did not push — commits it made and never pushed, or work still sitting in the working tree — is destroyed at that moment, whatever the run's recorded status says. One night of runs lost roughly 560 agent-turns of completed, gate-green work this way.

    Three properties make this safe to run unattended:

    Hooks are SKIPPED (``--no-verify``) on both the commit and the push. This is the deliberate opposite of :func:`main._commit_with_retry`, which must run hooks so fixer hooks can fix, and the two paths must NOT be merged. The difference is what is being committed: the normal path commits work an agent declared finished, where a hook rejection is a real signal worth acting on. This path commits work that is by definition INCOMPLETE and will usually fail a hook — and a blocked commit here does not produce a cleaner tree, it produces no tree at all. Preserving the evidence beats enforcing a standard on something nobody is going to merge as-is.

    It only ever writes to the run's OWN ``feat/*`` branch — the same branch the dispatch worker already pushes to on its success path — so it is a retry of an access that already exists, not a new one. It must never push to a default branch, and never force-push. When that branch already holds an older run's commits (a non-fast-forward rejection), the work goes to ``<branch>-r<N>`` instead, exactly as on the success path (:func:`push_with_fallback`).

    A redundant rescue costs nothing: if the worker already pushed, git refuses the ref update and the result simply records it.
    """
    result = RescueResult(repo_name, attempted=False, pushed=False, committed=False)
    try:
        has_work, dirty = _repo_has_unrescued_work(repo_path, branch_name)
    except OSError as exc:
        return RescueResult(
            repo_name,
            attempted=True,
            pushed=False,
            committed=False,
            error=f"could not inspect {repo_name}: {exc}",
        )
    if not has_work:
        return result

    committed = False
    if dirty:
        add = subprocess.run(
            _sudo_wrap(["git", "add", "-A"]),
            cwd=repo_path,
            check=False,
            capture_output=True,
        )
        if add.returncode != 0:
            return RescueResult(
                repo_name,
                attempted=True,
                pushed=False,
                committed=False,
                error=f"git add failed in {repo_name}: {git_output_excerpt(add)}",
            )
        commit = subprocess.run(
            _sudo_wrap(
                [
                    "git",
                    "-c",
                    "user.name=agent-gtd-dispatch",
                    "-c",
                    "user.email=agent-gtd-dispatch@localhost",
                    "commit",
                    "--no-verify",
                    "-m",
                    (
                        "chore(rescue): partial work recovered from abandoned run"
                        f" {run_id}\n\n"
                        "Committed by the dispatch worker at teardown, NOT by the"
                        " agent. This is unreviewed, incomplete work that was"
                        " about to be deleted with the workspace. Hooks were"
                        " skipped; it has not passed any quality gate."
                    ),
                ]
            ),
            cwd=repo_path,
            check=False,
            capture_output=True,
        )
        # A non-zero commit with a now-clean tree means there was nothing to
        # commit after all (a race, or an ignored-only diff). Not an error —
        # fall through to the push, which is the part that matters.
        committed = commit.returncode == 0

    attempt = push_with_fallback(repo_path, branch_name, no_verify=True)
    if attempt.proc.returncode != 0:
        return RescueResult(
            repo_name,
            attempted=True,
            pushed=False,
            committed=committed,
            error=(
                f"git push failed in {repo_name}: {git_output_excerpt(attempt.proc)}"
            ),
            branch=branch_name,
        )
    return RescueResult(
        repo_name,
        attempted=True,
        pushed=True,
        committed=committed,
        branch=attempt.branch,
    )


REAP_TERM_GRACE_SECS = 3.0
_REAP_CMDLINE_MAX_CHARS = 200

# Run as the agent user (python3 is in the sudoers NOPASSWD list, pkill cannot
# select by pid): print "<pid>\t<cmdline>" for every process whose exe or cwd
# lies under argv[1]. Self-contained: the agent user cannot import this package.
_SCAN_SCRIPT = (
    "import os,sys\n"
    "from pathlib import Path\n"
    "w=Path(sys.argv[1])\n"
    "for d in os.listdir('/proc'):\n"
    "  if not d.isdigit(): continue\n"
    "  for l in ('exe','cwd'):\n"
    "    try: t=Path(os.path.realpath('/proc/'+d+'/'+l))\n"
    "    except OSError: continue\n"
    "    if t.is_relative_to(w):\n"
    "      try: c=open('/proc/'+d+'/cmdline','rb').read().replace(b'\\0',b' ').decode('replace')\n"
    "      except OSError: c=''\n"
    "      print(d+'\\t'+c.strip()); break\n"
)
_KILL_SCRIPT = "import os,sys; os.kill(int(sys.argv[1]), int(sys.argv[2]))"


@dataclass(frozen=True)
class ReapedProcess:
    """One process reaped from a run's workspace at teardown."""

    pid: int
    cmdline: str
    killed_with: Literal["SIGTERM", "SIGKILL"]


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return text[:_REAP_CMDLINE_MAX_CHARS]


def _proc_is_live(pid: int) -> bool:
    """True when pid exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # state is the first field after the ``(comm)`` which may itself contain spaces
    state = stat.rsplit(")", 1)[-1].split()[:1]
    return bool(state) and state[0] not in ("Z", "X")


def _ancestor_pids() -> set[int]:
    pids = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[-1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def _scan_workspace_pids(workspace: Path) -> dict[int, str]:
    """Map pid -> cmdline for processes whose exe or cwd is under workspace.

    Path-component match (``is_relative_to``), never a string prefix. Entries
    that vanish or are unreadable are skipped.
    """
    found: dict[int, str] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        for link in ("exe", "cwd"):
            try:
                target = Path(os.path.realpath(f"/proc/{entry}/{link}"))
            except OSError:
                continue
            if target.is_relative_to(workspace):
                found[int(entry)] = _proc_cmdline(int(entry))
                break
    return found


def _scan_as_agent_user(workspace: Path) -> dict[int, str]:
    """Scan /proc as the agent user, whose processes the service user cannot inspect."""
    try:
        proc = subprocess.run(
            _sudo_wrap(["python3", "-c", _SCAN_SCRIPT, str(workspace)]),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        pid_s, _, cmd = line.partition("\t")
        if pid_s.isdigit():
            found[int(pid_s)] = cmd[:_REAP_CMDLINE_MAX_CHARS]
    return found


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        if not config.AGENT_SUBPROCESS_USER:
            return
        subprocess.run(
            _sudo_wrap(["python3", "-c", _KILL_SCRIPT, str(pid), str(int(sig))]),
            capture_output=True,
            timeout=10,
            check=False,
        )


def reap_workspace_processes(
    workspace: Path, *, term_grace_secs: float = REAP_TERM_GRACE_SECS
) -> list[ReapedProcess]:
    """Kill processes left running from a run's workspace; never raises.

    Agents that verify by running the product can leave detached daemons (own
    session, so killing the agent tree misses them). Selects processes whose
    ``/proc/<pid>/exe`` or ``cwd`` is the workspace or beneath it — by path only,
    never by name, so other runs' workspaces are untouched (``/ws/repos-a`` does
    not match ``/ws/repos-ab``). The worker and its ancestors are never signalled,
    and a workspace outside ``config.WORKSPACE_ROOT`` is ignored (same guard as
    ``cleanup_workspace``). SIGTERM, poll up to ``term_grace_secs``, then SIGKILL
    survivors; zombies do not count as survivors.

    With ``AGENT_SUBPROCESS_USER`` set the agent's processes belong to another
    user and are invisible to ``/proc/<pid>/{exe,cwd}`` here, so the scan is also
    run as that user (``sudo python3``) and signals that hit EPERM are relayed
    the same way. Unreadable /proc entries are skipped, not fatal.
    """
    try:
        workspace = Path(workspace)
        if config.WORKSPACE_ROOT not in workspace.parents:
            return []
        found = _scan_workspace_pids(workspace)
        if config.AGENT_SUBPROCESS_USER:
            found.update(_scan_as_agent_user(workspace))
        for pid in _ancestor_pids():
            found.pop(pid, None)
        found = {p: c for p, c in found.items() if _proc_is_live(p)}
        if not found:
            return []
        for pid in found:
            _signal_pid(pid, signal.SIGTERM)
        deadline = time.monotonic() + term_grace_secs
        alive = set(found)
        while alive and time.monotonic() < deadline:
            alive = {p for p in alive if _proc_is_live(p)}
            if alive:
                time.sleep(0.05)
        alive = {p for p in alive if _proc_is_live(p)}
        for pid in alive:
            _signal_pid(pid, signal.SIGKILL)
        if alive:
            time.sleep(0.2)
        return [
            ReapedProcess(p, c, "SIGKILL" if p in alive else "SIGTERM")
            for p, c in found.items()
        ]
    except Exception:
        logger.exception("reap_workspace_processes failed for %s", workspace)
        return []


def cleanup_workspace(workspace: Path) -> None:
    """Remove a workspace directory after a run completes."""
    if workspace.exists() and config.WORKSPACE_ROOT in workspace.parents:
        if config.AGENT_SUBPROCESS_USER:
            subprocess.run(_sudo_wrap(["rm", "-rf", str(workspace)]), check=False)
        else:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)


def write_transcript(workspace: Path, result: subprocess.CompletedProcess[str]) -> None:
    """No-op: transcript is now streamed continuously during the run by run_agent().

    Kept to avoid breaking any external callers. The file is written by run_agent()
    via subprocess.Popen; this function does nothing.
    """


# Run-scoped paths that must never be committed by an agent: the streamed
# transcript and the completion-artifact directory.  Both names are static.  The
# staged-attachments directory is ALSO run-scoped but its name embeds the run id
# (``{run_id}-attachments/``), so it is passed per call via ``extra_lines``
# rather than mutating this constant — a module-level list that accumulated
# run-scoped entries would leak one run's paths into the next run's excludes.
_GIT_EXCLUDE_LINES: tuple[str, ...] = ("transcript.txt", ".dispatch/")


def attachments_exclude_line(run_id: str) -> str:
    """Git-exclude entry for a run's staged-attachments directory."""
    return f"{run_id}-attachments/"


def _append_exclude_lines(git_exclude: Path, lines: Sequence[str]) -> None:
    """Append the given exclude lines to one exclude file, idempotently.

    Failures are logged at WARNING (never raised): a silent no-op here is how
    ``transcript.txt`` ended up committed.
    """
    try:
        existing = {
            line.strip()
            for line in git_exclude.read_text().splitlines()
            if line.strip()
        }
    except OSError as exc:
        logger.warning("git exclude: cannot read %s: %s", git_exclude, exc)
        return
    missing = [line for line in lines if line not in existing]
    if not missing:
        return
    try:
        with git_exclude.open("a") as f:
            f.write("\n" + "\n".join(missing) + "\n")
    except OSError as exc:
        logger.warning("git exclude: cannot write %s: %s", git_exclude, exc)


def _exclude_repo(repo: Path, lines: Sequence[str]) -> None:
    """Ensure ``<repo>/.git/info/exclude`` exists, then append ``lines``."""
    repo_exclude = repo / ".git" / "info" / "exclude"
    info_dir = repo_exclude.parent
    try:
        # The service may run as a different user from the agent (two-user
        # split). Whatever it creates here must stay group-writable, or the
        # agent cannot write .git/info later (lefthook install puts its
        # checksum there and fails with "permission denied").
        if not info_dir.exists():
            info_dir.mkdir(parents=True)
            info_dir.chmod(0o2775)
        if not repo_exclude.exists():
            repo_exclude.write_text("")
            repo_exclude.chmod(0o664)
    except OSError as exc:
        logger.warning("git exclude: cannot create %s: %s", repo_exclude, exc)
        return
    _append_exclude_lines(repo_exclude, lines)


def _setup_git_exclude(workspace: Path, extra_lines: Sequence[str] = ()) -> None:
    """Exclude the run-scoped paths from git before the subprocess starts.

    Always excludes ``transcript.txt`` and ``.dispatch/``; ``extra_lines`` adds
    per-run entries (the staged ``{run_id}-attachments/`` directory) without the
    module-level constant ever carrying run state between calls.

    Handles both repo modes.  In monorepo mode the workspace root IS the repo
    (``<workspace>/.git`` is a directory); the ``info/exclude`` file is created
    if the clone lacks it.  In multi-repo (workspace) mode the root is not a git
    repo at all and each clone lives in an immediate subdirectory.

    If ``<workspace>/.git`` is a FILE (worktree/gitlink) behaviour is
    deliberately unchanged: nothing is resolved or written for it.

    Repeated calls never duplicate an entry.
    """
    lines = (*_GIT_EXCLUDE_LINES, *extra_lines)
    if (workspace / ".git").is_dir():
        _exclude_repo(workspace, lines)

    try:
        children = sorted(workspace.iterdir())
    except OSError as exc:
        logger.warning("git exclude: cannot list %s: %s", workspace, exc)
        return
    for child in children:
        if not child.is_dir() or not (child / ".git").is_dir():
            continue
        _exclude_repo(child, lines)


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe filesystem use.

    Strips path separators to prevent directory traversal, keeps only
    safe characters [A-Za-z0-9._-], and truncates to 200 chars.
    """
    # Strip path separators to prevent directory traversal
    name = re.sub(r"[/\\]", "", filename)
    # Keep only safe characters
    name = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    # Truncate to 200 chars; fall back to "attachment" if nothing survives
    return name[:200] or "attachment"


async def stage_attachments(
    workspace: Path, run_id: str, item_id: str, *, token: str | None = None
) -> list[dict[str, Any]]:
    """Fetch attachments for the item, write them into {run_id}-attachments/.

    Returns the list of staged attachments (metadata only; for use in the prompt).
    Empty list if the item has no attachments.
    Individual download failures are logged but don't abort the run — the failed
    entry is omitted from the returned list.

    The optional ``token`` is forwarded to ``gtd_client`` so attachments are
    fetched under the dispatching user's identity (when the sender provided a
    per-run callback token). None falls back to the static service key.
    """
    try:
        attachments = await gtd_client.list_attachments(item_id, token=token)
    except Exception as exc:
        logger.warning("Failed to list attachments for item %s: %s", item_id, exc)
        return []

    if not attachments:
        return []

    attach_dir = workspace / f"{run_id}-attachments"
    attach_dir.mkdir(mode=0o700, exist_ok=True)

    staged: list[dict[str, Any]] = []
    for attachment in attachments:
        att_id = attachment["id"]
        raw_filename = attachment.get("filename", "attachment")
        filename = _sanitize_filename(raw_filename)
        try:
            data = await gtd_client.download_attachment(att_id, token=token)
            (attach_dir / filename).write_bytes(data)
            staged.append(attachment)
        except Exception as exc:
            logger.warning(
                "Failed to download attachment %s: %s — skipping", att_id, exc
            )

    return staged


def _build_supporting_files_section(
    attachments: list[dict[str, Any]] | None, run_id: str
) -> str:
    """Build the Supporting Files prompt section, or empty string if not applicable."""
    if not attachments or not run_id:
        return ""

    att_lines = []
    for att in attachments:
        filename = att.get("filename", "attachment")
        mime_type = att.get("mime_type", "application/octet-stream")
        size_kb = round(att.get("size_bytes", 0) / 1024, 1)
        att_lines.append(f"- `{filename}` ({mime_type}, {size_kb} KB)")

    file_list = "\n".join(att_lines)
    return (
        "## Supporting Files\n\n"
        "The human attached these files to this item. They're available in the\n"
        f"`{run_id}-attachments/` directory of your workspace:\n\n"
        f"{file_list}\n\n"
        "Read them when relevant to your task. The\n"
        f"`{run_id}-attachments/` directory exists only for this run and is\n"
        "already git-excluded for you — you cannot commit it by accident."
    )


def _build_workspace_layout_section_build(
    workspace_repo_dirs: list[str], branch_name: str
) -> str:
    """'## Workspace Layout' section for build-mode workspace runs."""
    repo_bullets = "\n".join(f"- `{d}/`" for d in workspace_repo_dirs)
    return textwrap.dedent(
        f"""\
        ## Workspace Layout

        Your working directory is a **workspace root** containing these cloned repos:

        {repo_bullets}

        Branch `{branch_name}` is already created and checked out in every repo —
        the service did this before launching. **Never create branches.**

        - **Commit** your changes in whichever repos you modify.
        - **Push `{branch_name}` to origin ONLY in repos where you made commits.**
        - Run `git push` **in the foreground** in each such repo. NEVER invoke `git push`
          with `run_in_background` or any other async/background execution mechanism.
          Pre-push hooks may run the full test suite and take several minutes — that is
          expected; wait for the command to exit. Do not end your turn or session while a
          `git push` you started is still running.
        - After each push exits 0, verify the remote ref advanced. Run in that repo's directory:
          ```bash
          git ls-remote origin refs/heads/{branch_name}
          ```
          Compare the returned SHA against `git rev-parse HEAD`. If the SHAs do not match
          (or no SHA is returned), post a failure comment and do NOT set item status to `review`.
        - Your final success comment **MUST list exactly which repos you pushed to**."""
    )


def _build_workspace_layout_section_plan(workspace_repo_dirs: list[str]) -> str:
    """'## Workspace Layout' section for plan-mode workspace runs (read-only)."""
    repo_bullets = "\n".join(f"- `{d}/`" for d in workspace_repo_dirs)
    example_dirs = ", ".join(f"`{d}/`" for d in workspace_repo_dirs)
    first = workspace_repo_dirs[0]
    return textwrap.dedent(
        f"""\
        ## Workspace Layout

        Your working directory is a **workspace root** containing these cloned repos:

        {repo_bullets}

        Explore across all of them as needed to understand the codebase.

        **Path prefix rule:** every `files_to_modify` path MUST be prefixed with its
        repo directory ({example_dirs}), e.g. `{first}/src/foo.py`, not `src/foo.py`.
        The implementer's working directory is the workspace root, so unprefixed
        paths are ambiguous. Use the repo directories listed above."""
    )


def build_system_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    branch_name: str | None,
    max_turns: int,
    mode: DispatchMode = DispatchMode.BUILD,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    rollout_id: str | None = None,
    workspace_repo_dirs: list[str] | None = None,
    workspace: Path | None = None,
    merge_notes: list[dict[str, Any]] | None = None,
    engine_name: str | None = None,
) -> str:
    """Build the headless agent system prompt.

    ``merge_notes`` is the rollout's most recent merge notes (newest first);
    manage mode renders up to ``MERGE_NOTE_CONTEXT_LIMIT`` of them so the
    AC-reconciliation step reasons from a durable record rather than memory.
    """
    if mode == DispatchMode.PLAN:
        return _build_plan_prompt(
            item,
            project,
            max_turns,
            attachments=attachments,
            run_id=run_id,
            workspace_repo_dirs=workspace_repo_dirs,
            engine_name=engine_name,
        )
    if mode == DispatchMode.REVIEW:
        # REVIEW prompts need the review envelope (branch, diff target, gate
        # result, merge notes, re-dispatch tally) that this signature does not
        # carry. The wave loop calls build_review_prompt directly; routing here
        # would only be able to build a wrong prompt silently.
        msg = "REVIEW mode builds its prompt via build_review_prompt()"
        raise ValueError(msg)
    if mode == DispatchMode.MANAGE:
        return _build_manage_prompt(
            rollout_id or "",
            project,
            max_turns,
            workspace_repo_dirs=workspace_repo_dirs,
            merge_notes=merge_notes,
            engine_name=engine_name,
        )
    return _build_build_prompt(
        item,
        project,
        branch_name or "",
        max_turns,
        workspace=workspace or Path("."),
        attachments=attachments,
        run_id=run_id,
        workspace_repo_dirs=workspace_repo_dirs,
        engine_name=engine_name,
    )


ENGINE_RUBRIC_DESCRIPTIONS: dict[str, str] = {
    "claude-code": "cloud Opus, expensive, slower, most capable reasoning (the default engine)",
    "claude-code-sonnet": "cloud Sonnet, medium cost, fast, strong reasoning for well-scoped work",
    "claude-code-haiku": "cloud Haiku, very cheap, fast, weaker reasoning",
    "claude-code-ollama": "Claude Code on a local Ollama model, free, weak; not routed by the rubric",
    "claude-code-glm": "Claude Code on a GLM model, cheap, moderate reasoning",
    "kiro": "Kiro CLI engine, alternative agent harness",
    "talos-haiku": "talos harness with Haiku, very cheap, mechanical work",
    "talos-sonnet": "talos harness with Sonnet, medium cost, well-scoped work",
    "talos-opus": "talos harness with Opus, expensive, most capable",
    "talos-qwen": "talos harness with Qwen, cheap, moderate reasoning",
    "talos-glm": "talos harness with GLM, cheap, moderate reasoning",
    "talos-glm-flash": "talos harness with GLM Flash, cheapest, mechanical work",
}


def _render_engine_rubric_list() -> str:
    """Bullet list of every BuildEngine member, in the plan prompt's 8-space indent."""
    return "\n".join(
        f"        - **`{m.value}`** — "
        f"{ENGINE_RUBRIC_DESCRIPTIONS.get(m.value, 'no description')}"
        for m in BuildEngine
    )


def _build_plan_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    max_turns: int,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    workspace_repo_dirs: list[str] | None = None,
    engine_name: str | None = None,
) -> str:
    """System prompt for plan mode — groom a task, don't build it."""
    item_id = item["id"]
    title = item["title"]
    description = item.get("description", "")
    project_name = project["name"]

    desc_block = (
        f"**Description:**\n{description}"
        if description
        else "No description provided — work from the title only."
    )

    files_section = _build_supporting_files_section(attachments, run_id)
    current_engine = item.get("build_engine")
    if current_engine:
        current_engine_line = (
            f'This item already has `build_engine="{current_engine}"`. Keep it '
            "(pass the same value) unless the rubric clearly says otherwise."
        )
    else:
        current_engine_line = (
            "The item has no build_engine yet; choose one per the rubric."
        )
    engine_list = _render_engine_rubric_list()

    prompt = textwrap.dedent(
        f"""\
        You are a headless planning agent dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: PLAN** — You are grooming this task, NOT implementing it.

        **Project:** {project_name}
        **Item:** {title}
        **Item ID:** {item_id}

        {desc_block}
        """
    )

    if files_section:
        prompt += "\n" + files_section + "\n"

    if workspace_repo_dirs:
        prompt += (
            "\n" + _build_workspace_layout_section_plan(workspace_repo_dirs) + "\n"
        )

    prompt += "\n" + _standard_headless_rules(engine_name) + "\n"

    prompt += textwrap.dedent(
        f"""\

        ## Before You Begin

        Before writing any acceptance criteria, complete these three steps:

        1. **Read repo conventions** — Read `docs/codebase.md`, `docs/architecture.md`,
           `docs/domain.md` (any that exist). Fall back to `CLAUDE.md` if none found;
           note the gap in the plan output.
        2. **Search the KB** — Call `kb_search(project_ref="{project_name}")` to skim for
           relevant conventions, anti-patterns, and prior decisions. Pull applicable
           `kb-XXXXX` IDs into the plan output.
        3. **Architectural-awareness sweep** — Before finalizing AC, explicitly call out:
           - **Magic strings**: should any be a `Literal` type or enum instead of bare strings?
           - **Duplication risk**: does this logic risk duplicating something already in a
             shared module or utility?
           - **Typed data homes**: do any data shapes already have a typed home (Pydantic
             model, TypedDict, or dataclass)?
           State "No architectural concerns found" if clean.
        """
    )

    example_path = (
        f"{workspace_repo_dirs[0]}/src/foo.py" if workspace_repo_dirs else "src/foo.py"
    )
    prompt += textwrap.dedent(
        f"""\

        ## What to do

        1. **Read the codebase.** Understand existing patterns, architecture, and conventions.
        2. **Write structured fields.** Call `update_item` with the structured fields — legality
           validation reads these, not description prose, so these calls are mandatory:
           - `acceptance_criteria`: list of testable AC strings, e.g.
             `acceptance_criteria=["AC-1: the widget renders", "AC-2: tests pass"]`
           - `files_to_modify`: list of dicts with `"path"` and `"change"` keys, e.g.
             `files_to_modify=[{{"path": "{example_path}", "change": "add error handling"}}, ...]`
           - `scope_out`: list of things explicitly out of scope, e.g.
             `scope_out=["Do NOT change the API surface", "Do NOT touch unrelated modules"]`
           - `commit_type`: exactly one of `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `chore` — the conventional-commit type the squash commit will carry (feat = new observable capability, fix = corrects wrong behaviour, perf = faster with no behaviour change, refactor = no observable change, test = only tests, docs = only docs, chore = anything else). plan_rollout rejects an item without it, e.g. `commit_type="fix"`
           Free-form `description` is still fine for context/lead paragraph, but the structured
           fields are the source of truth that the legality validator checks.
        3. **Add patterns to follow.** Reference existing code the implementer should copy.
        4. **Define scope boundaries.** Explicitly state what NOT to touch (use `scope_out`).
        5. **Add verification steps.** How to test the changes (commands, expected output).
        6. **Select build engine.** Evaluate this task against the Engine-Selection Rubric below.
           - ALWAYS call `update_item` with `build_engine` set to exactly one engine name
             from the rubric, e.g. `update_item(build_engine="<engine-name>")`.
           - Never write the `build_engine` key without a value, and never omit it to mean
             "default". Every plan run names an engine explicitly.
           - {current_engine_line}
           - When uncertain, route UP (toward the most capable engine), not down.
        7. **Ask questions if unclear.** If the intent is ambiguous, post a comment asking
           for clarification and stop. Do NOT guess.

        ## Rules

        - Do NOT write code, create branches, or push anything.
        - Do NOT modify any files in the repo.
        - Use `update_item` (with the item's current version) to set structured fields
          (`acceptance_criteria`, `files_to_modify`, `scope_out`, `commit_type`) and optionally `description`.
        - **Legality validation reads `acceptance_criteria` and `files_to_modify` from the
          structured fields only** — prose Markdown in `description` is ignored by the validator.
        - Use `add_comment` with item_id="{item_id}" for questions or notes.
        - When grooming is complete, set item status to `ready` using `update_item`.
        - If an `update_item` call fails, retry with fewer fields per call (for example
          `acceptance_criteria` alone, then the other fields one call each) rather than
          resending the same payload. Write `acceptance_criteria` first.

        ## Engine-Selection Rubric

        These engines are available for build-mode dispatches:

{engine_list}

        ### Route to `claude-code-haiku` when ALL of these hold

        1. **Single-file or tightly bounded** — changes touch 1-3 files, no orchestration across modules
        2. **Pattern-following** — the AC can be expressed as "make X look like Y"; a clear template exists
        3. **Mechanical edits dominate** — renames, string/copy changes, format fixes, type tightening, null guards
        4. **Tests are clone-and-modify** — new tests fit an existing test pattern; no novel test design
        5. **No cross-cutting decisions or novel design** — right place is obvious from AC; no judgment needed
        6. **Wall-clock speed matters** — Haiku completes in <60 s

        ### Route to `claude-code-sonnet` when

        - Item has populated `acceptance_criteria` and `files_to_modify` structured fields AND
        - Task is too complex for mechanical pattern-matching (4+ files, or per-file logic is non-trivial), BUT
        - No novel design decisions, no debugging, no cross-cutting judgment
        - "Well-scoped non-trivial" sweet spot — the plan agent did the thinking, builder needs strong execution

        ### Route to `claude-code` when ANY of these hold

        1. Multi-file orchestration with coordinating intent across modules
        2. Novel design decisions ("decide whether…", "design a way to…")
        3. Debugging (root cause not named in the description)
        4. Cross-cutting concerns (auth, error handling, migrations, threading)
        5. New API/protocol surface
        6. Test design from scratch
        7. Wide blast radius (model field changes, schema migrations affecting many consumers)
        8. Security or data-integrity sensitive
        9. Plan/manage mode — these always use the default; rubric only applies to `mode=build`

        ### Default policy

        When uncertain, route UP (toward the most capable engine), not down. The cost of a failed cheap-engine attempt (re-dispatch + lead intervention) outweighs the savings.

        ## Reporting

        Post a comment when you start: "Planning..."

        **On success:**
        1. Post a comment summarizing the structured fields you set and the build engine selected
        2. Set item status to `ready`

        **On failure/blocked:**
        1. Post a comment explaining what's unclear
        2. Leave status unchanged

        ## Important

        - You have max {max_turns} turns. Budget them wisely.
        - Focus only on this task. Don't groom other items you notice.
    """
    )

    return prompt


def _indent_prompt_block(text: str) -> str:
    """Re-indent a prompt fragment to the manage templates' 8-space body indent.

    The manage prompts are ``textwrap.dedent(f"...")`` literals: every body line
    carries eight leading spaces. An interpolated fragment must carry the same
    indentation or ``dedent()`` finds a zero-width common prefix and silently
    becomes a no-op for the whole prompt. The leading indent of the first line is
    stripped because the interpolation point already sits at that column.
    """
    return textwrap.indent(text.rstrip("\n"), " " * 8).lstrip(" ")


# Number of most-recent merge notes rendered into each manage prompt.
#
# Trade-off: every note costs prompt tokens in EVERY subsequent manage launch
# (including each relaunch), so an unbounded list would grow the prompt with the
# rollout.  Too few, and the AC-reconciliation step stops seeing the change that
# actually invalidated the item it is about to dispatch — which is the exact
# failure this record exists to prevent.  Ten covers a handful of items per wave
# across two or three waves at a cost of a few hundred tokens.  Raise it only
# alongside a measurement of manage-prompt size.
MERGE_NOTE_CONTEXT_LIMIT = 10


def _manage_merge_bar_block(
    gate_command: str, step_label: str, *, workspace: bool
) -> str:
    """Warm-up merge-bar step — stored ``gate_command``, or inference fallback.

    The merge bar used to be INFERRED: warm-up had the manager read
    ``CLAUDE.md`` / ``README.md`` and derive a test/lint/coverage command.  Two
    managers could infer two different bars for the same repo and apply
    different standards to consecutive items.  The project record already
    carries ``gate_command`` — the same command the post-run gate re-runs — so
    the bar is a stored value, not a guess.

    When ``gate_command`` is empty the prompt says so EXPLICITLY and falls back
    to the old inference behaviour: a gate-less project must not silently become
    a rollout with no quality bar.
    """
    where = "the workspace root" if workspace else "the repo root"
    gate = gate_command.strip()
    if gate:
        body = f"""\
**{step_label} — The merge bar is the project's stored `gate_command`**

The merge bar is NOT something you infer. This project has a stored
`gate_command`, and that command IS the definition of Done — the same command
the dispatch worker re-runs as the post-run gate. Use it verbatim, from
{where}:

```bash
{gate}
```

Do NOT derive a different test / lint / coverage command from `CLAUDE.md` or
`README.md` and then merge against that instead. Two managers inferring two
different bars for the same repo apply two different standards to consecutive
items — that is a correctness problem, not a style one. The stored
`gate_command` is the one bar for every item in this rollout.

Still READ `CLAUDE.md` / `README.md`, for project CONVENTIONS — commit style,
branch rules, directory layout, anything a reviewer should honour. Just do not
take the executable merge bar from them."""
    else:
        body = f"""\
**{step_label} — The merge bar (this project has an EMPTY `gate_command`)**

This project has NO stored `gate_command`, so you are in FALLBACK mode: you
must INFER the merge bar. Read `CLAUDE.md` and/or `README.md` and record:
- Test command (e.g. `uv run pytest`, `npm test`)
- Lint command (e.g. `uv run ruff check src/ tests/`, `npm run lint`)
- Coverage threshold (if any)
- Any project-specific merge conventions

Say the consequence to yourself plainly: a gate-less project is NOT a rollout
with no quality bar. Run whatever you inferred, from {where}, before every
merge, and apply the SAME inferred bar to every item in this rollout — do not
re-derive it per item. If a repo has no discoverable test or lint command at
all, record `none` for that repo and continue — do NOT halt."""
    return _indent_prompt_block(body)


def _manage_recent_merge_notes_block(merge_notes: list[dict[str, Any]] | None) -> str:
    """Render the most recent merge notes for this rollout into the prompt.

    This is the read side of the durable record that replaces a manager's
    in-context memory. A relaunched manager used to lose every cross-item change
    the previous manager had seen; now it is handed the last
    ``MERGE_NOTE_CONTEXT_LIMIT`` notes verbatim.
    """
    notes = list(merge_notes or [])[:MERGE_NOTE_CONTEXT_LIMIT]
    if not notes:
        body = """\
## Recent Merge Notes — none yet

No item in this rollout has been merged with a merge note yet. You are the
first: every item you merge must carry one (see **Step 6b** below)."""
        return _indent_prompt_block(body)

    rows = []
    for n in notes:
        item_id = str(n.get("item_id") or "unknown")
        note = " ".join(str(n.get("note") or "").split())
        rows.append(f"- item `{item_id}`: {note}")
    rendered = "\n".join(rows)
    body = f"""\
## Recent Merge Notes — the durable cross-item record

These are the last {len(notes)} merge notes recorded for THIS rollout, newest
first. They are the persisted record of what already-merged items changed that
could invalidate a later item's spec. Read them as fact, and use them in
**Step 4 — AC reconciliation** instead of relying on what you happen to
remember; if you are a relaunched manager, this is context you would otherwise
have lost entirely.

{rendered}

At most {MERGE_NOTE_CONTEXT_LIMIT} notes are carried here. If an item's spec
looks inconsistent with something older, read the rollout's full event history
rather than assuming nothing else changed."""
    return _indent_prompt_block(body)


def _manage_merge_note_block() -> str:
    """Step 6b — the merge-note instruction, shared by both manage variants."""
    body = """\
**Step 6b — Record the MERGE NOTE (required for every item you merge)**

Before you complete the item, write down what the merged work changed that
could make a LATER item's spec wrong. You pass this as the `merge_note`
argument of the SAME `complete_item_in_rollout` call you make in Step 7 — it is
persisted as a durable `merge_note` rollout event and rendered into every
subsequent manage prompt.

The content requirement is narrow and concrete. Name ONLY:
- changed or new PUBLIC function / method signatures (module + name + what changed)
- renamed classes, modules or files
- changed or new config keys, settings names, env vars, DB columns or API routes

Do NOT write a prose summary of the diff — that is what the commit message is
for. A vague note is WORSE than no note, because it looks like coverage while
carrying none. One or two lines. If the merged work changed none of the above,
write exactly: `no signature, rename or config-key changes`.

Example of the right shape:

```
resolve_max_turns() gained a third param `mode`; class WaveManager renamed to
RolloutManager; new config key dispatch.manager_default_timeout_minutes
```"""
    return _indent_prompt_block(body)


def _manage_commit_type_block() -> str:
    """Squash-commit type derivation rule, shared by both manage variants."""
    body = """\
**Deriving the squash commit TYPE — never hard-code `feat`**

The squash commit drives semantic-release, so its type is a VERSION decision,
not a formatting one. Derive `<type>` per item with these rules, stopping at the
first that matches:

1. The item TITLE begins with a conventional-commit prefix
   (`feat` / `fix` / `chore` / `docs` / `refactor` / `test` / `perf` / `build` /
   `ci`, with or without a `(scope)`, followed by `:`) → use that type, and
   strip the prefix from the commit subject so it is not repeated.
2. Otherwise, the item's LABELS name a type: `bug` or `fix` → `fix`;
   `feature` or `enhancement` → `feat`; `docs` → `docs`;
   `refactor` → `refactor`; `chore`, `prep` or `maintenance` → `chore`.
3. Otherwise, derive the type from what the merged diff does. First match wins:
   - `feat` — adds or changes a capability a user, operator or calling agent
     can observe (new or changed endpoint, CLI verb, MCP tool, config option or
     env var, UI, or behaviour).
   - `fix` — corrects behaviour that was wrong.
   - `perf` — same behaviour, measurably faster or cheaper.
   - `refactor` — restructures code with no observable behaviour change.
   - `test` — only tests or test fixtures change.
   - `docs` — only documentation changes.
   - `chore` — anything else (dependency bumps, build or tooling housekeeping).

A change someone using the project could notice is `feat` or `fix`, never
`chore`, because a chore is invisible to the changelog."""
    return _indent_prompt_block(body)


STANDARD_RULES_HEADING: str = "## Standard rules for headless runs"


def _standard_headless_rules(engine_name: str | None = None) -> str:
    """The always-true rules for every headless run, rendered into every mode's prompt.

    One section, one wording. The copy is engine-agnostic on purpose: it says "the agent", "the worker" and "the configured engine" and never names a product. The single engine-conditional bullet (a talos-family agent must not commit, because the worker owns the commit) renders only when ``engine_name`` is a talos-family engine.
    """
    lines = [
        STANDARD_RULES_HEADING,
        "",
        "- Never end your turn while anything you started is still running: the "
        "run ends with your turn, and a backgrounded command dies with it.",
        "- You cannot read service credentials. Never claim an acceptance "
        "criterion that needs a credentialed check is met; say plainly that it "
        "needs the lead.",
        "- You cannot install software or system packages, and must not design a "
        "fix that installs anything at run time; host tooling is provisioned by "
        "the host setup. If a tool is missing, say so in your final report.",
        "- What decides the run's outcome is the commits on the remote branch, "
        "the gate result and the worker's result record; nothing you say about "
        "your own work changes it.",
        "- Never push to or commit on the default branch, except through a merge "
        "procedure this prompt explicitly gives you.",
    ]
    if engine_name and is_talos_engine(engine_name):
        lines.append(
            "- Never run `git commit`: the worker owns the commit for this "
            "configured engine and makes it from your working tree."
        )
    return "\n".join(lines) + "\n"


def _standard_rules_block(engine_name: str | None = None) -> str:
    """:func:`_standard_headless_rules` re-indented for the manage/review templates."""
    return _indent_prompt_block(_standard_headless_rules(engine_name))


_TURN_DISCIPLINE_BODY: str = """\
## Turn Discipline — Never End Your Turn With Something Still Running

This is an INVARIANT about your process, not a list of commands to avoid:

**Never end your turn while anything you started is still running.** Whatever
you launch — a command, a wait, a build, a check — you stay in the foreground
until it exits and you have read its result.

Why it is absolute, stated plainly because you cannot recover from it: you are
launched with `claude --print`, which is one-shot and non-interactive. Your
process IS your turn. When the turn ends the process exits, every child process
and background shell you started is killed with it, and no notification, hook or
callback can ever wake you to collect a result. There is no "come back to it
later" — later does not exist for you.

So: no `run_in_background: true`, no trailing `&`, no `nohup`, no `disown`, no
detached poller script, no waiting on a hand-off that arrives after your turn.
If a command is slow, wait for it anyway. A long foreground wait is always
correct; a backgrounded command is always fatal.

`git push` and the project's quality gate are the two that most often tempt an
agent to background them, because both can take minutes while pre-push hooks or
a full test suite run. They are ILLUSTRATIONS, not the rule. The rule covers
EVERY command you run. An agent that had a perfect rule about `git push` still
lost its entire run by backgrounding a gate command — because a rule that names
specific commands reads as a whitelist of everything it does not name.
"""


def _turn_discipline_block() -> str:
    """Turn-discipline section shared by the build prompt and BOTH manage prompt variants.

    One block, one wording, every prompt. It used to render into the manage prompts only, with the build prompt carrying two bullets scoped to ``git push`` instead — and an agent that had followed that narrower rule perfectly still died backgrounding the project gate, because naming two commands implicitly permits every command not named. The rule here is therefore written as an invariant about the process (``claude --print`` dies with the turn, so nothing can wake you) with specific commands demoted to examples.
    """
    return _indent_prompt_block(_TURN_DISCIPLINE_BODY)


def _manage_wait_step(rollout_id: str, gate_exception: str) -> str:
    """Step 3 — wait for each dispatched run, in the FOREGROUND.

    Deliberately short.  The fuller version (``_manage_step3_block``, with the
    warm-up-skip block beside it) existed to keep a RESIDENT manager alive
    across relaunches; both were retired with that machinery (item 7baf77d0).
    The invariant they encoded survives here and in the shared turn-discipline
    block: never wait on a run by backgrounding it (item 3889efbb).
    """
    body = f"""\
**Step 3 — Wait for each run to finish (FOREGROUND)**

Publish polling state:
```
mcp__agent-gtd__update_rollout_state(
    rollout_id="{rollout_id}",
    phase="polling",
    current_step="Waiting for build runs to complete",
)
```

Wait on each dispatched run in the FOREGROUND, one at a time:

```bash
agent-gtd run-status <run_id> --wait --timeout 540
```

Exit `0` is success and `2` is a terminal failure. Exit `124` means the run is
STILL RUNNING: re-issue the same command. Never background this wait — see
**Turn Discipline** above. Once a wait returns terminal, confirm it with
`mcp__agent-gtd__get_run_status(<run_id>)` and continue with Step 4 for THAT run.

If a run ended with `failed`, `timed_out`, or `cancelled`: treat as a halt
candidate (see Halt path) with reason
`"build agent <status>: run <run_id> for item <item_id>"`.
If a run ended with `already_satisfied`: do NOT halt and do NOT reconcile —
go straight to the skip-and-advance path below."""
    return _indent_prompt_block(body) + gate_exception


def _build_manage_workspace_main_prompt(
    rollout_id: str,
    project: dict[str, Any],
    max_turns: int,
    workspace_repo_dirs: list[str],
    merge_notes: list[dict[str, Any]] | None = None,
    engine_name: str | None = None,
) -> str:
    """Workspace-variant manage prompt: per-repo review, merge, push, cleanup."""
    project_name = project["name"]
    project_id = project.get("id", "")
    repo_bullets = ("\n        ").join(f"- `{d}/`" for d in workspace_repo_dirs)
    first_repo = workspace_repo_dirs[0]
    repos_order = " → ".join(f"`{d}/`" for d in workspace_repo_dirs)

    _gate = (project.get("gate_command") or "").strip()
    _gate_ind = _gate.replace("\n", "\n        ")
    gate_exception = ""
    if _gate:
        gate_exception = (
            "\n\n        "
            "Exception — post-run gate failure: if the run's `error_msg` (from "
            "`get_run_status`) starts with `post-run gate`, the build agent's "
            "branch WAS pushed and only the project gate command failed or "
            "timed out. Do NOT halt yet. Read the item's comment starting "
            "`Post-run gate` for the output tail, then continue with Step 4. "
            "In Step 5b, after checking out the branch in every pushed repo, "
            "ALSO run the project gate command from the workspace root: "
            f"`{_gate_ind}`. Proceed to Step 6 only once it exits 0. If it "
            "fails, apply the inline-fix rules (small fix, then re-run the "
            "same command); otherwise halt with reason "
            '`"post-run gate failure: run <run_id> for item <item_id>"`.'
        )

    step3 = _manage_wait_step(rollout_id, gate_exception)
    turn_discipline = _turn_discipline_block()
    standard_rules = _standard_rules_block(engine_name)
    merge_bar = _manage_merge_bar_block(_gate, "3", workspace=True)
    recent_notes = _manage_recent_merge_notes_block(merge_notes)
    merge_note_step = _manage_merge_note_block()
    commit_type = _manage_commit_type_block()

    return textwrap.dedent(
        f"""\
        You are a headless rollout-manager executor dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: MANAGE** — You are orchestrating a rollout execution and merging build results.

        **Project:** {project_name}
        **Workspace Repos:**
        {repo_bullets}
        **Rollout ID:** {rollout_id}
        **Project ID:** {project_id}
        **Turns remaining:** {max_turns}
        **Time budget:** {config.MANAGE_TIMEOUT_SECONDS // 60} min of wall-clock time. Nothing relaunches you: exiting while any build run is still in flight strands it.

        This rollout ID is your primary anchor. Every action you take is scoped to it.
        Your workspace is a **workspace root** containing one git clone per repo listed above, each checked out on its own default branch (auto-detected).

        ## Launch item_id — Ignore It

        The `item_id` you received as the dispatch trigger is a positional placeholder,
        not a rollout item to act on. **Ignore it entirely.** Your sole source of truth for
        which items to dispatch is the rollout plan — read it via `advance_rollout`.
        Do NOT add comments to the launch item_id.
        Do NOT mark it complete.
        Do NOT treat it as a gate.

        {standard_rules}

        {turn_discipline}

        {recent_notes}

        ## Phase 1 — Warm-up (run once at start, concurrently with wave-1 builds)

        IMPORTANT: Dispatch all wave-1 items first (Phase 2 Step 1 below), THEN run
        warm-up steps while waiting for those builds to complete. Warm-up happens
        concurrently with wave-1 builds — not before them.

        At the start of warm-up, publish your state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="warm_up",
            current_step="Verifying Workspace Repos are green",
        )
        ```

        NOTE on `update_rollout_state`: each call REPLACES all four state fields
        (phase, current_item_id, current_step, last_updated). Fields you omit
        are reset to None. If you want to preserve `current_item_id` across a
        phase change, pass it in every subsequent call.

        For EACH repo in Workspace Repos (run all four steps in that repo's directory):

        **1. Record the default branch** — run BEFORE any other checkout in this repo:
        ```bash
        cd <repo_dir>
        git rev-parse --abbrev-ref HEAD
        ```
        Store this as `<repo_dir>_default_branch` in your working memory.

        **2. Install dependencies**:
        ```bash
        [ -f pyproject.toml ] && uv sync
        [ -f package.json ] && npm install
        ```

        If this repo uses lefthook, pre-commit, husky, or a committed `.agent-gtd/setup`
        override, the dispatch worker already installed and verified its git hooks before
        you launched — do not reinstall or change them.

        {merge_bar}

        **4. Verify that repo's default branch is green** — run the merge bar you just
        established. If it fails, call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="warm-up failure in <repo_dir>: <command>: <error snippet>"
        )
        ```
        and STOP. The project is not in a mergeable state — a human must intervene.

        ## Phase 2 — Wave Loop

        Repeat until `advance_rollout` reports `graph_complete=true`:

        **Step 1 — Advance**
        ```
        mcp__agent-gtd__advance_rollout(rollout_id="{rollout_id}")
        ```
        Returns: `{{next_ready: [...], in_progress: [...], graph_complete: bool}}`

        If `advance_rollout` fails: retry up to 3 times with 30 s sleep between attempts.
        After 3 failures: call `halt_rollout(rollout_id="{rollout_id}",
        reason="advance_rollout failed 3 times")` and EXIT.
        If `graph_complete=true` and `next_ready=[]`: EXIT with success (all done).

        **Step 2 — Dispatch ready items**

        For each `item_id` in `next_ready`, publish state then dispatch:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="dispatching",
            current_item_id=item_id,
            current_step=f"Dispatching {{item_id}}",
        )
        mcp__agent-gtd__dispatch_item(
            item_id=item_id,
            mode="build",
            rollout_id="{rollout_id}",
        )
        ```
        NOTE: `rollout_id` is REQUIRED on every child dispatch — include it always.
        Record the returned `run_id` alongside `item_id`.

        {step3}

        **Step 4 — AC reconciliation**

        Publish reconciliation state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reconciling_ac",
            current_step="Checking downstream AC impact",
        )
        ```

        Start from the **Recent Merge Notes** section above — that is the durable
        record of what earlier items already changed, and it is authoritative over
        your own recollection (a relaunched manager has none). Then call `get_item`
        on items in later waves that share a module or interface with the just-merged
        work. Check whether the just-merged code introduced changes (new function
        signatures, renamed classes, changed config keys) that would cause a later
        item's AC or spec to be wrong.
        If so, call `update_item` to patch that item's description and post a comment
        explaining the change:
        ```
        mcp__agent-gtd__add_comment(
            item_id=<later_item_id>,
            content_markdown="AC updated: <what changed and why>"
        )
        ```

        **Step 5a — Discover pushed repos**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step="Discovering pushed repos for <branch_name>",
        )
        ```

        For each completed build item, determine which Workspace Repos received the feature
        branch. Run in EVERY repo directory:
        ```bash
        cd <repo_dir>
        git ls-remote origin refs/heads/<branch_name>
        ```
        Non-empty output = that repo has the branch. `git ls-remote` is AUTHORITATIVE for
        push verification.

        Also cross-check against the build agent's final success comment. Build agents are
        REQUIRED to list exactly which repos they pushed to. Both disagreement directions
        require a halt — name both sources in the halt reason:
        - If the build comment claims a repo received the branch but `git ls-remote` does
          NOT confirm it: halt — the push was reported as success but the remote ref is absent.
        - If `git ls-remote` shows the branch in a repo that the build comment did NOT list:
          halt — there is unreported partial work in that repo.

        Do NOT use `get_run_status` to determine push verification — the `claude_runs` schema
        has no `push_results` column; structured push results exist only in `git ls-remote`
        output and build agent comments.

        **Step 5b — Review all pushed repos (quality gates — ALL before any merge)**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step="Running quality gates across all pushed repos for <branch_name>",
        )
        ```

        For EACH pushed repo (run inside that repo's directory):
        ```bash
        cd <repo_dir>
        git fetch origin <branch_name>
        git checkout <branch_name>
        # run the merge bar established in warm-up
        ```

        Also inspect the diff for **unrelated manifest changes**. If the diff
        includes additions to `package.json` / `package-lock.json` /
        `pyproject.toml` / `uv.lock` that are NOT directly tied to the item's
        stated scope, treat them as suspect — revert those specific changes via
        `git checkout HEAD -- <file>` and re-run gates.

        **Inline-fix phase boundary:**
        - While ZERO repos for this item have been merged: if a gate failure is small
          (formatting, single missing import, one-line change, coverage ratchet bump,
          stale test assertion), apply an inline fix with `Edit`/`Bash` and re-run gates.
          If the fix fails or is non-trivial, halt.
        - ALL pushed repos must pass quality gates before merging any repo.
        - Once the FIRST repo for an item is merged+pushed, ANY subsequent failure halts
          IMMEDIATELY — no inline fixes, no retries.

        **Step 6 — Squash merge (repo-by-repo in Workspace Repos list order)**

        Publish merging state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="merging",
            current_item_id=item_id,
            current_step=f"Merging <branch_name> repo-by-repo",
        )
        ```

        Merge order: {repos_order} (Workspace Repos list order — pushed repos only).

        For EACH pushed repo, in list order:

        1. Run the commit-count guard immediately before this repo's squash merge.
           Use THAT repo's default branch recorded in warm-up (never a global value):
           ```bash
           cd <repo_dir>
           git fetch origin <branch_name>
           commit_count=$(git rev-list origin/<repo_default_branch>..<branch_name> --count)
           ```
           If `commit_count` is 0: the build agent pushed nothing. Before halting,
           check WHY — call `mcp__agent-gtd__get_run_status(<run_id>)` for that item's
           child build run and read its `status` field.

           If `status` is exactly `already_satisfied`, the work was already done.
           Only a talos build can reach this status (talos exit code 30,
           AlreadySatisfied), and talos's own checks ran green before it emitted
           that verdict. A claude-code build can NEVER report this — a commit-less
           claude-code run is recorded `failed`, so if you see zero commits on one,
           it is a halt candidate, not a skip.
           Skip the merge and advance:
           ```
           mcp__agent-gtd__complete_item_in_rollout(
               rollout_id="{rollout_id}",
               item_id=item_id,
               outcome="skipped",
               merge_actor="manager-autonomous",
               decision_rule="already-satisfied",
           )
           ```
           Then post a comment that NAMES every item id in that call's `newly_ready`
           list (or states `no downstream items unblocked` when the list is empty),
           and ADVANCE to the next item. Do NOT halt and do NOT merge this item.

           For ANY other status, halt as described below.

           Otherwise, if `commit_count` is 0: halt with the multi-repo halt template
           below — step = `commit-count-guard`.

        {commit_type}

        2. Squash merge sequence (inside that repo's directory, against THAT repo's default branch):
           ```bash
           set -euo pipefail
           git checkout <repo_default_branch>
           git merge --squash <branch_name>
           git commit -F - <<'COMMITEOF'
           <type>(<item_id short>): <item title>

           Rollout: {rollout_id}
           Item: <item_id>
           COMMITEOF
           git push origin <repo_default_branch>
           ```

           If ANY line above fails (a squash-merge conflict, a rejected push), STOP:
           leave the feature branch in place and take the halt path. Never delete a
           feature branch whose merge did not land.

        `<type>` is the value you derived immediately above — the SAME type in every
        repo for a given item. Never emit a `feat` type unless the derivation
        actually produced one.

        Record `merged+pushed (<sha>)` for this repo after a successful push.

        Once the FIRST repo for an item is merged+pushed, ANY subsequent failure
        (`fetch` | `gates` | `commit-count-guard` | `squash-merge` | `push` | `branch-cleanup`)
        halts IMMEDIATELY — no inline fixes, no retries.

        Use this EXACT halt template for multi-repo merge failures:
        ```
        Rollout halted: multi-repo merge failure on item <item_id> (branch <branch_name>)
        Per-repo state:
        - <repo_dir>: merged+pushed (<merge commit sha>)
        - <repo_dir>: FAILED — <step>: <error snippet>
        - <repo_dir>: untouched
        ```
        Exactly three per-repo states: `merged+pushed`, `FAILED`, `untouched`.
        `<step>` is one of: `fetch` | `gates` | `commit-count-guard` | `squash-merge` | `push` | `branch-cleanup`.

        **Absolute prohibitions (never cross these):**
        - Do NOT roll back or revert already merged+pushed repos.
        - Do NOT force-push.
        - Do NOT continue merging remaining repos after a failure.

        {merge_note_step}

        **Step 7 — Complete in rollout**

        ```
        result = mcp__agent-gtd__complete_item_in_rollout(
            rollout_id="{rollout_id}",
            item_id=item_id,
            outcome="completed",
            merge_actor="manager-autonomous",
            decision_rule="agent-judgment",
            merge_note=<the merge note you wrote in Step 6b>,
        )
        ```

        `merge_note` is REQUIRED on every merged item — never pass an empty string.

        `complete_item_in_rollout` does two things for you on `outcome="completed"`:
        1. Cascades the item's GTD status to `done` (no need to call
           `complete_item` separately).
        2. Closes the rollout automatically if this was the last terminal item,
           and signals that via `result["graph_complete"]`.

        Check the response:
        - If `result["graph_complete"]` is `true`: the rollout is closed. Before
          exiting, run cleanup (feature-branch deletion and manage-branch cleanup
          below), then EXIT with success — do NOT call `advance_rollout` again
          (it will reject the now-completed rollout).
        - Otherwise: go back to Step 1 (advance) for the next wave / next
          unblocked items.

        **Cleanup — after all pushed repos for an item are merged+pushed**

        Feature branch cleanup (run in EACH pushed repo's directory):
        Never delete a feature branch whose merge did not land. Delete only after
        the squash commit is verified on origin's default branch:
        ```bash
        set -euo pipefail
        cd <repo_dir>
        git fetch origin <repo_default_branch> && test -n "$(git log origin/<repo_default_branch> -1 --format=%H --grep='^Item: <item_id>$')"
        git push origin --delete <branch_name>
        git branch -D <branch_name>
        ```
        If the verification fails, do NOT delete anything — leave the branch in place
        and take the halt path.

        Manage branch cleanup — run ONCE in `{first_repo}/` only, NOT repeated per repo:
        ```bash
        cd {first_repo}
        git push origin --delete feat/{rollout_id[:8]}-manage || true
        ```

        **Halt path**

        When halting, the feature branch must be left intact on origin (never delete
        it) and the halt reason must include the branch name and its tip sha.

        Before halting, publish halted state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="halted",
            current_step=<reason>,
        )
        ```

        On any non-recoverable failure, post a comment to the offending rollout item
        (NOT the launch placeholder item_id):
        ```
        mcp__agent-gtd__add_comment(
            item_id=<offending_rollout_item_id>,
            content_markdown="Rollout halted: <reason>"
        )
        ```
        If there is no specific offending item (e.g. `advance_rollout` failed 3 times),
        post to the project instead:
        ```
        mcp__agent-gtd__add_comment(
            project_id="{project_id}",
            content_markdown="Rollout halted: <reason>"
        )
        ```
        Then call:
        ```
        mcp__agent-gtd__halt_rollout(rollout_id="{rollout_id}", reason=<reason>)
        ```
        And STOP.

        ## Phase 3 — Sensitive-area guidance

        Before auto-merging, inspect the diff. If the build touches any of the following
        patterns, **halt rather than auto-merge** — post a comment explaining why, then
        call `halt_rollout`. This is judgment guidance, not a hard predicate: use your
        discretion about whether the change is routine (e.g. a tiny doc fix in a Dockerfile)
        or substantively risky.

        Patterns that warrant a halt:
        - **Auth code**: `**/auth.py`, `**/auth_routes.py`, route authentication modules
        - **Deploy/release scripts**: `deploy.sh`, `release.sh`, `start.sh`
        - **CI/hooks**: `.github/**`, `.pre-commit-config.yaml`
        - **Infrastructure units**: `*.service`, `Dockerfile*`, `nginx*.conf`
        - **Env/secrets**: `.env*`, `.envrc*`

        If the diff touches any of these areas, call `halt_rollout` and post a comment on
        the offending item explaining why — don't attempt to auto-merge.

        ## Guardrails — Never Lower the Quality Bar

        These rules are absolute. No circumstance justifies violating them.

        **Coverage threshold — ratchets up only:**
        - NEVER lower `[tool.coverage.report] fail_under` in `pyproject.toml`.
        - Coverage threshold ratchets up only. After adding tests that increase coverage,
          raise `fail_under` to lock in the gain — never edit it downward.
        - If a build fails the pre-push coverage gate, your only options are:
          1. Add tests to recover coverage (fix the deficit properly).
          2. Halt the rollout and flag the lead — if the deficit is too large to fix inline.
        - A `chore: lower coverage threshold` commit is a guardrail violation. If you see
          one on the branch, revert it before merging.

        **Additional prohibitions — do not cross these lines:**
        - Do not comment out `pytest` hooks or skip the test suite.
        - Do not skip linting (`--skip` flags, removing lint steps, etc.).
        - Do not add blanket `# type: ignore` suppressions to silence type errors.
        - Do not use `git push --no-verify` to bypass pre-push hooks.

        When in doubt: halt. A halted rollout recovers. A merged regression does not.

        ## MCP Tools Available

        `advance_rollout`, `complete_item_in_rollout`, `halt_rollout`,
        `dispatch_item`, `add_comment`, `get_item`, `update_item`, `list_items`,
        `get_run_status`, `list_runs`, `list_comments`, `update_rollout_state`

        ## Rules

        - You have max {max_turns} turns. Budget them wisely.
        - Never touch rollouts or items outside `rollout_id={rollout_id}`.
        - Never force-push. Push only via the squash merge sequence above.
        - If you are uncertain whether a merge is safe, halt — halting is always safe.
    """
    )


def _build_manage_prompt(
    rollout_id: str,
    project: dict[str, Any],
    max_turns: int,
    workspace_repo_dirs: list[str] | None = None,
    merge_notes: list[dict[str, Any]] | None = None,
    engine_name: str | None = None,
) -> str:
    """System prompt for manage mode — run the rollout-manager executor loop.

    ``merge_notes`` is the rollout's most recent merge notes (newest first), the
    durable record of what already-merged items changed.  At most
    ``MERGE_NOTE_CONTEXT_LIMIT`` are rendered.
    """
    project_name = project["name"]
    git_origin = project.get("git_origin", "")
    project_id = project.get("id", "")

    if workspace_repo_dirs:
        return _build_manage_workspace_main_prompt(
            rollout_id,
            project,
            max_turns,
            workspace_repo_dirs,
            merge_notes,
            engine_name=engine_name,
        )

    _gate = (project.get("gate_command") or "").strip()
    _gate_ind = _gate.replace("\n", "\n        ")
    gate_exception = ""
    if _gate:
        gate_exception = (
            "\n\n        "
            "Exception — post-run gate failure: if the run's `error_msg` (from "
            "`get_run_status`) starts with `post-run gate`, the build agent's "
            "branch WAS pushed and only the project gate command failed or "
            "timed out. Do NOT halt yet. Read the item's comment starting "
            "`Post-run gate` for the output tail, then continue with Step 4. "
            "In Step 5, after checking out the branch, ALSO run the project "
            f"gate command from the repo root: `{_gate_ind}`. Proceed to Step "
            "6 only once it exits 0. If it fails, apply the inline-fix rules "
            "(small fix, then re-run the same command); otherwise halt with "
            'reason `"post-run gate failure: run <run_id> for item <item_id>"`.'
        )

    step3 = _manage_wait_step(rollout_id, gate_exception)
    turn_discipline = _turn_discipline_block()
    standard_rules = _standard_rules_block(engine_name)
    merge_bar = _manage_merge_bar_block(_gate, "2", workspace=False)
    recent_notes = _manage_recent_merge_notes_block(merge_notes)
    merge_note_step = _manage_merge_note_block()
    commit_type = _manage_commit_type_block()

    main_prompt = textwrap.dedent(
        f"""\
        You are a headless rollout-manager executor dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        **Mode: MANAGE** — You are orchestrating a rollout execution and merging build results.

        **Project:** {project_name}
        **Git Origin:** {git_origin}
        **Rollout ID:** {rollout_id}
        **Project ID:** {project_id}
        **Turns remaining:** {max_turns}
        **Time budget:** {config.MANAGE_TIMEOUT_SECONDS // 60} min of wall-clock time. Nothing relaunches you: exiting while any build run is still in flight strands it.

        This rollout ID is your primary anchor. Every action you take is scoped to it.
        Your workspace is a git clone of the project's default branch (auto-detected).

        ## Launch item_id — Ignore It

        The `item_id` you received as the dispatch trigger is a positional placeholder,
        not a rollout item to act on. **Ignore it entirely.** Your sole source of truth for
        which items to dispatch is the rollout plan — read it via `advance_rollout`.
        Do NOT add comments to the launch item_id.
        Do NOT mark it complete.
        Do NOT treat it as a gate.

        {standard_rules}

        {turn_discipline}

        {recent_notes}

        ## Phase 1 — Warm-up (run once at start, concurrently with wave-1 builds)

        IMPORTANT: Dispatch all wave-1 items first (Phase 2 Step 1 below), THEN run
        warm-up steps while waiting for those builds to complete. Warm-up happens
        concurrently with wave-1 builds — not before them.

        At the start of warm-up, publish your state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="warm_up",
            current_step="Verifying main is green",
        )
        ```

        NOTE on `update_rollout_state`: each call REPLACES all four state fields
        (phase, current_item_id, current_step, last_updated). Fields you omit
        are reset to None. If you want to preserve `current_item_id` across a
        phase change, pass it in every subsequent call.

        **1. Install dependencies** (Bash):
        ```bash
        # If pyproject.toml exists:
        [ -f pyproject.toml ] && uv sync
        # If package.json exists:
        [ -f package.json ] && npm install
        ```

        If this repo uses lefthook, pre-commit, husky, or a committed `.agent-gtd/setup`
        override, the dispatch worker already installed and verified its git hooks before
        you launched — do not reinstall or change them.

        {merge_bar}

        **3. Verify `main` is green** — run the merge bar you just established.
        If it fails, call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="<exact failure: command + error snippet>"
        )
        ```
        and STOP. The project is not in a mergeable state — a human must intervene.

        ## Phase 2 — Wave Loop

        Repeat until `advance_rollout` reports `graph_complete=true`:

        **Step 1 — Advance**
        ```
        mcp__agent-gtd__advance_rollout(rollout_id="{rollout_id}")
        ```
        Returns: `{{next_ready: [...], in_progress: [...], graph_complete: bool}}`

        If `advance_rollout` fails: retry up to 3 times with 30 s sleep between attempts.
        After 3 failures: call `halt_rollout(rollout_id="{rollout_id}",
        reason="advance_rollout failed 3 times")` and EXIT.
        If `graph_complete=true` and `next_ready=[]`: EXIT with success (all done).

        **Step 2 — Dispatch ready items**

        For each `item_id` in `next_ready`, publish state then dispatch:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="dispatching",
            current_item_id=item_id,
            current_step=f"Dispatching {{item_id}}",
        )
        mcp__agent-gtd__dispatch_item(
            item_id=item_id,
            mode="build",
            rollout_id="{rollout_id}",
        )
        ```
        NOTE: `rollout_id` is REQUIRED on every child dispatch — include it always.
        Record the returned `run_id` alongside `item_id`.

        {step3}

        **Step 4 — AC reconciliation**

        Publish reconciliation state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reconciling_ac",
            current_step="Checking downstream AC impact",
        )
        ```

        Start from the **Recent Merge Notes** section above — that is the durable
        record of what earlier items already changed, and it is authoritative over
        your own recollection (a relaunched manager has none). Then call `get_item`
        on items in later waves that share a module or interface with the just-merged
        work. Check whether the just-merged code introduced changes (new function
        signatures, renamed classes, changed config keys) that would cause a later
        item's AC or spec to be wrong.
        If so, call `update_item` to patch that item's description and post a comment
        explaining the change:
        ```
        mcp__agent-gtd__add_comment(
            item_id=<later_item_id>,
            content_markdown="AC updated: <what changed and why>"
        )
        ```

        **Step 5 — Quality gates**

        Publish reviewing state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id=item_id,
            current_step=f"Running quality gates on <branch_name>",
        )
        ```

        Check out the build branch in your workspace and run the merge bar
        established in warm-up:
        ```bash
        git fetch origin <branch_name>
        git checkout <branch_name>
        # run the merge bar from warm-up
        ```

        Also inspect the diff for **unrelated manifest changes**. If the diff
        includes additions to `package.json` / `package-lock.json` /
        `pyproject.toml` / `uv.lock` that are NOT directly tied to the item's
        stated scope, treat them as suspect — they're usually defensive
        workarounds for warnings on the build agent's host (e.g. silencing a
        peer-dep warning). Revert those specific changes via
        `git checkout HEAD -- <file>` and re-run gates. Production manifests
        should only change when the actual feature requires it.

        If gates pass (and no unrelated manifest changes remain):
        proceed to Step 6 (squash merge).

        If gates fail:
        - Attempt an inline fix if it is small: formatting, single missing import,
          one-line change, coverage ratchet bump, stale test assertion that the
          current change makes correct — use `Edit`/`Bash` to fix. If the fix
          succeeds, re-run gates.
        - If the fix fails or is non-trivial, halt:
          ```
          mcp__agent-gtd__halt_rollout(
              rollout_id="{rollout_id}",
              reason="quality gate failure on <branch>: <command>: <error snippet> in <file>"
          )
          ```

        **Step 6 — Squash merge**

        Publish merging state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="merging",
            current_item_id=item_id,
            current_step=f"Merging <branch_name> → main",
        )
        ```

        Before merging, run a commit-count guard to confirm the build agent actually
        pushed commits (guards against a build agent that reported success but pushed
        no commits):
        ```bash
        git fetch origin <branch_name>
        commit_count=$(git rev-list origin/<default_branch>..<branch_name> --count)
        ```
        If `commit_count` is 0: the build agent pushed nothing. Before halting, check
        WHY — call `mcp__agent-gtd__get_run_status(<run_id>)` for that item's child
        build run and read its `status` field.

        If `status` is exactly `already_satisfied`, the work was already done. Only a
        talos build can reach this status (talos exit code 30, AlreadySatisfied), and
        talos's own checks ran green before it emitted that verdict. A claude-code
        build can NEVER report this — a commit-less claude-code run is recorded
        `failed`, so if you see zero commits on one, it is a halt candidate, not a
        skip.
        Skip the merge and advance:
        ```
        mcp__agent-gtd__complete_item_in_rollout(
            rollout_id="{rollout_id}",
            item_id=item_id,
            outcome="skipped",
            merge_actor="manager-autonomous",
            decision_rule="already-satisfied",
        )
        ```
        Then post a comment that NAMES every item id in that call's `newly_ready` list
        (or states `no downstream items unblocked` when the list is empty), and ADVANCE
        to the next item. Do NOT halt and do NOT merge this item.

        For ANY other status, the build agent reported success but pushed no commits.
        Call:
        ```
        mcp__agent-gtd__halt_rollout(
            rollout_id="{rollout_id}",
            reason="build agent reported success but pushed no commits: <branch_name> has no commits beyond origin/<default_branch>"
        )
        ```
        and STOP — do not attempt the squash merge.

        {commit_type}

        ```bash
        set -euo pipefail
        git checkout <default_branch>
        git merge --squash <branch_name>
        git commit -F - <<'COMMITEOF'
        <type>(<item_id short>): <item title>

        Rollout: {rollout_id}
        Item: <item_id>
        COMMITEOF
        merge_sha=$(git rev-parse HEAD)
        git push origin <default_branch>
        # Verify the merge landed BEFORE any branch deletion; failure aborts here.
        git fetch origin <default_branch> && git merge-base --is-ancestor "$merge_sha" origin/<default_branch>
        git push origin --delete <branch_name>
        git branch -D <branch_name>
        ```

        Never delete a feature branch whose merge did not land; on any failure, leave
        the branch in place and take the halt path. The halt reason must include the
        branch name and its tip sha, and the branch must stay intact on origin.

        `<type>` is the value you derived immediately above. Never emit a `feat`
        type unless the derivation actually produced one.

        {merge_note_step}

        **Step 7 — Complete in rollout**

        ```
        result = mcp__agent-gtd__complete_item_in_rollout(
            rollout_id="{rollout_id}",
            item_id=item_id,
            outcome="completed",
            merge_actor="manager-autonomous",
            decision_rule="agent-judgment",
            merge_note=<the merge note you wrote in Step 6b>,
        )
        ```

        `merge_note` is REQUIRED on every merged item — never pass an empty string.

        `complete_item_in_rollout` does two things for you on `outcome="completed"`:
        1. Cascades the item's GTD status to `done` (no need to call
           `complete_item` separately).
        2. Closes the rollout automatically if this was the last terminal item,
           and signals that via `result["graph_complete"]`.

        Check the response:
        - If `result["graph_complete"]` is `true`: the rollout is closed. Before
          exiting, clean up the manage branch from origin:
          ```bash
          git push origin --delete feat/{rollout_id[:8]}-manage || true
          ```
          Then EXIT with success — do NOT call `advance_rollout` again (it will
          reject the now-completed rollout).
        - Otherwise: go back to Step 1 (advance) for the next wave / next
          unblocked items.

        **Halt path**

        When halting, the feature branch must be left intact on origin (never delete
        it) and the halt reason must include the branch name and its tip sha.

        Before halting, publish halted state:
        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="halted",
            current_step=<reason>,
        )
        ```

        On any non-recoverable failure, post a comment to the offending rollout item
        (NOT the launch placeholder item_id):
        ```
        mcp__agent-gtd__add_comment(
            item_id=<offending_rollout_item_id>,
            content_markdown="Rollout halted: <reason>"
        )
        ```
        If there is no specific offending item (e.g. `advance_rollout` failed 3 times),
        post to the project instead:
        ```
        mcp__agent-gtd__add_comment(
            project_id="{project_id}",
            content_markdown="Rollout halted: <reason>"
        )
        ```
        Then call:
        ```
        mcp__agent-gtd__halt_rollout(rollout_id="{rollout_id}", reason=<reason>)
        ```
        And STOP.

        ## Phase 3 — Sensitive-area guidance

        Before auto-merging, inspect the diff. If the build touches any of the following
        patterns, **halt rather than auto-merge** — post a comment explaining why, then
        call `halt_rollout`. This is judgment guidance, not a hard predicate: use your
        discretion about whether the change is routine (e.g. a tiny doc fix in a Dockerfile)
        or substantively risky.

        Patterns that warrant a halt:
        - **Auth code**: `**/auth.py`, `**/auth_routes.py`, route authentication modules
        - **Deploy/release scripts**: `deploy.sh`, `release.sh`, `start.sh`
        - **CI/hooks**: `.github/**`, `.pre-commit-config.yaml`
        - **Infrastructure units**: `*.service`, `Dockerfile*`, `nginx*.conf`
        - **Env/secrets**: `.env*`, `.envrc*`

        If the diff touches any of these areas, call `halt_rollout` and post a comment on
        the offending item explaining why — don't attempt to auto-merge.

        ## Guardrails — Never Lower the Quality Bar

        These rules are absolute. No circumstance justifies violating them.

        **Coverage threshold — ratchets up only:**
        - NEVER lower `[tool.coverage.report] fail_under` in `pyproject.toml`.
        - Coverage threshold ratchets up only. After adding tests that increase coverage,
          raise `fail_under` to lock in the gain — never edit it downward.
        - If a build fails the pre-push coverage gate, your only options are:
          1. Add tests to recover coverage (fix the deficit properly).
          2. Halt the rollout and flag the lead — if the deficit is too large to fix inline.
        - A `chore: lower coverage threshold` commit is a guardrail violation. If you see
          one on the branch, revert it before merging.

        **Additional prohibitions — do not cross these lines:**
        - Do not comment out `pytest` hooks or skip the test suite.
        - Do not skip linting (`--skip` flags, removing lint steps, etc.).
        - Do not add blanket `# type: ignore` suppressions to silence type errors.
        - Do not use `git push --no-verify` to bypass pre-push hooks.

        When in doubt: halt. A halted rollout recovers. A merged regression does not.

        ## MCP Tools Available

        `advance_rollout`, `complete_item_in_rollout`, `halt_rollout`,
        `dispatch_item`, `add_comment`, `get_item`, `update_item`, `list_items`,
        `get_run_status`, `list_runs`, `list_comments`, `update_rollout_state`

        ## Rules

        - You have max {max_turns} turns. Budget them wisely.
        - Never touch rollouts or items outside `rollout_id={rollout_id}`.
        - Never force-push. Push only via the squash merge sequence above.
        - If you are uncertain whether a merge is safe, halt — halting is always safe.
    """
    )

    return main_prompt


# ---------------------------------------------------------------------------
# REVIEW mode — the short-lived per-build merge reviewer
# ---------------------------------------------------------------------------

# The CLOSED set of verdicts a reviewer may return. Anything else is a
# malformed verdict, which the worker treats as `halt` — an unrecognised
# verdict must never be interpreted charitably as `merge`.
REVIEW_VERDICTS: frozenset[str] = frozenset({"merge", "re-dispatch", "skip", "halt"})

# Where the reviewer writes its verdict, relative to the rollout workspace
# root. A file at a fixed path is the only channel that survives a subprocess
# whose stdout is a transcript.
#
# This is the one artifact contract the worker still branches on, and the
# asymmetry is deliberate. A BUILD agent's claim about its own run is
# unfalsifiable and was deleted for that reason; a REVIEWER's verdict is an
# instruction to the worker about what to do next, which has no mechanical
# substitute — there is nothing to observe instead. The safety property is
# different too: an unusable build claim used to be read charitably, whereas an
# unusable verdict HALTS (see :func:`read_review_verdict`).
VERDICT_ARTIFACT_RELPATH: str = ".dispatch/verdict.json"

# Where the per-rollout default-branch record is cached inside the reused
# workspace, so a reviewer never re-derives (or guesses) a repo's base branch.
DEFAULT_BRANCHES_RELPATH: str = ".dispatch/default-branches.json"

# Cap on the reviewer's rationale text kept in logs/comments.
REVIEW_RATIONALE_MAX_CHARS: int = 2000


@dataclass(frozen=True, slots=True)
class RolloutWorkspace:
    """A per-rollout workspace reused by every reviewer for that rollout."""

    root: Path
    #: repo directory name -> absolute path. For a monorepo project there is a
    #: single entry whose path IS ``root``.
    repo_paths: dict[str, Path]
    #: repo directory name -> detected default branch, recorded ONCE at clone
    #: time and reused by every subsequent reviewer.
    default_branches: dict[str, str]
    #: True when this call created the workspace, False when it was reused.
    created: bool
    #: True for a workspace (multi-repo) project, where each repo lives in its
    #: own subdirectory under ``root``. False for a monorepo, where ``root`` IS
    #: the single clone. Carried explicitly because a workspace project with
    #: exactly one repo is still a workspace project.
    workspace_mode: bool = False


def rollout_workspace_path(rollout_id: str, slot: int = 0) -> Path:
    """Return the stable workspace path for *rollout_id*'s review *slot*.

    Named ``rollout-<id>`` rather than ``repos-<run_id>`` precisely because it
    is NOT per-run: it outlives every reviewer that uses it.  Slot 0 keeps the
    historical ``rollout-<id>`` name; slot ``k >= 1`` is ``rollout-<id>-s<k>``.
    """
    if slot == 0:
        return config.WORKSPACE_ROOT / f"rollout-{rollout_id}"
    return config.WORKSPACE_ROOT / f"rollout-{rollout_id}-s{slot}"


def _read_default_branches(root: Path) -> dict[str, str]:
    """Read the cached default-branch record, or {} when absent/unreadable.

    A plain read — see :func:`read_review_verdict` for why no sudo escalation is needed or wanted here.
    """
    path = root / DEFAULT_BRANCHES_RELPATH
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_default_branches(root: Path, branches: dict[str, str]) -> None:
    """Persist the default-branch record inside the reused workspace.

    The directory is still created via sudo as the AGENT user, because the reviewer agent writes its verdict into that same ``.dispatch/`` directory and must own it. With the agent's 0002 umask that directory is group-writable and group-owned by the agent's group, of which the service user is a member — so this write needs no escalation of its own. The ``tee`` it replaced was never authorised in sudoers anyway.
    """
    payload = json.dumps(branches, indent=2, sort_keys=True)
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root / ".dispatch")]),
        check=False,
        capture_output=True,
    )
    try:
        (root / DEFAULT_BRANCHES_RELPATH).write_text(payload)
    except OSError:
        logger.warning("could not write default-branch record under %s", root)


def prepare_rollout_workspace(
    rollout_id: str,
    *,
    git_origin: str = "",
    repo_urls: list[str] | None = None,
    slot: int = 0,
) -> RolloutWorkspace:
    """Prepare (or REUSE) one persistent review-slot workspace of a rollout.

    Why reuse rather than clone per item: ``prepare_manage_workspace`` does a
    ``--depth=50`` clone per manage run, and the workspace variant clones every
    repo in the project.  Amortised once per rollout that is fine; paid once per
    ITEM on a Raspberry Pi it is the dominant cost of reviewing a small diff.
    A persistent workspace also preserves the recorded default branches for
    free — the reviewer never has to re-derive a repo's base branch, which is
    the input to the commit-count guard and to the merge itself.

    Reuse is safe because each SLOT is single-writer by construction: the
    wave loop assigns a slot to at most one item at a time (its review, then
    its land), and calls :func:`refresh_rollout_workspace` before every
    reviewer launch, so each reviewer is handed a slot that has just been HARD
    RESET to origin's default branch in every repo.  Concurrent reviews of one
    rollout run in different slots and never share a tree.  Nothing from
    a previous reviewer's tree survives that reset, so a reviewer cannot inherit
    a half-applied inline fix from the item before it.

    Idempotent: when the workspace already exists it is reused as-is and
    ``created`` is False.  When it does not, every repo is cloned and its
    default branch detected and recorded.

    Args:
        rollout_id: The rollout this workspace belongs to.
        git_origin: Monorepo clone URL. Used when *repo_urls* is empty.
        repo_urls: Workspace-project clone URLs, in project order.
        slot: The review slot (see :func:`rollout_workspace_path`).

    Returns:
        A :class:`RolloutWorkspace`.

    Raises:
        ValueError: If neither *git_origin* nor *repo_urls* is supplied, or if
            two URLs map to the same directory name.
        RuntimeError: On clone or checkout failure.
    """
    urls = list(repo_urls or [])
    if not urls and not git_origin:
        raise ValueError("prepare_rollout_workspace needs git_origin or repo_urls")

    root = rollout_workspace_path(rollout_id, slot)

    if urls:
        dir_names = [repo_dir_from_url(u) for u in urls]
        seen: set[str] = set()
        for name in dir_names:
            if name in seen:
                raise ValueError(f"Duplicate workspace repo directory: '{name}'")
            seen.add(name)
        repo_paths = {name: root / name for name in dir_names}
    else:
        dir_names = [repo_name_from_origin(git_origin)]
        urls = [git_origin]
        repo_paths = {dir_names[0]: root}

    if root.exists():
        cached = _read_default_branches(root)
        if cached:
            return RolloutWorkspace(
                root=root,
                repo_paths=repo_paths,
                default_branches=cached,
                created=False,
                workspace_mode=bool(repo_urls),
            )
        # Directory exists but carries no record — treat it as reusable and
        # re-derive rather than deleting someone else's tree.
        branches = {
            name: _detect_default_branch(path) for name, path in repo_paths.items()
        }
        _write_default_branches(root, branches)
        return RolloutWorkspace(
            root=root,
            repo_paths=repo_paths,
            default_branches=branches,
            created=False,
            workspace_mode=bool(repo_urls),
        )

    config.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _sudo_wrap(["mkdir", "-p", str(root)]),
        check=True,
        capture_output=True,
    )

    default_branches: dict[str, str] = {}
    for url, name in zip(urls, dir_names, strict=True):
        dest = repo_paths[name]
        result = subprocess.run(
            _sudo_wrap(["git", "clone", "--depth=50", url, str(dest)]),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(
                f"rollout workspace clone failed for {url}: {stderr_tail}"
            )
        branch = _detect_default_branch(dest)
        checkout = subprocess.run(
            _sudo_wrap(["git", "checkout", branch]),
            cwd=dest,
            check=False,
            capture_output=True,
        )
        if checkout.returncode != 0:
            stderr_tail = checkout.stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(
                f"rollout workspace checkout failed for {url}: {stderr_tail}"
            )
        default_branches[name] = branch

    _write_default_branches(root, default_branches)
    return RolloutWorkspace(
        root=root,
        repo_paths=repo_paths,
        default_branches=default_branches,
        created=True,
        workspace_mode=bool(repo_urls),
    )


def refresh_rollout_workspace(workspace: RolloutWorkspace) -> None:
    """Return every repo in a reused workspace to a clean origin/<default> state.

    Run immediately BEFORE each reviewer launch.  This is what makes reuse
    safe: a previous reviewer may have left a feature branch checked out, an
    inline fix in the tree, or a half-applied merge.  Fetch, hard-reset to
    ``origin/<default>`` and clean untracked files, so every reviewer starts
    from the same state a fresh clone would give it — minus the clone.

    Best effort per repo: a repo that cannot be refreshed is logged and left
    alone rather than failing the whole wave, because the reviewer's own
    contract makes it verify the base before merging onto it.
    """
    for name, path in workspace.repo_paths.items():
        branch = workspace.default_branches.get(name) or _DEFAULT_BRANCH_CANDIDATES[0]
        for argv in (
            ["git", "fetch", "origin", "--prune"],
            ["git", "checkout", branch],
            ["git", "reset", "--hard", f"origin/{branch}"],
            ["git", "clean", "-fd"],
        ):
            result = subprocess.run(
                _sudo_wrap(argv),
                cwd=path,
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "rollout workspace refresh: %s failed in %s: %s",
                    " ".join(argv),
                    path,
                    result.stderr.decode("utf-8", errors="replace")[-300:],
                )


def clear_review_verdict(workspace_root: Path) -> None:
    """Delete any verdict artifact left behind in a REUSED rollout workspace.

    Load-bearing for workspace reuse: without it, a reviewer that dies before
    writing its verdict would be credited with the PREVIOUS item's answer —
    quite possibly a `merge`.
    """
    subprocess.run(
        _sudo_wrap(["rm", "-f", str(workspace_root / VERDICT_ARTIFACT_RELPATH)]),
        check=False,
        capture_output=True,
    )


COMMIT_TYPES: frozenset[str] = frozenset(
    {"feat", "fix", "perf", "refactor", "test", "docs", "chore", "build", "ci"}
)
ITEM_COMMIT_TYPES: frozenset[str] = COMMIT_TYPES - {"build", "ci"}

# A conventional-commit prefix already present on an item title, stripped so
# the composed squash subject does not repeat it. Case-sensitive on purpose.
_TITLE_TYPE_PREFIX_RE = re.compile(
    r"^(feat|fix|perf|refactor|test|docs|chore|build|ci)(\([^)]*\))?!?:\s*"
)


def compose_squash_subject(item: dict[str, Any]) -> str | None:
    """Compose the exact squash-commit subject from the item's groomed type.

    Returns None when the item carries no valid groomed ``commit_type`` (unset,
    empty, or outside ITEM_COMMIT_TYPES) — the reviewer then derives the type.
    A conventional-commit prefix already on the title is stripped so it is not
    repeated; if stripping leaves nothing, the whole title is kept.
    """
    commit_type = item.get("commit_type")
    if commit_type not in ITEM_COMMIT_TYPES:
        return None
    raw_title = str(item.get("title") or "")
    title = _TITLE_TYPE_PREFIX_RE.sub("", raw_title, count=1).strip()
    if not title:
        title = raw_title.strip()
    return f"{commit_type}({item['id'][:8]}): {title}"


def find_landed_commit_subjects(
    workspace: RolloutWorkspace, item_id: str, *, rollout_id: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Find each repo's landed squash commit for ``item_id`` by its trailer.

    With *rollout_id* set, the commit must ALSO carry ``Rollout: <rollout_id>``
    (``--all-match``): squash commits carry both trailers.

    Returns ``(subjects_by_repo, failed_repos)``. A repo whose default branch
    has no commit carrying ``Item: <item_id>`` is simply absent from the
    subjects; a repo whose lookup itself failed is listed in ``failed_repos``.
    Never raises.
    """
    subjects: dict[str, str] = {}
    failed: list[str] = []
    for repo, repo_path in workspace.repo_paths.items():
        branch = workspace.default_branches.get(repo) or _DEFAULT_BRANCH_CANDIDATES[0]
        try:
            fetch = subprocess.run(
                _sudo_wrap(["git", "fetch", "origin", branch]),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if fetch.returncode != 0:
                logger.warning(
                    "rollout-loop: landed-commit-lookup-failed repo=%s item_id=%s "
                    "step=fetch rc=%s stderr=%r",
                    repo,
                    item_id,
                    fetch.returncode,
                    (fetch.stderr or "")[:200],
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "rollout-loop: landed-commit-lookup-failed repo=%s item_id=%s "
                "step=fetch rc=%s stderr=%r",
                repo,
                item_id,
                None,
                str(exc)[:200],
            )
        try:
            log = subprocess.run(
                _sudo_wrap(
                    [
                        "git",
                        "log",
                        f"origin/{branch}",
                        "-1",
                        "--format=%s",
                        f"--grep=^Item: {item_id}$",
                        *(
                            [f"--grep=^Rollout: {rollout_id}$", "--all-match"]
                            if rollout_id
                            else []
                        ),
                    ]
                ),
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "rollout-loop: landed-commit-lookup-failed repo=%s item_id=%s "
                "step=log rc=%s stderr=%r",
                repo,
                item_id,
                None,
                str(exc)[:200],
            )
            failed.append(repo)
            continue
        if log.returncode != 0:
            logger.warning(
                "rollout-loop: landed-commit-lookup-failed repo=%s item_id=%s "
                "step=log rc=%s stderr=%r",
                repo,
                item_id,
                log.returncode,
                (log.stderr or "")[:200],
            )
            failed.append(repo)
            continue
        subject = (log.stdout or "").strip()
        if subject:
            subjects[repo] = subject
    return subjects, failed


# ---------------------------------------------------------------------------
# Land step git helpers — the worker's fetch / rebase / gate / push of a
# reviewer-approved squash commit.  Every call goes through ``_sudo_wrap``
# with an explicit timeout; a timeout raises :class:`LandGitTimeout`, which the
# worker turns into a halt naming the command.  These are synchronous and MUST
# be called via ``loop.run_in_executor``.
# ---------------------------------------------------------------------------


class LandGitTimeout(Exception):  # noqa: N818 — reads as the event it names
    """A land-step git command exceeded its timeout."""

    def __init__(self, command: str) -> None:
        """Record the timed-out git subcommand (e.g. ``fetch``)."""
        super().__init__(f"git {command}")
        self.command = command


class LandGitError(Exception):
    """A land-step git command that must succeed exited non-zero."""

    def __init__(self, command: str, excerpt: str) -> None:
        """Record the failed git subcommand and its output excerpt."""
        super().__init__(f"git {command} failed: {excerpt}")
        self.command = command
        self.excerpt = excerpt


def _land_git(
    repo: Path, args: list[str], timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git <args>`` in *repo*; raise :class:`LandGitTimeout` on timeout."""
    try:
        return subprocess.run(
            _sudo_wrap(["git", *args]),
            cwd=repo,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LandGitTimeout(args[0]) from exc


def _land_git_ok(repo: Path, args: list[str], timeout: int) -> str:
    """Run ``git <args>``, raise :class:`LandGitError` on non-zero, return stdout."""
    proc = _land_git(repo, args, timeout)
    if proc.returncode != 0:
        raise LandGitError(" ".join(args[:2]), git_output_excerpt(proc))
    return proc.stdout.decode("utf-8", errors="replace").strip()


def land_fetch(repo: Path, branch: str, timeout: int) -> None:
    """``git fetch origin <branch>``; raises on failure or timeout."""
    _land_git_ok(repo, ["fetch", "origin", branch], timeout)


def land_fetch_feature_branch(repo: Path, branch_name: str, timeout: int) -> bool:
    """Fetch the feature branch from origin into its full-name tracking ref.

    Works in a shallow single-branch clone whose configured refspec covers
    only the default branch.  True on success; False when origin has no such
    branch; other failures raise like the other helpers.
    """
    proc = _land_git(
        repo,
        [
            "fetch",
            "origin",
            f"+refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
        ],
        timeout,
    )
    if proc.returncode == 0:
        return True
    excerpt = git_output_excerpt(proc)
    if "couldn't find remote ref" in excerpt.lower():
        return False
    raise LandGitError("fetch origin", excerpt)


def land_rev_parse(repo: Path, ref: str, timeout: int) -> str:
    """``git rev-parse <ref>``; raises on failure or timeout."""
    return _land_git_ok(repo, ["rev-parse", ref], timeout)


def land_origin_shas(
    repo_paths: dict[str, Path], branches: dict[str, str], timeout: int
) -> dict[str, str]:
    """Map each repo to ``git rev-parse origin/<default_branch>`` ("" on failure)."""
    shas: dict[str, str] = {}
    for repo, path in repo_paths.items():
        branch = branches.get(repo) or _DEFAULT_BRANCH_CANDIDATES[0]
        try:
            shas[repo] = land_rev_parse(path, f"origin/{branch}", timeout)
        except (LandGitError, LandGitTimeout, OSError) as exc:
            logger.warning(
                "rollout-loop: pre-review-origin lookup failed repo=%s: %s", repo, exc
            )
            shas[repo] = ""
    return shas


def land_ahead_count(repo: Path, branch: str, timeout: int) -> int:
    """``git rev-list --count origin/<branch>..<branch>``."""
    out = _land_git_ok(
        repo, ["rev-list", "--count", f"origin/{branch}..{branch}"], timeout
    )
    return int(out or "0")


def land_count_between(repo: Path, base: str, tip: str, timeout: int) -> int:
    """``git rev-list --count <base>..<tip>``; 0 when either side is unknown."""
    if not base or not tip:
        return 0
    proc = _land_git(repo, ["rev-list", "--count", f"{base}..{tip}"], timeout)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.decode().strip() or "0")
    except ValueError:
        return 0


def land_current_branch(repo: Path, timeout: int) -> str:
    """``git rev-parse --abbrev-ref HEAD``."""
    return _land_git_ok(repo, ["rev-parse", "--abbrev-ref", "HEAD"], timeout)


def land_has_item_trailer(repo: Path, branch: str, item_id: str, timeout: int) -> bool:
    """True when some line of ``git log -1 --format=%B <branch>`` is the trailer."""
    body = _land_git_ok(repo, ["log", "-1", "--format=%B", branch], timeout)
    want = f"Item: {item_id}"
    return any(line.strip() == want for line in body.splitlines())


def land_reviewer_pushed(
    repo: Path,
    base: str,
    branch: str,
    rollout_id: str,
    item_id: str,
    timeout: int,
) -> list[str]:
    """Commits for this item that reached origin AFTER *base* (time-scoped)."""
    out = _land_git_ok(
        repo,
        [
            "log",
            f"{base}..origin/{branch}",
            "--format=%H",
            f"--grep=^Rollout: {rollout_id}$",
            f"--grep=^Item: {item_id}$",
            "--all-match",
        ],
        timeout,
    )
    return [line for line in out.splitlines() if line.strip()]


def land_rebase(repo: Path, branch: str, timeout: int) -> tuple[bool, list[str], str]:
    """``git rebase origin/<branch>``.

    Returns ``(ok, conflict_paths, excerpt)``. On failure the conflicting paths
    are collected BEFORE ``git rebase --abort``, so the repo is left with no
    rebase in progress.
    """
    proc = _land_git(repo, ["rebase", f"origin/{branch}"], timeout)
    if proc.returncode == 0:
        return True, [], ""
    excerpt = git_output_excerpt(proc)
    diff = _land_git(repo, ["diff", "--name-only", "--diff-filter=U"], timeout)
    paths = [
        p
        for p in diff.stdout.decode("utf-8", errors="replace").splitlines()
        if p.strip()
    ]
    _land_git(repo, ["rebase", "--abort"], timeout)
    return False, paths, excerpt


def land_reset_to_origin(repo: Path, branch: str, timeout: int) -> None:
    """Put a NON-landing repo on ``origin/<branch>`` (checkout, then hard reset)."""
    _land_git_ok(repo, ["checkout", "-f", branch], timeout)
    _land_git_ok(repo, ["reset", "--hard", f"origin/{branch}"], timeout)


_NON_FAST_FORWARD_MARKERS: tuple[str, ...] = (
    "[rejected]",
    "non-fast-forward",
    "fetch first",
)


def land_push(
    repo: Path, sha: str, branch: str, *, no_verify: bool, timeout: int
) -> tuple[str, str]:
    """Push exactly *sha* to ``refs/heads/<branch>``. Never forces.

    Returns ``(status, excerpt)`` where status is ``pushed``,
    ``non_fast_forward`` or ``failed``.
    """
    argv = ["push"]
    if no_verify:
        argv.append("--no-verify")
    argv += ["origin", f"{sha}:refs/heads/{branch}"]
    proc = _land_git(repo, argv, timeout)
    if proc.returncode == 0:
        return "pushed", ""
    excerpt = git_output_excerpt(proc)
    if any(marker in excerpt for marker in _NON_FAST_FORWARD_MARKERS):
        return "non_fast_forward", excerpt
    return "failed", excerpt


def land_ls_remote(repo: Path, branch: str, timeout: int) -> str:
    """The live remote sha of ``refs/heads/<branch>`` ("" when absent)."""
    out = _land_git_ok(repo, ["ls-remote", "origin", f"refs/heads/{branch}"], timeout)
    return out.split()[0] if out.split() else ""


def normalize_remote_url(url: str) -> str:
    """Strip one trailing ``/`` and then one trailing ``.git``."""
    url = url.strip()
    url = url.removesuffix("/")
    return url.removesuffix(".git")


def land_remote_url(repo: Path, timeout: int) -> str:
    """The normalized ``git remote get-url origin``."""
    return normalize_remote_url(
        _land_git_ok(repo, ["remote", "get-url", "origin"], timeout)
    )


def land_diff_stat(
    repo: Path, before: str, after: str, timeout: int
) -> tuple[int, int, int, list[str]]:
    """``(files_changed, insertions, deletions, paths)`` between two shas."""
    out = _land_git_ok(repo, ["diff", "--numstat", before, after], timeout)
    files = ins = dels = 0
    paths: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:  # numstat: added<TAB>deleted<TAB>path
            continue
        files += 1
        ins += int(parts[0]) if parts[0].isdigit() else 0
        dels += int(parts[1]) if parts[1].isdigit() else 0
        paths.append(parts[2])
    return files, ins, dels, paths


def land_delete_feature_branch(repo: Path, branch_name: str, timeout: int) -> list[str]:
    """Best-effort remote + local feature-branch deletion. Returns failure notes."""
    notes: list[str] = []
    for argv in (
        ["push", "--no-verify", "origin", "--delete", branch_name],
        ["branch", "-D", branch_name],
    ):
        try:
            proc = _land_git(repo, argv, timeout)
        except LandGitTimeout:
            notes.append(f"git {argv[0]} timed out")
            continue
        if proc.returncode != 0:
            notes.append(f"git {' '.join(argv[:2])}: {git_output_excerpt(proc)[:200]}")
    return notes


# The file extensions that count as documentation for the reviewer's
# conflict allowance: a squash-merge conflict confined to these paths may be
# resolved by the reviewer; any other conflicting path aborts the merge.  The
# ONLY copy of the list — the review prompt and the land step's conflict guard
# both render / test against it.
DOCUMENTATION_EXTENSIONS: tuple[str, ...] = (".md", ".mdx", ".rst", ".txt")


def is_documentation_path(path: str) -> bool:
    """True when *path*'s suffix (case-insensitive) is a documentation extension."""
    return PurePosixPath(path).suffix.lower() in DOCUMENTATION_EXTENSIONS


def land_conflict_paths(repo: Path, base: str, tip: str, timeout: int) -> list[str]:
    """Paths a merge of *tip* into *base* would conflict on (``[]`` when clean).

    Uses ``git merge-tree --write-tree`` (git >= 2.38), which merges in memory
    and touches neither the index nor the working tree.  Exit 1 means the merge
    has conflicts; with ``-z --name-only --no-messages`` stdout is the tree OID
    followed by the NUL-separated conflicting paths.
    """
    proc = _land_git(
        repo,
        [
            "merge-tree",
            "--write-tree",
            "-z",
            "--name-only",
            "--no-messages",
            base,
            tip,
        ],
        timeout,
    )
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        raise LandGitError(
            "merge-tree", f"exit {proc.returncode}: {git_output_excerpt(proc)}"
        )
    entries = proc.stdout.decode("utf-8", errors="replace").split("\0")
    if not entries[0].strip():
        # merge-tree also exits 1 when a side does not resolve ("not something
        # we can merge"), printing no tree OID.  That is not "conflicts on
        # nothing": fail closed rather than report a clean merge.
        raise LandGitError("merge-tree", f"exit 1: {git_output_excerpt(proc)}")
    entries = entries[1:]
    paths: list[str] = []
    for entry in entries:
        if entry and entry not in paths:
            paths.append(entry)
    return paths


def land_resolve_ref(repo: Path, ref: str, timeout: int) -> str:
    """The commit sha *ref* names, or "" when it does not resolve."""
    proc = _land_git(
        repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


CONFLICT_MARKER_PATTERN = r"^(<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)( |$)"


def land_conflict_markers(
    repo: Path, rev: str, paths: list[str], timeout: int
) -> list[str]:
    """Those of *paths* that still carry a conflict marker line at *rev*.

    ``=======`` is deliberately not a marker here: it is also a Markdown setext
    heading underline, and every real conflict carries ``<<<<<<<`` too.
    """
    if not paths:
        return []
    proc = _land_git(
        repo, ["grep", "-lE", CONFLICT_MARKER_PATTERN, rev, "--", *paths], timeout
    )
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise LandGitError("grep", git_output_excerpt(proc))
    prefix = f"{rev}:"
    return [
        line.removeprefix(prefix)
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def read_review_verdict(workspace_root: Path) -> tuple[dict[str, Any] | None, str]:
    """Read and validate the reviewer's verdict artifact.

    Returns ``(verdict, reason)``.  ``verdict`` is None whenever the artifact
    could not be used, and ``reason`` is one of the literals ``ok``, ``absent``,
    ``not_json``, ``not_object``, ``unknown_schema_version``,
    ``unknown_verdict``, ``missing_rationale`` — so telemetry can distinguish a
    malformed verdict from a missing one while the worker's decision stays
    binary.

    A reviewer that returns no usable verdict does NOT get the benefit of the
    doubt: the caller halts.  Merging on a guess is the one outcome that cannot
    be undone.

    The read is PLAIN, not sudo-escalated, and that matters more here than anywhere else in this module: ``cat`` is not in the sudoers NOPASSWD list, so the escalated read this replaced was denied every single time. An unreadable verdict halts a rollout, so on the shipped sudoers EVERY reviewer verdict would have halted its wave. The workspace is mode 2775 owned by the agent user with the service user in that group, and the agent's umask is 0002, so a plain read reaches the file.
    """
    path = workspace_root / VERDICT_ARTIFACT_RELPATH
    try:
        raw = path.read_text()
    except OSError:
        return None, "absent"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "not_json"
    if not isinstance(data, dict):
        return None, "not_object"
    if data.get("schema_version") != 1:
        return None, "unknown_schema_version"
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in REVIEW_VERDICTS:
        return None, "unknown_verdict"
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return None, "missing_rationale"
    return data, "ok"


def _review_conflict_contract_block(*, workspace_mode: bool) -> str:
    """The merge-conflict contract. Verbatim in the prompt.

    The reviewer no longer pushes, so the non-fast-forward rejection it used to
    handle is gone from this block: the worker's land step owns the push and
    retries a rejection itself (fetch, rebase, re-gate).  What remains is the
    local ``git merge --squash`` conflict.  The reviewer resolves it only when
    EVERY conflicting path is documentation (:data:`DOCUMENTATION_EXTENSIONS`);
    any conflict touching another path is detected, aborted and reported.  The
    worker's land step re-checks both halves independently.
    """
    extensions = ", ".join(f"`{e}`" for e in DOCUMENTATION_EXTENSIONS)
    if workspace_mode:
        partial = """\

**Workspace mode.** In the abort case (c), or on any other failure, reset every repo's local default branch to
`origin/<default_branch>` and return `halt`; no repo has been pushed, so there
is no split state. Name every repo in `repo_states` with exactly one of
`committed`, `failed` or `untouched`, plus `failure.kind` and
`failure.detail`. A repo whose documentation-only conflict was resolved and
committed under (b) reports `repo_states[<repo>] = "committed"`, and its paths
go in `resolved_conflict_paths` prefixed `<repo_dir>/`."""
    else:
        partial = """\

**Single repo.** There is no partial state to describe: either the one squash
commit exists locally or nothing does."""

    body = f"""\
## Merge Conflict — the contract

A merge conflict that touches any non-documentation path is handled one way:
**attempt, detect, abort cleanly, report.** Only a conflict confined entirely
to documentation files may be resolved, and only as described in (b).

**(a) List the conflicting paths.** When `git merge --squash <branch>` exits
non-zero, list every conflicting path before doing anything else:

```bash
git diff --name-only --diff-filter=U
```

**(b) Documentation-only conflict — resolve it.** If EVERY listed path ends in
one of these documentation extensions (case-insensitive, at any depth):
{extensions}
then the agent resolves them:

1. Edit each conflicting file so it integrates BOTH the branch's change and the
   default branch's change. Never drop either side's text wholesale — keeping
   only "ours" or only "theirs" is not a resolution.
2. Confirm no conflict markers remain; this MUST print nothing:
   `git grep -nE '^(<<<<<<<|>>>>>>>|\\|\\|\\|\\|\\|\\|\\|)( |$)' -- <paths>`
3. `git add` exactly those paths, and nothing else.
4. Confirm `git diff --name-only --diff-filter=U` now prints nothing.
5. Continue with the normal squash commit below, and record every resolved
   path in `resolved_conflict_paths`.

**(c) Any non-documentation path conflicts — abort.** If ANY listed path is not
documentation, today's contract applies unchanged, and nothing is resolved —
not even the documentation paths:

```bash
git merge --abort || git reset --hard HEAD
git status --porcelain   # MUST be empty before you go on
```

Never improvise a resolution of a conflict that touches a non-documentation
path. Not "just the import block", not "obviously the branch's version wins".
Such a conflict means two changes disagree about the same lines and nobody has
decided which is right — that is a human's call, and a reviewer that picks one
has silently discarded the other.

**NEVER `git push --force` and NEVER `--force-with-lease`** — and in fact never
push at all: the worker lands your squash commit.

**Leaving the workspace unmodified is part of the contract.** After an abort,
`git status --porcelain` must be EMPTY and HEAD must be on the repo's default
branch. Verify it; do not assume it.

**Report it structurally.** On an abort, set `failure.kind` to `merge_conflict`
and put the git output tail in `failure.detail`.

**(d) The worker checks your work.** Before it lands anything, the worker
independently recomputes the squash merge's conflicting paths and halts the
rollout if a non-documentation conflict was resolved, or if conflict markers
remain in a resolved file.
{partial}"""
    return _indent_prompt_block(body)


# The exact phrase ``main._gate_result_summary`` renders when the build's
# post-run gate passed.  The reviewer prompt's Step 4 keys off it verbatim.
GATE_PASSED_WORDING = "the post-run gate PASSED"


def _review_envelope_block(
    item: dict[str, Any],
    branch_name: str,
    run_status: str,
    gate_result: str,
    merge_notes: list[dict[str, Any]] | None,
) -> str:
    """The evidence envelope handed to a reviewer: AC, branch, gate, notes."""
    criteria = item.get("acceptance_criteria") or []
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except ValueError:
            criteria = [criteria]
    ac_lines = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, start=1))
    if not ac_lines:
        ac_lines = "(this item declares no structured acceptance criteria)"

    scope_out = item.get("scope_out") or []
    if isinstance(scope_out, str):
        try:
            scope_out = json.loads(scope_out)
        except ValueError:
            scope_out = [scope_out]
    scope_lines = "\n".join(f"- {c}" for c in scope_out) or "(none declared)"

    notes = list(merge_notes or [])[:MERGE_NOTE_CONTEXT_LIMIT]
    if notes:
        note_rows = "\n".join(
            f"- item `{n.get('item_id') or 'unknown'}`: "
            f"{' '.join(str(n.get('note') or '').split())}"
            for n in notes
        )
    else:
        note_rows = (
            "(no item in this rollout has been merged with a merge note yet — "
            "you are the first)"
        )

    body = f"""\
## The Build You Are Reviewing

**Item:** {item.get("title", "(untitled)")} (`{item.get("id", "")}`)
**Branch:** `{branch_name}`
**Child build run terminal status:** `{run_status}`
**Post-run gate result (from the dispatch worker):** {gate_result}

### Item description

{item.get("description", "(no description)")}

### Acceptance criteria — the contract this diff must satisfy

{ac_lines}

### Declared out of scope

{scope_lines}

### Recent merge notes for this rollout (newest first)

These are the persisted record of what already-merged items in this rollout
changed that could invalidate a later item's spec. Read them as fact. They are
what replaces a resident manager's accumulated context — you have none, and you
are not supposed to.

{note_rows}"""
    return _indent_prompt_block(body)


def _review_fix_mode_block(
    gate_failure_output: str, *, workspace_mode: bool, where: str
) -> str:
    """The FIX MODE section that replaces Steps 2-5 on a land-gate relaunch."""
    repos = (
        "each landing repo (every repo that has a squash commit for this item)"
        if workspace_mode
        else "the repo"
    )
    body = f"""\
## FIX MODE — the land gate failed on your squash commit

You already approved this build and created its squash commit. The dispatch
worker then fetched origin, rebased that squash commit onto the CURRENT
`origin/<default_branch>`, and ran the project gate from {where} on the exact
tree it was about to push. The gate FAILED. This is its output (tail):

```text
{gate_failure_output}
```

HEAD in {repos} is the item's squash commit, already rebased onto current
origin. Usually the failure is a contract between this item and a commit that
landed on origin after the build was cut (a test asserting state another item
changed).

Do exactly this:

1. Read the failure and make the MINIMAL fix that makes the gate green.
2. `git add` the changed files, then `git commit --amend --no-edit` in the repo
   that needs the fix, so the squash commit keeps its subject and both its
   `Rollout:` and `Item:` trailers and stays the ONLY commit ahead of origin.
3. Re-run the gate yourself to confirm it is green.

FORBIDDEN in this mode: `git checkout`, `git reset`, `git rebase`, `git merge`
and `git push`. The worker owns the branch layout and the push; it re-fetches,
re-rebases and re-gates your amended commit itself.

If the fix would need a change in a repo that has NO squash commit for this
item, the verdict is `halt` — do not create a new commit there.

Your verdict in this mode is restricted to `merge` (fixed and amended) or
`halt` (anything else, including a fix that is not small). Never
`re-dispatch` or `skip`."""
    return _indent_prompt_block(body)


def build_review_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    rollout_id: str,
    branch_name: str,
    max_turns: int,
    workspace: Path,
    *,
    repo_dirs: list[str] | None = None,
    default_branches: dict[str, str] | None = None,
    workspace_mode: bool = False,
    run_status: str = "unknown",
    gate_result: str = "(not reported)",
    merge_notes: list[dict[str, Any]] | None = None,
    redispatches_used: int = 0,
    redispatch_cap: int = 1,
    gate_failure_output: str | None = None,
    engine_name: str | None = None,
) -> str:
    """System prompt for REVIEW mode — review ONE build, then approve or refuse.

    The scope is deliberately one item.  A full map of both manage prompt
    variants found that of roughly twenty steps, exactly FOUR are irreducibly
    model work — acceptance-criteria reconciliation, the unrelated-manifest
    scope judgment, the inline-fix small-or-not decision, and sensitive-area
    discretion — and all four sit inside the review window for a SINGLE
    completed build.  Everything else in the old loop is deterministic and
    lives in the worker — including, now, the push: the reviewer leaves its
    squash commit LOCAL and the worker's land step fetches, rebases onto
    current origin, gates that exact tree and pushes it.

    When *gate_failure_output* is not None the prompt is in FIX MODE: Steps 2-5
    and the conflict contract are replaced by a section asking for a minimal
    amend of the existing squash commit after the land gate failed.
    """
    fix_mode = gate_failure_output is not None
    project_name = project.get("name", "")
    dirs = list(repo_dirs or [])
    branches = dict(default_branches or {})
    gate_command = (project.get("gate_command") or "").strip()
    item_id = str(item.get("id", ""))

    if workspace_mode:
        repo_bullets = "\n".join(
            f"- `{d}/` (default branch `{branches.get(d, 'main')}`)" for d in dirs
        )
        layout = f"""\
Your workspace is a **workspace root** containing one git clone per repo, each
already checked out on its own default branch and hard-reset to origin:

{repo_bullets}

Squash-commit order is that list order, repos that received the branch only."""
        where = "the workspace root"
    else:
        only = dirs[0] if dirs else project_name
        layout = (
            f"Your workspace is a single git clone of `{only}`, checked out on "
            f"`{branches.get(only, 'main')}` and hard-reset to origin."
        )
        where = "the repo root"

    if gate_command:
        gate_block = f"""\
The merge bar is the project's stored `gate_command`. It is NOT something you
infer, and it is the same command the dispatch worker already ran as the
post-run gate (and will run again on the rebased tree before it pushes). Run it
verbatim from {where}:

```bash
{gate_command}
```

Do not derive a different test/lint command from `CLAUDE.md` or `README.md` and
approve against that instead. Two reviewers inferring two different bars for
the same repo apply two different standards to consecutive items — that is a
correctness problem, not a style one."""
    else:
        gate_block = f"""\
This project has NO stored `gate_command`, so you are in FALLBACK mode: read
`CLAUDE.md` / `README.md`, infer the test and lint commands, and run them from
{where} before creating the squash commit. A gate-less project is
not a rollout with no quality bar. If a repo has no discoverable test or lint command at all,
record `none` for it and continue — do NOT halt for that alone."""

    envelope = _review_envelope_block(
        item, branch_name, run_status, gate_result, merge_notes
    )
    conflict_contract = _review_conflict_contract_block(workspace_mode=workspace_mode)
    pinned_subject = compose_squash_subject(item)
    if pinned_subject is not None:
        pinned_type = str(item["commit_type"])
        commit_type = (
            f"This item's commit type was groomed: `{pinned_type}`. The squash "
            f"commit subject is EXACTLY: `{pinned_subject}`. Do NOT change the "
            "type, the scope or the subject text. Do not apply the derivation rules."
        )
        squash_subject = pinned_subject
        verdict_type_example = pinned_type
        verdict_reason_example = "groomed on the item"
        verdict_type_rules = _indent_prompt_block(
            f"""\
- `commit_type` is REQUIRED when `verdict` is `merge` and MUST be exactly `{pinned_type}` (groomed on the item).
- `commit_type_reason` MUST be exactly `groomed on the item`."""
        )
    else:
        commit_type = _manage_commit_type_block()
        squash_subject = f"<type>({item_id[:8]}): {item.get('title', '')}"
        verdict_type_example = "feat"
        verdict_reason_example = "rule 3: adds a new MCP endpoint"
        verdict_type_rules = _indent_prompt_block(
            """\
- `commit_type` is REQUIRED when `verdict` is `merge` and is exactly one
  of `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `chore`,
  `build`, `ci`; it MUST equal the type of the squash commit you created.
- `commit_type_reason` is one short sentence naming which rule (1, 2 or
  3) fired and why."""
        )
    merge_note_rules = _manage_merge_note_block()
    turn_discipline = _turn_discipline_block()
    standard_rules = _standard_rules_block(engine_name)

    redispatch_line = (
        f"This item has already been re-dispatched {redispatches_used} time(s); "
        f"the cap is {redispatch_cap} per item. "
        + (
            "A further `re-dispatch` verdict will be converted to a HALT by the "
            "worker, so choose it only if you believe one more build genuinely "
            "fixes this."
            if redispatches_used >= redispatch_cap
            else "A `re-dispatch` verdict is still available."
        )
    )

    layout = _indent_prompt_block(layout)
    if fix_mode:
        steps = _review_fix_mode_block(
            str(gate_failure_output), workspace_mode=workspace_mode, where=where
        )
    else:
        steps = textwrap.indent(
            textwrap.dedent(
                f"""\
                ## Step 2 — Find which repos actually received the branch

                In EVERY repo directory:
                ```bash
                git fetch origin
                git ls-remote origin refs/heads/{branch_name}
                ```
                Non-empty output means that repo has the branch. `git ls-remote` is
                AUTHORITATIVE. Cross-check against the build agent's final comment,
                which is required to list exactly which repos it pushed to. Either
                direction of disagreement is a `halt` — name both sources in your
                rationale:
                - the comment claims a repo the remote does not have: a push was
                  reported that did not land;
                - the remote has a branch the comment did not list: unreported work.

                If NO repo has the branch: read the child run's terminal status in the
                envelope above. `already_satisfied` can now only come from a talos child,
                where the talos harness itself ran the item's checks and exited 30 to say
                the criteria were already met — it is the harness's verdict, not anything
                the agent claimed, and a `claude-code` child can no longer produce it at
                all. Return `skip`. Any other status with no branch is a `halt` (or a
                `re-dispatch`, if the run failed for a reason another build would
                plausibly not hit).

                Do NOT infer "the work was already done" from a green gate plus no
                commits. An unchanged tree passes a gate trivially because it IS the base
                commit, and treating that as evidence of a completed no-op is what
                discarded four agents' worth of real work in a single night.

                ## Step 3 — Reconcile the acceptance criteria against the diff

                ```bash
                git checkout {branch_name}
                git diff origin/<default_branch>...{branch_name}
                ```

                This is the judgment you were launched for. Read the diff against the
                acceptance criteria listed above, one at a time, and decide whether the
                work actually satisfies them — not whether it looks plausible, and not
                whether the tests pass (the gate answers that separately).

                Three specific judgments are yours and cannot be reduced to a predicate:

                1. **Unrelated manifest changes.** If the diff adds to
                   `package.json` / `package-lock.json` / `pyproject.toml` / `uv.lock`
                   things not tied to the item's stated scope, treat them as suspect:
                   revert those specific changes with `git checkout HEAD -- <file>` and
                   re-run the bar. A dependency added in passing is how supply chain
                   surface grows without anyone deciding to grow it.
                2. **Is the fix small?** See Step 4.
                3. **Sensitive areas.** If the diff touches auth code (`**/auth.py`,
                   `**/auth_routes.py`, route authentication), deploy/release scripts
                   (`deploy.sh`, `release.sh`, `start.sh`), CI or hooks (`.github/**`,
                   `.pre-commit-config.yaml`), infrastructure units (`*.service`,
                   `Dockerfile*`, `nginx*.conf`) or env/secrets (`.env*`, `.envrc*`),
                   prefer `halt` over `merge`. This is discretion, not a hard predicate:
                   a one-word typo fix in a Dockerfile comment is routine; a change to
                   how a route authenticates is not.

                If the approved work will invalidate a LATER item's spec, say so in your
                rationale — the worker persists it as this rollout's merge note and
                every subsequent reviewer is handed it.

                ## Step 4 — Run the merge bar in every repo that received the branch

                **Skip the merge bar when the post-run gate already passed.** If the
                `Post-run gate result` line above says "{GATE_PASSED_WORDING}",
                do NOT run the merge bar yourself unless you make an inline change.
                The worker's land step re-runs the project gate on the rebased
                post-merge tip before anything is pushed, so a reviewer gate run
                on the same tip is duplication. If that line reports a failure,
                says the gate may have been skipped, is absent or is unclear, or
                if you apply ANY inline fix, run the merge bar exactly as
                described below.

                {{gate_block}}

                **No long-running verification.** Do not run long-running
                verification to check an acceptance criterion: no mutation testing
                (e.g. `cargo mutants`, `mutmut`, `stryker`), no benchmarks, no
                soak or load runs, and no single command you expect to take more
                than 5 minutes. Judge such criteria from the diff, the tests the
                build added, the build agent's reported evidence in its final
                comment, and the post-run gate. If a criterion can only be
                confirmed by such a run and the build reported no evidence for
                it, return `halt` naming that criterion instead of running it.
                Short targeted checks (a single test file or module) remain
                allowed.

                **Inline-fix phase boundary.** A SMALL gate failure may be fixed inline
                before you create the squash commit; after the squash commit exists, any
                failure is `halt`. Small means formatting, a single missing import, a
                one-line change, a coverage ratchet bump, a stale test assertion — then
                re-run the bar. "Small" is your judgment; if the fix is non-trivial, or
                your first attempt does not make the bar green, stop and return
                `re-dispatch` (the build agent has the item's full context and you do
                not) or `halt`.

                ALL repos that received the branch must pass the bar before ANY squash
                commit is created.

                ## Step 5 — Create the squash commit LOCALLY (never push)

                Immediately before each repo's squash, run the commit-count guard using
                THAT repo's recorded default branch:
                ```bash
                git fetch origin {branch_name}
                commit_count=$(git rev-list origin/<default_branch>..{branch_name} --count)
                ```
                `commit_count == 0` with a child status of `already_satisfied` is a
                `skip`. `commit_count == 0` with any other status is a `halt`.

                {{commit_type}}

                Squash, inside each repo's directory, onto THAT repo's LOCAL default
                branch:
                ```bash
                set -euo pipefail
                git checkout <default_branch>
                git merge --squash {branch_name}
                git commit -F - <<'COMMITEOF'
                {squash_subject}

                Rollout: {rollout_id}
                Item: {item_id}
                COMMITEOF
                ```

                Leave the commit UNPUSHED, leave HEAD on `<default_branch>`, and set
                `repo_states[<repo>] = "committed"` for that repo. The squash commit
                must be the ONLY commit your default branch has ahead of
                `origin/<default_branch>`. Do not delete the feature branch.

                {{conflict_contract}}"""
            ),
            " " * 8,
        ).lstrip(" ")
        # str.replace, not str.format: the rendered text carries the item
        # title and shell braces, either of which would break format().
        steps = (
            steps.replace("{gate_block}", _indent_prompt_block(gate_block))
            .replace("{commit_type}", commit_type)
            .replace("{conflict_contract}", conflict_contract)
        )

    if fix_mode:
        decide = """\
## What You Decide In FIX MODE

You return `merge` once your amended squash commit makes the gate green, or
`halt` for anything else. The worker re-checks, re-rebases, re-gates and pushes
on `merge`; it halts the rollout on `halt`."""
    else:
        decide = f"""\
## What You Decide, And What You Do Not

You return a VERDICT from a CLOSED set. When — and only when — that verdict is
`merge`, you create the item's squash commit LOCALLY; you never push it:

| verdict | meaning | who acts |
|---|---|---|
| `merge` | the work satisfies the ACs and passes the bar — YOU created the local squash commit | the WORKER fetches, rebases onto current origin, runs the project gate and pushes; then it records the item complete |
| `re-dispatch` | the work is wrong or incomplete but another build plausibly fixes it | the WORKER re-dispatches the item |
| `skip` | nothing to merge — the work was already done, or the branch is empty and legitimately so | the WORKER records the item skipped and advances |
| `halt` | anything you are not certain about | the WORKER halts the rollout for a human |

**YOU DO NOT PUSH.** The dispatch worker fetches origin, rebases your squash
commit onto the CURRENT `origin/<default_branch>`, runs the project gate on that
exact tree and pushes it only on green. Another item may land on origin while
you review; only the worker can see that. A push from you is detected and halts
the rollout.

**YOU DO NOT DISPATCH.** You have no dispatch URL and no dispatch key,
deliberately: re-dispatching is the worker's decision to execute, not
yours. Do not try to call a dispatch tool, do not shell out to one, and
do not treat their absence as a misconfiguration. Returning
`re-dispatch` IS how you ask for another build.

**YOU DO NOT RECORD OUTCOMES.** The worker records the item's outcome in
the rollout after reading your verdict. Do not call
`complete_item_in_rollout`, `complete_item`, `advance_rollout` or
`halt_rollout` yourself — these tools are blocked for you, and recording
an outcome yourself makes the worker's own record collide with it.

{redispatch_line}

**When in doubt, `halt`.** A halted rollout recovers. A landed
regression does not. `halt` is never the wrong answer when you are
uncertain; `merge` frequently is."""
    decide = _indent_prompt_block(decide)

    return textwrap.dedent(
        f"""\
        You are a headless MERGE REVIEWER dispatched by the Agent GTD wave loop.
        No human is available for questions — you must work autonomously.

        **Mode: REVIEW** — You review ONE completed build and either approve it
        (by creating its squash commit locally) or refuse it. You are short-lived
        by design: you were launched for this one item and you exit when you have
        written your verdict.

        **Project:** {project_name}
        **Rollout ID:** {rollout_id}
        **Turns remaining:** {max_turns}

        {layout}

        {envelope}

        {decide}

        ## Step 1 — Publish your phase

        ```
        mcp__agent-gtd__update_rollout_state(
            rollout_id="{rollout_id}",
            phase="reviewing",
            current_item_id="{item_id}",
            current_step="Reviewing {branch_name}",
        )
        ```
        The call REPLACES all four state fields — pass `current_item_id`.

        {steps}

        {merge_note_rules}

        ## Guardrails — Never Lower the Quality Bar

        - NEVER lower `[tool.coverage.report] fail_under`. Coverage ratchets up
          only. A `chore: lower coverage threshold` commit on the branch is a
          guardrail violation: revert it before creating the squash commit, or
          halt.
        - Do not comment out test hooks, skip the suite, add blanket
          `# type: ignore`, or bypass hooks with `--no-verify`.
        - Never force-push, and never push at all.

        {standard_rules}

        {turn_discipline}

        ## Completion Artifact — this is how you return your verdict

        Write this file as the LAST action of your run, on EVERY path. It is the
        ONLY channel the worker reads; a run that ends without it is treated as
        `halt`, and your reasoning is lost.

        ```
        {workspace}/{VERDICT_ARTIFACT_RELPATH}
        ```

        Create the parent directory first if needed. Write it at that ABSOLUTE
        path regardless of which repo directory you are currently in.

        ```json
        {{
          "schema_version": 1,
          "verdict": "merge",
          "rationale": "one or two sentences on why",
          "merge_note": "signature/rename/config-key changes, or 'no signature, rename or config-key changes'",
          "commit_type": "{verdict_type_example}",
          "commit_type_reason": "{verdict_reason_example}",
          "repo_states": {{"<repo_dir>": "committed"}},
          "resolved_conflict_paths": [],
          "failure": {{"kind": "", "detail": ""}}
        }}
        ```

        - `verdict` is exactly one of `merge`, `re-dispatch`, `skip`, `halt`.
          Any other string is rejected and treated as `halt`.
        - `rationale` is REQUIRED and must be non-empty.
        - `merge_note` is REQUIRED when `verdict` is `merge`.
        {verdict_type_rules}
        - `repo_states` maps each repo directory to exactly one of `committed`,
          `failed`, `untouched`. Required whenever anything was committed or
          attempted.
        - `resolved_conflict_paths` lists every path whose squash-merge
          conflict you resolved: repo-relative, prefixed `<repo_dir>/` in
          workspace mode, and `[]` when nothing was resolved.
        - `failure.kind` is one of `merge_conflict`, `gate_failed`,
          `commit_count_zero`, `ac_not_satisfied`, `sensitive_area`, ``
          (empty when there was no failure).

        ## Rules

        - You have max {max_turns} turns. Budget them wisely.
        - Never touch rollouts or items outside `rollout_id={rollout_id}`.
        - Do not dispatch anything. Do not halt the rollout yourself — return
          the `halt` verdict and let the worker do it.
        - Post at most one comment on the item, summarising your verdict. On
          `merge` it must say the build was APPROVED and that the dispatch
          worker lands it — not that it was merged, because nothing has
          reached origin yet.
          When `resolved_conflict_paths` is non-empty, the item comment lists
          those paths.
    """
    )


def _build_gate_section_build(gate_command: str, workspace_mode: bool) -> str:
    """'## Quality Gate' section for build-mode runs with a project gate_command."""
    where = "the workspace root" if workspace_mode else "the repo root"
    return (
        "## Quality Gate\n\n"
        "This project's quality gate command is:\n\n"
        f"```bash\n{gate_command}\n```\n\n"
        f"Run it from {where} and make it exit 0 before you push. After you "
        "exit, the dispatch worker re-runs this exact command from "
        f"{where} and marks the run failed if it exits non-zero."
    )


def _build_build_prompt(
    item: dict[str, Any],
    project: dict[str, Any],
    branch_name: str,
    max_turns: int,
    workspace: Path,
    attachments: list[dict[str, Any]] | None = None,
    run_id: str = "",
    workspace_repo_dirs: list[str] | None = None,
    engine_name: str | None = None,
) -> str:
    """System prompt for build mode — implement and push a branch.

    The prompt asks the agent for NOTHING that its run's outcome depends on. It used to end with a "completion artifact" contract — a JSON file the agent wrote to declare how its run ended — and the worker branched on what that file said. That is gone: an agent-originated signal is a request, not a guarantee, and a run's status must be derivable from what the worker can observe for itself (commits on origin, the gate result, the CLI's own result envelope). What the agent is still asked for — terse progress comments — is for HUMANS to read, and no code anywhere reads it.

    ``workspace`` is retained in the signature for the workspace-layout section and for callers that pass it positionally.
    """
    item_id = item["id"]
    turn_discipline = _turn_discipline_block()
    standard_rules = _standard_rules_block(engine_name)

    files_section = _build_supporting_files_section(attachments, run_id)

    # No attachments rule here: the staged `{run_id}-attachments/` directory is
    # git-excluded mechanically by `_setup_git_exclude` before the agent starts,
    # so asking the agent not to `git add` it would be a second signal for a
    # constraint the worker already enforces.
    prompt = textwrap.dedent(
        f"""\
        You are a headless coding agent dispatched by Agent GTD.
        No human is available for questions — you must work autonomously.

        ## Your Task

        Fetch GTD item `{item_id}` via the `get_item` MCP tool. Implement it
        per its acceptance criteria, modifying the files it specifies.
        The plan agent has already done the research — trust the spec.
        """
    )

    if files_section:
        prompt += "\n" + files_section + "\n"

    if workspace_repo_dirs:
        prompt += (
            "\n"
            + _build_workspace_layout_section_build(workspace_repo_dirs, branch_name)
            + "\n"
        )

    _gate_command = (project.get("gate_command") or "").strip()
    if _gate_command:
        prompt += (
            "\n"
            + _build_gate_section_build(_gate_command, bool(workspace_repo_dirs))
            + "\n"
        )

    prompt += textwrap.dedent(
        f"""\

        {standard_rules}

        {turn_discipline}

        ## Rules

        1. **Fetch the item first.** Call `get_item` with item_id="{item_id}" as your first action.
        2. **Branch.** You are already on branch `{branch_name}`. Stay on it.
        3. **Test.** Run the project's test suite before committing. Fix failures.
        4. **Commit.** Use conventional commit messages. Small, focused commits.
           **Commit as you go** — do not leave finished work uncommitted while you
           move on to the next thing. Work that is only in the working tree when
           your turn ends is work nobody can review.
        5. **Push.** When done, run `git push` to push `{branch_name}` to origin, in the
           foreground (see **Turn Discipline** above). You do not need to verify the
           push landed — the dispatch worker verifies every repo's remote ref itself
           and fails the run if any commit did not reach origin.
        6. **Stop if stuck.** If the task is too ambiguous, you lack information, or
           you cannot complete it cleanly — STOP. Commit and push whatever is
           finished, say what stopped you in a comment, and end. Do not guess or
           produce low-quality work.

        ## How your run is judged

        Your outcome is decided from three things the dispatch worker observes for
        itself: the commits that reached origin on `{branch_name}`, the project's
        quality gate, and the agent CLI's own result envelope. Nothing you write
        decides it. In particular:

        - **A run that ends with zero commits on origin fails.** There is no
          declaration, file or comment that makes a commit-less run succeed. If the
          acceptance criteria turn out to be already satisfied by existing code, say
          so in a comment naming what already exists and where — a human will read
          it — but understand the run itself is still recorded as having produced
          nothing.
        - You do NOT set the item's status. The worker moves the item itself.

        ## Reporting

        Post progress comments to the GTD item as you work. Use `add_comment` with
        item_id="{item_id}". Humans and lead agents read these in the UI, and they are
        the only place a decision you made is visible to them.

        Keep them terse — one line is fine. Post only what a reader could NOT get from
        the run itself: a judgement call you had to make, a surprise in the code, an
        acceptance criterion you read differently than it was probably meant. Do not
        narrate the phases of your work, and do not restate the branch name, the
        commit count or the test result — the worker already reports all of that.

        On success, a final one-line comment with anything the reviewer should know
        before reading the diff. If you stopped early or got blocked, say what stopped
        you and what information would unblock you.

        ## Important

        - You have max {max_turns} turns. Budget them wisely.
        - Never force-push, never delete branches you didn't create.
        - Never modify CI/CD configs, deployment scripts, or secrets.
        - Focus only on this task. Don't fix unrelated issues you notice.
    """
    )

    return prompt


# Rollout-mutating tools the merge reviewer must never call: the worker records
# the item's outcome itself after reading the verdict, and a reviewer-made call
# makes the worker's own record collide with it.
REVIEWER_DISALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__agent-gtd__complete_item_in_rollout",
    "mcp__agent-gtd__complete_item",
    "mcp__agent-gtd__advance_rollout",
    "mcp__agent-gtd__halt_rollout",
    "mcp__agent-gtd__cancel_rollout",
    "mcp__agent-gtd__start_rollout",
    "mcp__agent-gtd__dispatch_item",
    "mcp__agent-gtd__dispatch_rollout",
    # Advisory only: claude-code engines honour it and it does not match
    # `git -C <dir> push`. The worker's reviewer-pushed guard is authoritative.
    "Bash(git push:*)",
)


async def run_agent(
    engine: Engine,
    workspace: Path,
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None = None,
    timeout_seconds: int | None = None,
    allowed_tools: list[str] | None = None,
    mode: DispatchMode = DispatchMode.BUILD,
    attribution: str | None = None,
    popen_callback: Callable[[subprocess.Popen[bytes]], None] | None = None,
    callback_token: str | None = None,
    run_id: str = "",
    disallowed_tools: list[str] | None = None,
    executor: concurrent.futures.ThreadPoolExecutor | None = None,
    tmp_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a headless agent CLI as a subprocess.

    ``tmp_dir`` is the run's private TMPDIR (see ``run_tmp``); it is passed
    as ``/usr/bin/env TMPDIR=.. TMP=.. TEMP=..`` in the argv, inside the sudo
    wrap.  None leaves the argv exactly as before.

    ``executor`` picks the thread pool the blocking subprocess wait runs on;
    None means the shared ``_executor``.  Rollout-loop reviewers pass
    ``_review_executor``.

    ``disallowed_tools`` (claude-code engines only) is passed as
    ``--disallowedTools``. It is a deny-list because REVIEW runs use
    ``--dangerously-skip-permissions``, under which an allow-list restricts
    nothing.

    ``callback_token`` is the run's per-run GTD JWT (scoped to the dispatching
    user). It is threaded into ``build_env`` so the agent's own agent-gtd MCP
    identity authenticates as that user; when None, the static host key is used.

    ``run_id`` is used only to git-exclude this run's staged-attachments
    directory; an empty value simply adds no extra exclude line.
    """
    if timeout_seconds is None:
        # Single source for the mode default — see config.timeout_seconds_for_mode.
        timeout_seconds = config.timeout_seconds_for_mode(mode)
    if engine.name == "kiro":
        (workspace / "system_prompt.md").write_text(
            f"{system_prompt}\n\n---\n\n## Task\n\n{title}"
        )
    cmd = engine.build_command(system_prompt, title, max_turns, agent_name)
    if allowed_tools is not None and engine.name == "claude-code":
        # Insert --allowedTools BEFORE --print.  claude's argparser breaks
        # when --allowedTools sits between --print and the positional prompt
        # ("Error: Input must be provided ... when using --print"); --print
        # must be the last flag before the prompt.
        print_idx = cmd.index("--print")
        cmd[print_idx:print_idx] = ["--allowedTools", ",".join(allowed_tools)]
    if disallowed_tools and engine.name.startswith("claude-code"):
        # Same placement rule as --allowedTools: before --print.
        print_idx = cmd.index("--print")
        cmd[print_idx:print_idx] = ["--disallowedTools", ",".join(disallowed_tools)]
    env = build_env(engine, mode=mode, callback_token=callback_token)
    if attribution:
        env["AGENT_GTD_AGENT_NAME"] = attribution
    # Tag the subprocess env with the engine identifier so KB-side telemetry can
    # distinguish headless build agents from interactive control-plane sessions and
    # attribute which engine pulled a whispered map.  This var is set ONLY on the
    # per-subprocess env dict — it is never written to os.environ.
    env["HEADLESS_BUILD_ENGINE"] = engine.name

    cmd = _sudo_wrap(with_tmp_env(cmd, tmp_dir))
    transcript_path = workspace / "transcript.txt"
    # Exclude transcript.txt, .dispatch/ and this run's staged attachments
    # BEFORE the subprocess starts — the agent is never asked not to commit
    # them, it is prevented from doing so.
    _setup_git_exclude(workspace, [attachments_exclude_line(run_id)] if run_id else [])

    def _stream() -> subprocess.CompletedProcess[str]:
        with transcript_path.open("wb") as f:
            proc = subprocess.Popen(
                cmd,
                cwd=workspace,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                # Session leader: the sudo wrapper's pid is then the session
                # id, so kill_run_session can reach the agent and its whole
                # command tree on timeout/cancel (kb-03965).
                start_new_session=True,
            )
            if popen_callback is not None:
                popen_callback(proc)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                # Kill the whole session, not just the sudo wrapper: SIGKILL
                # cannot be relayed by sudo, so proc.kill() alone orphans the
                # agent and every command it spawned (kb-03965).
                kill_run_session(proc, signal.SIGKILL)
                proc.wait()
                raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout="", stderr="")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor or _executor, _stream)
