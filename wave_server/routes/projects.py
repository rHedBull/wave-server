import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.models import (
    Command,
    Event,
    Execution,
    Project,
    ProjectContextFile,
    ProjectRepository,
    Sequence,
)
from wave_server.schemas import (
    ProjectContextFileCreate,
    ProjectContextFileResponse,
    ProjectCreate,
    ProjectRepositoryCreate,
    ProjectRepositoryResponse,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter()


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(name=body.name, description=body.description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return result.scalars().all()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str, body: ProjectUpdate, db: AsyncSession = Depends(get_db)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    await db.commit()
    await db.refresh(project)
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    # Cascade: delete repositories and context files
    await db.execute(
        ProjectRepository.__table__.delete().where(
            ProjectRepository.project_id == project_id
        )
    )
    await db.execute(
        ProjectContextFile.__table__.delete().where(
            ProjectContextFile.project_id == project_id
        )
    )
    # Cascade: delete sequences -> executions -> events/commands
    seqs = await db.execute(
        select(Sequence).where(Sequence.project_id == project_id)
    )
    for seq in seqs.scalars().all():
        execs = await db.execute(
            select(Execution).where(Execution.sequence_id == seq.id)
        )
        for exc in execs.scalars().all():
            await db.execute(
                Event.__table__.delete().where(Event.execution_id == exc.id)
            )
            await db.execute(
                Command.__table__.delete().where(Command.execution_id == exc.id)
            )
            await db.delete(exc)
        await db.delete(seq)
    await db.delete(project)
    await db.commit()


@router.post(
    "/projects/{project_id}/regenerate-key", response_model=ProjectResponse
)
async def regenerate_key(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    project.api_key = str(uuid.uuid4())
    await db.commit()
    await db.refresh(project)
    return project


# --- Repositories ---


@router.get(
    "/projects/{project_id}/repositories",
    response_model=list[ProjectRepositoryResponse],
)
async def list_repositories(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProjectRepository)
        .where(ProjectRepository.project_id == project_id)
        .order_by(ProjectRepository.created_at)
    )
    return result.scalars().all()


@router.post(
    "/projects/{project_id}/repositories",
    response_model=ProjectRepositoryResponse,
    status_code=201,
)
async def add_repository(
    project_id: str,
    body: ProjectRepositoryCreate,
    db: AsyncSession = Depends(get_db),
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    # Accept remote URLs (https://..., git@...) or local paths
    from wave_server.engine.repo_cache import is_repo_url
    if is_repo_url(body.path):
        repo_path_str = body.path
    else:
        # Validate local path exists and is a directory
        repo_path = Path(body.path).expanduser().resolve()
        if not repo_path.is_dir():
            raise HTTPException(400, f"Path does not exist or is not a directory: {repo_path}")
        repo_path_str = str(repo_path)
    repo = ProjectRepository(
        project_id=project_id, path=repo_path_str, label=body.label
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


@router.delete(
    "/projects/{project_id}/repositories/{repo_id}", status_code=204
)
async def delete_repository(
    project_id: str, repo_id: str, db: AsyncSession = Depends(get_db)
):
    repo = await db.get(ProjectRepository, repo_id)
    if not repo or repo.project_id != project_id:
        raise HTTPException(404, "Repository not found")
    await db.delete(repo)
    await db.commit()


# --- Context Files ---


@router.get(
    "/projects/{project_id}/context-files",
    response_model=list[ProjectContextFileResponse],
)
async def list_context_files(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProjectContextFile)
        .where(ProjectContextFile.project_id == project_id)
        .order_by(ProjectContextFile.created_at)
    )
    return result.scalars().all()


@router.post(
    "/projects/{project_id}/context-files",
    response_model=ProjectContextFileResponse,
    status_code=201,
)
async def add_context_file(
    project_id: str,
    body: ProjectContextFileCreate,
    db: AsyncSession = Depends(get_db),
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    cf = ProjectContextFile(
        project_id=project_id, path=body.path, description=body.description
    )
    db.add(cf)
    await db.commit()
    await db.refresh(cf)
    return cf


@router.delete(
    "/projects/{project_id}/context-files/{file_id}", status_code=204
)
async def delete_context_file(
    project_id: str, file_id: str, db: AsyncSession = Depends(get_db)
):
    cf = await db.get(ProjectContextFile, file_id)
    if not cf or cf.project_id != project_id:
        raise HTTPException(404, "Context file not found")
    await db.delete(cf)
    await db.commit()


# --- Environment Variables ---


def _get_env(project: Project) -> dict[str, str]:
    """Parse env_vars JSON from project. Returns empty dict if unset."""
    if not project.env_vars:
        return {}
    try:
        return json.loads(project.env_vars)
    except (json.JSONDecodeError, TypeError):
        return {}


def _set_env(project: Project, env: dict[str, str]) -> None:
    """Serialize env dict to project. Clears column if empty."""
    project.env_vars = json.dumps(env) if env else None


@router.put("/projects/{project_id}/env", status_code=200)
async def set_env_vars(
    project_id: str,
    body: dict[str, str],
    db: AsyncSession = Depends(get_db),
):
    """Set environment variables (merge with existing). Values are stored, keys returned."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    env = _get_env(project)
    env.update(body)
    _set_env(project, env)
    await db.commit()
    return {"keys": sorted(env.keys()), "count": len(env)}


@router.get("/projects/{project_id}/env")
async def list_env_vars(project_id: str, db: AsyncSession = Depends(get_db)):
    """List environment variable keys (values are never exposed via API)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    env = _get_env(project)
    return {"keys": sorted(env.keys()), "count": len(env)}


@router.delete("/projects/{project_id}/env/{key}", status_code=204)
async def delete_env_var(
    project_id: str, key: str, db: AsyncSession = Depends(get_db)
):
    """Remove a single environment variable."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    env = _get_env(project)
    if key not in env:
        raise HTTPException(404, f"Environment variable '{key}' not found")
    del env[key]
    _set_env(project, env)
    await db.commit()


@router.delete("/projects/{project_id}/env", status_code=204)
async def clear_env_vars(project_id: str, db: AsyncSession = Depends(get_db)):
    """Remove all environment variables."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    _set_env(project, {})
    await db.commit()
