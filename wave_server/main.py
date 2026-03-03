from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from wave_server.config import settings
from wave_server.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="Wave Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from wave_server.routes.health import router as health_router  # noqa: E402
from wave_server.routes.projects import router as projects_router  # noqa: E402
from wave_server.routes.sequences import router as sequences_router  # noqa: E402
from wave_server.routes.executions import router as executions_router  # noqa: E402

app.include_router(health_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(sequences_router, prefix="/api")
app.include_router(executions_router, prefix="/api")
