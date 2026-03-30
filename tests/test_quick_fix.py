"""Schema validation tests for QuickFixRequest, QuickFixResponse, StandalonePromoteRequest."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import RunnerConfig, RunnerResult
from wave_server.schemas import (
    QuickFixRequest,
    QuickFixResponse,
    StandalonePromoteRequest,
)


# ---------------------------------------------------------------------------
# QuickFixRequest
# ---------------------------------------------------------------------------


def test_quickfix_request_required_fields():
    """prompt, branch, pr_title are required; defaults are correct."""
    r = QuickFixRequest(prompt="fix it", branch="fix/1", pr_title="Fix")
    assert r.prompt == "fix it"
    assert r.branch == "fix/1"
    assert r.pr_title == "Fix"
    assert r.files == []
    assert r.pr_body == ""
    assert r.source_branch is None
    assert r.auto_promote is False
    assert r.model is None
    assert r.timeout_ms is None


def test_quickfix_request_all_fields():
    """All fields populated."""
    r = QuickFixRequest(
        prompt="fix it",
        branch="fix/1",
        pr_title="Fix",
        files=["a.py"],
        pr_body="body",
        source_branch="dev",
        auto_promote=True,
        model="claude-sonnet-4-5",
        timeout_ms=60000,
    )
    assert r.auto_promote is True
    assert r.files == ["a.py"]
    assert r.pr_body == "body"
    assert r.source_branch == "dev"
    assert r.model == "claude-sonnet-4-5"
    assert r.timeout_ms == 60000


def test_quickfix_request_missing_required():
    """Missing required fields raises ValidationError."""
    with pytest.raises(ValidationError):
        QuickFixRequest()  # missing prompt, branch, pr_title


def test_quickfix_request_missing_prompt():
    """Missing only prompt raises ValidationError."""
    with pytest.raises(ValidationError):
        QuickFixRequest(branch="fix/1", pr_title="Fix")


def test_quickfix_request_missing_branch():
    """Missing only branch raises ValidationError."""
    with pytest.raises(ValidationError):
        QuickFixRequest(prompt="fix it", pr_title="Fix")


def test_quickfix_request_missing_pr_title():
    """Missing only pr_title raises ValidationError."""
    with pytest.raises(ValidationError):
        QuickFixRequest(prompt="fix it", branch="fix/1")


def test_quickfix_request_files_default_empty_list():
    """files defaults to an empty list, not None."""
    r = QuickFixRequest(prompt="p", branch="b", pr_title="t")
    assert r.files == []
    assert isinstance(r.files, list)


def test_quickfix_request_multiple_files():
    """files accepts a list with multiple entries."""
    r = QuickFixRequest(
        prompt="p",
        branch="b",
        pr_title="t",
        files=["a.py", "b.py", "c/d.py"],
    )
    assert r.files == ["a.py", "b.py", "c/d.py"]


# ---------------------------------------------------------------------------
# QuickFixResponse
# ---------------------------------------------------------------------------


def test_quickfix_response_defaults():
    """Only success and branch required; defaults are correct."""
    r = QuickFixResponse(success=True, branch="fix/1")
    assert r.success is True
    assert r.branch == "fix/1"
    assert r.pr_url is None
    assert r.pr_number is None
    assert r.promoted is False
    assert r.promotion_pr_url is None
    assert r.execution_time_ms == 0
    assert r.worker_output == ""
    assert r.error is None


def test_quickfix_response_full():
    """All fields populated."""
    r = QuickFixResponse(
        success=True,
        branch="fix/1",
        pr_url="https://github.com/o/r/pull/1",
        pr_number=1,
        promoted=True,
        promotion_pr_url="https://github.com/o/r/pull/2",
        execution_time_ms=5000,
        worker_output="done",
    )
    assert r.pr_number == 1
    assert r.pr_url == "https://github.com/o/r/pull/1"
    assert r.promoted is True
    assert r.promotion_pr_url == "https://github.com/o/r/pull/2"
    assert r.execution_time_ms == 5000
    assert r.worker_output == "done"


def test_quickfix_response_failure_with_error():
    """Response can represent a failure with an error message."""
    r = QuickFixResponse(success=False, branch="fix/1", error="worker crashed")
    assert r.success is False
    assert r.error == "worker crashed"
    assert r.pr_url is None
    assert r.promoted is False


def test_quickfix_response_missing_required():
    """Missing success or branch raises ValidationError."""
    with pytest.raises(ValidationError):
        QuickFixResponse()

    with pytest.raises(ValidationError):
        QuickFixResponse(success=True)  # missing branch

    with pytest.raises(ValidationError):
        QuickFixResponse(branch="fix/1")  # missing success


# ---------------------------------------------------------------------------
# StandalonePromoteRequest
# ---------------------------------------------------------------------------


def test_standalone_promote_request_defaults():
    """Only pr_url required; defaults are correct."""
    r = StandalonePromoteRequest(pr_url="https://github.com/o/r/pull/1")
    assert r.pr_url == "https://github.com/o/r/pull/1"
    assert r.promotion_target is None
    assert r.merge_method == "squash"


def test_standalone_promote_request_all_fields():
    """All fields populated."""
    r = StandalonePromoteRequest(
        pr_url="https://github.com/o/r/pull/42",
        promotion_target="main",
        merge_method="merge",
    )
    assert r.pr_url == "https://github.com/o/r/pull/42"
    assert r.promotion_target == "main"
    assert r.merge_method == "merge"


def test_standalone_promote_request_rebase_method():
    """merge_method accepts 'rebase'."""
    r = StandalonePromoteRequest(
        pr_url="https://github.com/o/r/pull/1",
        merge_method="rebase",
    )
    assert r.merge_method == "rebase"


def test_standalone_promote_request_missing_pr_url():
    """Missing pr_url raises ValidationError."""
    with pytest.raises(ValidationError):
        StandalonePromoteRequest()


# ---------------------------------------------------------------------------
# Endpoint tests — quick-fix route (synchronous, no DB records)
# ---------------------------------------------------------------------------


class MockQuickFixRunner:
    """Mock runner for quick-fix endpoint tests.

    Simulates a worker that runs synchronously, records calls,
    and returns configurable results.
    """

    def __init__(self, exit_code: int = 0, delay_s: float = 0.01):
        self.exit_code = exit_code
        self.delay_s = delay_s
        self.spawned: list[str] = []
        self.prompts: dict[str, str] = {}
        self.configs: list[RunnerConfig] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        self.prompts[config.task_id] = config.prompt
        self.configs.append(config)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        output = f"Fixed: {config.task_id}"
        return RunnerResult(
            exit_code=self.exit_code,
            stdout=json.dumps({"type": "result", "result": output}),
            stderr="" if self.exit_code == 0 else "failed",
        )

    def extract_final_output(self, stdout: str) -> str:
        for line in stdout.split("\n"):
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    return msg.get("result", "")
            except (json.JSONDecodeError, KeyError):
                continue
        return stdout


# Verify protocol compliance at import time
assert isinstance(MockQuickFixRunner(), AgentRunner)


async def _setup_project_with_repo(client: AsyncClient, tmp_path: Path) -> str:
    """Create project + repo dir. Returns project_id."""
    r = await client.post("/api/v1/projects", json={"name": "qf-test"})
    assert r.status_code == 201
    pid = r.json()["id"]
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    await client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"path": str(repo_dir)},
    )
    return pid


@pytest.mark.asyncio
async def test_quickfix_endpoint_success(client: AsyncClient, tmp_path: Path):
    """Happy path: worker succeeds, response has success=True and worker output."""
    pid = await _setup_project_with_repo(client, tmp_path)
    runner = MockQuickFixRunner(exit_code=0)

    with (
        patch("wave_server.routes.quick_fix.get_runner", return_value=runner),
        patch("wave_server.routes.quick_fix.is_git_repo", return_value=False),
    ):
        r = await client.post(
            f"/api/v1/projects/{pid}/quick-fix",
            json={
                "prompt": "fix it",
                "branch": "fix/1",
                "pr_title": "Fix bug",
            },
        )

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "Fixed:" in data["worker_output"]
    assert data["execution_time_ms"] > 0
    assert data["branch"] == "fix/1"
    assert data["error"] is None

    # Verify runner was actually called with the prompt text
    assert len(runner.spawned) == 1
    prompt_sent = list(runner.prompts.values())[0]
    assert "fix it" in prompt_sent


@pytest.mark.asyncio
async def test_quickfix_endpoint_worker_failure(client: AsyncClient, tmp_path: Path):
    """Worker exits non-zero: endpoint returns 200 but success=False with error."""
    pid = await _setup_project_with_repo(client, tmp_path)
    runner = MockQuickFixRunner(exit_code=1)

    with (
        patch("wave_server.routes.quick_fix.get_runner", return_value=runner),
        patch("wave_server.routes.quick_fix.is_git_repo", return_value=False),
    ):
        r = await client.post(
            f"/api/v1/projects/{pid}/quick-fix",
            json={
                "prompt": "fix it",
                "branch": "fix/fail",
                "pr_title": "Fix bug",
            },
        )

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert data["error"] is not None
    assert data["pr_url"] is None
    assert data["pr_number"] is None


@pytest.mark.asyncio
async def test_quickfix_endpoint_missing_project(client: AsyncClient):
    """POST to a non-existent project returns 404."""
    r = await client.post(
        "/api/v1/projects/nonexistent/quick-fix",
        json={
            "prompt": "fix it",
            "branch": "fix/1",
            "pr_title": "Fix",
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_quickfix_endpoint_missing_repo(client: AsyncClient):
    """Project exists but has no repo registered — returns 422."""
    r = await client.post("/api/v1/projects", json={"name": "no-repo-proj"})
    pid = r.json()["id"]

    r = await client.post(
        f"/api/v1/projects/{pid}/quick-fix",
        json={
            "prompt": "fix it",
            "branch": "fix/1",
            "pr_title": "Fix",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_quickfix_endpoint_prompt_includes_files(
    client: AsyncClient, tmp_path: Path
):
    """When files are specified, they appear in the prompt sent to the runner."""
    pid = await _setup_project_with_repo(client, tmp_path)
    runner = MockQuickFixRunner(exit_code=0)

    with (
        patch("wave_server.routes.quick_fix.get_runner", return_value=runner),
        patch("wave_server.routes.quick_fix.is_git_repo", return_value=False),
    ):
        r = await client.post(
            f"/api/v1/projects/{pid}/quick-fix",
            json={
                "prompt": "fix the dark mode bug",
                "branch": "fix/dark-mode",
                "pr_title": "Fix dark mode",
                "files": ["src/foo.py"],
            },
        )

    assert r.status_code == 200
    assert r.json()["success"] is True

    # The files hint must appear in the prompt sent to the runner
    assert len(runner.prompts) == 1
    prompt_sent = list(runner.prompts.values())[0]
    assert "src/foo.py" in prompt_sent


@pytest.mark.asyncio
async def test_quickfix_endpoint_custom_model(client: AsyncClient, tmp_path: Path):
    """When model is specified, it is passed through to the runner config."""
    pid = await _setup_project_with_repo(client, tmp_path)
    runner = MockQuickFixRunner(exit_code=0)

    with (
        patch("wave_server.routes.quick_fix.get_runner", return_value=runner),
        patch("wave_server.routes.quick_fix.is_git_repo", return_value=False),
    ):
        r = await client.post(
            f"/api/v1/projects/{pid}/quick-fix",
            json={
                "prompt": "fix it",
                "branch": "fix/model-test",
                "pr_title": "Fix with custom model",
                "model": "claude-sonnet-4-5",
            },
        )

    assert r.status_code == 200
    assert r.json()["success"] is True

    # Verify the runner config received the custom model
    assert len(runner.configs) == 1
    assert runner.configs[0].model == "claude-sonnet-4-5"
