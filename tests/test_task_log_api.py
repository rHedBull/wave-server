"""Tests for task log API endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server import storage
from wave_server.config import settings
from wave_server.models import Execution, Sequence


async def _create_execution(db: AsyncSession) -> tuple[str, str]:
    """Create a sequence + execution in the DB, return (sequence_id, execution_id)."""
    from wave_server.models import Project

    project = Project(name="test-proj")
    db.add(project)
    await db.flush()
    seq = Sequence(project_id=project.id, name="test-seq")
    db.add(seq)
    await db.flush()
    exc = Execution(sequence_id=seq.id, runtime="mock")
    db.add(exc)
    await db.commit()
    await db.refresh(exc)
    return seq.id, exc.id


SAMPLE_LOG = """\
# ✅ 🔨 t1: Implement feature

- **Agent**: worker
- **Status**: passed

## Execution Trace

### Turn 1

I'll implement the JWT authentication service.

**→ Bash**
```
pytest tests/test_auth.py
```

## Final Output

Feature implemented.
"""

SAMPLE_LOG_2 = """\
# ❌ 🧪 t2: Test feature

- **Agent**: test-writer
- **Status**: failed

## Execution Trace

**→ Bash**
```
pytest tests/ -v
```

**← result (Bash) ❌ ERROR**
```
FAILED test_feature.py::test_main - AssertionError
```

## Final Output

Tests failed.
"""


@pytest.mark.asyncio
async def test_list_task_logs_empty(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    r = await client.get(f"/api/v1/executions/{eid}/task-logs")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_task_logs(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    storage.write_task_log(eid, "t1", SAMPLE_LOG, "worker")
    storage.write_task_log(eid, "t2", SAMPLE_LOG_2, "test-writer")
    r = await client.get(f"/api/v1/executions/{eid}/task-logs")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    ids = {d["task_id"] for d in data}
    assert ids == {"t1", "t2"}


@pytest.mark.asyncio
async def test_list_task_logs_not_found(client: AsyncClient):
    r = await client.get("/api/v1/executions/nonexistent/task-logs")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_task_log(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    storage.write_task_log(eid, "t1", SAMPLE_LOG, "worker")
    r = await client.get(f"/api/v1/executions/{eid}/task-logs/t1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "Implement feature" in r.text
    assert "JWT authentication" in r.text


@pytest.mark.asyncio
async def test_get_task_log_not_found(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    r = await client.get(f"/api/v1/executions/{eid}/task-logs/no-such-task")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_task_log_execution_not_found(client: AsyncClient):
    r = await client.get("/api/v1/executions/nonexistent/task-logs/t1")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_search_task_logs(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    storage.write_task_log(eid, "t1", SAMPLE_LOG, "worker")
    storage.write_task_log(eid, "t2", SAMPLE_LOG_2, "test-writer")
    r = await client.get(
        f"/api/v1/executions/{eid}/task-logs/search", params={"q": "JWT"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_matches"] >= 1
    assert data["total_files"] == 1
    assert data["results"][0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_search_task_logs_no_results(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    storage.write_task_log(eid, "t1", SAMPLE_LOG, "worker")
    r = await client.get(
        f"/api/v1/executions/{eid}/task-logs/search", params={"q": "nonexistent_xyz"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_matches"] == 0
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_task_logs_agent_filter(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    storage.write_task_log(eid, "t1", SAMPLE_LOG, "worker")
    storage.write_task_log(eid, "t2", SAMPLE_LOG_2, "test-writer")
    # Both logs have "Bash" but filter to test-writer only
    r = await client.get(
        f"/api/v1/executions/{eid}/task-logs/search",
        params={"q": "Bash", "agent": "test-writer"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_files"] == 1
    assert data["results"][0]["agent"] == "test-writer"


@pytest.mark.asyncio
async def test_search_task_logs_missing_query(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)
    r = await client.get(f"/api/v1/executions/{eid}/task-logs/search")
    assert r.status_code == 422  # validation error — q is required


@pytest.mark.asyncio
async def test_search_task_logs_execution_not_found(client: AsyncClient):
    r = await client.get(
        "/api/v1/executions/nonexistent/task-logs/search",
        params={"q": "test"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_tasks_endpoint_has_task_log_flag(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
):
    """The /tasks endpoint should include has_task_log in its response."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, eid = await _create_execution(db_session)

    # Create a task event so the tasks endpoint returns something
    from wave_server.models import Event
    import json

    event = Event(
        execution_id=eid,
        event_type="task_started",
        task_id="t1",
        phase="foundation",
        payload=json.dumps({"task_id": "t1", "title": "Test task"}),
    )
    db_session.add(event)
    await db_session.commit()

    # No log yet
    r = await client.get(f"/api/v1/executions/{eid}/tasks")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    assert tasks[0]["has_task_log"] is False

    # Write a log
    storage.write_task_log(eid, "t1", "# log", "worker")
    r = await client.get(f"/api/v1/executions/{eid}/tasks")
    tasks = r.json()
    assert tasks[0]["has_task_log"] is True
