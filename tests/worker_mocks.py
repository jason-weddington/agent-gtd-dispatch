"""Shared stubs for tests that MagicMock the whole ``main.dispatch`` module.

Patching an entire module means every attribute the worker touches becomes a
MagicMock, including ones added later — so a new call in ``_dispatch_worker``
silently changes what these tests exercise instead of failing loudly.  Anything
whose RETURN VALUE the worker inspects therefore needs a real stub here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_gtd_dispatch import dispatch


def stub_rescue(mock_dispatch: MagicMock) -> None:
    """Make the pre-teardown rescue a no-op.

    ``main._rescue_before_teardown`` calls ``dispatch.rescue_abandoned_work`` once
    per repo and reads ``.attempted`` / ``.pushed`` / ``.repo_name`` off each
    result.  On a MagicMock'd module those are truthy MagicMocks, so every test
    would look as though it had rescued abandoned work — and the log line that
    joins ``repo_name`` would raise.

    Returns a real "nothing to rescue" result instead, which is the normal case
    for a run that pushed cleanly.  Tests that exercise the rescue override it.
    """
    mock_dispatch.RescueResult = dispatch.RescueResult
    mock_dispatch.rescue_abandoned_work = MagicMock(
        side_effect=lambda name, _path, _branch, _run_id: dispatch.RescueResult(
            name, attempted=False, pushed=False, committed=False
        )
    )
