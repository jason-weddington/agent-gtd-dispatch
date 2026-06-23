"""HTTP client for the Agent GTD API."""

from __future__ import annotations

from typing import Any

import httpx

from . import config


async def _request(
    method: str, path: str, *, token: str | None = None, **kwargs: Any
) -> Any:
    """Make an authenticated request to the GTD API and return parsed JSON.

    If `token` is provided, it is used as the Bearer credential; otherwise
    `config.AGENT_GTD_API_KEY` is used. Falling back to the static key when
    `token` is None is mandatory — it preserves admin dispatch + legacy senders
    (Phase 1 of the per-run callback token rollout).
    """
    url = f"{config.AGENT_GTD_URL}/api{path}"
    headers = {"Authorization": f"Bearer {token or config.AGENT_GTD_API_KEY}"}
    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def _request_raw(
    method: str, path: str, *, token: str | None = None, **kwargs: Any
) -> bytes:
    """Make an authenticated request to the GTD API and return raw bytes.

    Same token fallback semantics as `_request`.
    """
    url = f"{config.AGENT_GTD_URL}/api{path}"
    headers = {"Authorization": f"Bearer {token or config.AGENT_GTD_API_KEY}"}
    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.content


async def get_item(item_id: str, *, token: str | None = None) -> dict[str, Any]:
    """Fetch a GTD item by ID."""
    result: dict[str, Any] = await _request("GET", f"/items/{item_id}", token=token)
    return result


async def get_project(project_id: str, *, token: str | None = None) -> dict[str, Any]:
    """Fetch a GTD project by ID."""
    result: dict[str, Any] = await _request(
        "GET", f"/projects/{project_id}", token=token
    )
    return result


async def post_comment(
    item_id: str, content: str, created_by: str, *, token: str | None = None
) -> None:
    """Post a comment on a GTD item."""
    await _request(
        "POST",
        f"/items/{item_id}/comments",
        token=token,
        json={
            "content_markdown": content,
            "created_by": created_by,
        },
    )


async def list_attachments(
    item_id: str, *, token: str | None = None
) -> list[dict[str, Any]]:
    """GET /api/items/{item_id}/attachments — returns the list as-is."""
    result: list[dict[str, Any]] = await _request(
        "GET", f"/items/{item_id}/attachments", token=token
    )
    return result


async def download_attachment(attachment_id: str, *, token: str | None = None) -> bytes:
    """GET /api/attachments/{attachment_id} — returns raw file bytes."""
    return await _request_raw("GET", f"/attachments/{attachment_id}", token=token)


async def advance_rollout(
    rollout_id: str, *, token: str | None = None
) -> dict[str, Any]:
    """GET /api/rollouts/{rollout_id}/advance.

    Returns {next_ready, in_progress, blocked, graph_complete}.
    """
    result: dict[str, Any] = await _request(
        "GET", f"/rollouts/{rollout_id}/advance", token=token
    )
    return result


async def complete_in_rollout(
    rollout_id: str,
    item_id: str,
    outcome: str,
    merge_actor: str = "manager-allowlist",
    decision_rule: str = "",
    *,
    token: str | None = None,
) -> None:
    """POST /api/rollouts/{rollout_id}/complete-item."""
    await _request(
        "POST",
        f"/rollouts/{rollout_id}/complete-item",
        token=token,
        json={
            "item_id": item_id,
            "outcome": outcome,
            "merge_actor": merge_actor,
            "decision_rule": decision_rule,
        },
    )


async def get_rollout(rollout_id: str, *, token: str | None = None) -> dict[str, Any]:
    """GET /api/rollouts/{rollout_id}.

    Returns rollout dict including status and manage_retry_count.
    """
    result: dict[str, Any] = await _request(
        "GET", f"/rollouts/{rollout_id}", token=token
    )
    return result


async def relaunch_manage_rollout(
    rollout_id: str, *, token: str | None = None
) -> dict[str, Any]:
    """POST /api/rollouts/{rollout_id}/relaunch-manage.

    Atomically increments manage_retry_count and returns the updated rollout.
    """
    result: dict[str, Any] = await _request(
        "POST", f"/rollouts/{rollout_id}/relaunch-manage", token=token
    )
    return result


async def halt_rollout(
    rollout_id: str, reason: str, *, token: str | None = None
) -> None:
    """POST /api/rollouts/{rollout_id}/halt."""
    await _request(
        "POST",
        f"/rollouts/{rollout_id}/halt",
        token=token,
        json={"reason": reason},
    )


async def list_comments(
    item_id: str, *, token: str | None = None
) -> list[dict[str, Any]]:
    """GET /items/{item_id}/comments → list of comment dicts."""
    result: list[dict[str, Any]] = await _request(
        "GET", f"/items/{item_id}/comments", token=token
    )
    return result


async def list_running_rollouts(*, token: str | None = None) -> list[dict[str, Any]]:
    """GET /api/rollouts?status=running — returns list of running rollout dicts.

    Passes status=running as a query param; also filters client-side as a
    safety net in case the endpoint ignores the parameter.
    """
    result: list[dict[str, Any]] = await _request(
        "GET", "/rollouts", token=token, params={"status": "running"}
    )
    return [r for r in result if r.get("status") == "running"]
