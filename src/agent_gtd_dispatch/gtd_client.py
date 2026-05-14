"""HTTP client for the Agent GTD API."""

from __future__ import annotations

from typing import Any

import httpx

from . import config


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Make an authenticated request to the GTD API and return parsed JSON."""
    url = f"{config.AGENT_GTD_URL}/api{path}"
    headers = {"Authorization": f"Bearer {config.AGENT_GTD_API_KEY}"}
    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def _request_raw(method: str, path: str, **kwargs: Any) -> bytes:
    """Make an authenticated request to the GTD API and return raw bytes."""
    url = f"{config.AGENT_GTD_URL}/api{path}"
    headers = {"Authorization": f"Bearer {config.AGENT_GTD_API_KEY}"}
    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.content


async def get_item(item_id: str) -> dict[str, Any]:
    """Fetch a GTD item by ID."""
    result: dict[str, Any] = await _request("GET", f"/items/{item_id}")
    return result


async def get_project(project_id: str) -> dict[str, Any]:
    """Fetch a GTD project by ID."""
    result: dict[str, Any] = await _request("GET", f"/projects/{project_id}")
    return result


async def post_comment(item_id: str, content: str, created_by: str) -> None:
    """Post a comment on a GTD item."""
    await _request(
        "POST",
        f"/items/{item_id}/comments",
        json={
            "content_markdown": content,
            "created_by": created_by,
        },
    )


async def list_attachments(item_id: str) -> list[dict[str, Any]]:
    """GET /api/items/{item_id}/attachments — returns the list as-is."""
    result: list[dict[str, Any]] = await _request(
        "GET", f"/items/{item_id}/attachments"
    )
    return result


async def download_attachment(attachment_id: str) -> bytes:
    """GET /api/attachments/{attachment_id} — returns raw file bytes."""
    return await _request_raw("GET", f"/attachments/{attachment_id}")


async def advance_rollout(rollout_id: str) -> dict[str, Any]:
    """GET /api/rollouts/{rollout_id}/advance.

    Returns {next_ready, in_progress, blocked, graph_complete}.
    """
    result: dict[str, Any] = await _request("GET", f"/rollouts/{rollout_id}/advance")
    return result


async def complete_in_rollout(
    rollout_id: str,
    item_id: str,
    outcome: str,
    merge_actor: str = "manager-allowlist",
    decision_rule: str = "",
) -> None:
    """POST /api/rollouts/{rollout_id}/complete-item."""
    await _request(
        "POST",
        f"/rollouts/{rollout_id}/complete-item",
        json={
            "item_id": item_id,
            "outcome": outcome,
            "merge_actor": merge_actor,
            "decision_rule": decision_rule,
        },
    )


async def halt_rollout(rollout_id: str, reason: str) -> None:
    """POST /api/rollouts/{rollout_id}/halt."""
    await _request(
        "POST",
        f"/rollouts/{rollout_id}/halt",
        json={"reason": reason},
    )


async def list_comments(item_id: str) -> list[dict[str, Any]]:
    """GET /items/{item_id}/comments → list of comment dicts."""
    result: list[dict[str, Any]] = await _request("GET", f"/items/{item_id}/comments")
    return result
