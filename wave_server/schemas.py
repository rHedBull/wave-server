from datetime import datetime

from pydantic import BaseModel


# --- Projects ---


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    api_key: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Project Repositories ---


class ProjectRepositoryCreate(BaseModel):
    path: str
    label: str | None = None


class ProjectRepositoryResponse(BaseModel):
    id: str
    project_id: str
    path: str
    label: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Project Context Files ---


class ProjectContextFileCreate(BaseModel):
    path: str
    description: str | None = None


class ProjectContextFileResponse(BaseModel):
    id: str
    project_id: str
    path: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Sequences ---


class SequenceCreate(BaseModel):
    name: str
    description: str | None = None


class SequenceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None


class SequenceResponse(BaseModel):
    id: str
    project_id: str
    name: str
    description: str | None
    status: str
    spec_path: str | None
    plan_path: str | None
    wave_count: int | None
    task_count: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Executions ---


class ExecutionCreate(BaseModel):
    runtime: str | None = None
    concurrency: int | None = None
    timeout_ms: int | None = None
    source_branch: str | None = None
    source_sha: str | None = None
    model: str | None = None
    agent_models: dict[str, str] | None = None


class ExecutionResponse(BaseModel):
    id: str
    sequence_id: str
    continued_from: str | None = None
    status: str
    trigger: str
    runtime: str
    total_tasks: int
    completed_tasks: int
    current_wave: int
    waves_state: str | None
    config: str | None
    source_branch: str | None = None
    source_sha: str | None = None
    work_branch: str | None = None
    pr_url: str | None = None
    git_sha_before: str | None = None
    git_sha_after: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Events ---


class EventResponse(BaseModel):
    id: str
    execution_id: str
    event_type: str
    task_id: str | None
    phase: str | None
    payload: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Commands ---


class CommandResolve(BaseModel):
    action: str  # "retry" | "skip"
    message: str | None = None


class CommandResponse(BaseModel):
    id: str
    execution_id: str
    task_id: str
    action: str | None
    message: str | None
    picked_up: bool
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}
