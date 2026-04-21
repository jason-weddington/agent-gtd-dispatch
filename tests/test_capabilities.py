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
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
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
        assert set(data.keys()) == {"engine", "version"}


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
