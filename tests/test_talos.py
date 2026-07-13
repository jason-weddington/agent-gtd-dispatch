"""Tests for the talos execution path.

Covers the pure-function surface (serialize_task_spec, talos_env_overlay,
build_talos_argv, map_talos_result, parse_disposition_summary) and the
main.py wiring that consumes them (engine registration + gating, dispatch-time
validations, plan-mode swap, worker branch behaviour).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "item_response.json"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "OLLAMA_BASE_URL": "http://ollama.local:11434",
        "OLLAMA_API_KEY": "local-ollama-key",
        "OLLAMA_CLOUD_API_KEY": "ollama-cloud-key",
    }
    with patch.dict(os.environ, env):
        from agent_gtd_dispatch import config

        config.load()
        yield


@pytest.fixture
def item_fixture() -> dict:
    """Load the committed ItemResponse fixture; strip the `_comment` field."""
    data = json.loads(FIXTURE_PATH.read_text())
    data.pop("_comment", None)
    return data


@pytest.fixture
def project_fixture() -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "agent-gtd-dev",
        "repo_mode": "monorepo",
        "git_origin": "git@ubuntu-vm01:repos/agent_gtd",
        "workspace_repos": [],
        "gate_command": "uv run pytest && uv run ruff check",
    }


# ---------------------------------------------------------------------------
# ENGINE ROSTER + NAME RESOLUTION INVARIANT
# ---------------------------------------------------------------------------


class TestTalosEngineRoster:
    def test_all_five_talos_engines_registered(self) -> None:
        from agent_gtd_dispatch.engines import (
            TALOS_ENGINES,
            get_engine,
        )

        expected = {
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
            "talos-qwen",
            "talos-glm",
        }
        assert frozenset(expected) == TALOS_ENGINES
        for name in expected:
            eng = get_engine(name)  # must not raise
            assert eng.name == name
            assert eng.binary == "talos"

    def test_talos_anthropic_engines_have_anthropic_auth_env(self) -> None:
        from agent_gtd_dispatch.engines import get_engine

        for name in ("talos-haiku", "talos-sonnet", "talos-opus"):
            assert get_engine(name).auth_env_key == "ANTHROPIC_API_KEY"

    def test_talos_ollama_engines_have_empty_auth_env(self) -> None:
        from agent_gtd_dispatch.engines import get_engine

        for name in ("talos-qwen", "talos-glm"):
            assert get_engine(name).auth_env_key == ""

    def test_talos_build_command_raises_not_implemented(self) -> None:
        from agent_gtd_dispatch.engines import get_engine

        eng = get_engine("talos-haiku")
        with pytest.raises(NotImplementedError, match="talos"):
            eng.build_command("prompt", "title", 10, None)

    def test_get_engine_unknown_talos_still_raises_value_error(self) -> None:
        from agent_gtd_dispatch.engines import get_engine

        with pytest.raises(ValueError):
            get_engine("talos-bogus")

    def test_is_talos_engine_discriminates(self) -> None:
        from agent_gtd_dispatch.engines import is_talos_engine

        assert is_talos_engine("talos-haiku")
        assert is_talos_engine("talos-glm")
        assert not is_talos_engine("claude-code")
        assert not is_talos_engine("claude-code-ollama")
        assert not is_talos_engine("")


# ---------------------------------------------------------------------------
# ENV OVERLAY — pinned literal dicts per engine
# ---------------------------------------------------------------------------


class TestTalosEnvOverlay:
    def test_haiku_overlay_literal(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        assert talos_env_overlay("talos-haiku") == {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-haiku-4-5",
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }

    def test_sonnet_overlay_literal(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        assert talos_env_overlay("talos-sonnet") == {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }

    def test_opus_overlay_literal(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        assert talos_env_overlay("talos-opus") == {
            "TALOS_BACKEND": "anthropic",
            "ANTHROPIC_MODEL": "claude-opus-4-8",
            "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        }

    def test_qwen_overlay_literal_with_think_and_num_ctx(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        assert talos_env_overlay("talos-qwen") == {
            "TALOS_BACKEND": "ollama",
            "OLLAMA_MODEL": "qwen3.6:35b",
            "OLLAMA_THINK": "on",
            "OLLAMA_NUM_CTX": "32768",
            "OLLAMA_BASE_URL": config.OLLAMA_BASE_URL,
            "OLLAMA_API_KEY": config.OLLAMA_API_KEY,
        }

    def test_qwen_num_ctx_pinned_not_config_derived(self) -> None:
        """OLLAMA_NUM_CTX=32768 is a hardcoded literal — pinning it because
        talos only self-defaults num_ctx for localhost URLs (main.rs:295-299).
        """
        from agent_gtd_dispatch.talos import talos_env_overlay

        overlay = talos_env_overlay("talos-qwen")
        assert overlay["OLLAMA_NUM_CTX"] == "32768"
        assert overlay["OLLAMA_THINK"] == "on"

    def test_glm_overlay_literal_with_cloud_url_and_key(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        overlay = talos_env_overlay("talos-glm")
        assert overlay == {
            "TALOS_BACKEND": "ollama",
            "OLLAMA_MODEL": "glm-5.2:cloud",
            "OLLAMA_BASE_URL": "https://ollama.com",
            "OLLAMA_API_KEY": config.OLLAMA_CLOUD_API_KEY,
        }
        # No fallback to OLLAMA_API_KEY
        assert overlay["OLLAMA_API_KEY"] != config.OLLAMA_API_KEY

    def test_anthropic_engines_expose_api_key(self) -> None:
        """ANTHROPIC_API_KEY IS deliberately exposed to talos anthropic engines —
        reversal of the claude-code kb-01512 convention (talos has no Max sub).
        """
        from agent_gtd_dispatch.talos import talos_env_overlay

        for name in ("talos-haiku", "talos-sonnet", "talos-opus"):
            overlay = talos_env_overlay(name)
            assert "ANTHROPIC_API_KEY" in overlay
            assert overlay["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_overlay_never_contains_git_identity_keys(self) -> None:
        from agent_gtd_dispatch.talos import talos_env_overlay

        for name in (
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
            "talos-qwen",
            "talos-glm",
        ):
            overlay = talos_env_overlay(name)
            for key in overlay:
                assert not key.startswith("GIT_"), (
                    f"{name} overlay must not carry git identity key {key!r}"
                )

    def test_glm_uses_cloud_key_distinct_from_local(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import talos_env_overlay

        monkeypatch.setattr(config, "OLLAMA_API_KEY", "local-secret")
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "cloud-secret")
        assert talos_env_overlay("talos-glm")["OLLAMA_API_KEY"] == "cloud-secret"
        assert talos_env_overlay("talos-qwen")["OLLAMA_API_KEY"] == "local-secret"

    def test_env_overlay_keys_are_subset_of_sudoers_env_keep(self) -> None:
        """Flywheel guard: every key talos_env_overlay() sets must survive the
        sudo boundary — i.e. be listed in the sudoers env_keep directive.

        Both sets are derived from source-of-truth code/templates, never
        hardcoded, so a future engine addition or template edit that breaks
        the invariant fails loudly here rather than silently at runtime.
        """
        from agent_gtd_dispatch.engines import TALOS_ENGINES
        from agent_gtd_dispatch.talos import talos_env_overlay

        # Build the overlay key-set by unioning across every registered talos engine.
        overlay_keys: set[str] = set()
        for name in TALOS_ENGINES:
            overlay_keys |= talos_env_overlay(name).keys()

        # Parse env_keep from the sudoers template (single source of truth).
        tmpl = Path(__file__).parent.parent / "templates" / "sudoers-dispatch-svc.tmpl"
        env_keep_line = next(
            line for line in tmpl.read_text().splitlines() if "env_keep +=" in line
        )
        quoted = env_keep_line[env_keep_line.index('"') + 1 : env_keep_line.rindex('"')]
        env_keep_keys: set[str] = set(quoted.split())

        # Guard against a vacuous pass from parse or overlay failure.
        assert env_keep_keys, "env_keep_keys is empty — sudoers template parse failed"
        assert overlay_keys, (
            "overlay_keys is empty — talos_env_overlay returned nothing"
        )
        # Single-key anchor: every talos engine sets TALOS_BACKEND, so its absence
        # means the overlay parse resolved nothing real.
        assert "TALOS_BACKEND" in overlay_keys, (
            "TALOS_BACKEND not in overlay_keys — talos_env_overlay returned unexpected keys"
        )

        # The real invariant: every key the overlay sets must be in env_keep.
        missing = overlay_keys - env_keep_keys
        assert overlay_keys <= env_keep_keys, (
            "talos_env_overlay sets keys not preserved across the sudo boundary — "
            f"add to env_keep in sudoers-dispatch-svc.tmpl: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# CLOUD-KEY CONFIG KNOB — no fallback
# ---------------------------------------------------------------------------


class TestOllamaCloudApiKeyConfig:
    def test_load_reads_cloud_key_from_env(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_CLOUD_API_KEY": "ollama-cloud-XYZ",
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.OLLAMA_CLOUD_API_KEY == "ollama-cloud-XYZ"

    def test_load_no_fallback_to_local_key(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "OLLAMA_API_KEY": "local-only",
            # OLLAMA_CLOUD_API_KEY intentionally unset
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.OLLAMA_API_KEY == "local-only"
            assert config.OLLAMA_CLOUD_API_KEY == ""  # NO fallback

    def test_talos_bin_default_is_talos(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.TALOS_BIN == "talos"

    def test_talos_bin_override(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "TALOS_BIN": "/opt/talos/bin/talos",
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.TALOS_BIN == "/opt/talos/bin/talos"

    def test_talos_gate_timeout_secs_default_is_900(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.TALOS_GATE_TIMEOUT_SECS == 900

    def test_talos_gate_timeout_secs_override(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://x",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "a",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
            "TALOS_GATE_TIMEOUT_SECS": "600",
        }
        with patch.dict(os.environ, env, clear=True):
            from agent_gtd_dispatch import config

            config.load()
            assert config.TALOS_GATE_TIMEOUT_SECS == 600


# ---------------------------------------------------------------------------
# CAPABILITIES ADVERTISEMENT + is_engine_available GATING
# ---------------------------------------------------------------------------


class TestTalosAvailabilityGating:
    def test_all_available_with_all_prereqs_set(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_available_engine_names

        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://x:11434")
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "cloud")
        names = get_available_engine_names()
        for talos in (
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
            "talos-qwen",
            "talos-glm",
        ):
            assert talos in names

    def test_talos_anthropic_absent_without_api_key(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_available_engine_names

        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
        names = get_available_engine_names()
        for talos in ("talos-haiku", "talos-sonnet", "talos-opus"):
            assert talos not in names

    def test_talos_glm_absent_without_cloud_key(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_available_engine_names

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
        assert "talos-glm" not in get_available_engine_names()

    def test_talos_qwen_absent_without_base_url(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_available_engine_names

        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
        assert "talos-qwen" not in get_available_engine_names()


# ---------------------------------------------------------------------------
# TASKSPEC SERIALIZATION — GTD-verbatim projection
# ---------------------------------------------------------------------------


class TestTaskSpecSerialization:
    def test_projects_five_keys_from_fixture(
        self, item_fixture, project_fixture
    ) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert set(spec.keys()) == {
            "title",
            "description",
            "acceptance_criteria",
            "files_to_modify",
            "gate_command",
        }
        assert spec["title"] == item_fixture["title"]
        assert spec["description"] == item_fixture["description"]
        assert spec["acceptance_criteria"] == item_fixture["acceptance_criteria"]
        assert spec["files_to_modify"] == item_fixture["files_to_modify"]
        assert spec["gate_command"] == project_fixture["gate_command"]

    def test_extra_item_keys_not_copied(self, item_fixture, project_fixture) -> None:
        """id/status/labels/version/blockers must NOT appear in the TaskSpec."""
        from agent_gtd_dispatch.talos import serialize_task_spec

        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        for forbidden in (
            "id",
            "status",
            "labels",
            "version",
            "blockers",
            "priority",
            "assigned_to",
            "created_by",
            "project_id",
        ):
            assert forbidden not in spec, (
                f"{forbidden!r} leaked into TaskSpec — projection must be 5 keys"
            )

    def test_null_description_maps_to_empty_string(
        self, item_fixture, project_fixture
    ) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture["description"] = None
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["description"] == ""

    def test_absent_description_maps_to_empty_string(
        self, item_fixture, project_fixture
    ) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture.pop("description", None)
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["description"] == ""

    # --- Fails-if-unread per consumed field ---

    def test_fails_if_title_dropped(self, item_fixture, project_fixture) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture["title"] = "MUTATED-TITLE"
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["title"] == "MUTATED-TITLE"

    def test_fails_if_description_dropped(self, item_fixture, project_fixture) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture["description"] = "MUTATED-DESC"
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["description"] == "MUTATED-DESC"

    def test_fails_if_acceptance_criteria_dropped(
        self, item_fixture, project_fixture
    ) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture["acceptance_criteria"] = ["MUTATED-AC-1", "MUTATED-AC-2"]
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["acceptance_criteria"] == ["MUTATED-AC-1", "MUTATED-AC-2"]

    def test_fails_if_files_to_modify_dropped(
        self, item_fixture, project_fixture
    ) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        item_fixture["files_to_modify"][0]["change"] = "MUTATED-CHANGE"
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["files_to_modify"][0]["change"] == "MUTATED-CHANGE"

    def test_fails_if_gate_command_dropped(self, item_fixture, project_fixture) -> None:
        from agent_gtd_dispatch.talos import serialize_task_spec

        project_fixture["gate_command"] = "MUTATED-GATE"
        spec = json.loads(serialize_task_spec(item_fixture, project_fixture))
        assert spec["gate_command"] == "MUTATED-GATE"


# ---------------------------------------------------------------------------
# ARGV BUILDER + SUDO WRAP
# ---------------------------------------------------------------------------


class TestBuildTalosArgv:
    def test_argv_shape_no_sudo(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import build_talos_argv

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        argv = build_talos_argv(Path("/workspace/work"), "task-123", attempt=1)
        assert argv == [
            "talos",
            "run",
            "--workspace",
            "/workspace/work",
            "--task-id",
            "task-123",
            "--attempt",
            "1",
            "--gate-timeout-secs",
            "900",
        ]

    def test_argv_sudo_wrapped_when_user_set(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import build_talos_argv

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        argv = build_talos_argv(Path("/workspace/work"), "task-1", attempt=2)
        assert argv[:4] == ["sudo", "-u", "dispatch", "-H"]
        assert "talos" in argv
        assert "--attempt" in argv and "2" in argv

    def test_argv_no_max_iterations_but_has_gate_timeout(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import build_talos_argv

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        argv = build_talos_argv(Path("/workspace/work"), "task-1", attempt=1)
        assert "--max-iterations" not in argv
        assert "--gate-timeout-secs" in argv
        assert "--file" not in argv  # spec is on stdin, never a --file path
        assert "--run-store" not in argv
        assert "--offload-dir" not in argv

    def test_argv_uses_config_talos_bin_override(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import build_talos_argv

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TALOS_BIN", "/opt/talos/bin/talos")
        argv = build_talos_argv(Path("/w"), "t", attempt=1)
        assert argv[0] == "/opt/talos/bin/talos"

    def test_argv_gate_timeout_secs_override(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.talos import build_talos_argv

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TALOS_GATE_TIMEOUT_SECS", 600)
        argv = build_talos_argv(Path("/workspace/work"), "task-1", attempt=1)
        i = argv.index("--gate-timeout-secs")
        assert argv[i + 1] == "600"


# ---------------------------------------------------------------------------
# EXIT-CODE MAPPER — all four codes, 1-vs-20 never conflated
# ---------------------------------------------------------------------------


_SAMPLE_RUN_SUMMARY = '{"outcome":"Finished","disposition":{"Done":{"summary":"ok","verification":"NoChecksConfigured"}},"iterations":3}'
_BLOCKED_SUMMARY = '{"outcome":"Finished","disposition":{"Blocked":{"decision_needed":"which lib?"}},"iterations":2}'
_FAILED_SUMMARY = '{"outcome":"Finished","disposition":{"Failed":{"mode":"Loop","summary":"gate never green"}},"iterations":5}'
_BACKEND_ERROR_SUMMARY = '{"outcome":"BackendError","disposition":{"Failed":{"mode":"TransientInfra","summary":"llm 500"}},"iterations":1}'
_PRE_RUN_ERROR = '{"error":"cannot open workspace"}'


class TestMapTalosResult:
    def test_exit_0_verified_done_pushes(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        status, push, text = map_talos_result(0, _SAMPLE_RUN_SUMMARY, "")
        assert status == RunStatus.succeeded
        assert push is True
        assert "Done" in text or "verified" in text.lower()

    def test_exit_10_blocked_no_push(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        status, push, text = map_talos_result(10, _BLOCKED_SUMMARY, "")
        assert status == RunStatus.failed
        assert push is False
        assert "block" in text.lower()

    def test_exit_20_task_failed_no_push(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        status, push, text = map_talos_result(20, _FAILED_SUMMARY, "")
        assert status == RunStatus.failed
        assert push is False
        assert "fail" in text.lower()

    def test_exit_1_pre_run_infra_error_no_push(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        status, push, text = map_talos_result(1, "", _PRE_RUN_ERROR)
        assert status == RunStatus.failed
        assert push is False
        # Distinct wording from exit-20 — must include "engine error"
        assert "engine error" in text.lower()

    def test_exit_1_backend_error_no_push(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        status, push, text = map_talos_result(1, _BACKEND_ERROR_SUMMARY, "")
        assert status == RunStatus.failed
        assert push is False
        assert "engine error" in text.lower()

    def test_malformed_exit_0_treated_as_engine_broke(self) -> None:
        from agent_gtd_dispatch.models import RunStatus
        from agent_gtd_dispatch.talos import map_talos_result

        # Empty stdout
        status, push, _ = map_talos_result(0, "", "")
        assert status == RunStatus.failed
        assert push is False
        # Unparseable
        status, push, _ = map_talos_result(0, "not json", "")
        assert status == RunStatus.failed
        assert push is False

    def test_exit_1_and_exit_20_yield_distinct_comment_text(self) -> None:
        from agent_gtd_dispatch.talos import map_talos_result

        _s1, _p1, t1 = map_talos_result(1, "", _PRE_RUN_ERROR)
        _s20, _p20, t20 = map_talos_result(20, _FAILED_SUMMARY, "")
        assert t1 != t20
        assert "engine error" in t1.lower()
        assert "engine error" not in t20.lower()

    def test_only_exit_0_yields_push_true(self) -> None:
        from agent_gtd_dispatch.talos import map_talos_result

        assert map_talos_result(0, _SAMPLE_RUN_SUMMARY, "")[1] is True
        for code in (10, 20, 1):
            summary = _SAMPLE_RUN_SUMMARY if code != 1 else ""
            assert map_talos_result(code, summary, "err")[1] is False


# ---------------------------------------------------------------------------
# DISPOSITION JSON PARSING — externally-tagged
# ---------------------------------------------------------------------------


class TestParseDispositionSummary:
    def test_done_with_checks_verification(self) -> None:
        from agent_gtd_dispatch.talos import parse_disposition_summary

        disposition = {
            "Done": {
                "summary": "landed the feature",
                "verification": {"Checks": {"status": "green", "duration_ms": 1234}},
            }
        }
        out = parse_disposition_summary(disposition)
        assert "landed the feature" in out
        assert "green" in out
        assert "Checks" in out

    def test_done_with_no_checks_configured(self) -> None:
        from agent_gtd_dispatch.talos import parse_disposition_summary

        disposition = {
            "Done": {
                "summary": "trusting the gate",
                "verification": "NoChecksConfigured",
            }
        }
        out = parse_disposition_summary(disposition)
        assert "trusting the gate" in out
        assert "NoChecksConfigured" in out

    def test_blocked_decision_needed(self) -> None:
        from agent_gtd_dispatch.talos import parse_disposition_summary

        disposition = {"Blocked": {"decision_needed": "which auth library?"}}
        assert "which auth library?" in parse_disposition_summary(disposition)

    def test_failed_with_mode(self) -> None:
        from agent_gtd_dispatch.talos import parse_disposition_summary

        disposition = {"Failed": {"mode": "Loop", "summary": "gate never went green"}}
        out = parse_disposition_summary(disposition)
        assert "Loop" in out
        assert "gate never went green" in out


# ---------------------------------------------------------------------------
# DISPATCH-TIME VALIDATIONS + PLAN/MANAGE SWAP
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from agent_gtd_dispatch.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key"}


def _mk_project(
    *,
    repo_mode: str = "monorepo",
    workspace_repos: list[str] | None = None,
    gate_command: str | None = "uv run pytest",
) -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "agent-gtd-dev",
        "repo_mode": repo_mode,
        "workspace_repos": workspace_repos or [],
        "git_origin": "git@ubuntu-vm01:repos/agent_gtd",
        "gate_command": gate_command,
    }


def _mk_item(project_id: str = "22222222-2222-2222-2222-222222222222") -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "Do the thing",
        "description": "",
        "project_id": project_id,
        "acceptance_criteria": ["AC-1"],
        "files_to_modify": [{"path": "x.py", "change": "y"}],
    }


class TestDispatchWorkspaceRejection:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_workspace_mode_talos_proceeds_to_insert_run(
        self, mock_client, client, auth_headers
    ) -> None:
        """BUILD-mode talos dispatch against a workspace project no longer 400s.

        The workspace-mode 400 guard was removed — workspace talos projects with
        non-empty ``workspace_repos`` and a non-empty ``gate_command`` proceed
        past the talos guard to ``db.insert_run`` and return 200.
        """
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(
                repo_mode="workspace",
                workspace_repos=[
                    "git@x:repo-a",
                    "git@x:repo-b",
                ],
                gate_command="uv run pytest",
            )
        )
        with (
            patch("agent_gtd_dispatch.db.insert_run", new_callable=AsyncMock),
            patch("agent_gtd_dispatch.main.asyncio.create_task"),
        ):
            resp = client.post(
                "/dispatch",
                json={
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "engine": "talos-haiku",
                    "max_turns": 50,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_non_talos_engine_workspace_still_ok(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(
                repo_mode="workspace",
                workspace_repos=["git@x:repo-a"],
                gate_command=None,
            )
        )
        with (
            patch("agent_gtd_dispatch.db.insert_run", new_callable=AsyncMock),
            patch("agent_gtd_dispatch.main.asyncio.create_task"),
        ):
            resp = client.post(
                "/dispatch",
                json={
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "engine": "claude-code",
                    "max_turns": 50,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200


class TestDispatchGateCommandRequired:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_missing_gate_command_returns_400(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(repo_mode="monorepo", gate_command=None)
        )
        resp = client.post(
            "/dispatch",
            json={
                "item_id": "11111111-1111-1111-1111-111111111111",
                "engine": "talos-haiku",
                "max_turns": 50,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "gate_command" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_whitespace_gate_command_returns_400(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(repo_mode="monorepo", gate_command="   ")
        )
        resp = client.post(
            "/dispatch",
            json={
                "item_id": "11111111-1111-1111-1111-111111111111",
                "engine": "talos-haiku",
                "max_turns": 50,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "gate_command" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_non_talos_with_empty_gate_command_ok(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(repo_mode="monorepo", gate_command=None)
        )
        with (
            patch("agent_gtd_dispatch.db.insert_run", new_callable=AsyncMock),
            patch("agent_gtd_dispatch.main.asyncio.create_task"),
        ):
            resp = client.post(
                "/dispatch",
                json={
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "engine": "claude-code",
                    "max_turns": 50,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200


class TestPlanModeTalosSwap:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_plan_mode_talos_swaps_to_claude_code(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(gate_command="uv run pytest")
        )
        with (
            patch("agent_gtd_dispatch.db.insert_run", new_callable=AsyncMock),
            patch("agent_gtd_dispatch.main.asyncio.create_task"),
        ):
            resp = client.post(
                "/dispatch",
                json={
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "engine": "talos-haiku",
                    "mode": "plan",
                    "max_turns": 50,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "talos-haiku"
        assert data["engine_actual"] == "claude-code"
        assert data["engine_swap"] is not None
        assert "talos" in data["engine_swap"]["reason"]

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_build_mode_talos_engine_actual_records_talos(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(return_value=_mk_item())
        mock_client.get_project = AsyncMock(
            return_value=_mk_project(gate_command="uv run pytest")
        )
        with (
            patch("agent_gtd_dispatch.db.insert_run", new_callable=AsyncMock),
            patch("agent_gtd_dispatch.main.asyncio.create_task"),
        ):
            resp = client.post(
                "/dispatch",
                json={
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "engine": "talos-haiku",
                    "max_turns": 50,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["engine_actual"] == "talos-haiku"


# ---------------------------------------------------------------------------
# gtd_client.set_item_status — unversioned PATCH, tolerant of failure
# ---------------------------------------------------------------------------


class TestSetItemStatus:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_patches_status_without_version(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import set_item_status

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = b"{}"
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()
        mock_client.request.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_client

        await set_item_status("item-xyz", "review")

        called = mock_client.request.call_args
        assert called.args[0] == "PATCH"
        assert "/items/item-xyz" in called.args[1]
        body = called.kwargs["json"]
        assert body == {"status": "review"}
        assert "version" not in body


# ---------------------------------------------------------------------------
# build_comment_body assembly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _run_talos worker branch — subprocess + git + status set + comment-back
# ---------------------------------------------------------------------------


def _make_talos_run() -> tuple:
    """Return (run, engine, item, project) fixtures for _run_talos."""
    from agent_gtd_dispatch.engines import get_engine
    from agent_gtd_dispatch.models import Run

    engine = get_engine("talos-haiku")
    run = Run(
        item_id="item-abc",
        project_name="agent-gtd-dev",
        branch_name="feat/x-do-thing",
        engine="talos-haiku",
        engine_actual="talos-haiku",
    )
    item = {
        "id": "item-abc",
        "title": "Do the thing",
        "description": "",
        "acceptance_criteria": ["AC-1"],
        "files_to_modify": [{"path": "x.py", "change": "y"}],
    }
    project = {
        "name": "agent-gtd-dev",
        "gate_command": "uv run pytest",
    }
    return run, engine, item, project


class TestRunTalosWorkerBranch:
    async def test_timeout_marks_run_timed_out_and_posts_comment(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        run, engine, item, project = _make_talos_run()

        insert_run_mock = AsyncMock()
        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_run", insert_run_mock)
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="talos", timeout=1
        )
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        with patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=1,
                attribution=None,
                register_cb=lambda _p: None,
            )

        # Marked timed_out
        call_kwargs = update_run_mock.call_args.kwargs
        assert call_kwargs["status"] == RunStatus.timed_out
        assert post_comment_mock.await_count >= 1
        comment = post_comment_mock.await_args_list[0].args[1]
        assert "timed out" in comment.lower()

    async def test_file_not_found_marks_failed_with_binary_name(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        monkeypatch.setattr(config, "TALOS_BIN", "talos")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)

        def _raise(*_a, **_kw):
            raise FileNotFoundError(2, "no such", "talos")

        with patch("agent_gtd_dispatch.main.subprocess.Popen", side_effect=_raise):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
            )

        call_kwargs = update_run_mock.call_args.kwargs
        assert call_kwargs["status"] == RunStatus.failed
        assert "talos" in call_kwargs["error"]
        # Comment body includes "talos binary not found"
        comment = post_comment_mock.await_args_list[0].args[1]
        assert "talos binary not found" in comment

    async def test_exit_0_commits_pushes_and_sets_review(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        # talos exit 0 with a valid RunSummary line on stdout
        stdout = (
            '{"outcome":"Finished","iterations":3,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        commands: list[list[str]] = []

        def _fake_run(cmd, **_kwargs):
            commands.append(cmd)
            rc = MagicMock()
            rc.returncode = 0
            rc.stderr = b""
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
            )

        # Marked succeeded
        assert any(
            call.kwargs.get("status") == RunStatus.succeeded
            for call in update_run_mock.await_args_list
        )
        # Status set to review
        set_status_mock.assert_awaited_once()
        assert set_status_mock.await_args.args == ("item-abc", "review")

        # Commands issued: git add -A, git -c user.name/email commit -m 'feat: <title>',
        # git push -u origin <branch>. All wrapped with sudo prefix.
        joined = [" ".join(c) for c in commands]
        assert any("git add -A" in j for j in joined), joined
        assert any(
            "git -c user.name=talos-haiku -c user.email=talos-haiku@agent-gtd-dispatch"
            " commit -m feat: Do the thing" in j
            for j in joined
        ), joined
        assert any("git push -u origin feat/x-do-thing" in j for j in joined), joined
        # Sudo wrapping applied
        for cmd in commands:
            assert cmd[0] == "sudo"
            assert cmd[:4] == ["sudo", "-u", "dispatch", "-H"]

    async def test_exit_0_status_set_failure_still_leaves_run_succeeded(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        # Set-item-status raises — must NOT flip run to failed
        set_status_mock = AsyncMock(side_effect=httpx.HTTPError("boom"))
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":1,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        def _fake_run(cmd, **_kw):
            rc = MagicMock()
            rc.returncode = 0
            rc.stderr = b""
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
            )

        # Even though set_item_status raised, the run is succeeded and the
        # comment is still posted (mirrors the ollama-fallback tolerance).
        assert any(
            call.kwargs.get("status") == RunStatus.succeeded
            for call in update_run_mock.await_args_list
        )
        post_comment_mock.assert_awaited()

    async def test_exit_10_no_commit_no_push_no_status_set(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":2,'
            '"disposition":{"Blocked":{"decision_needed":"which lib?"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 10

        run_mock = MagicMock()
        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", run_mock),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
            )

        # No git subprocess.run calls (no commit, no push).
        run_mock.assert_not_called()
        # Status never patched to review.
        set_status_mock.assert_not_awaited()
        # Failed status persisted.
        assert any(
            call.kwargs.get("status") == RunStatus.failed
            for call in update_run_mock.await_args_list
        )
        # Comment includes the blocked decision.
        body = post_comment_mock.await_args.args[1]
        assert "which lib?" in body

    # ------------------------------------------------------------------
    # Workspace (multi-repo) branch — workspace_repo_dirs non-empty.
    # ------------------------------------------------------------------

    async def test_workspace_exit_0_commits_only_changed_repo(
        self, tmp_path, monkeypatch
    ) -> None:
        """Multi-repo exit-0 commits+pushes ONLY the changed repo.

        Two-repo fixture with staged changes only in `agent_gtd`. The unchanged
        `agent-gtd-dispatch` repo is skipped (no commit/push). Run succeeds,
        item status set to review, exactly one comment posted.
        """
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":3,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        # Capture (cmd, cwd) so we can assert per-repo cwd routing.
        calls: list[tuple[list[str], object]] = []

        def _fake_run(cmd, **kwargs):
            cwd = kwargs.get("cwd")
            calls.append((cmd, cwd))
            rc = MagicMock()
            rc.stderr = b""
            # git diff --cached --quiet: rc 0 = no staged changes, rc 1 = staged.
            if "diff" in cmd and "--cached" in cmd and "--quiet" in cmd:
                cwd_s = str(cwd)
                if cwd_s.endswith("agent_gtd"):
                    rc.returncode = 1  # staged changes present
                else:
                    rc.returncode = 0  # no staged changes
            else:
                rc.returncode = 0
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
                workspace_repo_dirs=["agent_gtd", "agent-gtd-dispatch"],
            )

        # Run marked succeeded; status set to review once.
        assert any(
            call.kwargs.get("status") == RunStatus.succeeded
            for call in update_run_mock.await_args_list
        )
        set_status_mock.assert_awaited_once()
        assert set_status_mock.await_args.args == ("item-abc", "review")

        # commit/push issued with cwd ending in agent_gtd
        agent_gtd_cmds = [c for c, cwd in calls if str(cwd).endswith("agent_gtd")]
        agent_gtd_dispatch_cmds = [
            c for c, cwd in calls if str(cwd).endswith("agent-gtd-dispatch")
        ]
        assert any("commit" in " ".join(c) for c in agent_gtd_cmds), calls
        assert any("push" in " ".join(c) for c in agent_gtd_cmds), calls
        # No commit/push issued against the unchanged repo
        joined_dispatch = [" ".join(c) for c in agent_gtd_dispatch_cmds]
        assert not any("commit" in j for j in joined_dispatch), joined_dispatch
        assert not any("push" in j for j in joined_dispatch), joined_dispatch
        # Exactly one comment posted (summary comment).
        assert post_comment_mock.await_count == 1

    async def test_workspace_no_changes_anywhere_demotes_failed(
        self, tmp_path, monkeypatch
    ) -> None:
        """Exit-0 with no staged changes in any repo → run failed, no review.

        `git diff --cached --quiet` returns 0 for BOTH repos. Run demotes to
        failed, set_item_status NOT awaited, exactly one no-changes comment
        posted.
        """
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":1,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        def _fake_run(cmd, **kwargs):
            rc = MagicMock()
            rc.stderr = b""
            # All commands rc 0 — no staged changes anywhere.
            rc.returncode = 0
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
                workspace_repo_dirs=["agent_gtd", "agent-gtd-dispatch"],
            )

        # Run demoted to failed; status NOT set to review.
        assert any(
            call.kwargs.get("status") == RunStatus.failed
            for call in update_run_mock.await_args_list
        )
        set_status_mock.assert_not_awaited()
        # Exactly one no-changes comment posted.
        assert post_comment_mock.await_count == 1
        body = post_comment_mock.await_args.args[1]
        assert "no committed changes" in body.lower()

    async def test_workspace_push_failure_fail_closed(
        self, tmp_path, monkeypatch
    ) -> None:
        """git push non-zero for the changed repo → run failed, fail-closed.

        Staged changes in agent_gtd, push returns rc!=0. The worker fails
        closed — no commit/push attempted against a later repo. Exactly one
        failure comment posted.
        """
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":1,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        calls: list[tuple[list[str], object]] = []

        def _fake_run(cmd, **kwargs):
            cwd = kwargs.get("cwd")
            calls.append((cmd, cwd))
            rc = MagicMock()
            rc.stderr = b"push error"
            if "diff" in cmd and "--cached" in cmd and "--quiet" in cmd:
                # agent_gtd has staged changes; agent-gtd-dispatch would not be
                # reached (loop order: agent_gtd first).
                rc.returncode = 1 if str(cwd).endswith("agent_gtd") else 0
            elif "push" in cmd:
                rc.returncode = 1  # push fails for the changed repo
            else:
                rc.returncode = 0
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
                workspace_repo_dirs=["agent_gtd", "agent-gtd-dispatch"],
            )

        # Run demoted to failed; status NOT set to review.
        assert any(
            call.kwargs.get("status") == RunStatus.failed
            for call in update_run_mock.await_args_list
        )
        set_status_mock.assert_not_awaited()
        # Fail-closed: agent-gtd-dispatch (later in loop order) was never
        # committed/pushed — only add/diff against it could appear, but since
        # agent_gtd is first and push fails, the loop returns before reaching
        # agent-gtd-dispatch at all.
        dispatch_cwds = [
            str(cwd) for _c, cwd in calls if str(cwd).endswith("agent-gtd-dispatch")
        ]
        assert dispatch_cwds == [], calls
        # Exactly one failure comment posted.
        assert post_comment_mock.await_count == 1

    async def test_monorepo_branch_unchanged_when_workspace_repo_dirs_none(
        self, tmp_path, monkeypatch
    ) -> None:
        """Monorepo branch (workspace_repo_dirs=None) is byte-for-byte the prior
        behavior — exact git add -A / commit / push sequence with cwd=str(workspace)
        and sudo-wrapping, identical to test_exit_0_commits_pushes_and_sets_review.
        """
        from agent_gtd_dispatch import config, db, gtd_client, main
        from agent_gtd_dispatch.models import RunStatus

        monkeypatch.setattr(config, "AGENT_SUBPROCESS_USER", "dispatch")
        run, engine, item, project = _make_talos_run()

        update_run_mock = AsyncMock()
        post_comment_mock = AsyncMock()
        set_status_mock = AsyncMock()
        monkeypatch.setattr(db, "update_run", update_run_mock)
        monkeypatch.setattr(gtd_client, "post_comment", post_comment_mock)
        monkeypatch.setattr(gtd_client, "set_item_status", set_status_mock)

        stdout = (
            '{"outcome":"Finished","iterations":3,'
            '"disposition":{"Done":{"summary":"ok",'
            '"verification":"NoChecksConfigured"}}}'
        )
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        commands: list[list[str]] = []
        cwds: list[object] = []

        def _fake_run(cmd, **kwargs):
            commands.append(cmd)
            cwds.append(kwargs.get("cwd"))
            rc = MagicMock()
            rc.returncode = 0
            rc.stderr = b""
            return rc

        with (
            patch("agent_gtd_dispatch.main.subprocess.Popen", return_value=mock_proc),
            patch("agent_gtd_dispatch.main.subprocess.run", side_effect=_fake_run),
        ):
            await main._run_talos(
                run,
                engine,
                tmp_path,
                item,
                project,
                timeout_seconds=60,
                attribution=None,
                register_cb=lambda _p: None,
                # Default: workspace_repo_dirs=None → monorepo path.
            )

        # Marked succeeded; status set to review once.
        assert any(
            call.kwargs.get("status") == RunStatus.succeeded
            for call in update_run_mock.await_args_list
        )
        set_status_mock.assert_awaited_once()
        assert set_status_mock.await_args.args == ("item-abc", "review")

        # Commands issued: git add -A, git -c user.name/email commit -m 'feat: <title>',
        # git push -u origin <branch>. All wrapped with sudo prefix.
        joined = [" ".join(c) for c in commands]
        assert any("git add -A" in j for j in joined), joined
        assert any(
            "git -c user.name=talos-haiku -c user.email=talos-haiku@agent-gtd-dispatch"
            " commit -m feat: Do the thing" in j
            for j in joined
        ), joined
        assert any("git push -u origin feat/x-do-thing" in j for j in joined), joined
        # Sudo wrapping applied
        for cmd in commands:
            assert cmd[0] == "sudo"
            assert cmd[:4] == ["sudo", "-u", "dispatch", "-H"]
        # All git commands ran with cwd=str(workspace) == str(tmp_path)
        for cwd in cwds:
            assert cwd == str(tmp_path), cwds


import httpx  # noqa: E402 — imported here for HTTPError in the status-set test


class TestBuildCommentBody:
    def test_success_comment_includes_branch_and_verification(self) -> None:
        from agent_gtd_dispatch.talos import build_comment_body

        stdout_line = json.dumps(
            {
                "outcome": "Finished",
                "iterations": 4,
                "disposition": {
                    "Done": {
                        "summary": "shipped it",
                        "verification": {"Checks": {"status": "green"}},
                    }
                },
            }
        )
        body = build_comment_body(0, stdout_line, "", "feat/foo")
        assert "feat/foo" in body
        assert "iterations=4" in body
        assert "green" in body
        assert "shipped it" in body

    def test_blocked_comment_includes_decision_needed(self) -> None:
        from agent_gtd_dispatch.talos import build_comment_body

        stdout_line = json.dumps(
            {
                "outcome": "Finished",
                "iterations": 2,
                "disposition": {"Blocked": {"decision_needed": "pick a lib"}},
            }
        )
        body = build_comment_body(10, stdout_line, "", "feat/bar")
        assert "pick a lib" in body
        assert "block" in body.lower()

    def test_exit_1_comment_has_engine_broke_wording(self) -> None:
        from agent_gtd_dispatch.talos import build_comment_body

        body = build_comment_body(1, "", '{"error":"cannot reach ollama"}', None)
        assert "engine error" in body.lower()
        assert "cannot reach ollama" in body
