"""Cloud API key validation for the Ollama cloud service.

This module provides a probe function for verifying that a key is actually
accepted by the remote API, not just present in the environment.  Presence
and validity are not the same — this distinction caught a real incident where
a truncated OLLAMA_CLOUD_API_KEY silently failed at run-time (kb-03080).
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Literal

logger = logging.getLogger(__name__)

_OLLAMA_CLOUD_PROBE_URL = "https://ollama.com/api/me"
_PROBE_TIMEOUT_SECONDS = 5

# Process-lifetime cache.  None = not yet probed.  Reset in tests via monkeypatch.
_probe_result: Literal["valid", "invalid", "unknown"] | None = None


def probe_ollama_cloud_key(key: str) -> Literal["valid", "invalid", "unknown"]:
    """Probe whether *key* is accepted by the Ollama cloud API.

    Sends ``POST https://ollama.com/api/me`` with a Bearer token and a 5-second
    timeout.

    Returns:
        ``'valid'``   — HTTP 200.
        ``'invalid'`` — HTTP 401 or 403.
        ``'unknown'`` — any other status code, timeout, or network error.

    Caching: the result is cached for the process lifetime.  The probe
    **never** runs at import time.  An empty *key* short-circuits immediately
    to ``'invalid'`` without a network call and **without** updating the cache
    (so a later call with a real key still probes).
    """
    global _probe_result

    if not key:
        # Empty key is definitively invalid; don't cache — a later call with a
        # real key should still probe.
        return "invalid"

    if _probe_result is not None:
        return _probe_result

    req = urllib.request.Request(  # noqa: S310
        _OLLAMA_CLOUD_PROBE_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as resp:  # noqa: S310
            _probe_result = "valid" if resp.status == 200 else "unknown"
    except urllib.error.HTTPError as exc:
        _probe_result = "invalid" if exc.code in (401, 403) else "unknown"
    except Exception:  # network errors, timeouts, unexpected OS errors
        _probe_result = "unknown"

    return _probe_result
