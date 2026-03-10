# Architecture

## System overview

Wave Server is a FastAPI application that orchestrates multi-agent code execution. Clients submit plans (structured markdown), the server parses them into a DAG, spawns Claude Code subprocesses to execute tasks, and records progress in SQLite.

```
Clients                         Server (localhost:9718)            Workers
-------                         ----------------------            -------
Claude Code skill  -+
CLI / curl         -+-->  FastAPI REST API
any agent          -|    |-- /api/projects      SQLite
                    +    |-- /api/sequences      (metadata + events)
                         |-- /api/executions
                         |
Dashboard (Next.js       |-- GET /api/executions/{id}
 + Cloudscape UI) -----> |     (status, events, tasks)
  polls every 2-5s       |
                         +-- Execution Engine
                              |-- Plan parser (markdown -> DAG)
                              |-- DAG scheduler (level-by-level)
                              |-- Wave executor (3-phase loop)
                              |-- AgentRunner (spawns claude)
                              +-- Events -> SQLite
                                        |
                              Local filesystem
                              |-- storage/specs/{seq_id}/spec.md
                              |-- storage/plans/{seq_id}/plan.md
                              |-- storage/output/{exec_id}/{task_id}.txt
                              +-- storage/logs/{exec_id}/log.txt
```

Three main layers:

1. **REST API** — CRUD for projects, sequences, executions; event streaming; blocker resolution (see [API Reference](api.md) for all endpoints)
2. **Execution engine** — plan parsing, DAG scheduling, wave execution, subprocess management
3. **Storage** — SQLite for structured data, local filesystem for markdown artifacts and output blobs

The dashboard is a separate Next.js + Cloudscape app that polls the REST API.

## Data model

### Entity hierarchy

```
Project (has API key)
+-- Sequence ("add-oauth" lifecycle)
    |-- Spec markdown (on disk)
    |-- Plan markdown (on disk)
    +-- Execution[] (runs of this plan)
        |-- Events[] (append-only, in SQLite)
        |-- Task output blobs (on disk)
        +-- Commands[] (blocker resolution)
```

### Tables

**projects** — `id` (UUID), `name`, `description`, `api_key` (unique, auto-generated), `created_at`, `updated_at`

**sequences** — `id`, `project_id`, `name`, `description`, `status` (drafting|planned|executing|completed|failed), `spec_path`, `plan_path`, `wave_count`, `task_count`, `created_at`, `updated_at`. Indexed on `(project_id, created_at)`.

**executions** — `id`, `sequence_id`, `status` (queued|running|completed|failed|cancelled), `trigger` (initial|retry|continuation), `runtime`, `total_tasks`, `completed_tasks`, `current_wave`, `waves_state` (JSON), `config` (JSON), `started_at`, `finished_at`, `created_at`. Indexed on `(sequence_id, started_at)`.

**events** — `id`, `execution_id`, `event_type`, `task_id`, `phase`, `payload` (JSON), `created_at`. Indexed on `(execution_id, created_at)`.

**commands** — `id`, `execution_id`, `task_id`, `action` (null until resolved, then "retry"|"skip"), `message`, `picked_up`, `created_at`, `resolved_at`. Partial index on `(execution_id, task_id, picked_up) WHERE NOT picked_up`.

### Entity relationships

```
Project  1--*  Sequence  1--*  Execution  1--*  Event
                                           1--*  Command
```

All IDs are UUID v4 strings. No foreign key constraints at the database level (SQLite), enforced in application code.

## Data flow

### Write path (creating an execution)

1. Client `POST /api/sequences/{id}/executions`
2. Route creates `Execution` row (status=queued), launches background `asyncio.Task`
3. Background task loads plan markdown from filesystem, parses into `Plan` dataclass
4. DAG validation (cycle detection via Kahn's algorithm, cross-section dependency check, file overlap detection)
5. Sets status=running, iterates waves sequentially
6. Within each wave: foundation tasks -> feature tasks (parallel features) -> integration tasks
7. Each task: builds prompt, spawns `claude` subprocess via `asyncio.create_subprocess_exec`
8. Callbacks emit events to SQLite, write task output to filesystem
9. On completion: sets final status, emits `run_completed` event

### Read path (monitoring an execution)

1. Dashboard polls `GET /api/executions/{id}` every 2s for status
2. Polls `GET /api/executions/{id}/events?since=<timestamp>` for incremental events
3. Polls `GET /api/executions/{id}/tasks` every 5s for task summary
4. Task output fetched on demand via `GET /api/executions/{id}/output/{task_id}`

## Execution engine

The engine is a Python port of the TypeScript wave executor from pi-wave-workflow. Core components:

### Plan parser (`engine/plan_parser.py`)

Parses plan markdown into a `Plan` dataclass. Supports two formats:

- **Feature-based (v2)**: `## Wave N` -> `### Foundation` / `### Feature: X` / `### Integration` -> `#### Task ID: title`
- **Legacy flat**: `## Wave N` -> `### Task ID: title` (wraps all tasks in a single "default" feature)

Each task has metadata: agent type, files, dependencies, test files, spec refs, description.

### DAG scheduler (`engine/dag.py`)

- **Validation**: cycle detection (Kahn's algorithm), missing dependency check, self-dependency check
- **Plan validation**: per-section DAG validation, cross-section dependency detection, duplicate ID detection, file overlap between parallel features
- **Level building**: topological sort into parallel execution levels
- **Execution**: `execute_dag()` runs tasks level-by-level with configurable concurrency; failed tasks cause downstream dependents to be skipped

### Wave executor (`engine/wave_executor.py`)

Runs a complete wave in three phases:

1. **Foundation** — shared setup tasks, run via DAG
2. **Features** — independent feature task sets, run in parallel via `map_concurrent`; tasks within each feature run sequentially (stop on first failure)
3. **Integration** — cross-feature verification tasks, run via DAG

A fourth phase (**Merge** — merge feature branches back to main) is planned but not yet wired into the executor. The `git_worktree.py` module implements worktree create/merge/cleanup as infrastructure for this.

If foundation fails, features and integration are skipped. If any feature fails, integration is skipped.

### Runner (`engine/runner.py`)

`AgentRunner` protocol with `ClaudeCodeRunner` as the default implementation. Spawns `claude --output-format stream-json -p <prompt>` via `asyncio.create_subprocess_exec` (no shell, prevents injection). Extracts final output from the JSON stream.

Extensible: implement `AgentRunner` protocol for other runtimes.

### Supporting modules

- **`engine/types.py`** — all dataclasses: Plan, Wave, Feature, Task, TaskResult, FeatureResult, WaveResult, RunnerConfig, RunnerResult, ExecutionState, etc.
- **`engine/state.py`** — execution state tracking for resume (mark tasks done/failed/skipped, serialize to JSON)
- **`engine/execution_manager.py`** — bridges REST API to engine; launches/cancels background `asyncio.Task`s, emits events to DB
- **`engine/feature_executor.py`** — runs a single feature's task DAG (used by wave executor)
- **`engine/enforcement.py`** — generates file access rules for agent sandboxing
- **`engine/git_worktree.py`** — git worktree create/merge/cleanup for parallel feature isolation (implemented, not yet integrated into wave executor)

## Storage

### Database

SQLite via SQLAlchemy async (aiosqlite). Connection string configurable via `WAVE_DATABASE_URL`. Default: `sqlite+aiosqlite:///./data/wave-server.db`. Tables auto-created on startup via `Base.metadata.create_all`.

### Filesystem

Artifacts stored under `WAVE_DATA_DIR/storage/` (default `./data/storage/`):

```
data/
|-- wave-server.db
+-- storage/
    |-- specs/{sequence_id}/spec.md
    |-- plans/{sequence_id}/plan.md
    |-- output/{execution_id}/{task_id}.txt
    +-- logs/{execution_id}/log.txt
```

Storage operations are synchronous filesystem reads/writes via `storage.py`. Abstraction boundary designed for future swap to R2/S3.

## Auth

No authentication for v1 (localhost only). `auth.py` contains a no-op dependency. Structured for future bearer token swap.

## Module map

```
wave_server/
|-- main.py              FastAPI app, lifespan, CORS middleware
|-- config.py            Pydantic Settings (WAVE_ env prefix)
|-- db.py                SQLAlchemy async engine, session, Base
|-- models.py            SQLAlchemy ORM models (5 tables)
|-- schemas.py           Pydantic request/response schemas
|-- storage.py           Filesystem read/write for specs, plans, output, logs
|-- auth.py              Auth dependency (no-op for v1)
|-- routes/
|   |-- health.py        GET /api/health
|   |-- projects.py      Project CRUD + cascade delete + key regeneration
|   |-- sequences.py     Sequence CRUD + spec/plan upload/download
|   +-- executions.py    Execution lifecycle, events, tasks, output, log, blockers
+-- engine/
    |-- types.py         All dataclasses (Plan, Wave, Task, results, etc.)
    |-- plan_parser.py   Markdown -> Plan (v2 feature-based + legacy)
    |-- dag.py           DAG validation, topological sort, concurrent execution
    |-- wave_executor.py Wave loop (foundation -> features -> integration)
    |-- feature_executor.py  Single feature DAG execution
    |-- runner.py        AgentRunner protocol + ClaudeCodeRunner
    |-- state.py         Execution state for resume
    |-- execution_manager.py  Background task launcher, event emitter
    |-- enforcement.py   File access rule generation
    +-- git_worktree.py  Git worktree operations
```

## Testing

Three layers:

**Unit/integration tests** (`pytest`) — 417 tests, ~14s, run in CI. Cover API routes, plan parsing, DAG scheduling, wave execution, storage, and log formatting. Use mock runners and in-memory SQLite.

- `test_e2e_execution.py` — full API workflow with mock runner (14 tests): project → repo → sequence → plan → execute → verify events/artifacts/status. Covers happy path, failures, cancel/continue, context files, concurrent executions.

**Live evals** (`WAVE_LIVE_TEST=1 pytest tests/test_live_execution.py -v -s`) — manually triggered, spawn real Claude Code subprocesses. Validate the full system including subprocess spawning, stream-json parsing, and actual code quality.

| Eval | Tasks | What it stresses |
|---|---|---|
| `test_simple` | 2 | Code generation, test writing |
| `test_multi_agent` | 3 | All 3 agent types, foundation → features → integration phases |
| `test_capability` | 5 | Bash, git, CLI execution, data processing, test-driven bug diagnosis, parallel features |
| `test_process` | 2 | Background process lifecycle: start server, test endpoints, kill, verify stopped |

Each eval pre-plants fixture files in a temp git repo, runs execution through the full API, then verifies both the server artifacts (events, logs, outputs) and the actual results (files exist, tests pass, server stopped, etc.).

## Cloud migration path (v2)

| Component | Local (v1) | Cloud (v2) |
|-----------|-----------|------------|
| Database | SQLite | Supabase Postgres (change `DATABASE_URL`) |
| Storage | Local filesystem | Cloudflare R2 (swap `storage.py`) |
| Dashboard | localhost:9719 | Vercel |
| Server | localhost:9718 | EC2 / Fly.io |
| Auth | None | Single bearer token |

Abstraction boundaries (`storage.py`, `db.py`, `auth.py`) make these swaps mechanical.
