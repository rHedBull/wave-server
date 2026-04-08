import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.models import Command, Event


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- Projects ---


@pytest.mark.asyncio
async def test_create_project(client: AsyncClient):
    r = await client.post("/api/v1/projects", json={"name": "test-project"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "test-project"
    assert "id" in data
    assert "api_key" in data


@pytest.mark.asyncio
async def test_list_projects(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "p1"})
    await client.post("/api/v1/projects", json={"name": "p2"})
    r = await client.get("/api/v1/projects")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_get_project(client: AsyncClient):
    create = await client.post("/api/v1/projects", json={"name": "my-proj"})
    pid = create.json()["id"]
    r = await client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "my-proj"


@pytest.mark.asyncio
async def test_get_project_not_found(client: AsyncClient):
    r = await client.get("/api/v1/projects/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_project(client: AsyncClient):
    create = await client.post("/api/v1/projects", json={"name": "old-name"})
    pid = create.json()["id"]
    r = await client.patch(f"/api/v1/projects/{pid}", json={"name": "new-name"})
    assert r.status_code == 200
    assert r.json()["name"] == "new-name"


@pytest.mark.asyncio
async def test_delete_project(client: AsyncClient):
    create = await client.post("/api/v1/projects", json={"name": "doomed"})
    pid = create.json()["id"]
    r = await client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_regenerate_key(client: AsyncClient):
    create = await client.post("/api/v1/projects", json={"name": "keyed"})
    pid = create.json()["id"]
    old_key = create.json()["api_key"]
    r = await client.post(f"/api/v1/projects/{pid}/regenerate-key")
    assert r.status_code == 200
    assert r.json()["api_key"] != old_key


# --- Sequences ---


@pytest.mark.asyncio
async def test_create_sequence(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/sequences", json={"name": "add-auth"}
    )
    assert r.status_code == 201
    assert r.json()["name"] == "add-auth"
    assert r.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_list_sequences(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s1"})
    await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s2"})
    r = await client.get(f"/api/v1/projects/{pid}/sequences")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_get_sequence(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/v1/projects/{pid}/sequences", json={"name": "my-seq"}
    )
    sid = seq.json()["id"]
    r = await client.get(f"/api/v1/sequences/{sid}")
    assert r.status_code == 200
    assert r.json()["name"] == "my-seq"


@pytest.mark.asyncio
async def test_update_sequence(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "old"})
    sid = seq.json()["id"]
    r = await client.patch(f"/api/v1/sequences/{sid}", json={"status": "planned"})
    assert r.status_code == 200
    assert r.json()["status"] == "planned"


@pytest.mark.asyncio
async def test_update_sequence_name_and_description(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/v1/projects/{pid}/sequences", json={"name": "old-name"}
    )
    sid = seq.json()["id"]
    r = await client.patch(
        f"/api/v1/sequences/{sid}",
        json={"name": "new-name", "description": "updated desc"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "new-name"
    assert r.json()["description"] == "updated desc"


@pytest.mark.asyncio
async def test_delete_sequence(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/v1/projects/{pid}/sequences", json={"name": "doomed"}
    )
    sid = seq.json()["id"]
    r = await client.delete(f"/api/v1/sequences/{sid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/sequences/{sid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_sequence_not_found(client: AsyncClient):
    r = await client.delete("/api/v1/sequences/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_sequence_cascades(client: AsyncClient, ready_sequence):
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    r = await client.delete(f"/api/v1/sequences/{sid}")
    assert r.status_code == 204
    # Sequence gone
    r = await client.get(f"/api/v1/sequences/{sid}")
    assert r.status_code == 404
    # Execution gone too
    r = await client.get(f"/api/v1/executions/{eid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_sequence_cascades_events_and_commands(
    client: AsyncClient, db_session: AsyncSession, ready_sequence
):
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    # Insert events and commands directly via DB
    event = Event(execution_id=eid, event_type="task.start", task_id="t1", payload="{}")
    cmd = Command(execution_id=eid, task_id="t1", action="run", message="do it")
    db_session.add(event)
    db_session.add(cmd)
    await db_session.commit()
    event_id = event.id
    cmd_id = cmd.id
    # Delete the sequence
    r = await client.delete(f"/api/v1/sequences/{sid}")
    assert r.status_code == 204
    # Verify events and commands are gone
    from sqlalchemy import select
    from wave_server.models import Event as EventModel, Command as CommandModel

    result = await db_session.execute(
        select(EventModel).where(EventModel.id == event_id)
    )
    assert result.scalar_one_or_none() is None
    result = await db_session.execute(
        select(CommandModel).where(CommandModel.id == cmd_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_sequence_preserves_project(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    r = await client.delete(f"/api/v1/sequences/{sid}")
    assert r.status_code == 204
    # Project still exists
    r = await client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "proj"


@pytest.mark.asyncio
async def test_update_sequence_partial_name_only(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/v1/projects/{pid}/sequences",
        json={"name": "orig", "description": "keep this"},
    )
    sid = seq.json()["id"]
    r = await client.patch(f"/api/v1/sequences/{sid}", json={"name": "renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"
    assert r.json()["description"] == "keep this"


@pytest.mark.asyncio
async def test_update_sequence_partial_description_only(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/v1/projects/{pid}/sequences", json={"name": "keep-name"}
    )
    sid = seq.json()["id"]
    r = await client.patch(f"/api/v1/sequences/{sid}", json={"description": "new desc"})
    assert r.status_code == 200
    assert r.json()["name"] == "keep-name"
    assert r.json()["description"] == "new desc"


# --- Repositories ---


@pytest.mark.asyncio
async def test_add_repository(client: AsyncClient, tmp_path):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    r = await client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"path": str(repo_dir), "label": "main"},
    )
    assert r.status_code == 201
    assert r.json()["path"] == str(repo_dir)
    assert r.json()["label"] == "main"


@pytest.mark.asyncio
async def test_list_repositories(client: AsyncClient, tmp_path):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    d1 = tmp_path / "r1"
    d1.mkdir()
    d2 = tmp_path / "r2"
    d2.mkdir()
    await client.post(f"/api/v1/projects/{pid}/repositories", json={"path": str(d1)})
    await client.post(f"/api/v1/projects/{pid}/repositories", json={"path": str(d2)})
    r = await client.get(f"/api/v1/projects/{pid}/repositories")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_delete_repository(client: AsyncClient, tmp_path):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    d = tmp_path / "repo"
    d.mkdir()
    repo = await client.post(
        f"/api/v1/projects/{pid}/repositories", json={"path": str(d)}
    )
    rid = repo.json()["id"]
    r = await client.delete(f"/api/v1/projects/{pid}/repositories/{rid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/projects/{pid}/repositories")
    assert len(r.json()) == 0


@pytest.mark.asyncio
async def test_delete_repository_not_found(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    r = await client.delete(f"/api/v1/projects/{pid}/repositories/nonexistent")
    assert r.status_code == 404


# --- Context Files ---


@pytest.mark.asyncio
async def test_add_context_file(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/context-files",
        json={"path": "/docs/spec.md", "description": "Main spec"},
    )
    assert r.status_code == 201
    assert r.json()["path"] == "/docs/spec.md"
    assert r.json()["description"] == "Main spec"


@pytest.mark.asyncio
async def test_list_context_files(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    await client.post(f"/api/v1/projects/{pid}/context-files", json={"path": "/f1"})
    await client.post(f"/api/v1/projects/{pid}/context-files", json={"path": "/f2"})
    r = await client.get(f"/api/v1/projects/{pid}/context-files")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_delete_context_file(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    cf = await client.post(f"/api/v1/projects/{pid}/context-files", json={"path": "/f"})
    fid = cf.json()["id"]
    r = await client.delete(f"/api/v1/projects/{pid}/context-files/{fid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/projects/{pid}/context-files")
    assert len(r.json()) == 0


@pytest.mark.asyncio
async def test_delete_context_file_not_found(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    r = await client.delete(f"/api/v1/projects/{pid}/context-files/nonexistent")
    assert r.status_code == 404


# --- Spec/Plan ---


@pytest.mark.asyncio
async def test_spec_upload_and_get(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    r = await client.post(
        f"/api/v1/sequences/{sid}/spec",
        content="# My Spec\n\nDetails here.",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/v1/sequences/{sid}/spec")
    assert r.status_code == 200
    assert "My Spec" in r.text


@pytest.mark.asyncio
async def test_spec_not_found(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    r = await client.get(f"/api/v1/sequences/{sid}/spec")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_plan_upload_and_get(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    r = await client.post(
        f"/api/v1/sequences/{sid}/plan",
        content="# Plan\n\n## Wave 1",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/v1/sequences/{sid}/plan")
    assert r.status_code == 200
    assert "Wave 1" in r.text


# --- Executions ---


@pytest.mark.asyncio
async def test_create_execution(client: AsyncClient, ready_sequence):
    sid = ready_sequence["sequence_id"]
    r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    assert r.json()["runtime"] == "claude"


@pytest.mark.asyncio
async def test_cancel_execution(client: AsyncClient, ready_sequence):
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    r = await client.post(f"/api/v1/executions/{eid}/cancel")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/executions/{eid}")
    assert r.json()["status"] == "cancelled"


# --- Rerun ---


@pytest.mark.asyncio
async def test_rerun_execution(client: AsyncClient, ready_sequence):
    """Rerun creates a new execution with trigger='rerun'."""
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    # Cancel it so it's in a terminal state
    await client.post(f"/api/v1/executions/{eid}/cancel")
    r = await client.post(
        f"/api/v1/executions/{eid}/rerun",
        json={"task_ids": ["1a"], "cascade": True},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["trigger"] == "rerun"
    assert data["continued_from"] == eid


@pytest.mark.asyncio
async def test_rerun_running_execution_rejected(client: AsyncClient, ready_sequence):
    """Cannot rerun an execution that is still running."""
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    # Don't cancel — it's pending/running
    r = await client.post(
        f"/api/v1/executions/{eid}/rerun",
        json={"task_ids": ["1a"]},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_rerun_unknown_task_ids(client: AsyncClient, ready_sequence):
    """Unknown task IDs are rejected with 422."""
    sid = ready_sequence["sequence_id"]
    exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    await client.post(f"/api/v1/executions/{eid}/cancel")
    r = await client.post(
        f"/api/v1/executions/{eid}/rerun",
        json={"task_ids": ["nonexistent-task"]},
    )
    assert r.status_code == 422
    assert "nonexistent-task" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rerun_not_found(client: AsyncClient):
    """Rerun on non-existent execution returns 404."""
    r = await client.post(
        "/api/v1/executions/no-such-id/rerun",
        json={"task_ids": ["t1"]},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_project_cascades(client: AsyncClient):
    proj = await client.post("/api/v1/projects", json={"name": "cascade"})
    pid = proj.json()["id"]
    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "s"})
    sid = seq.json()["id"]
    await client.post(f"/api/v1/sequences/{sid}/executions", json={})
    r = await client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/sequences/{sid}")
    assert r.status_code == 404
