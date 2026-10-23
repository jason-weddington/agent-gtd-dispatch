"""Parity tests between the protocol BuildEngine enum and the engine registry."""

from __future__ import annotations

import pytest
from agent_gtd_dispatch_protocol import BuildEngine

from agent_gtd_dispatch.engines import (
    ENGINES,
    TALOS_ENGINES,
    get_available_engine_names,
    get_engine,
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
