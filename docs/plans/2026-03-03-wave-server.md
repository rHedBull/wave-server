# Spec: Wave Orchestration Server

## Context

The wave executor (`pi-wave-workflow/extensions/wave-executor/`) is a sophisticated multi-agent orchestration engine that runs inside pi or via a local CLI. It spawns parallel agent subprocesses (claude/pi), manages DAG-based task scheduling, handles git worktree isolation, stall detection, fix cycles, and resume. Currently it has no remote API — it's a library called directly.

We're extracting this into a **standalone server** with a REST API so that any client (pi, Claude Code, CLI, dashboard, any agent) can trigger and monitor executions. The server owns the orchestration — clients just say "execute this plan" and poll for status.

## Decisions

- **Backend:** Python + FastAPI (async, auto OpenAPI docs)
- **Database:** SQLite via SQLAlchemy (zero setup, swappable to Postgres via connection string)
- **Storage:** Local filesystem (specs, plans, task output blobs) — swappable to R2/S3 later
- **Execution:** Server spawns **Claude Code** (`claude`) worker subprocesses by default. Runner uses a protocol interface (`AgentRunner`) so other runtimes can be added later.
- **Realtime:** REST polling (no WebSocket/SSE for v1 — dashboard polls every 2-5s)
- **Frontend:** Next.js (React) dashboard with **Cloudscape Design System** (`@cloudscape-design/components`) — separate deployable
- **Auth:** No auth for v1 (localhost only). Structured for future single bearer token when deployed. Single-user, no RBAC.

## Architecture

```
Clients                         Server (local)                    Workers
-------                         --------------                    -------
Claude Code skill  -+
CLI `waves exec`   -+-->  FastAPI REST API
curl / any agent   -|    |-- /api/projects      SQLite <--+
                    +    |-- /api/sequences      (metadata, events)
                          |-- /api/executions               |
                          |     |                           |
Dashboard (Next.js       |-- GET /api/executions/{id}     |
 + Cloudscape UI) ------> |     (status, events, tasks)     |
  polls every 2-5s        |                                 |
                          +-- Executor Engine ---------------+
                               |-- Spawns claude subprocesses
                               |   (AgentRunner protocol — extensible)
                               |-- DAG scheduler (level-by-level)
                               |-- Git worktree isolation
                               |-- Stall detection + fix cycles
                               +-- Pushes events to SQLite
                                         |
                               Local filesystem
                               |-- specs/{seq_id}/spec.md
                               |-- plans/{seq_id}/plan.md
                               |-- output/{exec_id}/{task_id}.txt
                               +-- logs/{exec_id}/log.txt
```

## Data Model

### Hierarchy

```
Project (has API key)
+-- Sequence (one workflow lifecycle, e.g., "add-oauth")
    |-- Spec (markdown on disk)
    |-- Plan (markdown on disk)
    +-- Execution[] (one or more runs)
        |-- Events[] (append-only in SQLite)
        |-- Task output blobs (on disk)
        +-- Commands[] (blocker resolution)
```

### SQLAlchemy Models

```python
class Project(Base):
    id: str           # UUID
    name: str
    description: str | None
    api_key: str      # unique, auto-generated
    created_at: datetime
    updated_at: datetime

class Sequence(Base):
    id: str           # UUID
    project_id: str   # FK -> Project
    name: str         # slug, e.g. "add-oauth"
    description: str | None
    status: str       # drafting | planned | executing | completed | failed
    spec_path: str | None   # local filesystem path
    plan_path: str | None   # local filesystem path
    wave_count: int | None
    task_count: int | None
    created_at: datetime
    updated_at: datetime

class Execution(Base):
    id: str           # UUID
    sequence_id: str  # FK -> Sequence
    status: str       # queued | running | completed | failed | cancelled
    trigger: str      # initial | retry | continuation
    runtime: str      # "claude" (default; extensible via AgentRunner protocol)
    total_tasks: int
    completed_tasks: int
    current_wave: int
    waves_state: str  # JSON - compact wave statuses
    config: str       # JSON - concurrency, timeout, etc.
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

class Event(Base):
    id: str           # UUID
    execution_id: str # FK -> Execution
    event_type: str   # see Event Types below
    task_id: str | None
    phase: str | None # foundation | features | merge | integration
    payload: str      # JSON
    created_at: datetime

class Command(Base):
    id: str           # UUID
    execution_id: str # FK -> Execution
    task_id: str
    action: str | None    # null until resolved; then "retry" | "skip"
    message: str | None
    picked_up: bool
    created_at: datetime
    resolved_at: datetime | None
```

### Indexes

```
events: (execution_id, created_at)
commands: (execution_id, task_id, picked_up) WHERE NOT picked_up
sequences: (project_id, created_at DESC)
executions: (sequence_id, started_at DESC)
```

## Event Types

```python
EventType = Literal[
    "run_started",
    "phase_changed",       # { phase, wave_index, wave_name }
    "task_started",        # { task_id, title, agent, phase }
    "task_completed",      # { task_id, exit_code, duration_ms }
    "task_failed",         # { task_id, exit_code, error, duration_ms }
    "task_skipped",        # { task_id, reason }
    "feature_started",     # { feature_name }
    "feature_completed",   # { feature_name, passed, task_count }
    "merge_result",        # { source, target, success, had_changes }
    "stall_detected",      # { task_id, reason }
    "fix_cycle_started",   # { task_id }
    "fix_cycle_result",    # { task_id, succeeded }
    "blocker",             # { task_id, error }
    "blocker_resolved",    # { task_id, action }
    "log_batch",           # { lines: string[] }
    "wave_completed",      # { wave_name, passed, task_results }
    "run_completed",       # { passed, total_tasks, completed_tasks, duration_ms }
]
```

## REST API

### Auth

v1 (localhost): No authentication required. All routes are open.

v2 (deployed): Single bearer token (`Authorization: Bearer <token>`) gates all routes. Single-user, no RBAC. Auth middleware is a no-op in v1, swappable to token validation in v2.

### Projects

```
POST   /api/projects                    # Create project -> returns { id, api_key }
GET    /api/projects                    # List projects (filtered by API key)
GET    /api/projects/{id}               # Get project detail
PATCH  /api/projects/{id}               # Update name/description
DELETE /api/projects/{id}               # Delete + cascade all data
POST   /api/projects/{id}/regenerate-key # New API key, old invalidated
```

### Sequences

```
POST   /api/projects/{id}/sequences     # Create sequence
GET    /api/projects/{id}/sequences     # List sequences for project
GET    /api/sequences/{id}              # Get sequence detail
PATCH  /api/sequences/{id}              # Update name/description/status

POST   /api/sequences/{id}/spec         # Upload spec markdown (body = raw markdown)
GET    /api/sequences/{id}/spec         # Get spec content
POST   /api/sequences/{id}/plan         # Upload plan markdown
GET    /api/sequences/{id}/plan         # Get plan content
```

### Executions

```
POST   /api/sequences/{id}/executions   # Create + start execution
                                        # Body: { runtime?, concurrency?, timeout_ms? }
GET    /api/sequences/{id}/executions   # List executions for sequence
GET    /api/executions/{id}             # Get execution detail (status, waves_state, counts)
POST   /api/executions/{id}/cancel      # Cancel running execution
POST   /api/executions/{id}/continue    # Resume from failure (creates new linked execution)

GET    /api/executions/{id}/events      # List events (paginated: ?since=timestamp&limit=50)
GET    /api/executions/{id}/tasks       # Task summary: all tasks with current status
GET    /api/executions/{id}/output/{task_id}  # Get task output blob
GET    /api/executions/{id}/log         # Get execution log
```

### Blocker Commands

```
GET    /api/executions/{id}/blockers            # List unresolved blockers
POST   /api/executions/{id}/blockers/{cmd_id}   # Resolve: { action: "retry" | "skip" }
```

### Health

```
GET    /api/health                      # Server status + active execution count
```

## Execution Engine

### How it works

The server embeds the wave execution logic (ported from TypeScript to Python). When `POST /api/sequences/{id}/executions` is called:

1. Server validates plan (DAG validation, same logic as current `validatePlan()`)
2. Creates `Execution` row with status=`queued`
3. Launches execution in a background `asyncio.Task`
4. Returns immediately with execution ID

The background task:
1. Sets status=`running`, emits `run_started` event
2. Iterates waves (same loop as `runWaveExecution()`)
3. For each wave: foundation -> features (parallel) -> merge -> integration
4. Spawns `claude` or `pi` subprocesses via `asyncio.create_subprocess_exec()`
5. Each callback (onTaskStart, onTaskEnd, etc.) inserts an Event row
6. Task output written to `storage/{exec_id}/{task_id}.txt`
7. State updated after each task (for resume)
8. On completion: emits `run_completed`, sets status

### Ported components

These are ported from TypeScript to Python:

| TS file | Python module | What it does |
|---------|--------------|--------------|
| `plan-parser.ts` | `engine/plan_parser.py` | Parse plan markdown -> Plan dataclass |
| `dag.ts` | `engine/dag.py` | DAG validation, topological sort, map_concurrent |
| `wave-executor.ts` | `engine/wave_executor.py` | Wave execution: 4 phases, callbacks |
| `feature-executor.ts` | `engine/feature_executor.py` | Feature DAG with sub-worktrees |
| `state.ts` | `engine/state.py` | State persistence for resume |
| `helpers.ts` (runner) | `engine/runner.py` | Spawn claude/pi subprocesses |
| `helpers.ts` (enforcement) | `engine/enforcement.py` | File access enforcement extension |
| `widget.ts` | Not needed | Events replace widget rendering |

### Runner (subprocess spawning)

```python
class AgentRunner(Protocol):
    async def spawn(self, config: RunnerConfig) -> RunnerResult: ...
    def extract_final_output(self, stdout: str) -> str: ...

class ClaudeCodeRunner(AgentRunner): ... # spawns `claude --output-format stream-json -p ...`
# Additional runners (e.g. PiRunner) can implement AgentRunner protocol later
```

Default runtime: Claude Code. Configurable via `WAVE_RUNTIME` env var (default: `"claude"`).

### Concurrency

- `asyncio.Semaphore(max_concurrency)` for task-level parallelism
- `asyncio.gather()` for feature-level parallelism
- Each subprocess is an `asyncio.create_subprocess_exec()` - non-blocking

## Filesystem Layout (Storage)

```
data/
|-- wave-server.db              # SQLite database
+-- storage/
    |-- specs/{sequence_id}/
    |   +-- spec.md
    |-- plans/{sequence_id}/
    |   +-- plan.md
    |-- output/{execution_id}/
    |   |-- {task_id}.txt
    |   +-- ...
    +-- logs/{execution_id}/
        +-- log.txt
```

Configurable via `WAVE_DATA_DIR` env var (default: `./data`).

## Dashboard (Next.js + Cloudscape)

### Tech Stack

- Next.js 14+ (App Router)
- React 18+
- **Cloudscape Design System** (`@cloudscape-design/components`, `@cloudscape-design/global-styles`)
- REST polling (SWR or React Query with refetchInterval)

### Shell Layout

Persistent **AppLayout** (Cloudscape) with:
- **SideNavigation** — project list with **StatusIndicator** per project (idle / running / failed). Click to select project. Collapsible.
- **Main content area** — changes based on route
- **BreadcrumbGroup** — navigation breadcrumbs

### Pages

```
/                               # Redirect to /projects
/projects                       # Project list (Cloudscape Cards collection)
/projects/[id]                  # Project overview - sequences as Cards with StatusIndicator
/projects/[id]/settings         # Edit name, manage API key (Cloudscape Form)
/sequences/[id]                 # Sequence detail - spec/plan markdown, execution Table
/executions/[id]                # Live execution view (polls every 2s)
```

### Cloudscape Components Used

| Component | Where |
|-----------|-------|
| **AppLayout** | Shell (sidebar + content + breadcrumbs) |
| **SideNavigation** | Project list in sidebar |
| **StatusIndicator** | Project status (idle/running/failed), task status, execution status |
| **Cards** | Project list, sequence list |
| **Table** | Execution list, task summary, event log |
| **Header** | Page headers with action buttons |
| **Container** | Spec/plan markdown sections, task detail panels |
| **ExpandableSection** | Spec/plan content, task output |
| **Tabs** | Sequence detail (spec / plan / executions) |
| **ProgressBar** | Wave timeline progress |
| **Badge** | Agent type (test-writer, worker, verifier) |
| **Alert / Flashbar** | Blocker banner (retry/skip), error states |
| **Button** | Actions (retry, skip, cancel, create) |
| **Form / FormField / Input** | Project settings, create project |
| **SpaceBetween / Grid / ColumnLayout** | Layout structure |
| **Box** | Typography, spacing |
| **Link / BreadcrumbGroup** | Navigation |
| **Spinner** | Loading states during polling |
| **CodeView** | Task output, log viewer |

### Execution View (main feature)

Polls `GET /api/executions/{id}` + `GET /api/executions/{id}/events?since=<last>` every 2s.

**Layout:**
- **Wave timeline** — Cloudscape **ProgressBar** per wave, current wave highlighted with StatusIndicator
- **Task grid** — Cloudscape **Table** grouped by phase (foundation -> features -> integration)
  - StatusIndicator per task: pending, running (loading), success, error, stopped (skipped)
  - Badge for agent type: test-writer, worker, verifier
  - Live elapsed time for running tasks
  - Click row to expand -> ExpandableSection with task output (fetched from API, rendered in CodeView)
- **Feature lanes** — Cloudscape **ColumnLayout** with Cards per feature during feature phase
- **Log tail** — Cloudscape **CodeView** or Container with monospace text, auto-scrolling
- **Blocker banner** — Cloudscape **Flashbar** (type: warning) with retry/skip Buttons

### Polling Strategy

```
/api/executions/{id}               # every 2s while running, stop when completed
/api/executions/{id}/events?since= # every 2s, incremental (only new events)
/api/executions/{id}/tasks         # every 5s (less frequent, full task summary)
```

## Project Structure

```
wave-server/
|-- pyproject.toml
|-- wave_server/
|   |-- __init__.py
|   |-- main.py                 # FastAPI app, lifespan, CORS
|   |-- config.py               # Settings (data dir, default concurrency, etc.)
|   |-- auth.py                 # Auth middleware (no-op for v1, bearer token for v2)
|   |-- db.py                   # SQLAlchemy engine, session, Base
|   |-- models.py               # SQLAlchemy models
|   |-- schemas.py              # Pydantic request/response schemas
|   |-- storage.py              # Filesystem storage (read/write blobs)
|   |-- routes/
|   |   |-- __init__.py
|   |   |-- projects.py
|   |   |-- sequences.py
|   |   |-- executions.py
|   |   +-- health.py
|   +-- engine/
|       |-- __init__.py
|       |-- types.py            # Plan, Wave, Feature, Task dataclasses
|       |-- plan_parser.py      # Markdown -> Plan
|       |-- dag.py              # Validation, topological sort, map_concurrent
|       |-- wave_executor.py    # Wave loop (4 phases)
|       |-- feature_executor.py # Feature DAG with sub-worktree isolation
|       |-- runner.py           # Spawn claude/pi, extract output
|       |-- state.py            # Execution state for resume
|       |-- enforcement.py      # File access enforcement extension generator
|       +-- git_worktree.py     # Git worktree create/merge/cleanup
|-- tests/
|   |-- test_plan_parser.py
|   |-- test_dag.py
|   |-- test_api.py
|   +-- test_engine.py
|-- dashboard/
|   |-- package.json
|   |-- next.config.ts
|   |-- src/
|   |   |-- app/
|   |   |   |-- layout.tsx
|   |   |   |-- page.tsx
|   |   |   |-- projects/
|   |   |   |   |-- page.tsx
|   |   |   |   +-- [id]/
|   |   |   |       |-- page.tsx
|   |   |   |       +-- settings/page.tsx
|   |   |   |-- sequences/[id]/page.tsx
|   |   |   +-- executions/[id]/page.tsx
|   |   |-- components/
|   |   |   |-- AppShell.tsx          # Cloudscape AppLayout + SideNavigation
|   |   |   |-- ProjectCards.tsx      # Cloudscape Cards collection
|   |   |   |-- SequenceCards.tsx     # Cloudscape Cards collection
|   |   |   |-- WaveTimeline.tsx      # ProgressBar per wave
|   |   |   |-- TaskTable.tsx         # Cloudscape Table with StatusIndicator
|   |   |   |-- TaskDetail.tsx        # ExpandableSection + CodeView
|   |   |   |-- FeatureLanes.tsx      # ColumnLayout with Cards
|   |   |   |-- LogTail.tsx           # CodeView with auto-scroll
|   |   |   |-- BlockerBanner.tsx     # Flashbar with retry/skip
|   |   |   +-- MarkdownView.tsx      # Container with rendered markdown
|   |   |-- hooks/
|   |   |   |-- useExecution.ts
|   |   |   +-- usePolling.ts
|   |   +-- lib/
|   |       +-- api.ts
|   +-- .env.local
+-- .env.example
```

## Cloud Deployment Path (v2)

| Component | Local (v1) | Cloud (v2) |
|-----------|-----------|------------|
| Database | SQLite file | Supabase Postgres (change DATABASE_URL) |
| Storage | Local filesystem | Cloudflare R2 (swap storage.py) |
| Dashboard | localhost:9719 | Vercel |
| Server | localhost:9718 | EC2 / Fly.io / Railway |
| Auth | None (localhost) | Single bearer token |
| Realtime | REST polling | + Supabase Realtime subscriptions |

## Agent Integration (Skills)

Agents interact with the server via REST, guided by skills in the Claude Code plugin. The skill describes the API shape - no MCP server needed. Any agent that reads skill docs can orchestrate executions.

## Implementation Order

### Phase 1: Server core (Python)
1. Project scaffolding (pyproject.toml, FastAPI app, config)
2. SQLAlchemy models + SQLite setup
3. Storage module (filesystem read/write)
4. Auth middleware (no-op for v1, structured for bearer token swap)
5. Project CRUD routes
6. Sequence CRUD + spec/plan upload routes
7. Tests for API routes

### Phase 2: Execution engine (Python port)
1. Types (dataclasses matching TS types)
2. Plan parser (markdown -> Plan)
3. DAG module (validate, topological sort, map_concurrent)
4. Runner (ClaudeCodeRunner + AgentRunner protocol for extensibility)
5. Wave executor (4 phases, callbacks)
6. Feature executor (sub-worktree isolation)
7. State management (resume)
8. Integration: execution routes + background task launch
9. Tests for engine

### Phase 3: Dashboard (Next.js + Cloudscape)
1. Project scaffolding (Next.js, Cloudscape Design System)
2. AppShell (AppLayout + SideNavigation with project list + StatusIndicators)
3. API client + polling hooks
4. Project list + detail pages (Cards, Header, Form)
5. Sequence detail page (Tabs: spec/plan markdown, execution Table)
6. Execution live view (TaskTable, WaveTimeline, FeatureLanes, LogTail)
7. Blocker resolution UI (Flashbar with retry/skip)

### Phase 4: Integration
1. Skill files for Claude Code plugin (update skilled-labor repo)
2. End-to-end test: skill -> API -> execution -> dashboard
3. CLI wrapper (optional thin `waves` command calling the API)

## Verification

1. `pytest tests/` - all API and engine tests pass
2. Start server: `uvicorn wave_server.main:app`
3. Create project via API -> get API key
4. Upload spec + plan -> create execution -> poll until complete
5. Dashboard shows live progress (task grid updates every 2s)
6. Resume: fail a task -> POST continue -> resumes from failure point
7. Blocker: task fails -> blocker in dashboard -> click retry -> continues
