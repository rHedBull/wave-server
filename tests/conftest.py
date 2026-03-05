import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from wave_server.db import Base, get_db
from wave_server.main import app

# Minimal valid plan used by tests that need a launchable sequence
MINIMAL_PLAN = """\
# Plan

## Wave 1: Setup

### Task 1a: Do something
- **Agent**: worker
- **Files**: `src/index.ts`
- **Depends**: (none)
- **Description**: Does something useful.
"""


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_network():
    """Prevent real network calls in all tests. Override per-test to simulate failure."""
    with patch("wave_server.routes.executions._check_network", return_value=True):
        yield


@pytest_asyncio.fixture
async def ready_sequence(client: AsyncClient, tmp_path: Path):
    """Project + sequence + valid plan + repo directory — ready to launch an execution."""
    proj = await client.post("/api/v1/projects", json={"name": "test-proj"})
    pid = proj.json()["id"]

    seq = await client.post(f"/api/v1/projects/{pid}/sequences", json={"name": "test-seq"})
    sid = seq.json()["id"]

    await client.post(
        f"/api/v1/sequences/{sid}/plan",
        content=MINIMAL_PLAN,
        headers={"content-type": "text/plain"},
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"path": str(repo_dir)},
    )

    return {"project_id": pid, "sequence_id": sid, "repo_dir": repo_dir}
