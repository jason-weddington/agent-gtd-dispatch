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
    """A disposition plus the provenance that says how much to trust it.

    ``model`` is recorded WHATEVER the outcome — on an inferred verdict it names
    the model that answered, and on a derived fallback it names the model that
    was tried and did not.  Recording it only on success (the original shape)
    threw away the single field that would have identified a misconfigured
    model/endpoint pairing, which is exactly how a permanently broken tier 2
    hid behind a bare ``api_error`` for a week.
    """

    disposition: str
    reason: str
    provenance: Provenance
    model: str | None = None
    latency_seconds: float | None = None
    # Full diagnostic one-liner — class, status code, model, base URL, attempt.
    classifier_failure: str | None = None
    # The bare failure CLASS, for grouping/querying without parsing the line.
    classifier_failure_class: str | None = None
    # The HTTP status, when the failure had one. 401 here is the tell that the
    # model is not served on this endpoint (see config.py's compatibility note).
    classifier_status_code: int | None = None


# The failure CLASSES.  A class answers "what kind of broken is this?" in one
# word, which is the question an operator reading a run comment is asking:
#
#   disabled       — the master switch is off; no call was made
#   no_credential  — the configured env var is empty; no call was made
#   timeout        — the call was made and did not answer in time
#   http_status    — the provider answered with an error status (carries it)
#   transport      — the call never got an answer (DNS, TLS, connection reset)
#   parse          — the provider answered, but not with a usable verdict
#
# `http_status` vs `transport` is the distinction that matters most: the first
# means the provider heard us and refused, the second that it never heard us.
FAILURE_CLASSES: tuple[str, ...] = (
    "disabled",
    "no_credential",
    "timeout",
    "http_status",
    "transport",
    "parse",
)


@dataclass(frozen=True)
class ClassificationFailure:
    """The inferred tier's failure sentinel — an expected outcome, not an error.

    ``reason`` is the failure CLASS (a member of :data:`FAILURE_CLASSES`).  The
    remaining fields are what makes a failure DIAGNOSABLE from the outside: a
    transient 429 and a permanently misconfigured model/endpoint pairing look
    identical when all that is recorded is a single word.
    """

    reason: str
    latency_seconds: float = 0.0
    status_code: int | None = None
    model: str | None = None
    base_url: str | None = None
    attempt: int = 0
    # Sub-kind or exception class: `empty_response`, `unrecognized_response`,
    # `APIConnectionError`, ... Never the exception's message — that can carry
    # the prompt or the credential.
    note: str = ""

    @property
    def detail(self) -> str:
        """The full one-liner recorded in the completion blob and the WARNING.

        Fields that are absent are omitted rather than rendered as ``None``, so
        a failure that never reached the wire stays short and a failure that did
        carries everything needed to tell misconfiguration from a blip.
        """
        parts = [self.reason]
        if self.note:
            parts.append(f"({self.note})")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.base_url:
            parts.append(f"base_url={self.base_url}")
        if self.attempt:
            parts.append(f"attempt={self.attempt}/{CLASSIFIER_MAX_ATTEMPTS}")
        return " ".join(parts)

    @property
    def summary(self) -> str:
        """The SHORT form for user-facing text: the class, plus a status code.

        An operator reading a GTD comment must be able to tell "the provider is
        having a moment" from "this is wired up wrong" without opening the
        completion blob.  ``http_status 401`` does that in two words.
        """
        if self.status_code is not None:
            return f"{self.reason} {self.status_code}"
        return self.reason


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
        return ClassificationFailure(reason="parse", note="empty_response")
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
    return ClassificationFailure(reason="parse", note="unrecognized_response")


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


def _client() -> anthropic.AsyncAnthropic:
    """Build the Anthropic-compatible client for the configured endpoint.

    NB the model must be one the ANTHROPIC-COMPATIBLE endpoint actually serves —
    see the transport/model compatibility note in config.py.  A model that the
    provider will not serve here comes back 401, not 404.
    """
    return anthropic.AsyncAnthropic(
        api_key=classifier_credential(),
        base_url=config.DISPOSITION_CLASSIFIER_BASE_URL,
        max_retries=0,  # retry policy is ours (CLASSIFIER_MAX_ATTEMPTS), not the SDK's
    )


async def _call_model(prompt: str) -> str:
    """Issue one Messages call to the configured classifier endpoint."""
    response = await _client().messages.create(
        model=config.DISPOSITION_CLASSIFIER_MODEL,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return _response_text(response)


def _status_code_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an SDK exception, or None if it had none.

    Read defensively rather than by isinstance on a specific SDK error type: the
    whole point of this field is that it survives an SDK upgrade and a swap to
    another Anthropic-compatible provider.
    """
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _failure_from_exception(
    exc: BaseException, *, attempt: int, latency: float
) -> ClassificationFailure:
    """Classify a transport exception into a DIAGNOSABLE failure.

    `http_status` (the provider answered and refused) is a different animal from
    `transport` (nothing answered), and only the first carries a status code.
    The exception's TYPE is recorded; its message never is, because it can echo
    the prompt or the credential back into the run record.
    """
    status = _status_code_of(exc)
    return ClassificationFailure(
        reason="http_status" if status is not None else "transport",
        latency_seconds=latency,
        status_code=status,
        model=config.DISPOSITION_CLASSIFIER_MODEL,
        base_url=config.DISPOSITION_CLASSIFIER_BASE_URL,
        attempt=attempt,
        note=type(exc).__name__,
    )


# Statuses worth a second attempt: a rate limit or a server-side blip may clear
# in a second. 4xx statuses outside these are a STATEMENT about the request —
# wrong credential, or (the trap this module was bitten by) a model the endpoint
# does not serve — and retrying one only spends the run's remaining budget to
# receive the same refusal.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504})


def _is_retryable(failure: ClassificationFailure) -> bool:
    """Whether a second attempt could plausibly succeed."""
    if failure.reason != "http_status":
        return True  # timeout / transport: a retry is exactly what these are for
    return failure.status_code in _RETRYABLE_STATUSES


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
    model = config.DISPOSITION_CLASSIFIER_MODEL
    base_url = config.DISPOSITION_CLASSIFIER_BASE_URL
    if not config.DISPOSITION_CLASSIFIER_ENABLED:
        return ClassificationFailure(reason="disabled")
    if not classifier_credential():
        logger.warning(
            "disposition classifier: no credential in %s (model=%s base_url=%s)"
            " — falling back to the derived tier",
            config.DISPOSITION_CLASSIFIER_API_KEY_ENV,
            model,
            base_url,
        )
        return ClassificationFailure(
            reason="no_credential",
            model=model,
            base_url=base_url,
            note=config.DISPOSITION_CLASSIFIER_API_KEY_ENV,
        )

    prompt = build_prompt(evidence)
    started = time.monotonic()
    failure = ClassificationFailure(reason="transport", model=model, base_url=base_url)
    for attempt in range(1, CLASSIFIER_MAX_ATTEMPTS + 1):
        try:
            text = await asyncio.wait_for(
                _call_model(prompt),
                timeout=config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            failure = ClassificationFailure(
                reason="timeout",
                latency_seconds=time.monotonic() - started,
                model=model,
                base_url=base_url,
                attempt=attempt,
                note=f"{config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS}s",
            )
            logger.warning(
                "disposition classifier: run %s attempt %d failed — %s",
                evidence.run_id,
                attempt,
                failure.detail,
            )
            continue
        except Exception as exc:  # every transport fault is an OUTCOME, not an error
            failure = _failure_from_exception(
                exc, attempt=attempt, latency=time.monotonic() - started
            )
            # The detail — class, status, model, base URL, attempt — goes in the
            # MESSAGE, not just the traceback: a 401 here means either a bad
            # credential or a model this endpoint will not serve, and the two
            # are indistinguishable without knowing which model was asked for.
            logger.warning(
                "disposition classifier: run %s attempt %d failed — %s",
                evidence.run_id,
                attempt,
                failure.detail,
                exc_info=True,
            )
            if not _is_retryable(failure):
                # A refusal, not a blip. Retrying buys nothing but latency.
                return failure
            continue
        latency = time.monotonic() - started
        parsed = _parse_response(text)
        if isinstance(parsed, ClassificationFailure):
            # An unusable ANSWER is not retried: the model already replied, and
            # a model that ignores the closed set once will ignore it twice.
            unusable = ClassificationFailure(
                reason=parsed.reason,
                latency_seconds=latency,
                model=model,
                base_url=base_url,
                attempt=attempt,
                note=parsed.note,
            )
            logger.warning(
                "disposition classifier: run %s unusable response — %s: %r",
                evidence.run_id,
                unusable.detail,
                text[:200],
            )
            return unusable
        return DispositionResult(
            disposition=parsed.disposition,
            reason=parsed.reason,
            provenance="inferred",
            model=model,
            latency_seconds=latency,
        )
    return failure


# Every gate decision the post-run gate can produce, named here so the
# derivation cannot silently mis-bucket one.  These strings are OWNED by the
# gate (main.py); this module only INTERPRETS them.
GATE_DECISIONS: tuple[str, ...] = (
    "passed",
    "failed",
    "timed_out",
    "launch_error",
    "skipped_no_gate_command",
    "skipped_no_pushed_repo",
)

# The gate ran and said no.  A real, negative verdict about the pushed tree.
_GATE_RAN_AND_FAILED: frozenset[str] = frozenset(
    {"failed", "timed_out", "launch_error"}
)


def derive(*, has_commits: bool, gate_decision: str | None) -> DispositionResult:
    """TIER 3. Compute a disposition mechanically.

    NEVER returns ``blocked``.  NEVER returns ``already_satisfied``.  The only
    two values this tier can produce are ``done`` and ``failed``, under every
    combination of inputs — see the two invariants at the bottom of this
    docstring, both of which are enforced by exhaustive tests.

    The full table, and only this table:

    ===========================  ===========  ==================
    gate decision                commits      disposition
    ===========================  ===========  ==================
    ``passed``                   yes          ``done``
    ``passed``                   no           ``failed``
    ``skipped_no_gate_command``  yes          ``done``
    ``skipped_no_gate_command``  no           ``failed``
    ``failed``                   either       ``failed``
    ``timed_out``                either       ``failed``
    ``launch_error``             either       ``failed``
    ``skipped_no_pushed_repo``   either       ``failed``
    ``None`` / unrecognized      either       ``failed``
    ===========================  ===========  ==================

    Read the ``commits`` column first: NO commits is ``failed`` whatever the
    gate said, and the gate decision only ever chooses between ``done`` and
    ``failed`` for a run that actually pushed something.

    The load-bearing distinction among the gate decisions is between a gate
    that RAN and said no and a gate that never ran at all.
    ``skipped_no_gate_command`` means the project has no ``gate_command``
    configured: that is INCONCLUSIVE, not negative, and treating it as negative
    made every artifact-missing run on every ungated project derive ``failed``
    regardless of the work — four consecutive false negatives on branches that
    were complete and correct.

    It also contradicted the asserted tier.  A ``done`` artifact on an ungated
    project SUCCEEDS (there is no gate result to fail), so the same project,
    same absent gate and same pushed commits produced opposite verdicts based
    only on whether the agent happened to write a file.  The two paths must
    agree, so with commits pushed this derives ``done`` as well; the reason
    text says plainly that the verdict rests on the commits alone.

    --- The two never-derived invariants ---

    ``blocked`` is deliberately underivable.  Nothing mechanical distinguishes
    "the agent stopped because a human must decide" from "the agent stopped
    because it broke", and guessing ``blocked`` would be worse than admitting
    this tier cannot tell — a wrong ``blocked`` parks an item on a human who
    has no question to answer.

    ``already_satisfied`` is underivable for the SAME reason, and more
    strongly.  This tier shipped mapping zero commits plus a passing gate to
    ``already_satisfied``, and within hours a run that produced nothing at all
    was reported to its lead as work that was already done.  An unchanged tree
    passes a test-suite gate TRIVIALLY — it is the base commit, and the base is
    green — so "the criteria were already met" and "the agent did nothing"
    leave byte-identical mechanical evidence.  The gate distinguishes broken
    from not-broken; it cannot distinguish already-done from not-attempted.
    ``already_satisfied`` is also a CLAIM ABOUT WHY that carries a REQUIRED
    reason only the agent (or a classifier reading the transcript) can supply,
    and it is not a neutral label: it routes the item to ``review``, records a
    rollout child as skipped so the wave ADVANCES past it, and tells a human
    the work exists.  A ``failed`` run is loud and re-dispatchable; a false
    ``already_satisfied`` is a silent hole in a wave that looks like progress.

    Both remaining tiers keep the disposition: an agent that ASSERTS
    ``already_satisfied`` with a reason still lands it, and so does a
    CLASSIFIER that reads one out of the transcript and the diff.  Only
    mechanical derivation of it is gone.
    """
    decision = gate_decision or "not_run"

    # Not a decision this module knows about — a string a future gate change
    # introduced.  Say so loudly instead of quietly adopting some neighbour's
    # meaning; every branch below maps an unrecognized value to `failed`.
    if decision != "not_run" and decision not in GATE_DECISIONS:
        logger.warning(
            "disposition: unrecognized gate decision %r — deriving `failed`."
            " Add it to GATE_DECISIONS and to derive()'s table.",
            decision,
        )

    if not has_commits:
        # THE regression this branch exists to prevent (run c2a8072e2e25): a
        # 22-minute run that produced no branch, no commits and no artifact was
        # derived `already_satisfied` and reported as work already done.  The
        # honest report is that the outcome is UNKNOWN.  Do not speculate in
        # either direction — a genuine no-op and a do-nothing run are
        # indistinguishable from here.
        return DispositionResult(
            disposition="failed",
            reason=(
                "derived mechanically: the run produced no commits and no"
                " completion artifact, so what the agent did — or whether it"
                " did anything at all — could not be established"
                f" (gate decision={decision}). An unchanged tree passes a"
                " quality gate trivially, so the gate cannot tell a genuine"
                " no-op apart from a run that produced nothing. The outcome is"
                " unknown; this item needs a human or a re-dispatch"
            ),
            provenance="derived",
        )

    # --- From here on the run DID push commits. ---------------------------
    # Each gate decision is enumerated; there is no catch-all `else` above the
    # final branch precisely so that an unrecognized value reaches it alone.

    if decision == "passed":
        return DispositionResult(
            disposition="done",
            reason=(
                "derived mechanically: commits were pushed and the project"
                " quality gate passed"
            ),
            provenance="derived",
        )

    if decision == "skipped_no_gate_command":
        return DispositionResult(
            disposition="done",
            reason=(
                "derived mechanically: commits were pushed; no quality gate"
                " is configured for this project, so the gate did not run"
                " and the verdict rests on the pushed commits alone."
                " Setting a project `gate_command` would make this outcome"
                " verifiable"
            ),
            provenance="derived",
        )

    if decision in _GATE_RAN_AND_FAILED:
        return DispositionResult(
            disposition="failed",
            reason=(
                "derived mechanically: the project quality gate did not pass"
                f" (decision={decision})"
            ),
            provenance="derived",
        )

    if decision == "skipped_no_pushed_repo":
        return DispositionResult(
            disposition="failed",
            reason=(
                "derived mechanically: no commits were pushed, so the quality"
                " gate was not run and nothing was produced"
            ),
            provenance="derived",
        )

    # `not_run`, or the unrecognized value warned about above.
    return DispositionResult(
        disposition="failed",
        reason=(
            "derived mechanically: the project quality gate produced no usable"
            f" result (decision={decision})"
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
        # The model is carried onto the DERIVED result on purpose: it names the
        # model that was tried and did not answer. Dropping it here is what left
        # `classifier_model: null` on the run that exposed a tier 2 which had
        # never once worked.
        model=outcome.model,
        latency_seconds=outcome.latency_seconds or None,
        classifier_failure=outcome.detail,
        classifier_failure_class=outcome.reason,
        classifier_status_code=outcome.status_code,
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
    # Short but never opaque: an operator reading this in a GTD comment must be
    # able to tell a transient provider blip from a permanent misconfiguration
    # without opening the completion blob. The class plus the status code plus
    # the model does that in one clause; the full line (with the base URL and
    # the attempt) is in the blob and the WARNING.
    if result.classifier_failure_class:
        _short = result.classifier_failure_class
        if result.classifier_status_code is not None:
            _short += f" {result.classifier_status_code}"
        if result.model:
            _short += f" from `{result.model}`"
        detail = f" (classifier unavailable: {_short})"
    elif result.classifier_failure:
        detail = f" (classifier unavailable: {result.classifier_failure})"
    else:
        detail = ""
    return (
        "The agent wrote no completion artifact and no classification was"
        f" available{detail}, so this outcome was **derived** mechanically from"
        " the commits and the quality-gate result — it is NOT the agent's own"
        " word."
    )


# --- First-use reachability probe -------------------------------------------
#
# A misconfigured classifier is INVISIBLE: every failure falls through to the
# derived tier by design, the run still ends, and the only trace is one word in
# a completion blob nobody reads until the verdicts start looking wrong.  That
# is how a tier 2 that had never once returned 200 survived a week in
# production.  One probe at startup turns "silently degraded forever" into a
# WARNING in the journal on the first line the operator greps.
#
# It is deliberately NOT fatal and deliberately NOT retried: the derived tier is
# the designed fallback, the service must start with a dead provider, and a
# provider that is merely having a bad minute must not spam the log.
_PROBE_RUN: bool = False


def _reset_reachability_probe() -> None:
    """Testing seam: forget that the one-shot probe already ran."""
    global _PROBE_RUN
    _PROBE_RUN = False


async def check_reachability() -> ClassificationFailure | None:
    """Issue ONE minimal call and report whether the classifier answers.

    Returns ``None`` when the configured model/endpoint/credential triple
    actually works, and a :class:`ClassificationFailure` describing the fault
    otherwise.  Never raises: like every other call in this module, a dead
    provider is an outcome.
    """
    if not config.DISPOSITION_CLASSIFIER_ENABLED:
        return ClassificationFailure(reason="disabled")
    model = config.DISPOSITION_CLASSIFIER_MODEL
    base_url = config.DISPOSITION_CLASSIFIER_BASE_URL
    if not classifier_credential():
        return ClassificationFailure(
            reason="no_credential",
            model=model,
            base_url=base_url,
            note=config.DISPOSITION_CLASSIFIER_API_KEY_ENV,
        )
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _client().messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return ClassificationFailure(
            reason="timeout",
            latency_seconds=time.monotonic() - started,
            model=model,
            base_url=base_url,
            attempt=1,
            note=f"{config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS}s",
        )
    except Exception as exc:  # a dead provider is an OUTCOME, not an error
        return _failure_from_exception(
            exc, attempt=1, latency=time.monotonic() - started
        )
    return None


async def warn_if_unreachable_once() -> ClassificationFailure | None:
    """Run :func:`check_reachability` at most once per process and log the result.

    Logs WARNING with the full diagnostic detail when the classifier cannot be
    reached, INFO when it can.  Returns the failure (or ``None``) so a caller
    can assert on it; callers MUST NOT treat a failure as fatal.
    """
    global _PROBE_RUN
    if _PROBE_RUN:
        return None
    _PROBE_RUN = True
    failure = await check_reachability()
    if failure is None:
        logger.info(
            "disposition classifier reachable: model=%s base_url=%s",
            config.DISPOSITION_CLASSIFIER_MODEL,
            config.DISPOSITION_CLASSIFIER_BASE_URL,
        )
        return None
    if failure.reason == "disabled":
        logger.info("disposition classifier disabled by config — derived tier only")
        return failure
    # NOT fatal. The derived tier absorbs this; the point is that it is SEEN.
    # A 401 here is ambiguous by construction: this provider answers 401 both
    # for a bad credential AND for a model it will not serve on this endpoint
    # (see config.py) — so the message names the model as well as the key.
    logger.warning(
        "disposition classifier UNREACHABLE — %s. Every unasserted run will"
        " fall through to the derived tier until this is fixed. If the status"
        " is 401, check BOTH the credential in %s AND that the model is served"
        " on this endpoint.",
        failure.detail,
        config.DISPOSITION_CLASSIFIER_API_KEY_ENV,
    )
    return failure
