"""Three-tier run disposition: asserted, then inferred, then derived.

A build run's disposition — ``done`` / ``already_satisfied`` / ``blocked`` /
``failed`` — is the only signal that distinguishes "I finished it" from "it was
already done", "I am stuck and a human must decide" and "I tried and could not".
The agent-authored completion artifact (:mod:`.completion`) is the ASSERTED tier
and always wins, but writing it is a volunteered side effect and a volunteered
side effect can always be skipped.  This module supplies the two tiers beneath
it so a run never ends with a silent gap:

1. **asserted** — the agent wrote the artifact.  Owned by :mod:`.completion`;
   this module is not consulted at all.  No cost, no latency.
2. **inferred** — the artifact is absent or unparseable, so the WORKER (never
   the agent) hands bounded evidence to a small model and asks for a pick from
   the closed four-value set.  The worker drives it, so it cannot be skipped.
3. **derived** — the classification call was disabled, timed out, errored, or
   answered with something outside the closed set, so the disposition is
   computed mechanically from commits and the gate result.

The LABEL travels with the verdict: an inferred ``blocked`` must never read as
though the agent said it.  Mechanical derivation is honest only as a FALLBACK —
as a primary design it would delete judgment, make ``blocked`` unrepresentable
and strip ``already_satisfied`` of the reason a human needs.

Nothing in here raises into the worker's terminal path.  Every failure mode of
the inferred tier is an expected outcome that returns
:class:`ClassificationFailure`, and :func:`resolve` always returns a usable
:class:`DispositionResult`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import anthropic

from . import config

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# The closed set.  A response outside it is a classification FAILURE, not a
# fifth disposition — see `_parse_response`.
VALID_DISPOSITIONS: tuple[str, ...] = (
    "done",
    "already_satisfied",
    "blocked",
    "failed",
)

Provenance = Literal["asserted", "inferred", "derived"]

# --- The evidence budget ----------------------------------------------------
#
# Every one of these is a byte ceiling, not a target.  A build transcript runs
# to hundreds of MB and the classifier's context window is measured in millions
# of tokens, so an unbounded prompt is not a crash — it is a silent per-run cost
# multiplier that nobody notices until the bill arrives.  Pin the budget here.
#
# The TAIL is what matters: an agent states why it stopped in its last few
# messages, and the beginning of a transcript is the prompt we already wrote.
# 24 KiB is roughly the last ~6k tokens — several full assistant turns — which
# is where "I could not find the config, stopping" lives.
TRANSCRIPT_TAIL_BYTES: int = 24576

# Gate output is already tail-truncated by the gate runner; 4 KiB keeps the
# failing assertion without pasting an entire pytest run into the prompt.
GATE_OUTPUT_TAIL_BYTES: int = 4096

# Acceptance criteria are included only because the worker ALREADY holds the
# item dict — no extra fetch.  Cap them so a pathological item cannot dominate.
ACCEPTANCE_CRITERIA_BYTES: int = 4096

# Belt and braces over the three budgets above: whatever the composition, the
# user message handed to the model is truncated to this.
MAX_PROMPT_BYTES: int = 49152

# --- Latency budget ---------------------------------------------------------
#
# This call runs INSIDE the worker's terminal path, after the post-run gate and
# before the run row is written, so a hung classification delays a run's
# completion and (in a rollout) the whole wave behind it.  The per-attempt
# timeout is `config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS` (default 45 s);
# with at most one retry that bounds the worst case at ~90 s — well under
# POST_RUN_GATE_MIN_SECONDS (600 s), the smallest slice of run budget the worker
# ever reserves for post-agent work.  It lives in config.py rather than here
# because a slower model is exactly the kind of change this is configured for.
#
# One retry at most.  A second attempt covers a dropped connection; a third
# would just spend the run's remaining budget on a provider that is down, which
# is precisely what the derived tier exists to absorb.
CLASSIFIER_MAX_ATTEMPTS: int = 2

# Response cap: we asked for one JSON object with two short fields.
CLASSIFIER_MAX_TOKENS: int = 256

_SYSTEM_PROMPT = (
    "You classify how a headless coding agent's run ended. You are given the"
    " tail of the agent's transcript, the mechanical facts about its commits,"
    " and the project's quality-gate result. The agent did NOT state its own"
    " outcome, which is why you are being asked.\n\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"disposition": "<one of: done | already_satisfied | blocked | failed>",'
    ' "reason": "<one line, at most 200 characters>"}\n\n'
    "Choose exactly one:\n"
    "- done: the agent implemented the work and pushed commits.\n"
    "- already_satisfied: the work was already present, so the agent correctly"
    " made no changes. The reason must name what already exists.\n"
    "- blocked: the agent could not proceed and a human must supply a decision"
    " or missing information. The reason must state what is needed.\n"
    "- failed: the agent tried and could not finish, or the quality gate did"
    " not pass.\n\n"
    "Do not invent a fifth value. Do not explain your choice outside the JSON"
    " object. Do not wrap the object in prose or code fences."
)


@dataclass(frozen=True)
class RunEvidence:
    """The bounded envelope handed up to the classifier.

    Every field is already truncated by its named budget above; constructing
    this object is the only supported way to build the prompt.
    """

    run_id: str
    engine: str
    branch: str
    transcript_tail: str
    repo_lines: tuple[str, ...]
    total_commits: int
    pushed_repos: int
    gate_decision: str
    gate_output_tail: str
    item_title: str
    acceptance_criteria: str
    artifact_reject_reason: str


@dataclass(frozen=True)
class DispositionResult:
    """A disposition plus the provenance that says how much to trust it."""

    disposition: str
    reason: str
    provenance: Provenance
    model: str | None = None
    latency_seconds: float | None = None
    classifier_failure: str | None = None


@dataclass(frozen=True)
class ClassificationFailure:
    """The inferred tier's failure sentinel — an expected outcome, not an error.

    ``reason`` is one of ``disabled``, ``no_credential``, ``timeout``,
    ``api_error``, ``empty_response``, ``unrecognized_response``.
    """

    reason: str
    latency_seconds: float = 0.0


def read_transcript_tail(path: Path, budget: int = TRANSCRIPT_TAIL_BYTES) -> str:
    """Return at most ``budget`` bytes from the END of ``path``, decoded lossily.

    Seeks rather than reads: a build transcript is routinely hundreds of MB and
    must never be pulled into memory just to look at its last few turns.
    Returns ``""`` for a missing or unreadable file — absence of evidence is not
    an error here, it is just less evidence.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - budget))
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, budget: int) -> str:
    """Clip ``text`` to ``budget`` bytes, keeping the tail and marking the cut."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= budget:
        return text
    return "…[truncated]…\n" + raw[-budget:].decode("utf-8", errors="replace")


def format_acceptance_criteria(item: dict[str, Any] | None) -> str:
    """Render an item's acceptance criteria, bounded, from the dict already held.

    Deliberately takes the worker's in-hand item dict: this evidence is included
    only because it is free.  No fetch is ever issued for it.
    """
    if not item:
        return ""
    raw = item.get("acceptance_criteria")
    if not isinstance(raw, list) or not raw:
        return ""
    return _truncate("\n".join(f"- {ac}" for ac in raw), ACCEPTANCE_CRITERIA_BYTES)


def build_evidence(
    *,
    run_id: str,
    engine: str,
    branch: str | None,
    transcript_tail: str,
    repo_lines: list[str],
    total_commits: int,
    pushed_repos: int,
    gate_decision: str | None,
    gate_output: str | None,
    item: dict[str, Any] | None,
    artifact_reject_reason: str,
) -> RunEvidence:
    """Assemble the bounded evidence envelope. Applies every byte budget."""
    return RunEvidence(
        run_id=run_id,
        engine=engine,
        branch=branch or "(unknown)",
        transcript_tail=_truncate(transcript_tail, TRANSCRIPT_TAIL_BYTES),
        repo_lines=tuple(repo_lines),
        total_commits=total_commits,
        pushed_repos=pushed_repos,
        gate_decision=gate_decision or "not_run",
        gate_output_tail=_truncate(gate_output or "", GATE_OUTPUT_TAIL_BYTES),
        item_title=str((item or {}).get("title") or ""),
        acceptance_criteria=format_acceptance_criteria(item),
        artifact_reject_reason=artifact_reject_reason,
    )


def build_prompt(ev: RunEvidence) -> str:
    """Render the user message for the classification call, size-capped."""
    lines = [
        f"# Run {ev.run_id} (engine: {ev.engine}, branch: {ev.branch})",
        "",
        f"## Item\n{ev.item_title or '(no title)'}",
    ]
    if ev.acceptance_criteria:
        lines.append(f"\n## Acceptance criteria\n{ev.acceptance_criteria}")
    lines.append("\n## Mechanical facts")
    lines.append(
        f"- commits across all repos: {ev.total_commits}"
        f" ({ev.pushed_repos} repo(s) pushed)"
    )
    for line in ev.repo_lines:
        lines.append(f"- {line}")
    lines.append(f"- post-run quality gate: {ev.gate_decision}")
    lines.append(
        f"- completion artifact: missing/unusable ({ev.artifact_reject_reason})"
    )
    if ev.gate_output_tail.strip():
        lines.append(f"\n## Quality gate output (tail)\n{ev.gate_output_tail}")
    lines.append(f"\n## Agent transcript (tail)\n{ev.transcript_tail}")
    lines.append(
        "\n## Your answer\nOne JSON object with keys `disposition` and"
        " `reason`, and nothing else."
    )
    return _truncate("\n".join(lines), MAX_PROMPT_BYTES)


def _parse_response(text: str) -> DispositionResult | ClassificationFailure:
    """Turn a raw model response into a verdict, or a failure sentinel.

    The response is only usable if it decodes to an object whose
    ``disposition`` is a member of the CLOSED set.  Anything else — prose, a
    fifth value, a truncated object, an empty string — is a classification
    failure that falls through to the derived tier.  The model is not free to
    widen the contract.
    """
    if not text.strip():
        return ClassificationFailure(reason="empty_response")
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        idx = end
        if not isinstance(obj, dict):
            continue
        disposition = obj.get("disposition")
        if isinstance(disposition, str) and disposition in VALID_DISPOSITIONS:
            reason = obj.get("reason")
            return DispositionResult(
                disposition=disposition,
                reason=str(reason or "").strip()[:500],
                provenance="inferred",
            )
    return ClassificationFailure(reason="unrecognized_response")


def _response_text(response: anthropic.types.Message) -> str:
    """Concatenate the text blocks of an Anthropic-shaped Messages response."""
    parts: list[str] = []
    for block in response.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def classifier_credential() -> str:
    """Return the API key for the configured classifier provider.

    The env var NAME is itself config
    (``DISPOSITION_CLASSIFIER_API_KEY_ENV``) so that pointing the classifier at
    a different provider is an env change on the hosts, not a code edit.
    """
    return os.environ.get(config.DISPOSITION_CLASSIFIER_API_KEY_ENV, "")


async def _call_model(prompt: str) -> str:
    """Issue one Messages call to the configured classifier endpoint."""
    client = anthropic.AsyncAnthropic(
        api_key=classifier_credential(),
        base_url=config.DISPOSITION_CLASSIFIER_BASE_URL,
        max_retries=0,  # retry policy is ours (CLASSIFIER_MAX_ATTEMPTS), not the SDK's
    )
    response = await client.messages.create(
        model=config.DISPOSITION_CLASSIFIER_MODEL,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return _response_text(response)


async def classify(
    evidence: RunEvidence,
) -> DispositionResult | ClassificationFailure:
    """TIER 2. Ask the configured model for a pick from the closed set.

    NEVER raises.  Every failure mode — disabled by config, missing credential,
    timeout, transport error, an unusable answer — returns
    :class:`ClassificationFailure` so the caller can fall through to the derived
    tier.  A classification failure is an expected outcome of a run, not an
    exception in the worker's terminal path.
    """
    if not config.DISPOSITION_CLASSIFIER_ENABLED:
        return ClassificationFailure(reason="disabled")
    if not classifier_credential():
        logger.warning(
            "disposition classifier: no credential in %s — falling back to the"
            " derived tier",
            config.DISPOSITION_CLASSIFIER_API_KEY_ENV,
        )
        return ClassificationFailure(reason="no_credential")

    prompt = build_prompt(evidence)
    started = time.monotonic()
    failure = ClassificationFailure(reason="api_error")
    for attempt in range(1, CLASSIFIER_MAX_ATTEMPTS + 1):
        try:
            text = await asyncio.wait_for(
                _call_model(prompt),
                timeout=config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "disposition classifier: attempt %d timed out after %ds (run %s)",
                attempt,
                config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS,
                evidence.run_id,
            )
            failure = ClassificationFailure(
                reason="timeout", latency_seconds=time.monotonic() - started
            )
            continue
        except Exception:
            logger.warning(
                "disposition classifier: attempt %d failed (run %s)",
                attempt,
                evidence.run_id,
                exc_info=True,
            )
            failure = ClassificationFailure(
                reason="api_error", latency_seconds=time.monotonic() - started
            )
            continue
        latency = time.monotonic() - started
        parsed = _parse_response(text)
        if isinstance(parsed, ClassificationFailure):
            # An unusable ANSWER is not retried: the model already replied, and
            # a model that ignores the closed set once will ignore it twice.
            logger.warning(
                "disposition classifier: unusable response (%s) for run %s: %r",
                parsed.reason,
                evidence.run_id,
                text[:200],
            )
            return ClassificationFailure(reason=parsed.reason, latency_seconds=latency)
        return DispositionResult(
            disposition=parsed.disposition,
            reason=parsed.reason,
            provenance="inferred",
            model=config.DISPOSITION_CLASSIFIER_MODEL,
            latency_seconds=latency,
        )
    return failure


def derive(*, has_commits: bool, gate_decision: str | None) -> DispositionResult:
    """TIER 3. Compute a disposition mechanically. NEVER returns ``blocked``.

    The mapping, and only this mapping:

    * commits pushed AND gate passed  -> ``done``
    * no commits AND gate passed      -> ``already_satisfied``
    * gate failed, skipped or not run -> ``failed``

    ``blocked`` is deliberately underivable.  Nothing mechanical distinguishes
    "the agent stopped because a human must decide" from "the agent stopped
    because it broke", and guessing ``blocked`` would be worse than admitting
    this tier cannot tell — a wrong ``blocked`` parks an item on a human who
    has no question to answer.
    """
    passed = gate_decision == "passed"
    if passed and has_commits:
        return DispositionResult(
            disposition="done",
            reason=(
                "derived mechanically: commits were pushed and the project"
                " quality gate passed"
            ),
            provenance="derived",
        )
    if passed:
        return DispositionResult(
            disposition="already_satisfied",
            reason=(
                "derived mechanically: no commits were produced and the"
                " project quality gate passed on the untouched tree"
            ),
            provenance="derived",
        )
    return DispositionResult(
        disposition="failed",
        reason=(
            "derived mechanically: the project quality gate did not pass"
            f" (decision={gate_decision or 'not_run'})"
        ),
        provenance="derived",
    )


async def resolve(
    evidence: RunEvidence,
    *,
    has_commits: bool,
    gate_decision: str | None,
) -> DispositionResult:
    """Resolve tiers 2 then 3 for a run with no usable asserted disposition.

    Always returns a usable result; the ``provenance`` field says which tier
    produced it so the caller can label it honestly.  Callers MUST NOT invoke
    this when the agent's own artifact parsed — the asserted tier wins outright
    and this call costs money and latency.
    """
    outcome = await classify(evidence)
    if isinstance(outcome, DispositionResult):
        return outcome
    derived = derive(has_commits=has_commits, gate_decision=gate_decision)
    return DispositionResult(
        disposition=derived.disposition,
        reason=derived.reason,
        provenance="derived",
        latency_seconds=outcome.latency_seconds or None,
        classifier_failure=outcome.reason,
    )


def provenance_sentence(result: DispositionResult) -> str:
    """One plain-words sentence naming where a non-asserted disposition came from.

    A reviewer must never mistake an inferred verdict for the agent's own word,
    so this never says "the agent reported" — it names the model, or says the
    outcome was computed from the diff and the gate.
    """
    if result.provenance == "asserted":
        return f"The agent asserted this run ended `{result.disposition}`."
    if result.provenance == "inferred":
        return (
            "The agent wrote no completion artifact, so this outcome was"
            " **inferred** from the transcript tail and the diff by"
            f" `{result.model or config.DISPOSITION_CLASSIFIER_MODEL}` — it is"
            " NOT the agent's own word."
        )
    detail = (
        f" (classifier unavailable: {result.classifier_failure})"
        if result.classifier_failure
        else ""
    )
    return (
        "The agent wrote no completion artifact and no classification was"
        f" available{detail}, so this outcome was **derived** mechanically from"
        " the commits and the quality-gate result — it is NOT the agent's own"
        " word."
    )
