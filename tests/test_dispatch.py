"""Tests for core dispatch logic."""

from __future__ import annotations

from agent_gtd_dispatch.dispatch import (
    branch_name_for_item,
    repo_name_from_origin,
)


class TestRepoNameFromOrigin:
    def test_scp_style(self) -> None:
        assert repo_name_from_origin("git@ubuntu-vm01:repos/agent_gtd") == "repos-agent_gtd"

    def test_ssh_url(self) -> None:
        assert (
            repo_name_from_origin("ssh://git@ubuntu-vm01/home/git/repos/agent_gtd")
            == "repos-agent_gtd"
        )

    def test_github_ssh(self) -> None:
        assert (
            repo_name_from_origin("git@github.com:jason-weddington/agent-gtd.git")
            == "jason-weddington-agent-gtd"
        )

    def test_https(self) -> None:
        assert (
            repo_name_from_origin("https://github.com/jason-weddington/agent-gtd.git")
            == "jason-weddington-agent-gtd"
        )

    def test_simple_name_fallback(self) -> None:
        # file:// URLs have a path; the regex picks up tmp/myrepo
        assert repo_name_from_origin("file:///tmp/myrepo") == "tmp-myrepo"


class TestBranchName:
    def test_basic(self) -> None:
        result = branch_name_for_item("abcd1234-5678", "Fix login bug")
        assert result == "feat/abcd1234-fix-login-bug"

    def test_truncates_long_title(self) -> None:
        long_title = "A" * 100
        result = branch_name_for_item("abcd1234", long_title)
        # slug portion should be at most 40 chars
        slug = result.split("/", 1)[1].split("-", 1)[1]
        assert len(slug) <= 40

    def test_special_characters(self) -> None:
        result = branch_name_for_item("abcd1234", "Fix: the @#$ API (broken)")
        assert result == "feat/abcd1234-fix-the-api-broken"

    def test_trailing_hyphens_stripped(self) -> None:
        result = branch_name_for_item("abcd1234", "Fix ---")
        assert not result.endswith("-")
