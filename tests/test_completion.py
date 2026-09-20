"""Tests for completion.py — result-envelope parsing.

The agent-authored completion artifact this module used to also parse is gone; its
tests went with it. A run's status is built only from evidence the worker observes.
"""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import completion, config
from agent_gtd_dispatch.completion import (
    ResultEnvelope,
    envelope_verdict,
    parse_result_envelope,
)


@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "DISPATCH_AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


# ---------------------------------------------------------------------------
# parse_result_envelope
# ---------------------------------------------------------------------------


class TestParseResultEnvelope:
    def test_clean_envelope(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 7,
                    "session_id": "s-9",
                    "total_cost_usd": 1.25,
                }
            )
        )
        env = parse_result_envelope(path)
        assert env is not None
        assert env.subtype == "success"
        assert env.num_turns == 7
        assert env.session_id == "s-9"
        assert env.total_cost_usd == 1.25

    def test_envelope_after_stderr_noise(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text(
            "warning: something on stderr\n"
            "Traceback (most recent call last): { not json\n"
            + json.dumps({"type": "result", "subtype": "success"})
            + "\n"
        )
        env = parse_result_envelope(path)
        assert env is not None
        assert env.subtype == "success"

    def test_last_envelope_wins(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text(
            json.dumps({"type": "result", "subtype": "success", "session_id": "first"})
            + "\n"
            + json.dumps({"type": "result", "subtype": "success", "session_id": "last"})
            + "\n"
        )
        env = parse_result_envelope(path)
        assert env is not None
        assert env.session_id == "last"

    def test_non_result_objects_ignored(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text(
            json.dumps({"type": "assistant", "message": "hi"})
            + "\n"
            + json.dumps({"type": "system", "subtype": "init"})
            + "\n"
        )
        assert parse_result_envelope(path) is None

    def test_truncated_object_returns_none(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text('{"type": "result", "subtype": "suc')
        assert parse_result_envelope(path) is None

    def test_empty_file_returns_none(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text("")
        assert parse_result_envelope(path) is None

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert parse_result_envelope(tmp_path / "nope.txt") is None

    def test_only_tail_is_read(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        head = json.dumps({"type": "result", "subtype": "success", "session_id": "old"})
        filler = "x" * (completion.MAX_TRANSCRIPT_TAIL_BYTES + 1024)
        tail = json.dumps({"type": "result", "subtype": "success", "session_id": "new"})
        path.write_text(head + "\n" + filler + "\n" + tail)
        env = parse_result_envelope(path)
        assert env is not None
        assert env.session_id == "new"

    def test_braces_inside_strings_do_not_confuse_the_scanner(self, tmp_path) -> None:
        path = tmp_path / "transcript.txt"
        path.write_text(
            json.dumps(
                {"type": "result", "subtype": "success", "result": 'a } and a \\" {'}
            )
        )
        env = parse_result_envelope(path)
        assert env is not None
        assert env.result == 'a } and a \\" {'


# ---------------------------------------------------------------------------
# envelope_verdict
# ---------------------------------------------------------------------------


class TestEnvelopeVerdict:
    def test_none_is_no_result_envelope(self) -> None:
        assert envelope_verdict(None) == "no_result_envelope"

    def test_non_result_type_is_no_result_envelope(self) -> None:
        # Defensive branch — the parser already filters on type.
        assert envelope_verdict(ResultEnvelope(type="error")) == "no_result_envelope"

    def test_observed_max_turns_envelope(self) -> None:
        env = ResultEnvelope(
            type="result",
            subtype="error_max_turns",
            is_error=True,
            stop_reason="tool_use",
            terminal_reason="max_turns",
        )
        assert envelope_verdict(env) == "max_turns_exhausted"

    def test_terminal_reason_alone_is_max_turns(self) -> None:
        env = ResultEnvelope(
            type="result", subtype="success", terminal_reason="max_turns"
        )
        assert envelope_verdict(env) == "max_turns_exhausted"

    def test_is_error_is_result_is_error(self) -> None:
        env = ResultEnvelope(type="result", subtype="success", is_error=True)
        assert envelope_verdict(env) == "result_is_error"

    def test_success_is_ok(self) -> None:
        env = ResultEnvelope(type="result", subtype="success", is_error=False)
        assert envelope_verdict(env) == "ok"

    def test_unrecognized_subtype_warns_and_is_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = ResultEnvelope(type="result", subtype="error_during_execution")
        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.completion"):
            assert envelope_verdict(env) == "result_is_error"
        assert "unrecognized result subtype" in caplog.text

    def test_success_subtype_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = ResultEnvelope(type="result", subtype="success")
        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.completion"):
            assert envelope_verdict(env) == "ok"
        assert "unrecognized result subtype" not in caplog.text

    def test_exit_zero_with_empty_transcript_is_still_no_envelope(
        self, tmp_path
    ) -> None:
        # A subprocess returncode of 0 NEVER excuses a missing envelope.
        path = tmp_path / "transcript.txt"
        path.write_text("")
        assert envelope_verdict(parse_result_envelope(path)) == "no_result_envelope"

    def test_extra_keys_are_preserved(self) -> None:
        env = ResultEnvelope.model_validate(
            {"type": "result", "subtype": "success", "brand_new_field": 3}
        )
        assert env.model_dump()["brand_new_field"] == 3
