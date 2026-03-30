from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.engine.plan_parser import parse_plan
from wave_server.models import Command, Event, Execution, Project, Sequence
from wave_server.schemas import SequenceCreate, SequenceResponse, SequenceUpdate
from wave_server import storage

router = APIRouter()


# Legacy status values from before the vocabulary unification.
_LEGACY_STATUS_MAP = {
    "executing": "running",
    "drafting": "pending",
    "queued": "pending",
}


def _normalize_status(status: str) -> str:
    return _LEGACY_STATUS_MAP.get(status, status)


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


async def _enrich_sequence_status(db: AsyncSession, seq: Sequence) -> Sequence:
    """Derive the effective sequence status from the latest execution.

    Handles two cases:
    1. status == 'pending' but an execution has already started → adopt its status.
    2. status == 'running' but no execution is actually active → fall back to
       the latest execution's terminal status.

    Also normalises legacy status values (e.g. 'executing' → 'running').

    Modifies `seq.status` in-place for the response but expunges the object
    so the change is NOT persisted.
    """
    # Normalise legacy values first
    normalised = _normalize_status(seq.status)
    needs_expunge = normalised != seq.status
    if needs_expunge:
        db.expunge(seq)
        seq.status = normalised

    # Terminal statuses are authoritative — nothing to reconcile.
    if seq.status in _TERMINAL_STATUSES:
        return seq

    # For 'pending' or 'running', verify against the latest execution.
    result = await db.execute(
        select(Execution.status)
        .where(Execution.sequence_id == seq.id)
        .order_by(Execution.created_at.desc())
        .limit(1)
    )
    latest_status = result.scalar_one_or_none()

    if latest_status is None:
        # No executions at all — should be pending.
        effective = "pending"
    else:
        effective = _normalize_status(latest_status)

    if effective != seq.status:
        if not needs_expunge:
            try:
                db.expunge(seq)
            except Exception:
                pass
        seq.status = effective
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
    seq = Sequence(project_id=project_id, name=body.name, description=body.description)
    db.add(seq)
    await db.commit()
    await db.refresh(seq)
    return seq


@router.get("/projects/{project_id}/sequences", response_model=list[SequenceResponse])
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
        await db.execute(Event.__table__.delete().where(Event.execution_id == exec_id))
        await db.execute(
            Command.__table__.delete().where(Command.execution_id == exec_id)
        )
        await db.execute(Execution.__table__.delete().where(Execution.id == exec_id))
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


# --- Plan Graph ---


@router.get("/sequences/{sequence_id}/plan-graph")
async def get_plan_graph(sequence_id: str, db: AsyncSession = Depends(get_db)):
    """Return the parsed plan as a structured JSON graph for visualization."""
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    content = storage.read_plan(sequence_id)
    if content is None:
        raise HTTPException(404, "Plan not found")

    plan = parse_plan(content)

    def task_to_dict(t):
        return {
            "id": t.id,
            "title": t.title,
            "agent": t.agent,
            "files": t.files,
            "depends": t.depends,
        }

    waves = []
    for i, wave in enumerate(plan.waves):
        waves.append(
            {
                "index": i,
                "name": wave.name,
                "description": wave.description or "",
                "foundation": [task_to_dict(t) for t in wave.foundation],
                "features": [
                    {
                        "name": f.name,
                        "files": f.files,
                        "tasks": [task_to_dict(t) for t in f.tasks],
                    }
                    for f in wave.features
                ],
                "integration": [task_to_dict(t) for t in wave.integration],
            }
        )

    return {"goal": plan.goal, "waves": waves}
