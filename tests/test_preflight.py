"""Tests for execution preflight checks.

Each test verifies that the correct HTTP 422 is returned (and no execution
record is created) when a specific precondition is not met.
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient

MINIMAL_PLAN = """\
# Plan

## Wave 1: Setup

### Task 1a: Do something
- **Agent**: worker
- **Files**: `src/index.ts`
- **Depends**: (none)
- **Description**: Does something useful.
"""

INVALID_PLAN = """\
# Plan

## Wave 1: Setup

### Task 1a: Bad deps
- **Agent**: worker
- **Files**: `src/index.ts`
- **Depends**: 9z
- **Description**: References a task that does not exist.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_project_and_sequence(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "preflight-proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    return pid, sid


async def _upload_plan(client: AsyncClient, sid: str, plan: str = MINIMAL_PLAN):
    await client.post(
        f"/api/v1/sequences/{sid}/plan",
        content=plan,
        headers={"content-type": "text/plain"},
    )


async def _add_repo(client: AsyncClient, pid: str, path: str):
    await client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"path": path},
    )


async def _execution_count(client: AsyncClient, sid: str) -> int:
    r = await client.get(f"/api/v1/sequences/{sid}/executions")
    return len(r.json())


# ---------------------------------------------------------------------------
# Preflight failure tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_no_plan(client: AsyncClient):
    """422 when sequence has no plan uploaded."""
    _, sid = await _make_project_and_sequence(client)

    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "No plan found" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_invalid_plan(client: AsyncClient):
    """422 when the uploaded plan fails DAG validation."""
    _, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid, INVALID_PLAN)

    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "Plan validation failed" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_no_repository(client: AsyncClient):
    """422 when no repository is configured for the project."""
    _, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)

    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "No repository configured" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_repo_path_missing(client: AsyncClient, tmp_path):
    """422 when the repository path no longer exists on disk."""
    pid, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)

    gone_dir = tmp_path / "deleted_repo"
    gone_dir.mkdir()
    await _add_repo(client, pid, str(gone_dir))
    gone_dir.rmdir()  # simulate directory disappearing after registration

    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_claude_not_installed(client: AsyncClient, tmp_path):
    """422 when the claude CLI is not found in PATH."""
    pid, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _add_repo(client, pid, str(repo_dir))

    with patch("wave_server.routes.executions.shutil.which", return_value=None):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "claude" in r.json()["detail"].lower()
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_no_network(client: AsyncClient, tmp_path):
    """422 when api.anthropic.com is not reachable."""
    pid, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _add_repo(client, pid, str(repo_dir))

    # Override the autouse mock_network fixture to simulate no connectivity
    with patch("wave_server.routes.executions._check_network", return_value=False):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "api.anthropic.com" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_all_pass(client: AsyncClient, ready_sequence):
    """201 when all preflight checks pass."""
    sid = ready_sequence["sequence_id"]

    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    assert await _execution_count(client, sid) == 1
