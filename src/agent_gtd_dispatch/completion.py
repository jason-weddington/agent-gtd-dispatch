"""Agent completion evidence: the result envelope and the completion artifact.

Two independent legs of the build-completion contract live here.

Leg 1 — the CLI's own ``--output-format json`` result envelope, extracted from the
merged stdout+stderr transcript that ``dispatch.run_agent`` streams to
``transcript.txt``.

Leg 2 — the agent-authored completion artifact at
``<workspace>/.dispatch/completion.json``, which states what the agent believes it
did.

This module only READS.  It never posts to GTD, never touches the database and
never decides a run's status — callers combine the two legs with the push results
to reach a terminal.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from .dispatch import _sudo_wrap

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Only the tail of the transcript is scanned: the envelope is the last thing the
# CLI writes, and a long build can leave a multi-hundred-MB transcript behind.
MAX_TRANSCRIPT_TAIL_BYTES: int = 1048576

# An artifact larger than this is refused outright — an agent that writes a
# multi-GB completion.json must not be able to block teardown.
MAX_ARTIFACT_BYTES: int = 65536

KNOWN_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

VALID_DISPOSITIONS: frozenset[str] = frozenset(
    {"done", "already_satisfied", "blocked", "failed"}
)


class ResultEnvelope(BaseModel):
    """The CLI's terminal ``{"type": "result", ...}`` object.

    Extra keys are preserved so a CLI upgrade that adds fields does not silently
    drop evidence.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    subtype: str | None = None
    is_error: bool | None = None
    num_turns: int | None = None
    stop_reason: str | None = None
    terminal_reason: str | None = None
    result: str | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None


class CompletionArtifact(BaseModel):
    """The agent-authored assertion about how its run ended."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    disposition: str
    summary: str = ""
    reason: str = ""
    decision_needed: str = ""


def _iter_json_objects(text: str) -> list[Any]:
    """Return every top-level JSON object decodable from ``text``, in order.

    Scanning with ``raw_decode`` from each ``{`` (rather than ``json.loads`` on the
    whole file) is mandatory: ``run_agent`` merges stderr into the transcript, so
    the file is almost never a single well-formed JSON document and stray braces
    in a traceback must not swallow the envelope that follows them.
    """
    decoder = json.JSONDecoder()
    found: list[Any] = []
    idx = 0
    length = len(text)
    while idx < length:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        found.append(obj)
        idx = end
    return found


def parse_result_envelope(transcript_path: Path) -> ResultEnvelope | None:
    """Return the LAST ``type == "result"`` JSON object in the transcript tail.

    Returns None when the file is missing, empty, or contains no parseable result
    object.  A truncated or unbalanced trailing object is simply skipped.
    """
    try:
        with transcript_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - MAX_TRANSCRIPT_TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")

    found: ResultEnvelope | None = None
    for data in _iter_json_objects(text):
        if not isinstance(data, dict):
            continue
        if data.get("type") != "result":
            continue
        try:
            found = ResultEnvelope.model_validate(data)
        except ValueError:
            continue
    return found


def envelope_verdict(env: ResultEnvelope | None) -> str:
    """Classify a result envelope into exactly one verdict literal.

    Returns one of ``ok``, ``no_result_envelope``, ``result_is_error``,
    ``max_turns_exhausted``.

    Order is behaviour, not style: a genuine max-turns envelope carries
    ``is_error=True``, so the max-turns test MUST run before the is_error test or
    every exhausted run is misclassified as ``result_is_error``.  Likewise
    ``stop_reason`` is NOT the max-turns signal (it was observed carrying
    ``tool_use`` on a real exhausted run) — detection keys on ``subtype`` and
    ``terminal_reason`` only.
    """
    if env is None:
        return "no_result_envelope"
    if env.type != "result":
        # Defensive: parse_result_envelope already filters on type. Kept so the
        # parser's contract stays narrow instead of being widened for coverage.
        return "no_result_envelope"
    if env.subtype == "error_max_turns" or env.terminal_reason == "max_turns":
        return "max_turns_exhausted"
    if env.is_error is True:
        return "result_is_error"
    if env.subtype != "success":
        logger.warning(
            "unrecognized result subtype: subtype=%r stop_reason=%r"
            " terminal_reason=%r is_error=%s num_turns=%s",
            env.subtype,
            env.stop_reason,
            env.terminal_reason,
            env.is_error,
            env.num_turns,
        )
        return "result_is_error"
    return "ok"


def artifact_read_argv(path: Path) -> list[str]:
    """Return the argv used to read an agent-created file.

    The artifact is written by the AGENT process, which runs as
    ``config.AGENT_SUBPROCESS_USER`` while the worker runs as the service user, so
    every read goes through ``dispatch._sudo_wrap`` exactly as ``verify_pushes``
    and ``cleanup_workspace`` do.
    """
    return _sudo_wrap(["cat", str(path)])


def _read_artifact_bytes(path: Path) -> bytes | None:
    """Read an agent-created file cross-user; None when it cannot be read."""
    try:
        result = subprocess.run(  # noqa: S603
            artifact_read_argv(path),
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def locate_completion_artifact(workspace: Path) -> tuple[Path | None, str]:
    """Find the completion artifact under ``workspace``.

    The contract path is ``<workspace>/.dispatch/completion.json``.  Belt and
    braces for an agent that wrote a relative path after ``cd``-ing into a repo
    directory: exactly one ``*/.dispatch/completion.json`` hit is accepted, two or
    more is ``ambiguous_location``.
    """
    primary = workspace / ".dispatch" / "completion.json"
    if primary.exists():
        return primary, "ok"
    try:
        hits = sorted(workspace.glob("*/.dispatch/completion.json"))
    except OSError:
        hits = []
    if len(hits) == 1:
        return hits[0], "ok"
    if len(hits) > 1:
        return None, "ambiguous_location"
    return None, "absent"


def read_completion_artifact(
    workspace: Path,
) -> tuple[CompletionArtifact | None, str]:
    """Read and validate the agent's completion artifact.

    Returns ``(artifact, "ok")`` or ``(None, reason)`` where reason is one of
    ``absent``, ``not_json``, ``not_object``, ``unknown_schema_version``,
    ``unknown_disposition``, ``missing_reason``, ``missing_decision_needed``,
    ``oversize``, ``ambiguous_location``.

    A MISSING ``schema_version`` defaults to 1 and is accepted — version skew
    between the worker and a build agent must degrade gracefully.
    """
    path, located = locate_completion_artifact(workspace)
    if path is None:
        return None, located

    try:
        size = path.stat().st_size
    except OSError:
        size = None
    if size is not None and size > MAX_ARTIFACT_BYTES:
        return None, "oversize"

    raw = _read_artifact_bytes(path)
    if raw is None:
        return None, "absent"
    if len(raw) > MAX_ARTIFACT_BYTES:
        return None, "oversize"

    text = raw.decode("utf-8", errors="replace")
    try:
        data: Any = json.loads(text)
    except ValueError:
        return None, "not_json"
    if not isinstance(data, dict):
        return None, "not_object"

    if "schema_version" in data and data["schema_version"] not in KNOWN_SCHEMA_VERSIONS:
        return None, "unknown_schema_version"

    disposition = data.get("disposition")
    if disposition not in VALID_DISPOSITIONS:
        return None, "unknown_disposition"

    reason = data.get("reason")
    if disposition == "already_satisfied" and not str(reason or "").strip():
        return None, "missing_reason"

    decision_needed = data.get("decision_needed")
    if disposition == "blocked" and not str(decision_needed or "").strip():
        return None, "missing_decision_needed"

    payload = dict(data)
    payload.setdefault("schema_version", 1)
    for key in ("summary", "reason", "decision_needed"):
        if payload.get(key) is None:
            payload[key] = ""
    try:
        artifact = CompletionArtifact.model_validate(payload)
    except ValueError:
        return None, "not_object"
    return artifact, "ok"


def artifact_state(artifact: CompletionArtifact | None, reason: str) -> str:
    """Map a ``read_completion_artifact`` outcome to present|absent|malformed."""
    if artifact is not None:
        return "present"
    if reason in {"absent", "ambiguous_location"}:
        return "absent"
    return "malformed"


def log_artifact_rejection(run_id: str, workspace: Path, reason: str) -> None:
    """Log a rejected artifact at WARNING with a short raw head for triage.

    Telemetry distinguishes malformed from absent; the terminal stays binary.
    """
    if reason == "ok":
        return
    head = b""
    path, located = locate_completion_artifact(workspace)
    if path is not None and located == "ok":
        raw = _read_artifact_bytes(path)
        if raw:
            head = raw[:200]
    logger.warning(
        "completion artifact rejected: run_id=%s reason=%s raw_head=%r",
        run_id,
        reason,
        head,
    )
