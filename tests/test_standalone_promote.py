"""Tests for the standalone POST /api/v1/promote endpoint.

This endpoint is a thin wrapper around promote_pr() — no execution ID needed.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from wave_server.engine.github_pr import PRInfo, PromoteResult


# ---------------------------------------------------------------------------
# 1. Review-bot not configured → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_no_review_bot(client):
    """When the review-bot GitHub App is not configured, return 400 with a
    descriptive error mentioning review-bot configuration."""
    with patch("wave_server.routes.executions.create_app_auth", return_value=None):
        resp = await client.post(
            "/api/v1/promote",
            json={"pr_url": "https://github.com/o/r/pull/1"},
        )

    assert resp.status_code == 400
    body = resp.json()
    # Error message should mention review-bot config
    assert "review-bot" in body["detail"].lower() or "Review-bot" in body["detail"]


# ---------------------------------------------------------------------------
# 2. Happy path — promote succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_promote_success(client):
    """When promote_pr succeeds, return 200 with success=True and
    promotion_pr_url set."""
    mock_auth = MagicMock()
    mock_auth.get_token = AsyncMock(return_value="fake-token")

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
        patch("wave_server.routes.executions.create_app_auth", return_value=mock_auth),
        patch("wave_server.routes.executions.promote_pr", new_callable=AsyncMock, return_value=promote_result),
    ):
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
    mock_auth = MagicMock()
    mock_auth.get_token = AsyncMock(return_value="fake-token")

    promote_result = PromoteResult(
        success=False,
        error="merge conflict",
    )

    with (
        patch("wave_server.routes.executions.create_app_auth", return_value=mock_auth),
        patch("wave_server.routes.executions.promote_pr", new_callable=AsyncMock, return_value=promote_result),
    ):
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
    mock_auth = MagicMock()
    mock_auth.get_token = AsyncMock(return_value="fake-token")

    # promote_pr returns an error for unparseable URLs
    promote_result = PromoteResult(
        success=False,
        error="Cannot parse PR URL: not-a-url",
    )

    with (
        patch("wave_server.routes.executions.create_app_auth", return_value=mock_auth),
        patch(
            "wave_server.routes.executions.promote_pr",
            new_callable=AsyncMock,
            return_value=promote_result,
        ) as mock_promote,
    ):
        resp = await client.post(
            "/api/v1/promote",
            json={"pr_url": "not-a-url"},
        )

    # The endpoint should have called promote_pr even with a bad URL
    mock_promote.assert_called_once()
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["error"] is not None
