"""Suite-wide safety net: the disposition classifier never reaches the network.

``config.DISPOSITION_CLASSIFIER_ENABLED`` defaults ON in production, and the
worker's unasserted path now calls it.  A developer machine or CI host that
happens to carry ``OLLAMA_CLOUD_API_KEY`` would otherwise make a real, paid
inference call from any test that drives a build terminal without a completion
artifact.  Setting the ENV VAR (not just the module global) is what makes it
survive the ``config.load()`` calls the per-module fixtures make.

Deliberately does NOT request the ``monkeypatch`` fixture: an autouse fixture in
this file is set up before every per-test fixture, so requesting ``monkeypatch``
here would make it the LAST thing torn down and would outlive fixtures (like
``test_api.py::client``) whose teardown runs against monkeypatched module state.

Tests that exercise the inferred tier opt back in explicitly — see
``tests/test_disposition.py``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from agent_gtd_dispatch import config

if TYPE_CHECKING:
    from collections.abc import Iterator

_ENV_VAR = "DISPATCH_DISPOSITION_CLASSIFIER_ENABLED"


@pytest.fixture(autouse=True)
def _disable_disposition_classifier() -> Iterator[None]:
    previous_env = os.environ.get(_ENV_VAR)
    previous_flag = config.DISPOSITION_CLASSIFIER_ENABLED
    os.environ[_ENV_VAR] = "0"
    config.DISPOSITION_CLASSIFIER_ENABLED = False
    try:
        yield
    finally:
        config.DISPOSITION_CLASSIFIER_ENABLED = previous_flag
        if previous_env is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = previous_env
