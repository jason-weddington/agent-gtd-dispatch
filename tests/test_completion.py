"""Tests for completion.py — result-envelope parsing and the completion artifact."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import completion, config
from agent_gtd_dispatch.completion import (
    KNOWN_SCHEMA_VERSIONS,
    ResultEnvelope,
    envelope_verdict,
    parse_result_envelope,
    read_completion_artifact,
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


# ---------------------------------------------------------------------------
# read_completion_artifact
# ---------------------------------------------------------------------------


def _write_artifact(workspace, payload, *, subdir=None) -> None:
    base = workspace if subdir is None else workspace / subdir
    target = base / ".dispatch"
    target.mkdir(parents=True, exist_ok=True)
    (target / "completion.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload)
    )


class TestReadCompletionArtifact:
    def test_absent(self, tmp_path) -> None:
        assert read_completion_artifact(tmp_path) == (None, "absent")

    def test_oversize(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"disposition": "done", "summary": "x" * 70000})
        artifact, reason = read_completion_artifact(tmp_path)
        assert artifact is None
        assert reason == "oversize"

    def test_not_json(self, tmp_path) -> None:
        _write_artifact(tmp_path, "{not json")
        assert read_completion_artifact(tmp_path) == (None, "not_json")

    def test_not_object(self, tmp_path) -> None:
        _write_artifact(tmp_path, "[1, 2, 3]")
        assert read_completion_artifact(tmp_path) == (None, "not_object")

    def test_unknown_schema_version(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"schema_version": 99, "disposition": "done"})
        assert read_completion_artifact(tmp_path) == (None, "unknown_schema_version")

    def test_missing_schema_version_defaults_to_one(self, tmp_path) -> None:
        assert 1 in KNOWN_SCHEMA_VERSIONS
        _write_artifact(tmp_path, {"disposition": "done"})
        artifact, reason = read_completion_artifact(tmp_path)
        assert reason == "ok"
        assert artifact is not None
        assert artifact.schema_version == 1

    def test_unknown_disposition(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"disposition": "vibes"})
        assert read_completion_artifact(tmp_path) == (None, "unknown_disposition")

    def test_missing_disposition_is_unknown_disposition(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"summary": "no disposition here"})
        assert read_completion_artifact(tmp_path) == (None, "unknown_disposition")

    @pytest.mark.parametrize("reason_value", [None, "", "   "])
    def test_missing_reason(self, tmp_path, reason_value) -> None:
        payload = {"disposition": "already_satisfied"}
        if reason_value is not None:
            payload["reason"] = reason_value
        _write_artifact(tmp_path, payload)
        assert read_completion_artifact(tmp_path) == (None, "missing_reason")

    @pytest.mark.parametrize("decision_value", [None, "", "   "])
    def test_missing_decision_needed(self, tmp_path, decision_value) -> None:
        payload = {"disposition": "blocked"}
        if decision_value is not None:
            payload["decision_needed"] = decision_value
        _write_artifact(tmp_path, payload)
        assert read_completion_artifact(tmp_path) == (
            None,
            "missing_decision_needed",
        )

    def test_ambiguous_location(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"disposition": "done"}, subdir="repo_a")
        _write_artifact(tmp_path, {"disposition": "done"}, subdir="repo_b")
        assert read_completion_artifact(tmp_path) == (None, "ambiguous_location")

    def test_misplaced_artifact_in_single_repo_subdir_is_found(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"disposition": "done"}, subdir="repo_a")
        artifact, reason = read_completion_artifact(tmp_path)
        assert reason == "ok"
        assert artifact is not None
        assert artifact.disposition == "done"

    @pytest.mark.parametrize(
        "payload",
        [
            {"disposition": "done"},
            {"disposition": "already_satisfied", "reason": "already there"},
            {"disposition": "blocked", "decision_needed": "which API?"},
            {"disposition": "failed"},
        ],
    )
    def test_valid_dispositions(self, tmp_path, payload) -> None:
        _write_artifact(tmp_path, payload)
        artifact, reason = read_completion_artifact(tmp_path)
        assert reason == "ok"
        assert artifact is not None
        assert artifact.disposition == payload["disposition"]

    def test_extra_keys_ignored(self, tmp_path) -> None:
        _write_artifact(
            tmp_path, {"disposition": "done", "invented_by_the_agent": True}
        )
        artifact, reason = read_completion_artifact(tmp_path)
        assert reason == "ok"
        assert artifact is not None
        assert not hasattr(artifact, "invented_by_the_agent")

    def test_missing_summary_defaults_to_empty(self, tmp_path) -> None:
        _write_artifact(tmp_path, {"disposition": "done"})
        artifact, reason = read_completion_artifact(tmp_path)
        assert reason == "ok"
        assert artifact is not None
        assert artifact.summary == ""

    def test_rejection_is_logged_with_raw_head(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_artifact(tmp_path, "{not json")
        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.completion"):
            completion.log_artifact_rejection("run-1", tmp_path, "not_json")
        assert "completion artifact rejected" in caplog.text
        assert "not_json" in caplog.text
        assert "not json" in caplog.text

    def test_artifact_state_mapping(self) -> None:
        artifact, _ = None, None
        assert completion.artifact_state(artifact, "absent") == "absent"
        assert completion.artifact_state(artifact, "ambiguous_location") == "absent"
        assert completion.artifact_state(artifact, "not_json") == "malformed"


# ---------------------------------------------------------------------------
# cross-user reads
# ---------------------------------------------------------------------------


class TestArtifactReadArgv:
    def test_sudo_prefix_when_agent_user_set(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        argv = completion.artifact_read_argv(tmp_path / "completion.json")
        assert argv[:4] == ["sudo", "-u", "dispatch", "-H"]

    def test_no_sudo_prefix_when_unset(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        argv = completion.artifact_read_argv(tmp_path / "completion.json")
        assert argv[0] != "sudo"
