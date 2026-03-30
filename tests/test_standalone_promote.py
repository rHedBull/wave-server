"""Tests for the standalone POST /api/v1/promote endpoint.

This endpoint is a thin wrapper around promote_pr() — no execution ID needed.
"""

import pytest
from unittest.mock import AsyncMock, patch

from wave_server.engine.github_pr import PRInfo, PromoteResult


# ---------------------------------------------------------------------------
# 1. GitHub token not configured → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_no_token(client):
    """When no GitHub token is configured, return 400."""
    with patch("wave_server.routes.executions.settings") as mock_settings:
        mock_settings.github_token = None
        resp = await client.post(
            "/api/v1/promote",
            json={"pr_url": "https://github.com/o/r/pull/1"},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "token" in body["detail"].lower()


# ---------------------------------------------------------------------------
# 2. Happy path — promote succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_success(client):
    """When promote_pr succeeds, return 200 with success=True and
    promotion_pr_url set."""
    promote_result = PromoteResult(
        success=True,
        merged_pr=PRInfo(
            number=1,
            url="https://github.com/o/r/pull/1",
            head_branch="fix/1",
            base_branch="dev",
            state="closed",
            merged=True,
        ),
        promotion_pr_url="https://github.com/o/r/pull/2",
    )

    with (
        patch("wave_server.routes.executions.settings") as mock_settings,
        patch(
            "wave_server.routes.executions.promote_pr",
            new_callable=AsyncMock,
            return_value=promote_result,
        ),
    ):
        mock_settings.github_token = "fake-token"
        resp = await client.post(
            "/api/v1/promote",
            json={
                "pr_url": "https://github.com/o/r/pull/1",
                "promotion_target": "main",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["promotion_pr_url"] == "https://github.com/o/r/pull/2"
    assert body["error"] is None


# ---------------------------------------------------------------------------
# 3. Promote fails — promote_pr returns failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_failure(client):
    """When promote_pr returns success=False, return 200 with the error message."""
    promote_result = PromoteResult(
        success=False,
        error="merge conflict",
    )

    with (
        patch("wave_server.routes.executions.settings") as mock_settings,
        patch(
            "wave_server.routes.executions.promote_pr",
            new_callable=AsyncMock,
            return_value=promote_result,
        ),
    ):
        mock_settings.github_token = "fake-token"
        resp = await client.post(
            "/api/v1/promote",
            json={"pr_url": "https://github.com/o/r/pull/1"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "merge conflict" in body["error"]


# ---------------------------------------------------------------------------
# 4. Invalid URL — endpoint still calls promote_pr (which returns error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_invalid_url(client):
    """When given a bad PR URL, the endpoint should still call promote_pr
    (which will parse-fail and return an error)."""
    promote_result = PromoteResult(
        success=False,
        error="Cannot parse PR URL: not-a-url",
    )

    with (
        patch("wave_server.routes.executions.settings") as mock_settings,
        patch(
            "wave_server.routes.executions.promote_pr",
            new_callable=AsyncMock,
            return_value=promote_result,
        ) as mock_promote,
    ):
        mock_settings.github_token = "fake-token"
        resp = await client.post(
            "/api/v1/promote",
            json={"pr_url": "not-a-url"},
        )

    mock_promote.assert_called_once()
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["error"] is not None
