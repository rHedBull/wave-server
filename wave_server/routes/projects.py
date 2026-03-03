import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.models import Project, Sequence, Execution, Event, Command
from wave_server.schemas import ProjectCreate, ProjectResponse, ProjectUpdate

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
