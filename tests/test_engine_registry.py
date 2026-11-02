"""Parity tests between the protocol BuildEngine enum and the engine registry."""

from __future__ import annotations

import pytest
from agent_gtd_dispatch_protocol import BuildEngine

from agent_gtd_dispatch.engines import (
    ENGINES,
    TALOS_ENGINES,
    get_available_engine_names,
    get_engine,
    is_engine_allowed,
)


def test_registry_keys_match_build_engine() -> None:
    assert set(ENGINES) == {m.value for m in BuildEngine}


def test_registry_keys_are_plain_str() -> None:
    assert all(type(k) is str for k in ENGINES)


def test_engine_name_matches_key() -> None:
    assert all(ENGINES[k].name == k for k in ENGINES)


def test_talos_engines_derived_from_build_engine() -> None:
    expected = frozenset(m.value for m in BuildEngine if m.value.startswith("talos-"))
    assert expected == TALOS_ENGINES


def test_unknown_engine_error_renders_plain_strings() -> None:
    with pytest.raises(ValueError, match="Unknown engine") as exc_info:
        get_engine("not-an-engine")
    msg = str(exc_info.value)
    assert "'not-an-engine'" in msg
    assert "'claude-code'" in msg
    assert "BuildEngine" not in msg


def test_available_engine_names_are_plain_str() -> None:
    assert all(type(n) is str for n in get_available_engine_names())


@pytest.fixture
def _clean_creds(monkeypatch):
    from agent_gtd_dispatch import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
    monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    return monkeypatch


def _set(mp, only, slots, qwen_url):
    from agent_gtd_dispatch import config

    mp.setattr(config, "ENGINES_ONLY", only)
    mp.setattr(config, "ENGINE_SLOTS", slots)
    mp.setattr(config, "TALOS_QWEN_BASE_URL", qwen_url)


def test_allowlist_advertises_only_listed(_clean_creds) -> None:
    _set(
        _clean_creds,
        frozenset({"talos-qwen"}),
        {"talos-qwen": 1},
        "http://127.0.0.1:11434",
    )
    assert get_available_engine_names() == ["talos-qwen"]


def test_allowlist_does_not_bypass_credentials(_clean_creds) -> None:
    _set(_clean_creds, frozenset({"talos-qwen"}), {"talos-qwen": 1}, "")
    assert get_available_engine_names() == []


def test_allowlist_does_not_bypass_zero_cap(_clean_creds) -> None:
    _set(
        _clean_creds,
        frozenset({"talos-qwen"}),
        {"talos-qwen": 0},
        "http://127.0.0.1:11434",
    )
    assert get_available_engine_names() == []


def test_no_allowlist_default_engines(_clean_creds) -> None:
    _set(_clean_creds, None, {}, "")
    assert get_available_engine_names() == [
        "claude-code",
        "claude-code-sonnet",
        "claude-code-haiku",
    ]


def test_is_engine_allowed_truth_table(monkeypatch) -> None:
    from agent_gtd_dispatch import config

    monkeypatch.setattr(config, "ENGINES_ONLY", None)
    assert is_engine_allowed("anything")
    monkeypatch.setattr(config, "ENGINES_ONLY", frozenset({"a"}))
    assert is_engine_allowed("a")
    assert not is_engine_allowed("b")
    monkeypatch.setattr(config, "ENGINES_ONLY", frozenset())
    assert not is_engine_allowed("a")
