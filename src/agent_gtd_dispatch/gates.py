"""Pre-launch per-repo gate install — hook-manager detection + install + verification.

Runs once per cloned repo, after the workspace clone and BEFORE the agent
launches (non-talos build/manage runs only — see ``main._dispatch_worker``).
Today a dispatched agent's clone has active hooks only through the host
``init.templateDir`` pre-commit shims (which call pre-commit with
``--skip-on-missing-config``), so lefthook and husky repos end up with NO
active hooks — an agent can push a tree that fails the gate while the run
reports success. This module makes hook installation deterministic and
worker-enforced instead of prompt-driven:

- :func:`detect_gate_steps` — filesystem-only detection (no subprocess calls)
  of which hook manager a repo declares, in precedence order: a committed
  ``.agent-gtd/setup`` override beats auto-detected lefthook, pre-commit, or
  husky. A repo with none of these is a no-op, not a failure.
- :func:`run_gate_steps` — runs each detected step as the agent subprocess
  user, then (for auto-detected managers, not the override) verifies the
  resulting hooks are actually LIVE — not just that ``.git/hooks/pre-commit``
  exists, since the templateDir shim means it always does.
- :func:`run_as_agent` / :func:`agent_shell_env` — the sudo-wrapped,
  minimal-env subprocess runner shared with the sibling post-run gate
  (850c058b).
- :func:`format_failure_comment` / :func:`format_success_lines` — comment
  bodies for the GTD item / rollout halt.
"""

from __future__ import annotations

import logging
import os

# Re-exported at module scope (rather than imported locally inside a function)
# so tests can patch `agent_gtd_dispatch.gates.pwd.getpwnam`. `pwd` is a
# process-wide singleton module object, so patching the `getpwnam` attribute
# here also affects `engines.agent_local_bin_dirs`'s own `import pwd` call —
# agent_shell_env() delegates its PATH computation to that helper rather than
# duplicating the lookup.
import pwd  # noqa: F401
import stat
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .dispatch import _sudo_wrap
from .engines import prepend_agent_bin_dirs

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Git client-side hook names lefthook/husky may install. Order matters for
# the "verified_hooks" log line and for scanning for a manager's live hook.
GIT_CLIENT_HOOK_NAMES: tuple[str, ...] = (
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "post-rewrite",
)

# All four lefthook config filename variants, checked in this order.
LEFTHOOK_CONFIG_NAMES: tuple[str, ...] = (
    "lefthook.yml",
    ".lefthook.yml",
    "lefthook.yaml",
    ".lefthook.yaml",
)

# Hook types `pre-commit install` is asked for. Deliberately excludes
# post-commit — see docs/architecture.md.
PRE_COMMIT_HOOK_TYPES: tuple[str, ...] = ("pre-commit", "commit-msg", "pre-push")

# Minimal env-var allowlist for gate-install subprocesses. Deliberately
# excludes every credential (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN,
# AGENT_GTD_API_KEY, DISPATCH_API_KEY, ...) — the hook-manager install or a
# committed `.agent-gtd/setup` script has no legitimate need for them.
GATE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG", "TERM", "SHELL")

# Config filenames/dirs that indicate a repo WANTS a hook manager the
# auto-detector doesn't support (e.g. a non-YAML lefthook config, or a
# .githooks core.hooksPath convention). Detected only to emit a warning — the
# repo still needs a `.agent-gtd/setup` override to get gate coverage.
UNSUPPORTED_HOOK_CONFIG_NAMES: tuple[str, ...] = (
    "lefthook.toml",
    ".lefthook.toml",
    "lefthook.json",
    ".lefthook.json",
    ".config/lefthook.yml",
    ".config/lefthook.yaml",
    ".githooks",
)

OUTPUT_TAIL_CHARS = 1500
OVERRIDE_LOG_TAIL_CHARS = 500


class HookManager(StrEnum):
    """Which mechanism installs/owns a repo's git hooks."""

    OVERRIDE = "override"
    LEFTHOOK = "lefthook"
    PRE_COMMIT = "pre-commit"
    HUSKY = "husky"


# Auto-detected managers (excludes OVERRIDE, which replaces auto-detection
# entirely rather than competing with it) and the marker path(s) each owns —
# used for the shadowed-markers warning.
_AUTO_MANAGER_MARKERS: dict[HookManager, tuple[str, ...]] = {
    HookManager.LEFTHOOK: LEFTHOOK_CONFIG_NAMES,
    HookManager.PRE_COMMIT: (".pre-commit-config.yaml",),
    HookManager.HUSKY: (".husky",),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateStep:
    """One repo's detected gate-install step, not yet sudo-wrapped."""

    repo_label: str
    repo_path: Path
    manager: HookManager
    argv: list[str]
    step: str
    marker: str


@dataclass(frozen=True, slots=True)
class GateFailure:
    """Why a gate step failed (install error, timeout, or verification)."""

    repo_label: str
    step: str
    reason: str
    output_tail: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class AgentCommandResult:
    """Result of a single :func:`run_as_agent` subprocess invocation."""

    returncode: int | None
    output: str
    error: str | None
    duration_ms: int


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _tail(raw: bytes | str | None) -> str:
    """Return the last OUTPUT_TAIL_CHARS characters of *raw* (decoded if bytes)."""
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return text[-OUTPUT_TAIL_CHARS:]


def _decode_bytes(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Agent-user subprocess runner (reusable by the sibling post-run gate)
# ---------------------------------------------------------------------------


def agent_shell_env() -> dict[str, str]:
    """Build a minimal env dict for gate-install subprocesses.

    Copies only GATE_ENV_KEYS from the parent env — no credentials
    (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, AGENT_GTD_API_KEY,
    DISPATCH_API_KEY, ...) ever reach a hook-manager install or a committed
    `.agent-gtd/setup` script. PATH is built from the same agent-bin-dir
    helper `engines.build_env` uses (`engines.prepend_agent_bin_dirs`), so
    gate installs see the same `~/.local/bin` (uvx/MCP binaries) and
    `~/.cargo/bin` (rustup toolchain binaries) prefix the agent subprocess
    itself gets.
    """
    env = {k: v for k, v in os.environ.items() if k in GATE_ENV_KEYS}
    env["PATH"] = prepend_agent_bin_dirs(env.get("PATH", ""))
    return env


def run_as_agent(
    argv: list[str], cwd: Path, timeout_seconds: int
) -> AgentCommandResult:
    """Run *argv* as the agent subprocess user, sudo-wrapped, with a minimal env.

    Never raises — subprocess.TimeoutExpired and OSError are both caught and
    mapped to an AgentCommandResult with `error` set. Reusable by the post-run
    gate (sibling 850c058b), which runs the project `gate_command` after a
    successful agent exit under the same sudo/env constraints.
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            _sudo_wrap(argv),
            cwd=cwd,
            env=agent_shell_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return AgentCommandResult(
            returncode=None,
            output=_decode_bytes(exc.output if isinstance(exc.output, bytes) else None),
            error=f"timed out after {timeout_seconds}s",
            duration_ms=duration_ms,
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return AgentCommandResult(
            returncode=None,
            output="",
            error=f"could not start: {exc}",
            duration_ms=duration_ms,
        )
    duration_ms = int((time.monotonic() - start) * 1000)
    return AgentCommandResult(
        returncode=result.returncode,
        output=_decode_bytes(result.stdout),
        error=None,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Detection (filesystem-only, no subprocess calls)
# ---------------------------------------------------------------------------


def _existing_markers(root: Path, manager: HookManager) -> list[str]:
    names = _AUTO_MANAGER_MARKERS[manager]
    if manager is HookManager.HUSKY:
        return [n for n in names if (root / n).is_dir()]
    return [n for n in names if (root / n).is_file()]


def _detect_one(root: Path) -> tuple[HookManager, list[str], str, str] | None:
    """Return (manager, argv, step, marker) for the first-matching manager."""
    override_path = root / ".agent-gtd" / "setup"
    if override_path.is_file():
        return (
            HookManager.OVERRIDE,
            ["/bin/bash", ".agent-gtd/setup"],
            ".agent-gtd/setup",
            ".agent-gtd/setup",
        )
    for name in LEFTHOOK_CONFIG_NAMES:
        if (root / name).is_file():
            return (
                HookManager.LEFTHOOK,
                ["/bin/bash", "-c", "lefthook install"],
                "lefthook install",
                name,
            )
    if (root / ".pre-commit-config.yaml").is_file():
        cmd = (
            "pre-commit install --hook-type pre-commit "
            "--hook-type commit-msg --hook-type pre-push"
        )
        return (
            HookManager.PRE_COMMIT,
            ["/bin/bash", "-c", cmd],
            cmd,
            ".pre-commit-config.yaml",
        )
    if (root / ".husky").is_dir():
        return (
            HookManager.HUSKY,
            ["git", "config", "core.hooksPath", ".husky"],
            "git config core.hooksPath .husky",
            ".husky",
        )
    return None


def detect_gate_steps(repos: list[tuple[str, Path]], *, run_id: str) -> list[GateStep]:
    """Detect the gate-install step for each (label, path) repo, filesystem-only.

    Precedence per repo: a committed `.agent-gtd/setup` override REPLACES
    auto-detection entirely; otherwise the first of lefthook / pre-commit /
    husky whose marker file/dir exists wins. A repo with none of these is a
    no-op (omitted from the result), not a failure. Never touches the network
    or spawns a subprocess.
    """
    steps: list[GateStep] = []
    for label, path in repos:
        root = Path(path)
        detected = _detect_one(root)

        if detected is not None:
            manager, argv, step, marker = detected
            logger.info(
                "gate: run_id=%s repo=%s manager=%s marker=%s decision=selected",
                run_id,
                label,
                manager.value,
                marker,
            )
            if manager in _AUTO_MANAGER_MARKERS:
                ignored: list[str] = []
                for other in _AUTO_MANAGER_MARKERS:
                    if other is manager:
                        continue
                    ignored.extend(_existing_markers(root, other))
                if ignored:
                    logger.warning(
                        "gate: run_id=%s repo=%s decision=shadowed-markers "
                        "selected=%s ignored=%s",
                        run_id,
                        label,
                        marker,
                        ",".join(ignored),
                    )
            steps.append(
                GateStep(
                    repo_label=label,
                    repo_path=root,
                    manager=manager,
                    argv=argv,
                    step=step,
                    marker=marker,
                )
            )
        else:
            logger.info(
                "gate: run_id=%s repo=%s manager=none marker=- decision=no-manager",
                run_id,
                label,
            )
            found = [n for n in UNSUPPORTED_HOOK_CONFIG_NAMES if (root / n).exists()]
            if found:
                logger.warning(
                    "gate: run_id=%s repo=%s decision=no-manager-unsupported-config "
                    "found=%s",
                    run_id,
                    label,
                    ",".join(found),
                )

    return steps


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _matching_hooks(
    hooks_dir: Path, predicate: Callable[[Path], bool]
) -> tuple[list[str], str | None]:
    """Scan GIT_CLIENT_HOOK_NAMES for regular files matching *predicate*.

    Returns (matched names in GIT_CLIENT_HOOK_NAMES order, first read/stat
    error message or None).
    """
    matched: list[str] = []
    for name in GIT_CLIENT_HOOK_NAMES:
        path = hooks_dir / name
        try:
            if not path.is_file():
                continue
            ok = predicate(path)
        except OSError as exc:
            return matched, f"could not read {path}: {exc}"
        if ok:
            matched.append(name)
    return matched, None


def _verify_pre_commit(hooks_dir: Path) -> tuple[list[str], str | None]:
    verified: list[str] = []
    for hook in PRE_COMMIT_HOOK_TYPES:
        path = hooks_dir / hook
        try:
            is_file = path.is_file()
            content = path.read_bytes() if is_file else b""
        except OSError as exc:
            return verified, f"could not read {path}: {exc}"
        if (
            not is_file
            or b"File generated by pre-commit" not in content
            or b"--skip-on-missing-config" in content
        ):
            return verified, f"{hook} in {hooks_dir} is not a pre-commit-installed hook"
        verified.append(hook)
    return verified, None


def _verify_lefthook(hooks_dir: Path) -> tuple[list[str], str | None]:
    matched, err = _matching_hooks(hooks_dir, lambda p: b"lefthook" in p.read_bytes())
    if err is not None:
        return matched, err
    if not matched:
        return matched, f"no lefthook-managed hook found in {hooks_dir}"
    return matched, None


def _verify_husky(step: GateStep, hooks_dir: Path) -> tuple[list[str], str | None]:
    expected = (step.repo_path / ".husky").resolve()
    if hooks_dir.resolve() != expected:
        return [], f"core.hooksPath resolves to {hooks_dir}, expected .husky"
    matched, err = _matching_hooks(
        hooks_dir, lambda p: bool(p.stat().st_mode & stat.S_IXUSR)
    )
    if err is not None:
        return matched, err
    if not matched:
        return matched, "no executable hook script found in .husky/"
    return matched, None


def _verify_manager(step: GateStep, hooks_dir: Path) -> tuple[list[str], str | None]:
    if step.manager is HookManager.PRE_COMMIT:
        return _verify_pre_commit(hooks_dir)
    if step.manager is HookManager.LEFTHOOK:
        return _verify_lefthook(hooks_dir)
    if step.manager is HookManager.HUSKY:
        return _verify_husky(step, hooks_dir)
    return [], None  # OVERRIDE never reaches verification


# ---------------------------------------------------------------------------
# Install + verify runner
# ---------------------------------------------------------------------------


def run_gate_steps(
    steps: list[GateStep], timeout_seconds: int, *, run_id: str
) -> GateFailure | None:
    """Run each step in order, stopping at the first failure.

    For non-OVERRIDE steps, an exit-0 install is followed by verification
    that the effective (core.hooksPath-aware) pre-commit hook actually
    belongs to the declared manager — the host's init.templateDir shims mean
    `.git/hooks/pre-commit` exists in every clone regardless, so existence
    alone proves nothing. OVERRIDE steps are not verified: exit 0 within the
    timeout is success. Never raises.
    """
    for step in steps:
        step_start = time.monotonic()
        result = run_as_agent(step.argv, step.repo_path, timeout_seconds)

        if result.error is not None or result.returncode != 0:
            reason = (
                result.error
                if result.error is not None
                else f"exit code {result.returncode}"
            )
            duration_ms = int((time.monotonic() - step_start) * 1000)
            return GateFailure(
                step.repo_label, step.step, reason, _tail(result.output), duration_ms
            )

        if step.manager is HookManager.OVERRIDE:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            logger.info(
                "gate: run_id=%s repo=%s manager=override decision=override-ok "
                "duration_ms=%d output_tail=%r",
                run_id,
                step.repo_label,
                duration_ms,
                result.output[-OVERRIDE_LOG_TAIL_CHARS:],
            )
            continue

        install_ms = int((time.monotonic() - step_start) * 1000)
        verify_start = time.monotonic()

        try:
            rev_result = subprocess.run(
                _sudo_wrap(["git", "rev-parse", "--git-path", "hooks"]),
                cwd=step.repo_path,
                env=agent_shell_env(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            return GateFailure(
                step.repo_label,
                f"hook verification ({step.manager.value})",
                "could not resolve hooks directory: git rev-parse --git-path hooks "
                f"timed out after {timeout_seconds}s",
                _tail(result.output),
                duration_ms,
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            return GateFailure(
                step.repo_label,
                f"hook verification ({step.manager.value})",
                f"could not resolve hooks directory: {exc}",
                _tail(result.output),
                duration_ms,
            )

        if rev_result.returncode != 0:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            return GateFailure(
                step.repo_label,
                f"hook verification ({step.manager.value})",
                "could not resolve hooks directory: git rev-parse --git-path hooks "
                f"exited {rev_result.returncode}",
                _tail(result.output),
                duration_ms,
            )

        hooks_dir = step.repo_path / rev_result.stdout.strip()
        verified_hooks, verify_reason = _verify_manager(step, hooks_dir)
        if verify_reason is not None:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            return GateFailure(
                step.repo_label,
                f"hook verification ({step.manager.value})",
                verify_reason,
                _tail(result.output),
                duration_ms,
            )

        verify_ms = int((time.monotonic() - verify_start) * 1000)
        logger.info(
            "gate: run_id=%s repo=%s manager=%s decision=installed-verified "
            "hooks_dir=%s verified_hooks=%s install_ms=%d verify_ms=%d",
            run_id,
            step.repo_label,
            step.manager.value,
            hooks_dir,
            ",".join(verified_hooks),
            install_ms,
            verify_ms,
        )

    return None


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


def format_failure_comment(run_id: str, failure: GateFailure) -> str:
    """Build the GTD item/rollout-halt comment body for a gate failure."""
    header = (
        f"Gate install failed before the agent launched (run `{run_id}`). "
        f"The agent was not started.\n\n"
        f"- Repo: `{failure.repo_label}`\n"
        f"- Step: `{failure.step}`\n"
        f"- Result: {failure.reason}"
    )
    if failure.output_tail:
        return header + f"\n\n```\n{failure.output_tail}\n```"
    return header


def format_success_lines(steps: list[GateStep]) -> str:
    """Build the dispatch-comment suffix lines for successfully gated repos."""

    def _verb(s: GateStep) -> str:
        if s.manager is HookManager.OVERRIDE:
            return "override ran (unverified)"
        return "verified"

    return "".join(
        f"\nGit hooks: `{s.repo_label}` {s.manager.value} — {_verb(s)}" for s in steps
    )


__all__ = [
    "GATE_ENV_KEYS",
    "GIT_CLIENT_HOOK_NAMES",
    "LEFTHOOK_CONFIG_NAMES",
    "OUTPUT_TAIL_CHARS",
    "OVERRIDE_LOG_TAIL_CHARS",
    "PRE_COMMIT_HOOK_TYPES",
    "UNSUPPORTED_HOOK_CONFIG_NAMES",
    "AgentCommandResult",
    "GateFailure",
    "GateStep",
    "HookManager",
    "agent_shell_env",
    "detect_gate_steps",
    "format_failure_comment",
    "format_success_lines",
    "run_as_agent",
    "run_gate_steps",
]
