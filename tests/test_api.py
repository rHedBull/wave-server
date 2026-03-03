import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- Projects ---


@pytest.mark.asyncio
async def test_create_project(client: AsyncClient):
    r = await client.post("/api/projects", json={"name": "test-project"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "test-project"
    assert "id" in data
    assert "api_key" in data


@pytest.mark.asyncio
async def test_list_projects(client: AsyncClient):
    await client.post("/api/projects", json={"name": "p1"})
    await client.post("/api/projects", json={"name": "p2"})
    r = await client.get("/api/projects")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_get_project(client: AsyncClient):
    create = await client.post("/api/projects", json={"name": "my-proj"})
    pid = create.json()["id"]
    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "my-proj"


@pytest.mark.asyncio
async def test_get_project_not_found(client: AsyncClient):
    r = await client.get("/api/projects/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_project(client: AsyncClient):
    create = await client.post("/api/projects", json={"name": "old-name"})
    pid = create.json()["id"]
    r = await client.patch(f"/api/projects/{pid}", json={"name": "new-name"})
    assert r.status_code == 200
    assert r.json()["name"] == "new-name"


@pytest.mark.asyncio
async def test_delete_project(client: AsyncClient):
    create = await client.post("/api/projects", json={"name": "doomed"})
    pid = create.json()["id"]
    r = await client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204
    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_regenerate_key(client: AsyncClient):
    create = await client.post("/api/projects", json={"name": "keyed"})
    pid = create.json()["id"]
    old_key = create.json()["api_key"]
    r = await client.post(f"/api/projects/{pid}/regenerate-key")
    assert r.status_code == 200
    assert r.json()["api_key"] != old_key


# --- Sequences ---


@pytest.mark.asyncio
async def test_create_sequence(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    r = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "add-auth"}
    )
    assert r.status_code == 201
    assert r.json()["name"] == "add-auth"
    assert r.json()["status"] == "drafting"


@pytest.mark.asyncio
async def test_list_sequences(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    await client.post(f"/api/projects/{pid}/sequences", json={"name": "s1"})
    await client.post(f"/api/projects/{pid}/sequences", json={"name": "s2"})
    r = await client.get(f"/api/projects/{pid}/sequences")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_get_sequence(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "my-seq"}
    )
    sid = seq.json()["id"]
    r = await client.get(f"/api/sequences/{sid}")
    assert r.status_code == 200
    assert r.json()["name"] == "my-seq"


@pytest.mark.asyncio
async def test_update_sequence(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "old"}
    )
    sid = seq.json()["id"]
    r = await client.patch(f"/api/sequences/{sid}", json={"status": "planned"})
    assert r.status_code == 200
    assert r.json()["status"] == "planned"


# --- Spec/Plan ---


@pytest.mark.asyncio
async def test_spec_upload_and_get(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    r = await client.post(
        f"/api/sequences/{sid}/spec",
        content="# My Spec\n\nDetails here.",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/sequences/{sid}/spec")
    assert r.status_code == 200
    assert "My Spec" in r.text


@pytest.mark.asyncio
async def test_spec_not_found(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    r = await client.get(f"/api/sequences/{sid}/spec")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_plan_upload_and_get(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    r = await client.post(
        f"/api/sequences/{sid}/plan",
        content="# Plan\n\n## Wave 1",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/sequences/{sid}/plan")
    assert r.status_code == 200
    assert "Wave 1" in r.text


# --- Executions ---


@pytest.mark.asyncio
async def test_create_execution(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    r = await client.post(f"/api/sequences/{sid}/executions", json={})
    assert r.status_code == 201
    assert r.json()["status"] == "queued"
    assert r.json()["runtime"] == "claude"


@pytest.mark.asyncio
async def test_cancel_execution(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "proj"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    exc = await client.post(f"/api/sequences/{sid}/executions", json={})
    eid = exc.json()["id"]
    r = await client.post(f"/api/executions/{eid}/cancel")
    assert r.status_code == 204
    r = await client.get(f"/api/executions/{eid}")
    assert r.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_delete_project_cascades(client: AsyncClient):
    proj = await client.post("/api/projects", json={"name": "cascade"})
    pid = proj.json()["id"]
    seq = await client.post(
        f"/api/projects/{pid}/sequences", json={"name": "s"}
    )
    sid = seq.json()["id"]
    await client.post(f"/api/sequences/{sid}/executions", json={})
    r = await client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204
    r = await client.get(f"/api/sequences/{sid}")
    assert r.status_code == 404
