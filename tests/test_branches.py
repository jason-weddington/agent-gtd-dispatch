"""Tests for the shared make_branch_name helper."""

from __future__ import annotations

from agent_gtd_dispatch_protocol.branches import make_branch_name


class TestMakeBranchName:
    def test_simple_ascii(self) -> None:
        assert (
            make_branch_name("abcd1234", "Fix login bug")
            == "feat/abcd1234-fix-login-bug"
        )

    def test_punctuation(self) -> None:
        assert (
            make_branch_name("abcd1234", "Fix: the @#$ API (broken)")
            == "feat/abcd1234-fix-the-api-broken"
        )

    def test_uppercase(self) -> None:
        assert (
            make_branch_name("abcd1234", "Implement The Feature")
            == "feat/abcd1234-implement-the-feature"
        )

    def test_title_longer_than_40_chars(self) -> None:
        assert make_branch_name("abcd1234", "A" * 100) == "feat/abcd1234-" + "a" * 40

    def test_unicode_title(self) -> None:
        assert (
            make_branch_name("abcd1234", "Héllo wörld") == "feat/abcd1234-h-llo-w-rld"
        )
