from fastapi import APIRouter

from wave_server import __version__
from wave_server.config import settings
from wave_server.engine.execution_manager import get_active_count

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "active_executions": get_active_count(),
        "github_configured": settings.github_token is not None,
    }
