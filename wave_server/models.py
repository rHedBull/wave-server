import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column

from wave_server.db import Base


class TZDateTime(TypeDecorator):
    """A DateTime type that ensures UTC timezone info survives SQLite round-trips.

    SQLite stores datetimes as naive strings, stripping timezone info.
    This decorator re-attaches UTC on reads so Pydantic serialises with +00:00
    and browsers can convert to local time correctly.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self):
        super().__init__(timezone=True)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str] = mapped_column(String, unique=True, default=_uuid)
    env_vars: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=_now, onupdate=_now
    )


class Sequence(Base):
    __tablename__ = "sequences"
    __table_args__ = (
        Index("ix_sequences_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    spec_path: Mapped[str | None] = mapped_column(String, nullable=True)
    plan_path: Mapped[str | None] = mapped_column(String, nullable=True)
    wave_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), default=_now, onupdate=_now
    )


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_sequence_started", "sequence_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    sequence_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    continued_from: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    trigger: Mapped[str] = mapped_column(String, default="initial")
    runtime: Mapped[str] = mapped_column(String, default="claude")
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    current_wave: Mapped[int] = mapped_column(Integer, default=0)
    waves_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    source_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    work_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    git_sha_before: Mapped[str | None] = mapped_column(String, nullable=True)
    git_sha_after: Mapped[str | None] = mapped_column(String, nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_execution_created", "execution_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    execution_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (
        Index(
            "ix_commands_pending",
            "execution_id",
            "task_id",
            "picked_up",
            sqlite_where=text("NOT picked_up"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    execution_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    picked_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class ProjectRepository(Base):
    __tablename__ = "project_repositories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    path: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)


class ProjectContextFile(Base):
    __tablename__ = "project_context_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    path: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_now)
