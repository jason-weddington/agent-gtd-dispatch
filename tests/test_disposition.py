"""Tests for the three-tier run disposition: asserted -> inferred -> derived."""

from __future__ import annotations

import asyncio
import os
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from agent_gtd_dispatch import config, disposition


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Config with the classifier ENABLED — every model call is mocked below."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
        "DISPATCH_DISPOSITION_CLASSIFIER_ENABLED": "1",
        "OLLAMA_CLOUD_API_KEY": "test-cloud-key",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


def _evidence(**overrides):
    fields = {
        "run_id": "run-1",
        "engine": "claude-code-glm",
        "branch": "feat/x",
        "transcript_tail": "I pushed the branch.",
        "repo_lines": ["repo_a: pushed (2 commit(s))"],
        "total_commits": 2,
        "pushed_repos": 1,
        "gate_decision": "passed",
        "gate_output": "ok",
        "item": {"title": "T", "acceptance_criteria": ["AC-1: it works"]},
        "artifact_reject_reason": "absent",
    }
    fields.update(overrides)
    return disposition.build_evidence(**fields)


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


def _reply(text: str):
    """Patch the classifier's Messages call with a canned response body."""
    return patch.object(
        disposition,
        "_call_model",
        new=AsyncMock(return_value=text),
    )


# ---------------------------------------------------------------------------
# Config: everything that steers the classifier is an env change
# ---------------------------------------------------------------------------


class TestConfigurability:
    def test_default_model_is_one_the_anthropic_endpoint_actually_serves(
        self, monkeypatch
    ) -> None:
        """The default must be a model VERIFIED 200 on /v1/messages.

        `glm-5.3-flash` is served only by Ollama Cloud's native /api/chat wire;
        the Anthropic-compatible /v1/messages endpoint this module speaks
        answers 401 for it — not 404 — so the original default looked like an
        auth fault and never once succeeded in production.
        """
        for key in (
            "DISPATCH_DISPOSITION_CLASSIFIER_MODEL",
            "DISPATCH_DISPOSITION_CLASSIFIER_BASE_URL",
            "DISPATCH_DISPOSITION_CLASSIFIER_API_KEY_ENV",
            "DISPATCH_DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS",
        ):
            monkeypatch.delenv(key, raising=False)
        config.load()
        assert config.DISPOSITION_CLASSIFIER_ENABLED is True
        assert config.DISPOSITION_CLASSIFIER_MODEL == "glm-5.3:cloud"
        assert "flash" not in config.DISPOSITION_CLASSIFIER_MODEL
        assert config.DISPOSITION_CLASSIFIER_BASE_URL == "https://ollama.com"
        assert config.DISPOSITION_CLASSIFIER_API_KEY_ENV == "OLLAMA_CLOUD_API_KEY"
        assert config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS == 45

    def test_the_transport_model_trap_is_recorded_where_the_model_is_set(
        self,
    ) -> None:
        """The comment is the deliverable — it is what stops the re-break."""
        import inspect

        src = inspect.getsource(config)
        assert "/v1/messages" in src
        assert "/api/chat" in src
        assert "401" in src

    def test_model_base_url_and_switch_are_env_overridable(self, monkeypatch) -> None:
        """Moving to GLM 5.4/6 (or another provider) must be an env change only."""
        monkeypatch.setenv("DISPATCH_DISPOSITION_CLASSIFIER_MODEL", "glm-6-flash")
        monkeypatch.setenv(
            "DISPATCH_DISPOSITION_CLASSIFIER_BASE_URL", "https://example.test"
        )
        monkeypatch.setenv(
            "DISPATCH_DISPOSITION_CLASSIFIER_API_KEY_ENV", "SOME_OTHER_KEY"
        )
        monkeypatch.setenv("DISPATCH_DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS", "12")
        monkeypatch.setenv("DISPATCH_DISPOSITION_CLASSIFIER_ENABLED", "0")
        config.load()
        assert config.DISPOSITION_CLASSIFIER_MODEL == "glm-6-flash"
        assert config.DISPOSITION_CLASSIFIER_BASE_URL == "https://example.test"
        assert config.DISPOSITION_CLASSIFIER_API_KEY_ENV == "SOME_OTHER_KEY"
        assert config.DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS == 12
        assert config.DISPOSITION_CLASSIFIER_ENABLED is False

    def test_credential_is_read_from_the_named_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "DISPATCH_DISPOSITION_CLASSIFIER_API_KEY_ENV", "MY_PROVIDER_KEY"
        )
        monkeypatch.setenv("MY_PROVIDER_KEY", "secret-value")
        config.load()
        assert disposition.classifier_credential() == "secret-value"


# ---------------------------------------------------------------------------
# The evidence envelope is BOUNDED
# ---------------------------------------------------------------------------


class TestEvidenceBudget:
    def test_transcript_tail_is_capped_and_keeps_the_end(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_bytes(b"x" * 500_000 + b"THE-ACTUAL-ENDING")
        tail = disposition.read_transcript_tail(path)
        assert len(tail.encode()) <= disposition.TRANSCRIPT_TAIL_BYTES
        assert tail.endswith("THE-ACTUAL-ENDING")

    def test_missing_transcript_is_empty_not_an_error(self, tmp_path) -> None:
        assert disposition.read_transcript_tail(tmp_path / "nope.txt") == ""

    def test_prompt_is_capped(self) -> None:
        ev = _evidence(
            transcript_tail="y" * 5_000_000,
            gate_output="z" * 5_000_000,
            item={
                "title": "T",
                "acceptance_criteria": ["w" * 100_000 for _ in range(50)],
            },
        )
        prompt = disposition.build_prompt(ev)
        assert len(prompt.encode()) <= disposition.MAX_PROMPT_BYTES

    def test_acceptance_criteria_only_when_present(self) -> None:
        assert disposition.format_acceptance_criteria(None) == ""
        assert disposition.format_acceptance_criteria({}) == ""
        assert (
            disposition.format_acceptance_criteria(
                {"acceptance_criteria": "not a list"}
            )
            == ""
        )
        assert "AC-1" in disposition.format_acceptance_criteria(
            {"acceptance_criteria": ["AC-1: x"]}
        )

    def test_prompt_carries_the_mechanical_facts(self) -> None:
        prompt = disposition.build_prompt(_evidence())
        assert "commits across all repos: 2" in prompt
        assert "repo_a: pushed (2 commit(s))" in prompt
        assert "post-run quality gate: passed" in prompt
        assert "AC-1: it works" in prompt
        assert "I pushed the branch." in prompt


# ---------------------------------------------------------------------------
# TIER 2 — the closed set, and every way it can fail
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "value", ["done", "already_satisfied", "blocked", "failed"]
    )
    async def test_each_valid_value_is_accepted(self, value) -> None:
        with _reply(f'{{"disposition": "{value}", "reason": "because"}}'):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.DispositionResult)
        assert result.disposition == value
        assert result.reason == "because"
        assert result.provenance == "inferred"
        assert result.model == "glm-5.3:cloud"
        assert result.latency_seconds is not None

    @pytest.mark.parametrize(
        "body",
        [
            "I think the agent did fine, honestly.",
            '{"disposition": "mostly_done", "reason": "a fifth value"}',
            '{"disposition": 7}',
            '{"reason": "no disposition key"}',
            "",
            "   ",
            '{"disposition": "done"',
        ],
    )
    async def test_unrecognized_response_is_a_classification_failure(
        self, body
    ) -> None:
        """The model is NOT free to invent a fifth value or answer in prose."""
        with _reply(body):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "parse"
        assert result.note in {"unrecognized_response", "empty_response"}

    async def test_json_embedded_in_prose_is_still_read(self) -> None:
        with _reply('Sure!\n```json\n{"disposition": "blocked", "reason": "r"}\n```'):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.DispositionResult)
        assert result.disposition == "blocked"

    async def test_disabled_makes_no_network_call(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "DISPOSITION_CLASSIFIER_ENABLED", False)
        call = AsyncMock()
        with patch.object(disposition, "_call_model", new=call):
            result = await disposition.classify(_evidence())
        call.assert_not_awaited()
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "disabled"

    async def test_missing_credential_makes_no_network_call(self, monkeypatch) -> None:
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "")
        call = AsyncMock()
        with patch.object(disposition, "_call_model", new=call):
            result = await disposition.classify(_evidence())
        call.assert_not_awaited()
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "no_credential"

    async def test_timeout_is_an_outcome_not_an_exception(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "DISPOSITION_CLASSIFIER_TIMEOUT_SECONDS", 0.01)

        async def _hang(_prompt):
            await asyncio.sleep(5)
            return "never"

        with patch.object(disposition, "_call_model", new=_hang):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "timeout"

    async def test_transport_error_is_an_outcome_not_an_exception(self) -> None:
        with patch.object(
            disposition, "_call_model", new=AsyncMock(side_effect=OSError("boom"))
        ):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "transport"
        assert result.status_code is None

    async def test_at_most_one_retry(self) -> None:
        call = AsyncMock(side_effect=OSError("boom"))
        with patch.object(disposition, "_call_model", new=call):
            await disposition.classify(_evidence())
        assert call.await_count == disposition.CLASSIFIER_MAX_ATTEMPTS == 2

    async def test_retry_recovers_from_a_transient_error(self) -> None:
        call = AsyncMock(
            side_effect=[OSError("boom"), '{"disposition": "done", "reason": "r"}']
        )
        with patch.object(disposition, "_call_model", new=call):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.DispositionResult)
        assert call.await_count == 2

    async def test_an_unusable_answer_is_not_retried(self) -> None:
        """A model that ignores the closed set once will ignore it twice."""
        call = AsyncMock(return_value="nonsense")
        with patch.object(disposition, "_call_model", new=call):
            result = await disposition.classify(_evidence())
        assert call.await_count == 1
        assert isinstance(result, disposition.ClassificationFailure)

    def test_response_text_joins_text_blocks(self) -> None:
        assert disposition._response_text(_FakeResponse("hello")) == "hello"


# ---------------------------------------------------------------------------
# TIER 3 — mechanical derivation, which can never say `blocked`
# ---------------------------------------------------------------------------


class TestDerive:
    def test_commits_and_green_gate_is_done(self) -> None:
        result = disposition.derive(has_commits=True, gate_decision="passed")
        assert result.disposition == "done"
        assert result.provenance == "derived"
        assert result.reason

    def test_no_commits_and_green_gate_is_already_satisfied(self) -> None:
        result = disposition.derive(has_commits=False, gate_decision="passed")
        assert result.disposition == "already_satisfied"
        assert result.reason

    # The FULL table. Every decision the post-run gate can emit appears here
    # exactly once per commit state; a decision that is not in this table is a
    # decision nobody decided what to do with.
    _TABLE: ClassVar[list[tuple[str | None, bool, str]]] = [
        ("passed", True, "done"),
        ("passed", False, "already_satisfied"),
        ("skipped_no_gate_command", True, "done"),
        ("skipped_no_gate_command", False, "failed"),
        ("failed", True, "failed"),
        ("failed", False, "failed"),
        ("timed_out", True, "failed"),
        ("timed_out", False, "failed"),
        ("launch_error", True, "failed"),
        ("launch_error", False, "failed"),
        ("skipped_no_pushed_repo", True, "failed"),
        ("skipped_no_pushed_repo", False, "failed"),
        (None, True, "failed"),
        (None, False, "failed"),
    ]

    @pytest.mark.parametrize(("gate", "has_commits", "expected"), _TABLE)
    def test_the_gate_decision_table(self, gate, has_commits, expected) -> None:
        result = disposition.derive(has_commits=has_commits, gate_decision=gate)
        assert result.disposition == expected
        assert result.reason
        assert result.provenance == "derived"

    def test_the_table_covers_every_decision_the_gate_can_emit(self) -> None:
        """A new gate decision must not fall silently into the wrong bucket."""
        assert {row[0] for row in self._TABLE if row[0]} == set(
            disposition.GATE_DECISIONS
        )

    def test_no_gate_command_with_commits_is_done_not_failed(self) -> None:
        """The defect: an ungated project derived `failed` for every run.

        `skipped_no_gate_command` means the project has no `gate_command`. That
        is inconclusive, not negative — there is no evidence AGAINST the work.
        """
        result = disposition.derive(
            has_commits=True, gate_decision="skipped_no_gate_command"
        )
        assert result.disposition == "done"
        assert "no quality gate is configured" in result.reason
        assert "gate_command" in result.reason
        # Never claim a verdict the gate never rendered.
        assert "did not pass" not in result.reason

    @pytest.mark.parametrize("gate", ["failed", "timed_out", "launch_error"])
    @pytest.mark.parametrize("has_commits", [True, False])
    def test_a_gate_that_ran_and_failed_still_derives_failed(
        self, gate, has_commits
    ) -> None:
        """No-regression: relaxing the SKIPPED case must not relax the RAN case."""
        result = disposition.derive(has_commits=has_commits, gate_decision=gate)
        assert result.disposition == "failed"
        assert "did not pass" in result.reason

    @pytest.mark.parametrize("has_commits", [True, False])
    def test_no_pushed_repo_still_derives_failed(self, has_commits) -> None:
        """No commits means nothing happened, whatever else is true."""
        result = disposition.derive(
            has_commits=has_commits, gate_decision="skipped_no_pushed_repo"
        )
        assert result.disposition == "failed"

    def test_an_unknown_decision_derives_failed_and_says_so(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            result = disposition.derive(
                has_commits=True, gate_decision="skipped_because_mercury_retrograde"
            )
        assert result.disposition == "failed"
        assert "unrecognized gate decision" in caplog.text

    def test_asserted_and_derived_agree_on_an_ungated_project(self) -> None:
        """The two paths must not contradict each other on the same evidence.

        On the ASSERTED path a `done` artifact on a project with no
        `gate_command` succeeds: `_gate_result` is None, so the "gate did not
        pass" branch never fires and the run reaches the success writer. Given
        the SAME project, the same absent gate and the same pushed commits, the
        derived tier must reach the same verdict — otherwise whether a run is
        recorded successful depends only on whether the agent happened to write
        a file.
        """
        asserted_outcome = "done"  # what completion.py yields for a `done` artifact
        derived = disposition.derive(
            has_commits=True, gate_decision="skipped_no_gate_command"
        )
        assert derived.disposition == asserted_outcome

    @pytest.mark.parametrize(
        "gate",
        [
            "passed",
            "failed",
            "timed_out",
            "launch_error",
            "skipped_no_gate_command",
            "skipped_no_pushed_repo",
            None,
        ],
    )
    @pytest.mark.parametrize("has_commits", [True, False])
    def test_blocked_is_never_derived(self, gate, has_commits) -> None:
        """Exhaustive over the mechanical input space: no path yields `blocked`.

        `blocked` is not inferable from commits and a gate result, and guessing
        it would park an item on a human who has no question to answer.
        """
        result = disposition.derive(has_commits=has_commits, gate_decision=gate)
        assert result.disposition != "blocked"
        assert result.disposition in disposition.VALID_DISPOSITIONS


# ---------------------------------------------------------------------------
# resolve() — tier 2, then tier 3, always labelled
# ---------------------------------------------------------------------------


class TestResolve:
    async def test_inferred_wins_when_the_classifier_answers(self) -> None:
        with _reply('{"disposition": "blocked", "reason": "which schema?"}'):
            result = await disposition.resolve(
                _evidence(), has_commits=True, gate_decision="passed"
            )
        assert result.disposition == "blocked"
        assert result.provenance == "inferred"
        assert result.classifier_failure is None

    @pytest.mark.parametrize(
        ("body", "failure"),
        [("nonsense", "unrecognized_response"), ("", "empty_response")],
    )
    async def test_unusable_answer_falls_through_to_derived(
        self, body, failure
    ) -> None:
        with _reply(body):
            result = await disposition.resolve(
                _evidence(), has_commits=True, gate_decision="passed"
            )
        assert result.provenance == "derived"
        assert result.disposition == "done"
        assert result.classifier_failure_class == "parse"
        assert failure in (result.classifier_failure or "")
        # Recorded even though the call FAILED — this is the field that was
        # `null` on the run that exposed a tier 2 which had never worked.
        assert result.model == "glm-5.3:cloud"

    async def test_disabled_falls_through_to_derived(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "DISPOSITION_CLASSIFIER_ENABLED", False)
        result = await disposition.resolve(
            _evidence(), has_commits=False, gate_decision="passed"
        )
        assert result.provenance == "derived"
        assert result.disposition == "already_satisfied"
        assert result.classifier_failure == "disabled"
        assert result.classifier_failure_class == "disabled"

    async def test_error_falls_through_to_derived(self) -> None:
        with patch.object(
            disposition, "_call_model", new=AsyncMock(side_effect=OSError("x"))
        ):
            result = await disposition.resolve(
                _evidence(), has_commits=True, gate_decision="failed"
            )
        assert result.provenance == "derived"
        assert result.disposition == "failed"
        assert result.classifier_failure_class == "transport"
        assert "model=glm-5.3:cloud" in (result.classifier_failure or "")

    async def test_resolve_never_raises_on_an_unexpected_error(self) -> None:
        with patch.object(
            disposition,
            "_call_model",
            new=AsyncMock(side_effect=RuntimeError("very unexpected")),
        ):
            result = await disposition.resolve(
                _evidence(), has_commits=True, gate_decision="passed"
            )
        assert result.disposition in disposition.VALID_DISPOSITIONS


class TestProvenanceSentence:
    def test_inferred_names_the_model_and_disclaims_authorship(self) -> None:
        sentence = disposition.provenance_sentence(
            disposition.DispositionResult(
                disposition="blocked",
                reason="r",
                provenance="inferred",
                model="glm-5.3:cloud",
            )
        )
        assert "inferred" in sentence
        assert "glm-5.3:cloud" in sentence
        assert "NOT the agent's own word" in sentence
        assert "the agent reported" not in sentence

    def test_derived_says_mechanical_and_disclaims_authorship(self) -> None:
        sentence = disposition.provenance_sentence(
            disposition.DispositionResult(
                disposition="done",
                reason="r",
                provenance="derived",
                classifier_failure="timeout",
            )
        )
        assert "derived" in sentence
        assert "timeout" in sentence
        assert "NOT the agent's own word" in sentence

    def test_asserted_is_the_agent_s_own_word(self) -> None:
        sentence = disposition.provenance_sentence(
            disposition.DispositionResult(
                disposition="done", reason="", provenance="asserted"
            )
        )
        assert "The agent asserted" in sentence


# ---------------------------------------------------------------------------
# Classifier failures must be DIAGNOSABLE
#
# `classifier unavailable: api_error` is four words with no status code, no
# model and no endpoint. A transient 429 and a permanently misconfigured
# model/endpoint pairing are indistinguishable from the outside — which is
# exactly how a tier 2 that had never once returned 200 stayed invisible.
# ---------------------------------------------------------------------------


class _Status401Error(Exception):
    """Stands in for an SDK status error; the field read is `status_code`."""

    status_code = 401


class _Status429Error(Exception):
    status_code = 429


class TestFailureDetail:
    async def test_http_error_records_status_model_base_url_and_attempt(self) -> None:
        with patch.object(
            disposition, "_call_model", new=AsyncMock(side_effect=_Status401Error())
        ):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.ClassificationFailure)
        assert result.reason == "http_status"
        assert result.status_code == 401
        assert result.model == "glm-5.3:cloud"
        assert result.base_url == "https://ollama.com"
        assert result.attempt == 1
        detail = result.detail
        for fragment in (
            "http_status",
            "status=401",
            "model=glm-5.3:cloud",
            "attempt=",
        ):
            assert fragment in detail
        # Short form for the GTD comment: still names the status.
        assert result.summary == "http_status 401"

    async def test_a_refusal_is_not_retried_but_a_rate_limit_is(self) -> None:
        """A 401 will be a 401 again; a 429 may not be."""
        refusal = AsyncMock(side_effect=_Status401Error())
        with patch.object(disposition, "_call_model", new=refusal):
            await disposition.classify(_evidence())
        assert refusal.await_count == 1

        throttled = AsyncMock(side_effect=_Status429Error())
        with patch.object(disposition, "_call_model", new=throttled):
            await disposition.classify(_evidence())
        assert throttled.await_count == disposition.CLASSIFIER_MAX_ATTEMPTS

    async def test_the_warning_log_carries_the_detail(self, caplog) -> None:
        with (
            caplog.at_level("WARNING"),
            patch.object(
                disposition, "_call_model", new=AsyncMock(side_effect=_Status401Error())
            ),
        ):
            await disposition.classify(_evidence())
        assert "status=401" in caplog.text
        assert "model=glm-5.3:cloud" in caplog.text
        assert "base_url=https://ollama.com" in caplog.text
        assert "attempt=1" in caplog.text
        assert "run-1" in caplog.text

    async def test_the_exception_message_is_never_logged_or_recorded(self) -> None:
        """Exception messages can echo the prompt or the credential back."""
        secret = "sk-do-not-leak-me"  # noqa: S105 — the point of the test
        with patch.object(
            disposition, "_call_model", new=AsyncMock(side_effect=OSError(secret))
        ):
            result = await disposition.classify(_evidence())
        assert isinstance(result, disposition.ClassificationFailure)
        assert secret not in result.detail
        assert result.note == "OSError"

    async def test_resolve_carries_the_detail_onto_the_derived_result(self) -> None:
        with patch.object(
            disposition, "_call_model", new=AsyncMock(side_effect=_Status401Error())
        ):
            result = await disposition.resolve(
                _evidence(), has_commits=True, gate_decision="passed"
            )
        assert result.provenance == "derived"
        assert result.classifier_failure_class == "http_status"
        assert result.classifier_status_code == 401
        assert result.model == "glm-5.3:cloud"
        assert "status=401" in (result.classifier_failure or "")

    def test_the_provenance_sentence_is_short_but_not_opaque(self) -> None:
        sentence = disposition.provenance_sentence(
            disposition.DispositionResult(
                disposition="done",
                reason="r",
                provenance="derived",
                model="glm-5.3:cloud",
                classifier_failure="http_status status=401 model=glm-5.3:cloud",
                classifier_failure_class="http_status",
                classifier_status_code=401,
            )
        )
        assert "http_status 401" in sentence
        assert "glm-5.3:cloud" in sentence
        # ...but not the whole diagnostic line, which lives in the blob.
        assert "base_url" not in sentence

    def test_detail_omits_fields_that_are_absent(self) -> None:
        bare = disposition.ClassificationFailure(reason="timeout")
        assert bare.detail == "timeout"
        assert bare.summary == "timeout"

    def test_every_class_used_is_a_declared_class(self) -> None:
        for cls in (
            "disabled",
            "no_credential",
            "timeout",
            "http_status",
            "transport",
            "parse",
        ):
            assert cls in disposition.FAILURE_CLASSES


# ---------------------------------------------------------------------------
# First-use reachability probe: a broken classifier must announce itself
# ---------------------------------------------------------------------------


class TestReachabilityProbe:
    @pytest.fixture(autouse=True)
    def _fresh(self):
        disposition._reset_reachability_probe()
        yield
        disposition._reset_reachability_probe()

    async def test_a_working_endpoint_logs_no_warning(self, caplog) -> None:
        with (
            caplog.at_level("WARNING"),
            patch.object(disposition, "_client") as client,
        ):
            client.return_value.messages.create = AsyncMock(return_value=object())
            failure = await disposition.warn_if_unreachable_once()
        assert failure is None
        assert "UNREACHABLE" not in caplog.text

    async def test_a_broken_pairing_warns_once_with_the_status(self, caplog) -> None:
        with (
            caplog.at_level("WARNING"),
            patch.object(disposition, "_client") as client,
        ):
            client.return_value.messages.create = AsyncMock(
                side_effect=_Status401Error()
            )
            first = await disposition.warn_if_unreachable_once()
            second = await disposition.warn_if_unreachable_once()
        assert first is not None
        assert first.status_code == 401
        assert second is None  # one-shot
        assert caplog.text.count("UNREACHABLE") == 1
        assert "status=401" in caplog.text
        assert "model=glm-5.3:cloud" in caplog.text
        # Ambiguity named explicitly: 401 here means bad key OR unserved model.
        assert "401" in caplog.text
        assert "OLLAMA_CLOUD_API_KEY" in caplog.text

    async def test_the_probe_is_never_fatal(self) -> None:
        """A dead provider must not raise into startup."""
        with patch.object(disposition, "_client") as client:
            client.return_value.messages.create = AsyncMock(
                side_effect=RuntimeError("very dead")
            )
            failure = await disposition.warn_if_unreachable_once()
        assert failure is not None
        assert failure.reason == "transport"

    async def test_disabled_makes_no_call(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(config, "DISPOSITION_CLASSIFIER_ENABLED", False)
        with (
            caplog.at_level("WARNING"),
            patch.object(disposition, "_client") as client,
        ):
            failure = await disposition.warn_if_unreachable_once()
        client.assert_not_called()
        assert failure is not None
        assert failure.reason == "disabled"
        assert "UNREACHABLE" not in caplog.text

    async def test_a_missing_credential_is_reported_not_called(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "")
        with patch.object(disposition, "_client") as client:
            failure = await disposition.warn_if_unreachable_once()
        client.assert_not_called()
        assert failure is not None
        assert failure.reason == "no_credential"
