from fastapi import APIRouter

from wave_server.engine.execution_manager import get_active_count

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "active_executions": get_active_count()}
