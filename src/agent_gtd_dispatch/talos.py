"""Talos engine execution path — TaskSpec + env overlay + argv + result mapping.

The worker orchestrates commit/push and comment-back in ``main._dispatch_worker``
(the talos binary has no GTD access by design); this module contains the pure
functions the worker composes:

- :func:`serialize_task_spec` — GTD-verbatim 5-key projection of item + project
- :func:`talos_env_overlay` — per-engine env dict (pinned literals per the ACs)
- :func:`build_talos_argv` — the ``talos run --workspace ... --task-id ...``
  argv, sudo-wrapped for the two-user split
- :func:`map_talos_result` — pure exit-code → (RunStatus, push, comment) mapper
  covering all four talos exit codes (0/10/20/1) and both exit-1 shapes without
  ever conflating exit 1 (engine broke) with exit 20 (task failed)
- :func:`parse_disposition_summary` — externally-tagged Disposition JSON parser
  for the comment-back path (Done/Blocked/Failed)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import config
from .dispatch import _sudo_wrap
from .models import RunStatus

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# TaskSpec serialization — GTD-verbatim 5-key projection
# ---------------------------------------------------------------------------

# The five keys the talos TaskSpec contract consumes. This projection is
# deliberately narrow — extra GTD item keys (id/status/labels/version/blockers)
# are NOT copied; talos tolerates extra keys but any drift here is invisible in
# tests. Any change to these five names is a wire-contract break.
_TASK_SPEC_KEYS = (
    "title",
    "description",
    "acceptance_criteria",
    "files_to_modify",
    "gate_command",
)


def serialize_task_spec(item: dict[str, Any], project: dict[str, Any]) -> str:
    """Serialize a GTD item + project to a talos TaskSpec JSON string.

    Projects EXACTLY five keys:

    - ``title`` ← ``item['title']``
    - ``description`` ← ``item.get('description', '')`` (null/absent → ``''``)
    - ``acceptance_criteria`` ← ``item['acceptance_criteria']``
    - ``files_to_modify`` ← ``item['files_to_modify']`` (list of ``{path, change}``
      dicts, passed through unchanged — GTD's list[dict[str, Any]] shape matches
      the talos TaskSpec.files_to_modify contract)
    - ``gate_command`` ← ``project['gate_command']``

    Args:
        item: GTD item dict (e.g. as returned by ``gtd_client.get_item``).
        project: GTD project dict (must carry ``gate_command``).

    Returns:
        The JSON-encoded TaskSpec ready to pipe to talos on STDIN.
    """
    # item.description is str | None in the GTD schema; null-safe default
    description = item.get("description")
    if description is None:
        description = ""
    spec: dict[str, Any] = {
        "title": item["title"],
        "description": description,
        "acceptance_criteria": item["acceptance_criteria"],
        # Pass files_to_modify through unchanged — GTD stores it as
        # list[dict[str, Any]] with {path, change} keys, which is the talos
        # TaskSpec.files_to_modify shape verbatim.
        "files_to_modify": item["files_to_modify"],
        "gate_command": project["gate_command"],
    }
    return json.dumps(spec)


# ---------------------------------------------------------------------------
# Per-engine env overlay
# ---------------------------------------------------------------------------


def talos_env_overlay(engine_name: str) -> dict[str, str]:
    """Return the env-var overlay applied to the talos subprocess for *engine_name*.

    The final subprocess env is a filtered base env (COMMON_ENV_KEYS only, no
    engine.env_keys inherited from the parent since talos gets everything via
    this overlay) MERGED with this overlay. Git identity/credential keys are
    intentionally absent — the WORKER (not talos) owns commit and push, so
    talos never sees GIT_AUTHOR_NAME / GIT_COMMITTER_NAME / etc.

    ANTHROPIC_API_KEY IS deliberately exposed here to the anthropic-backed
    talos engines (talos-haiku/sonnet/opus) — a DELIBERATE REVERSAL of the
    claude-code convention (engines.py withholds the key per kb-01512 because
    Claude Code prefers API billing over the user's Max subscription). Talos
    is a raw Anthropic API client with no Max subscription, so the key is
    REQUIRED for auth.

    Fixed literals (must not be made configurable):

    - ``OLLAMA_THINK='on'`` for talos-qwen — the qwen model expects thinking.
    - ``OLLAMA_NUM_CTX='32768'`` for talos-qwen — talos only self-defaults
      num_ctx for localhost URLs (main.rs:295-299); the Pi's Ollama runs on a
      remote host so omitting the pin silently shrinks context.
    - ``OLLAMA_BASE_URL='https://ollama.com'`` for talos-glm — glm-5.2:cloud
      lives only on Ollama Cloud; no operator override.
    """
    if engine_name == "talos-haiku":
        return {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-haiku-4-5",
            # ANTHROPIC_API_KEY reversal vs. claude-code: talos has no Max sub —
            # see kb-01512 and the module docstring above.
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }
    if engine_name == "talos-sonnet":
        return {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
            # ANTHROPIC_API_KEY reversal vs. claude-code — see kb-01512.
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }
    if engine_name == "talos-opus":
        return {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-opus-4-8",
            # ANTHROPIC_API_KEY reversal vs. claude-code — see kb-01512.
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }
    if engine_name == "talos-qwen":
        return {
            "TALOS_BACKEND": "ollama",
            "OLLAMA_MODEL": "qwen3.6:35b",
            # Hardcoded literals — see docstring for the num_ctx reasoning.
            "OLLAMA_THINK": "on",
            "OLLAMA_NUM_CTX": "32768",
            "OLLAMA_BASE_URL": config.OLLAMA_BASE_URL,
            "OLLAMA_API_KEY": config.OLLAMA_API_KEY,
        }
    if engine_name == "talos-glm":
        return {
            "TALOS_BACKEND": "ollama",
            "OLLAMA_MODEL": "glm-5.2:cloud",
            # The only hardcoded base URL — glm-5.2:cloud lives on Ollama Cloud.
            "OLLAMA_BASE_URL": "https://ollama.com",
            # Distinct cloud key — no fallback to config.OLLAMA_API_KEY (see
            # config.py comment on OLLAMA_CLOUD_API_KEY).
            "OLLAMA_API_KEY": config.OLLAMA_CLOUD_API_KEY,
        }
    msg = f"Not a talos engine: {engine_name!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Subprocess argv
# ---------------------------------------------------------------------------


def build_talos_argv(workspace_dir: Path, task_id: str, attempt: int) -> list[str]:
    """Return the sudo-wrapped ``talos run …`` argv for the subprocess launch.

    - Uses ``config.TALOS_BIN`` (default ``'talos'``, PATH-resolved).
    - Passes ``--workspace``, ``--task-id``, ``--attempt``. The TaskSpec JSON is
      piped on STDIN by the caller — NOT via ``--file``.
    - Does NOT pass ``--max-iterations`` or ``--gate-timeout-secs`` (relies on
      talos defaults 12 / 300 — see the scope-out note in the item).
    - Does NOT pass ``--run-store`` / ``--offload-dir`` — talos writes artifacts
      to its XDG default (``${XDG_STATE_HOME:-~/.local/state}/talos/<task-id>``),
      OUTSIDE the clone, so the worker's ``git add`` never touches them.
    - Sudo-wrapped like every other subprocess so the clone owner (dispatch,
      under the two-user split) runs it.
    """
    argv = [
        config.TALOS_BIN,
        "run",
        "--workspace",
        str(workspace_dir),
        "--task-id",
        task_id,
        "--attempt",
        str(attempt),
    ]
    return _sudo_wrap(argv)


# ---------------------------------------------------------------------------
# Exit-code → (RunStatus, push, comment) mapper
# ---------------------------------------------------------------------------


# Talos exit codes (verified against harness-design main.rs:153-162):
#   0  → verified Done (RunSummary on stdout, verification passed)
#   10 → Blocked (RunSummary on stdout, disposition=Blocked)
#   20 → task failed (RunSummary on stdout, disposition=Failed with mode
#        Loop|BudgetExhausted|PersistentToolError|StoppedWithoutFinish|
#        MaxIterations)
#   1  → engine/infra error, TWO shapes:
#        (a) pre-run infra error → stdout empty, one-line {"error":...} on stderr
#        (b) completed run with outcome=BackendError → full RunSummary on stdout


def map_talos_result(
    exit_code: int, stdout_line: str, stderr_line: str
) -> tuple[RunStatus, bool, str]:
    """Map a talos exit code + last stdout/stderr lines to (status, push, comment).

    ``push`` is True ONLY for verified Done (exit 0 with a parseable RunSummary).
    Exit codes 1 (engine broke) and 20 (task failed) are NEVER conflated — the
    caller must be able to distinguish them from comment text alone.

    Args:
        exit_code: The talos process's exit code.
        stdout_line: The last stdout line (talos writes one JSON RunSummary line
            per run; see main.rs). May be empty for pre-run infra errors.
        stderr_line: The last stderr line (JSON ``{"error": ...}`` for pre-run
            infra errors).

    Returns:
        A tuple ``(status, push, comment_text)`` where ``comment_text`` is a
        short human-readable summary suitable for the GTD comment prefix. The
        caller may enrich it with disposition details (via
        :func:`parse_disposition_summary`).
    """
    if exit_code == 0:
        # Malformed-stdout guard: exit 0 with unparseable/empty stdout is NEVER
        # treated as Done — we surface it as engine-broke instead so we can
        # never push work whose completion evidence is illegible.
        if not stdout_line:
            return (
                RunStatus.failed,
                False,
                "talos engine error (retryable/investigate): exit 0 with empty stdout",
            )
        try:
            json.loads(stdout_line)
        except json.JSONDecodeError:
            return (
                RunStatus.failed,
                False,
                (
                    "talos engine error (retryable/investigate): "
                    "exit 0 with unparseable RunSummary JSON"
                ),
            )
        return (RunStatus.succeeded, True, "talos verified task Done")
    if exit_code == 10:
        return (
            RunStatus.failed,
            False,
            "talos blocked — decision needed",
        )
    if exit_code == 20:
        return (
            RunStatus.failed,
            False,
            "talos task failed",
        )
    if exit_code == 1:
        # Two exit-1 shapes — distinguishable by stdout emptiness. Both are
        # engine-broke, but we surface the specific error the operator needs to
        # act on. Wording is deliberately DISTINCT from exit-20 so a reader can
        # tell the two apart from comment text alone.
        if not stdout_line:
            # Pre-run infra error — stderr carries the JSON {"error": ...}
            return (
                RunStatus.failed,
                False,
                (
                    "talos engine error (retryable/investigate): "
                    f"{stderr_line or 'no error details'}"
                ),
            )
        # Post-run BackendError — stdout carries a RunSummary with
        # outcome=BackendError and disposition=Failed. Surface it as engine
        # broke, not task failed.
        return (
            RunStatus.failed,
            False,
            "talos engine error (retryable/investigate): BackendError",
        )
    return (
        RunStatus.failed,
        False,
        f"talos engine error (retryable/investigate): unknown exit code {exit_code}",
    )


# ---------------------------------------------------------------------------
# Externally-tagged Disposition JSON parsing
# ---------------------------------------------------------------------------


def parse_disposition_summary(disposition: dict[str, Any]) -> str:
    """Extract a short human-readable summary from a talos Disposition dict.

    Disposition and Verification are serialized externally-tagged
    (harness-design run_record.rs:261-291 — no ``serde(tag=…)`` attr):

    - Done → ``{"Done": {"summary": str, "verification": {"Checks": ...}
      | "NoChecksConfigured"}}``
    - Blocked → ``{"Blocked": {"decision_needed": str}}``
    - Failed → ``{"Failed": {"mode": FailureMode, "summary": str}}`` where
      FailureMode ∈ {Loop, BudgetExhausted, PersistentToolError,
      TransientInfra, StoppedWithoutFinish, MaxIterations}

    Returns a compact human-readable string for the comment body; on Done it
    additionally appends the mechanical verification evidence.
    """
    if "Done" in disposition:
        done = disposition["Done"]
        summary = done.get("summary", "")
        verification = done.get("verification")
        # verification may be a string ("NoChecksConfigured") or a dict
        # ({"Checks": <CheckReport>}) — render both for the comment reader.
        if isinstance(verification, dict) and "Checks" in verification:
            checks_json = json.dumps(verification["Checks"], indent=2)
            return (
                f"Done: {summary}\n\n"
                f"Verification (Checks):\n```json\n{checks_json}\n```"
            )
        return f"Done: {summary}\n\nVerification: {verification}"
    if "Blocked" in disposition:
        blocked = disposition["Blocked"]
        return f"Blocked: decision needed — {blocked.get('decision_needed', '')}"
    if "Failed" in disposition:
        failed = disposition["Failed"]
        mode = failed.get("mode", "unknown")
        summary = failed.get("summary", "")
        return f"Failed ({mode}): {summary}"
    return f"Unknown disposition: {disposition!r}"


def build_comment_body(
    exit_code: int,
    stdout_line: str,
    stderr_line: str,
    branch_name: str | None,
) -> str:
    """Build the comment-back body from talos's output.

    Combines the exit-code header with disposition details (when the RunSummary
    is parseable) and the branch name. Called from the worker on every terminal
    exit — talos has no GTD access, so this comment is the reviewer's only
    surface for the mechanical verification evidence.
    """
    status, _push, header = map_talos_result(exit_code, stdout_line, stderr_line)
    parts: list[str] = [f"talos run finished (exit {exit_code}): {header}"]
    if branch_name:
        parts.append(f"Branch: `{branch_name}`")
    # Try to parse the RunSummary for disposition + iterations context. Any
    # parse failure is silent — the header alone is enough to render, and
    # engine-broke cases legitimately have no RunSummary on stdout.
    if stdout_line:
        try:
            summary = json.loads(stdout_line)
        except json.JSONDecodeError:
            summary = None
        if isinstance(summary, dict):
            outcome = summary.get("outcome")
            iterations = summary.get("iterations")
            if outcome is not None or iterations is not None:
                bits: list[str] = []
                if outcome is not None:
                    bits.append(f"outcome={outcome}")
                if iterations is not None:
                    bits.append(f"iterations={iterations}")
                parts.append(" ".join(bits))
            disposition = summary.get("disposition")
            if isinstance(disposition, dict):
                parts.append(parse_disposition_summary(disposition))
    elif stderr_line and status == RunStatus.failed:
        # Engine-broke pre-run infra error: quote the stderr line the operator
        # needs. map_talos_result already embedded it in the header for exit 1;
        # avoid duplicating it there.
        pass
    return "\n\n".join(parts)


__all__ = [
    "build_comment_body",
    "build_talos_argv",
    "map_talos_result",
    "parse_disposition_summary",
    "serialize_task_spec",
    "talos_env_overlay",
]
