"""Tests for execution preflight checks.

Each test verifies that the correct HTTP 422 is returned (and no execution
record is created) when a specific precondition is not met.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

MINIMAL_PLAN = """\
# Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Wave 1: Setup


### Foundation

#### Task 1a: Do something
- **Agent**: worker
- **Files**: `src/index.ts`
- **Depends**: (none)
- **Description**: Does something useful.
"""

INVALID_PLAN = """\
# Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Wave 1: Setup


### Foundation

#### Task 1a: Bad deps
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
    detail = r.json()["detail"].lower()
    assert "not installed" in detail or "not in path" in detail
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


# ---------------------------------------------------------------------------
# GitHub repo access preflight tests
# ---------------------------------------------------------------------------

async def _setup_url_repo(
    client: AsyncClient,
    env: dict[str, str] | None = None,
):
    """Create project + sequence + plan + URL-based repo + optional env vars."""
    pid, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)
    await _add_repo(client, pid, "https://github.com/test-owner/test-repo")
    if env:
        await client.put(f"/api/v1/projects/{pid}/env", json=env)
    return pid, sid


@pytest.mark.asyncio
async def test_preflight_repo_not_accessible_pat(client: AsyncClient):
    """422 when a personal access token cannot access the repository."""
    _, sid = await _setup_url_repo(client, {"GITHUB_TOKEN": "fake-token"})

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
        return_value=(False, "Cannot access repository 'test-owner/test-repo'. "
                      "Verify the URL is correct and the token has access to the repository."),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "Cannot access repository" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_repo_not_accessible_app(client: AsyncClient):
    """422 with specific App diagnostic when the GitHub App can't see the repo."""
    _, sid = await _setup_url_repo(client, {"GITHUB_TOKEN": "fake-token"})

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
        return_value=(False, "The GitHub App does not have access to 'test-owner/test-repo'. "
                      "It can currently access: owner/other-repo. "
                      "Install the App on this repository via GitHub → Settings → "
                      "Integrations → Configure."),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "does not have access" in r.json()["detail"]
    assert "other-repo" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_repo_accessible(client: AsyncClient):
    """Passes when the token can access the repository."""
    _, sid = await _setup_url_repo(client, {"GITHUB_TOKEN": "fake-token"})

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
        return_value=(True, ""),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    assert await _execution_count(client, sid) == 1


@pytest.mark.asyncio
async def test_preflight_repo_access_check_inconclusive(client: AsyncClient):
    """Passes when the access check cannot be performed (e.g. network error)."""
    _, sid = await _setup_url_repo(client, {"GITHUB_TOKEN": "fake-token"})

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
        return_value=(None, ""),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    assert await _execution_count(client, sid) == 1


@pytest.mark.asyncio
async def test_preflight_repo_access_skipped_no_token(client: AsyncClient):
    """Access check is skipped when no GitHub token or App is configured."""
    _, sid = await _setup_url_repo(client)  # no env vars → no token

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
    ) as mock_check:
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    mock_check.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_repo_access_skipped_local_repo(client: AsyncClient, ready_sequence):
    """Access check is skipped for local (non-URL) repos."""
    sid = ready_sequence["sequence_id"]

    with patch(
        "wave_server.routes.executions._check_repo_accessible",
        new_callable=AsyncMock,
    ) as mock_check:
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# PR target branch preflight tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_pr_target_branch_missing(client: AsyncClient):
    """422 when GITHUB_PR_TARGET branch does not exist on the remote."""
    _, sid = await _setup_url_repo(
        client, {"GITHUB_PR_TARGET": "dev", "GITHUB_TOKEN": "fake-token"},
    )

    with (
        patch(
            "wave_server.routes.executions._check_repo_accessible",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "wave_server.routes.executions._check_remote_branch_exists",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 422
    assert "dev" in r.json()["detail"]
    assert "does not exist" in r.json()["detail"]
    assert await _execution_count(client, sid) == 0


@pytest.mark.asyncio
async def test_preflight_pr_target_branch_exists(client: AsyncClient):
    """201 when GITHUB_PR_TARGET branch exists on the remote."""
    _, sid = await _setup_url_repo(
        client, {"GITHUB_PR_TARGET": "dev", "GITHUB_TOKEN": "fake-token"},
    )

    with (
        patch(
            "wave_server.routes.executions._check_repo_accessible",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "wave_server.routes.executions._check_remote_branch_exists",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    assert await _execution_count(client, sid) == 1


@pytest.mark.asyncio
async def test_preflight_pr_target_check_inconclusive(client: AsyncClient):
    """201 when the branch check cannot be performed (e.g. network error)."""
    _, sid = await _setup_url_repo(
        client, {"GITHUB_PR_TARGET": "dev", "GITHUB_TOKEN": "fake-token"},
    )

    with (
        patch(
            "wave_server.routes.executions._check_repo_accessible",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "wave_server.routes.executions._check_remote_branch_exists",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    assert await _execution_count(client, sid) == 1


@pytest.mark.asyncio
async def test_preflight_no_pr_target_skips_check(client: AsyncClient, ready_sequence):
    """Branch check is skipped when no GITHUB_PR_TARGET is configured."""
    sid = ready_sequence["sequence_id"]

    with patch(
        "wave_server.routes.executions._check_remote_branch_exists",
        new_callable=AsyncMock,
    ) as mock_check:
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    mock_check.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_pr_target_local_repo_skips_check(client: AsyncClient, tmp_path):
    """Branch check is skipped for local (non-URL) repos even if PR target is set."""
    pid, sid = await _make_project_and_sequence(client)
    await _upload_plan(client, sid)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _add_repo(client, pid, str(repo_dir))
    await client.put(
        f"/api/v1/projects/{pid}/env",
        json={"GITHUB_PR_TARGET": "dev"},
    )

    with patch(
        "wave_server.routes.executions._check_remote_branch_exists",
        new_callable=AsyncMock,
    ) as mock_check:
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})

    assert r.status_code == 201
    mock_check.assert_not_called()
