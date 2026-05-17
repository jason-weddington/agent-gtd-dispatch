"""Branch-name helpers for the agent-gtd-dispatch wire contract."""

from __future__ import annotations

import re


def make_branch_name(item_id: str, title: str) -> str:
    """Build a branch-safe name from item ID and title."""
    short_id = item_id[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")
    return f"feat/{short_id}-{slug}"
