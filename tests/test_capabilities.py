"""Tests for /info, /agents, and parse_list_agents_output."""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars and use tmp path for workspace/db."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        # Blank the optional engine credentials so a dev box's exported keys
        # can't leak in: availability would drift from the assertions below,
        # and a real OLLAMA_CLOUD_API_KEY would make the cloud-key probe hit
        # ollama.com for real.
        "KIRO_API_KEY": "",
        "OLLAMA_BASE_URL": "",
        "OLLAMA_API_KEY": "",
        "OLLAMA_CLOUD_API_KEY": "",
    }
    with patch.dict(os.environ, env):
        from agent_gtd_dispatch import config

        config.load()
        yield


@pytest.fixture
def client():
    from agent_gtd_dispatch.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key"}


# ---------------------------------------------------------------------------
# /info endpoint
# ---------------------------------------------------------------------------


class TestInfoEndpoint:
    def test_returns_engine_and_version(self, client) -> None:
        resp = client.get("/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "claude-code"
        assert "version" in data
        assert isinstance(data["version"], str)
        assert data["version"]  # non-empty

    def test_no_auth_required(self, client) -> None:
        # /info must be accessible without a bearer token
        resp = client.get("/info")
        assert resp.status_code == 200

    def test_shape(self, client) -> None:
        resp = client.get("/info")
        data = resp.json()
        assert set(data.keys()) == {
            "engine",
            "version",
            "max_concurrent_runs",
            "active_runs",
            "engines",
            "agents",
        }

    def test_capacity_fields(self, client) -> None:
        resp = client.get("/info")
        data = resp.json()
        assert isinstance(data["max_concurrent_runs"], int)
        assert data["max_concurrent_runs"] > 0
        assert isinstance(data["active_runs"], int)
        assert data["active_runs"] >= 0

    def test_engines_list_filtered_by_availability(self, client) -> None:
        # Fixture sets ANTHROPIC_API_KEY → claude-code/sonnet/haiku available
        # AND the three talos-anthropic engines available (they gate on
        # ANTHROPIC_API_KEY, not on CLAUDE_CODE_OAUTH_TOKEN).  KIRO_API_KEY,
        # OLLAMA_BASE_URL, and OLLAMA_CLOUD_API_KEY are not set → kiro,
        # claude-code-ollama, claude-code-glm, talos-qwen, and talos-glm are
        # excluded.
        resp = client.get("/info")
        engines = resp.json()["engines"]
        assert isinstance(engines, list)
        assert all(isinstance(e, str) for e in engines)
        assert set(engines) == {
            "claude-code",
            "claude-code-sonnet",
            "claude-code-haiku",
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
        }

    def test_agents_list_from_discovery_script(
        self, client, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\nprintf 'foo\\tFoo agent\\nbar\\n'\n")
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )
        resp = client.get("/info")
        data = resp.json()
        assert data["agents"] == ["foo", "bar"]

    def test_agents_list_empty_when_script_missing(
        self, client, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path",
            lambda: tmp_path / "nonexistent.sh",
        )
        resp = client.get("/info")
        assert resp.json()["agents"] == []


class TestEngineAvailability:
    @pytest.fixture(autouse=True)
    def _reset_cloud_probe(self, monkeypatch):
        """Reset probe cache and sentinels; mock probe to 'valid' so cloud-key
        tests don't make real network calls.  Empty-key tests are unaffected
        because the short-circuit fires before the probe is ever called."""
        from agent_gtd_dispatch import cloud_auth, engines

        monkeypatch.setattr(cloud_auth, "_probe_result", None)
        monkeypatch.setattr(engines, "_ollama_cloud_warning_logged", False)
        monkeypatch.setattr(engines, "_ollama_cloud_error_logged", False)
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "valid")

    def test_claude_code_available_with_oauth_token(self, monkeypatch) -> None:
        from agent_gtd_dispatch.engines import CLAUDE, is_engine_available

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        assert is_engine_available(CLAUDE) is True

    def test_claude_code_available_with_api_key(self, monkeypatch) -> None:
        from agent_gtd_dispatch.engines import CLAUDE, is_engine_available

        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert is_engine_available(CLAUDE) is True

    def test_claude_code_available_without_credentials(self, monkeypatch) -> None:
        # Claude Code is always available — the binary may be authenticated
        # externally (enterprise/managed distribution, internal wrapper,
        # Bedrock-backed login). We do not gate on env credentials.
        from agent_gtd_dispatch.engines import CLAUDE, is_engine_available

        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert is_engine_available(CLAUDE) is True

    def test_kiro_requires_kiro_api_key(self, monkeypatch) -> None:
        from agent_gtd_dispatch.engines import KIRO, is_engine_available

        monkeypatch.delenv("KIRO_API_KEY", raising=False)
        assert is_engine_available(KIRO) is False
        monkeypatch.setenv("KIRO_API_KEY", "k")
        assert is_engine_available(KIRO) is True

    def test_ollama_requires_base_url(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import CLAUDE_OLLAMA, is_engine_available

        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
        assert is_engine_available(CLAUDE_OLLAMA) is False
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://ollama.local:11434")
        assert is_engine_available(CLAUDE_OLLAMA) is True

    def test_glm_requires_cloud_api_key(self, monkeypatch) -> None:
        # claude-code-glm gates on the DISTINCT cloud key, NOT the local
        # OLLAMA_BASE_URL (unlike claude-code-ollama) — the two must not leak
        # into each other's availability signal.
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import CLAUDE_GLM, is_engine_available

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://ollama.local:11434")
        assert is_engine_available(CLAUDE_GLM) is False
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "cloud-key")
        assert is_engine_available(CLAUDE_GLM) is True

    def test_get_available_engine_names_filters(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_available_engine_names

        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.delenv("KIRO_API_KEY", raising=False)
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
        names = get_available_engine_names()
        # ANTHROPIC_API_KEY set → claude-code family AND talos-anthropic family
        # available.  Ollama disabled → claude-code-ollama, claude-code-glm,
        # talos-qwen, talos-glm excluded.  KIRO_API_KEY unset → kiro excluded.
        assert set(names) == {
            "claude-code",
            "claude-code-sonnet",
            "claude-code-haiku",
            "talos-haiku",
            "talos-sonnet",
            "talos-opus",
        }


# ---------------------------------------------------------------------------
# parse_list_agents_output — exhaustive contract tests
# ---------------------------------------------------------------------------


class TestParseListAgentsOutput:
    @pytest.fixture(autouse=True)
    def _import(self):
        from agent_gtd_dispatch.agent_discovery import parse_list_agents_output

        self.parse = parse_list_agents_output

    def test_name_only(self) -> None:
        result = self.parse("scratch\n")
        assert result == [{"name": "scratch", "description": ""}]

    def test_name_and_description(self) -> None:
        result = self.parse("code-reviewer\tReviews PRs for quality issues")
        assert result == [
            {"name": "code-reviewer", "description": "Reviews PRs for quality issues"}
        ]

    def test_blank_lines_ignored(self) -> None:
        result = self.parse("\n\nfoo\n\n")
        assert result == [{"name": "foo", "description": ""}]

    def test_whitespace_only_lines_ignored(self) -> None:
        result = self.parse("   \t   \nfoo\n   ")
        assert result == [{"name": "foo", "description": ""}]

    def test_comment_lines_ignored(self) -> None:
        result = self.parse("# this is a comment\nfoo\n# another comment")
        assert result == [{"name": "foo", "description": ""}]

    def test_comment_with_leading_whitespace_ignored(self) -> None:
        result = self.parse("  # indented comment\nfoo")
        assert result == [{"name": "foo", "description": ""}]

    def test_invalid_name_with_space_dropped(self) -> None:
        result = self.parse("invalid name\ngood")
        assert result == [{"name": "good", "description": ""}]

    def test_invalid_name_with_at_sign_dropped(self) -> None:
        result = self.parse("bad@name\ngood-agent")
        assert result == [{"name": "good-agent", "description": ""}]

    def test_invalid_name_with_dot_dropped(self) -> None:
        result = self.parse("bad.name\ngood_agent")
        assert result == [{"name": "good_agent", "description": ""}]

    def test_empty_name_dropped(self) -> None:
        # A line that is only a tab (empty name + description)
        result = self.parse("\tfoo")
        assert result == []

    def test_oversized_line_truncated_not_dropped(self) -> None:
        # A line > 4096 chars is truncated; if the truncated result is a valid
        # name, it is included.
        long_name = "a" * 5000
        result = self.parse(long_name)
        assert len(result) == 1
        assert result[0]["name"] == "a" * 4096
        assert result[0]["description"] == ""

    def test_oversized_line_with_tab_truncated(self) -> None:
        # Line with a tab is truncated to 4096 chars total.
        # Name is "abc", description is long.
        long_desc = "x" * 5000
        line = f"abc\t{long_desc}"
        result = self.parse(line)
        assert len(result) == 1
        assert result[0]["name"] == "abc"
        # After truncation to 4096 chars: "abc\t" = 4 chars, desc = 4092 chars
        assert len(result[0]["description"]) == 4092

    def test_tabs_in_description_normalised_to_spaces(self) -> None:
        result = self.parse("foo\tbar\tbaz\tqux")
        assert result == [{"name": "foo", "description": "bar baz qux"}]

    def test_leading_trailing_whitespace_trimmed_from_description(self) -> None:
        result = self.parse("foo\t  hello world  ")
        assert result == [{"name": "foo", "description": "hello world"}]

    def test_multiple_valid_agents(self) -> None:
        output = "agent1\tDesc 1\nagent2\nagent3\tDesc 3"
        result = self.parse(output)
        assert result == [
            {"name": "agent1", "description": "Desc 1"},
            {"name": "agent2", "description": ""},
            {"name": "agent3", "description": "Desc 3"},
        ]

    def test_mixed_valid_and_invalid_lines(self) -> None:
        output = "good-agent\tGood\nbad name\nalso-good\n# comment\n"
        result = self.parse(output)
        assert len(result) == 2
        assert result[0]["name"] == "good-agent"
        assert result[1]["name"] == "also-good"

    def test_empty_input(self) -> None:
        assert self.parse("") == []

    def test_all_comments_and_blanks(self) -> None:
        assert self.parse("# comment\n\n# another\n") == []

    def test_example_from_spec(self) -> None:
        output = (
            "code-reviewer\tReviews PRs for quality issues\n"
            "planner\tDesigns implementation plans\n"
            "# comment — this line is ignored\n"
            "scratch\n"
        )
        result = self.parse(output)
        assert result == [
            {"name": "code-reviewer", "description": "Reviews PRs for quality issues"},
            {"name": "planner", "description": "Designs implementation plans"},
            {"name": "scratch", "description": ""},
        ]

    def test_valid_name_chars(self) -> None:
        # Names can contain letters, digits, underscores, hyphens
        result = self.parse("Az09_-\tOK")
        assert result == [{"name": "Az09_-", "description": "OK"}]


# ---------------------------------------------------------------------------
# /agents endpoint
# ---------------------------------------------------------------------------


class TestAgentsEndpoint:
    def test_requires_auth(self, client) -> None:
        resp = client.get("/agents")
        assert resp.status_code == 401

    def test_happy_path(self, client, auth_headers, tmp_path, monkeypatch) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\nprintf 'foo\\tFoo agent\\nbar\\n'\n")
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )

        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert data["agents"] == [
            {"name": "foo", "description": "Foo agent"},
            {"name": "bar", "description": ""},
        ]

    def test_missing_script_returns_empty(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path",
            lambda: tmp_path / "nonexistent.sh",
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_non_executable_script_returns_empty(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\necho foo\n")
        # Write without execute bit
        script.chmod(stat.S_IRUSR | stat.S_IWUSR)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_non_zero_exit_returns_empty(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\nexit 1\n")
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_timeout_returns_empty(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\necho foo\n")
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )

        # Simulate a timeout by making asyncio.wait_for raise TimeoutError.
        # We close the unawaited coroutine to suppress "coroutine never awaited"
        # RuntimeWarning that would otherwise be emitted on garbage collection.
        async def _timed_out(coro, *args, **kwargs):
            # Suppress the unawaited-coroutine warning before raising
            if hasattr(coro, "close"):
                coro.close()
            raise TimeoutError()

        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery.asyncio.wait_for", _timed_out
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_mixed_valid_and_invalid_output(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'good-agent\\tGood\\nbad name\\nalso-good\\n'\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["agents"]) == 2
        assert data["agents"][0]["name"] == "good-agent"
        assert data["agents"][1]["name"] == "also-good"

    def test_empty_script_output_returns_empty_list(
        self, client, auth_headers, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "list_agents.sh"
        script.write_text("#!/usr/bin/env bash\n# no agents\n")
        script.chmod(0o755)
        monkeypatch.setattr(
            "agent_gtd_dispatch.agent_discovery._get_script_path", lambda: script
        )
        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}


# ---------------------------------------------------------------------------
# cloud_auth.probe_ollama_cloud_key
# ---------------------------------------------------------------------------


class TestProbeOllamaCloudKey:
    """Tests for probe_ollama_cloud_key — no real network calls permitted."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self, monkeypatch):
        """Reset the process-lifetime probe cache before each test."""
        from agent_gtd_dispatch import cloud_auth

        monkeypatch.setattr(cloud_auth, "_probe_result", None)

    def _mock_urlopen_200(self, monkeypatch):
        import urllib.request

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Response())

    def _mock_urlopen_http_error(self, monkeypatch, code):
        import urllib.error
        import urllib.request

        def _raise(req, timeout):
            raise urllib.error.HTTPError(
                url="https://ollama.com/api/me",
                code=code,
                msg="Error",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", _raise)

    def test_empty_key_returns_invalid_without_network(self, monkeypatch) -> None:
        """Empty key → 'invalid' immediately; cache must NOT be set."""
        import urllib.request

        from agent_gtd_dispatch import cloud_auth

        def _should_not_call(req, timeout):
            raise AssertionError("urlopen must not be called for an empty key")

        monkeypatch.setattr(urllib.request, "urlopen", _should_not_call)

        result = cloud_auth.probe_ollama_cloud_key("")
        assert result == "invalid"
        # Cache is NOT updated for empty-key short-circuit
        assert cloud_auth._probe_result is None

    def test_valid_key_returns_valid(self, monkeypatch) -> None:
        """HTTP 200 → 'valid'; result is cached."""
        from agent_gtd_dispatch import cloud_auth

        self._mock_urlopen_200(monkeypatch)

        result = cloud_auth.probe_ollama_cloud_key("good-key")
        assert result == "valid"
        assert cloud_auth._probe_result == "valid"

    def test_401_returns_invalid(self, monkeypatch) -> None:
        """HTTP 401 → 'invalid'; result is cached."""
        from agent_gtd_dispatch import cloud_auth

        self._mock_urlopen_http_error(monkeypatch, 401)

        result = cloud_auth.probe_ollama_cloud_key("bad-key")
        assert result == "invalid"
        assert cloud_auth._probe_result == "invalid"

    def test_403_returns_invalid(self, monkeypatch) -> None:
        """HTTP 403 → 'invalid'; result is cached."""
        from agent_gtd_dispatch import cloud_auth

        self._mock_urlopen_http_error(monkeypatch, 403)

        result = cloud_auth.probe_ollama_cloud_key("bad-key")
        assert result == "invalid"
        assert cloud_auth._probe_result == "invalid"

    def test_other_http_status_returns_unknown(self, monkeypatch) -> None:
        """Non-200, non-401/403 HTTP status → 'unknown'."""
        from agent_gtd_dispatch import cloud_auth

        self._mock_urlopen_http_error(monkeypatch, 500)

        result = cloud_auth.probe_ollama_cloud_key("some-key")
        assert result == "unknown"
        assert cloud_auth._probe_result == "unknown"

    def test_timeout_returns_unknown(self, monkeypatch) -> None:
        """Timeout → 'unknown'."""
        import urllib.request

        from agent_gtd_dispatch import cloud_auth

        def _timeout(req, timeout):
            raise TimeoutError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", _timeout)

        result = cloud_auth.probe_ollama_cloud_key("some-key")
        assert result == "unknown"
        assert cloud_auth._probe_result == "unknown"

    def test_network_error_returns_unknown(self, monkeypatch) -> None:
        """Generic network/OS error → 'unknown'."""
        import urllib.error
        import urllib.request

        from agent_gtd_dispatch import cloud_auth

        def _fail(req, timeout):
            raise urllib.error.URLError("name or service not known")

        monkeypatch.setattr(urllib.request, "urlopen", _fail)

        result = cloud_auth.probe_ollama_cloud_key("some-key")
        assert result == "unknown"

    def test_probe_runs_at_most_once(self, monkeypatch) -> None:
        """Second call with a non-empty key returns cached result (no re-probe)."""
        import urllib.request

        from agent_gtd_dispatch import cloud_auth

        call_count = 0

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _counting_urlopen(req, timeout):
            nonlocal call_count
            call_count += 1
            return _Response()

        monkeypatch.setattr(urllib.request, "urlopen", _counting_urlopen)

        r1 = cloud_auth.probe_ollama_cloud_key("key")
        r2 = cloud_auth.probe_ollama_cloud_key("key")

        assert r1 == "valid"
        assert r2 == "valid"
        assert call_count == 1  # exactly one HTTP call despite two invocations


# ---------------------------------------------------------------------------
# Ollama-cloud engine availability gating
# ---------------------------------------------------------------------------


class TestOllamaCloudEngineGating:
    """Tests for is_engine_available with mocked probe results."""

    @pytest.fixture(autouse=True)
    def _reset_state(self, monkeypatch):
        """Reset probe cache and per-process logging sentinels."""
        from agent_gtd_dispatch import cloud_auth, engines

        monkeypatch.setattr(cloud_auth, "_probe_result", None)
        monkeypatch.setattr(engines, "_ollama_cloud_warning_logged", False)
        monkeypatch.setattr(engines, "_ollama_cloud_error_logged", False)

    def test_empty_key_unavailable_no_probe(self, monkeypatch) -> None:
        """Empty OLLAMA_CLOUD_API_KEY → engine unavailable, probe never called."""
        import urllib.request

        from agent_gtd_dispatch import cloud_auth, config, engines

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")

        def _should_not_call(req, timeout):
            raise AssertionError("probe must not run for empty key")

        monkeypatch.setattr(urllib.request, "urlopen", _should_not_call)

        assert engines.is_engine_available(engines.TALOS_GLM) is False
        assert cloud_auth._probe_result is None

    def test_valid_probe_makes_engine_available(self, monkeypatch) -> None:
        """Key present + probe='valid' → engine available."""
        from agent_gtd_dispatch import cloud_auth, config, engines

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "valid-key")
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "valid")

        assert engines.is_engine_available(engines.TALOS_GLM) is True

    def test_invalid_probe_makes_engine_unavailable(self, monkeypatch) -> None:
        """Key present + probe='invalid' → engine unavailable."""
        from agent_gtd_dispatch import cloud_auth, config, engines

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "bad-key")
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "invalid")

        assert engines.is_engine_available(engines.TALOS_GLM) is False

    def test_unknown_probe_keeps_engine_available(self, monkeypatch) -> None:
        """Key present + probe='unknown' → engine remains available (network blip)."""
        from agent_gtd_dispatch import cloud_auth, config, engines

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "some-key")
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "unknown")

        assert engines.is_engine_available(engines.TALOS_GLM) is True

    def test_invalid_probe_logs_error_once_with_key_length(
        self, monkeypatch, caplog
    ) -> None:
        """'invalid' logs exactly one ERROR naming key length, never the key."""
        import logging

        from agent_gtd_dispatch import cloud_auth, config, engines

        key = "x" * 51  # real incident: truncated to 51 chars
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", key)
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "invalid")

        with caplog.at_level(logging.ERROR, logger="agent_gtd_dispatch.engines"):
            engines.is_engine_available(engines.TALOS_GLM)
            engines.is_engine_available(engines.TALOS_GLM)  # second call

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "ERROR must be logged exactly once"
        assert "51" in errors[0].message  # key length present
        assert key not in errors[0].message  # key value absent

    def test_unknown_probe_logs_warning_once(self, monkeypatch, caplog) -> None:
        """'unknown' logs exactly one WARNING; no re-log on subsequent calls."""
        import logging

        from agent_gtd_dispatch import cloud_auth, config, engines

        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "some-key")
        monkeypatch.setattr(cloud_auth, "probe_ollama_cloud_key", lambda _k: "unknown")

        with caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.engines"):
            engines.is_engine_available(engines.TALOS_GLM)
            engines.is_engine_available(engines.TALOS_GLM)  # second call

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "agent_gtd_dispatch.engines" in r.name
        ]
        assert len(warnings) == 1, "WARNING must be logged exactly once"
