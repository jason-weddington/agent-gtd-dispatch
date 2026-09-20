"""The agent CLI's own result envelope — the completion evidence the worker can trust.

The envelope is the CLI's ``--output-format json`` terminal object, extracted from
the merged stdout+stderr transcript that ``dispatch.run_agent`` streams to
``transcript.txt``.  It is MECHANICAL evidence: the harness emits it, not the agent,
so it cannot be skipped, forgotten or embellished by the model running inside it.

There used to be a second leg here — an agent-authored ``completion.json`` stating
what the agent believed it did.  It is gone, and deliberately so.  Anything the agent
originates (a file, a comment, an MCP call) is a REQUEST, not a guarantee: it is
produced by the same process whose reliability is in question, so a run's status must
never turn on whether it appeared.  A run's terminal is now built from this envelope,
the commits that reached origin, and the project gate result — three things the worker
observes for itself.

This module only READS.  It never posts to GTD, never touches the database and never
decides a run's status.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Only the tail of the transcript is scanned: the envelope is the last thing the
# CLI writes, and a long build can leave a multi-hundred-MB transcript behind.
MAX_TRANSCRIPT_TAIL_BYTES: int = 1048576


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
