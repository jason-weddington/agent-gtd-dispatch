"""HTTP client for the Agent GTD API."""

from __future__ import annotations

import httpx

from . import config


async def _request(method: str, path: str, **kwargs: object) -> dict:
    """Make an authenticated request to the GTD API."""
    url = f"{config.AGENT_GTD_URL}/api{path}"
    headers = {"Authorization": f"Bearer {config.AGENT_GTD_API_KEY}"}
    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def get_item(item_id: str) -> dict:
    return await _request("GET", f"/items/{item_id}")


async def get_project(project_id: str) -> dict:
    return await _request("GET", f"/projects/{project_id}")


async def post_comment(item_id: str, content: str) -> None:
    await _request(
        "POST",
        f"/items/{item_id}/comments",
        json={
            "content_markdown": content,
            "created_by": "claude-dispatch",
        },
    )
