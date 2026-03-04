from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.models import Command, Event, Execution, Project, Sequence
from wave_server.schemas import SequenceCreate, SequenceResponse, SequenceUpdate
from wave_server import storage

router = APIRouter()


async def _enrich_sequence_status(db: AsyncSession, seq: Sequence) -> Sequence:
    """Derive effective status from latest execution when DB still says 'drafting'.

    Modifies `seq.status` in-place for the response but expunges the object
    so the change is NOT persisted back to the database.
    """
    if seq.status != "drafting":
        return seq
    result = await db.execute(
        select(Execution.status)
        .where(Execution.sequence_id == seq.id)
        .order_by(Execution.created_at.desc())
        .limit(1)
    )
    latest_status = result.scalar_one_or_none()
    if latest_status:
        db.expunge(seq)
        seq.status = "executing" if latest_status == "running" else latest_status
    return seq


@router.post(
    "/projects/{project_id}/sequences",
    response_model=SequenceResponse,
    status_code=201,
)
async def create_sequence(
    project_id: str, body: SequenceCreate, db: AsyncSession = Depends(get_db)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    seq = Sequence(
        project_id=project_id, name=body.name, description=body.description
    )
    db.add(seq)
    await db.commit()
    await db.refresh(seq)
    return seq


@router.get(
    "/projects/{project_id}/sequences", response_model=list[SequenceResponse]
)
async def list_sequences(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Sequence)
        .where(Sequence.project_id == project_id)
        .order_by(Sequence.created_at.desc())
    )
    seqs = result.scalars().all()
    for s in seqs:
        await _enrich_sequence_status(db, s)
    return seqs


@router.get("/sequences/{sequence_id}", response_model=SequenceResponse)
async def get_sequence(sequence_id: str, db: AsyncSession = Depends(get_db)):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    await _enrich_sequence_status(db, seq)
    return seq


@router.patch("/sequences/{sequence_id}", response_model=SequenceResponse)
async def update_sequence(
    sequence_id: str, body: SequenceUpdate, db: AsyncSession = Depends(get_db)
):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    if body.name is not None:
        seq.name = body.name
    if body.description is not None:
        seq.description = body.description
    if body.status is not None:
        seq.status = body.status
    await db.commit()
    await db.refresh(seq)
    return seq


@router.delete("/sequences/{sequence_id}", status_code=204)
async def delete_sequence(sequence_id: str, db: AsyncSession = Depends(get_db)):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    # Cascade: delete executions -> events/commands
    exec_ids = await db.execute(
        select(Execution.id).where(Execution.sequence_id == sequence_id)
    )
    for (exec_id,) in exec_ids.all():
        await db.execute(
            Event.__table__.delete().where(Event.execution_id == exec_id)
        )
        await db.execute(
            Command.__table__.delete().where(Command.execution_id == exec_id)
        )
        await db.execute(
            Execution.__table__.delete().where(Execution.id == exec_id)
        )
    await db.delete(seq)
    await db.commit()


# --- Spec ---


@router.post("/sequences/{sequence_id}/spec", status_code=204)
async def upload_spec(
    sequence_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    content = (await request.body()).decode("utf-8")
    path = storage.write_spec(sequence_id, content)
    seq.spec_path = str(path)
    await db.commit()


@router.get("/sequences/{sequence_id}/spec")
async def get_spec(sequence_id: str, db: AsyncSession = Depends(get_db)):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    content = storage.read_spec(sequence_id)
    if content is None:
        raise HTTPException(404, "Spec not found")
    return PlainTextResponse(content)


# --- Plan ---


@router.post("/sequences/{sequence_id}/plan", status_code=204)
async def upload_plan(
    sequence_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    content = (await request.body()).decode("utf-8")
    path = storage.write_plan(sequence_id, content)
    seq.plan_path = str(path)
    await db.commit()


@router.get("/sequences/{sequence_id}/plan")
async def get_plan(sequence_id: str, db: AsyncSession = Depends(get_db)):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    content = storage.read_plan(sequence_id)
    if content is None:
        raise HTTPException(404, "Plan not found")
    return PlainTextResponse(content)
