"""Execution state persistence — tracks completed tasks for resume.

In the server context, state is stored in the database (Execution model)
and in a JSON state file for filesystem-level resume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wave_server.engine.types import ExecutionState


def create_initial_state(plan_file: str) -> ExecutionState:
    now = datetime.now(timezone.utc).isoformat()
    return ExecutionState(
        plan_file=Path(plan_file).name,
        started_at=now,
        updated_at=now,
        current_wave=0,
        task_states={},
    )


def mark_task_done(state: ExecutionState, task_id: str) -> None:
    state.task_states[task_id] = "completed"
    state.updated_at = datetime.now(timezone.utc).isoformat()


def mark_task_failed(state: ExecutionState, task_id: str) -> None:
    state.task_states[task_id] = "failed"
    state.updated_at = datetime.now(timezone.utc).isoformat()


def mark_task_skipped(state: ExecutionState, task_id: str) -> None:
    state.task_states[task_id] = "skipped"
    state.updated_at = datetime.now(timezone.utc).isoformat()


def advance_to_wave(state: ExecutionState, wave_index: int) -> None:
    state.current_wave = wave_index
    state.updated_at = datetime.now(timezone.utc).isoformat()


def completed_task_ids(state: ExecutionState) -> set[str]:
    return {tid for tid, status in state.task_states.items() if status == "completed"}


def state_to_json(state: ExecutionState) -> str:
    from dataclasses import asdict
    return json.dumps(asdict(state), indent=2)


def state_from_json(raw: str) -> ExecutionState:
    data = json.loads(raw)
    return ExecutionState(**data)
