# Wave Orchestration Server — Spec

## Problem

The wave executor runs inside pi or a local CLI. No remote API, no dashboard, no way for arbitrary agents to trigger executions. The orchestration logic is locked inside a TypeScript library.

## Solution

A standalone **Python server** (FastAPI) that:
1. Exposes a REST API for managing projects, sequences (spec/plan lifecycle), and executions
2. Embeds the wave execution engine (ported from TS to Python) — spawns **Claude Code** worker subprocesses (extensible via `AgentRunner` protocol)
3. Stores metadata in SQLite, artifacts on local filesystem
4. Serves a **Next.js + Cloudscape** dashboard for live monitoring

Any client (Claude Code skill, CLI, curl, any agent) can trigger and monitor executions via HTTP. No auth for v1 (localhost only); single bearer token when deployed.

## Architecture

```
Clients                         Server (localhost:9718)            Workers
-------                         ----------------------            -------
Claude Code skill  -+
CLI `waves exec`   -+-->  FastAPI REST API
curl / any agent   -|    |-- /api/projects      SQLite
                    +    |-- /api/sequences      (metadata + events)
                         |-- /api/executions
                         |
Dashboard (Next.js       |-- GET /api/executions/{id}
 + Cloudscape UI) -----> |     (status, events, tasks)
  polls every 2-5s       |
                         +-- Executor Engine
                              |-- Spawns claude subprocesses
                              |   (AgentRunner protocol — extensible)
                              |-- DAG scheduler (level-by-level)
                              |-- Git worktree isolation
                              |-- Stall detection + fix cycles
                              +-- Writes events to SQLite
                                        |
                              Local filesystem
                              |-- storage/specs/{seq_id}/spec.md
                              |-- storage/plans/{seq_id}/plan.md
                              |-- storage/output/{exec_id}/{task_id}.txt
                              +-- storage/logs/{exec_id}/log.txt
```

## Data Model

### Hierarchy

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

**projects** — id, name, description, api_key (unique), created_at, updated_at

**sequences** — id, project_id (FK), name, description, status (drafting|planned|executing|completed|failed), spec_path, plan_path, wave_count, task_count, created_at, updated_at

**executions** — id, sequence_id (FK), status (queued|running|completed|failed|cancelled), trigger (initial|retry|continuation), runtime ("claude" default, extensible), total_tasks, completed_tasks, current_wave, waves_state (JSON), config (JSON), started_at, finished_at, created_at

**events** — id, execution_id (FK), event_type, task_id, phase, payload (JSON), created_at

**commands** — id, execution_id (FK), task_id, action (null|"retry"|"skip"), message, picked_up, created_at, resolved_at

## Event Types

run_started, phase_changed, task_started, task_completed, task_failed, task_skipped, feature_started, feature_completed, merge_result, stall_detected, fix_cycle_started, fix_cycle_result, blocker, blocker_resolved, log_batch, wave_completed, run_completed

## REST API

### Auth
v1 (localhost): No authentication required. All routes are open.
v2 (deployed): Single bearer token (`Authorization: Bearer <token>`) gates all routes. Single-user, no RBAC.

### Projects
```
POST   /api/projects                     # Create -> { id, api_key }
GET    /api/projects                     # List (filtered by key)
GET    /api/projects/{id}                # Detail
PATCH  /api/projects/{id}                # Update
DELETE /api/projects/{id}                # Delete + cascade
POST   /api/projects/{id}/regenerate-key # New key
```

### Sequences
```
POST   /api/projects/{id}/sequences      # Create
GET    /api/projects/{id}/sequences      # List
GET    /api/sequences/{id}               # Detail
PATCH  /api/sequences/{id}               # Update
POST   /api/sequences/{id}/spec          # Upload spec (raw markdown body)
GET    /api/sequences/{id}/spec          # Get spec
POST   /api/sequences/{id}/plan          # Upload plan
GET    /api/sequences/{id}/plan          # Get plan
```

### Executions
```
POST   /api/sequences/{id}/executions    # Create + start { runtime?, concurrency? }
GET    /api/sequences/{id}/executions    # List
GET    /api/executions/{id}              # Detail (status, waves_state, counts)
POST   /api/executions/{id}/cancel       # Cancel
POST   /api/executions/{id}/continue     # Resume from failure
GET    /api/executions/{id}/events       # Events (?since=ts&limit=50)
GET    /api/executions/{id}/tasks        # Task summary
GET    /api/executions/{id}/output/{tid} # Task output blob
GET    /api/executions/{id}/log          # Execution log
```

### Blockers
```
GET    /api/executions/{id}/blockers              # Unresolved
POST   /api/executions/{id}/blockers/{cmd_id}     # Resolve { action }
```

### Health
```
GET    /api/health                       # Status + active count
```

## Execution Engine

Port from TypeScript. Server validates plan, creates Execution row (queued), launches background asyncio.Task, returns immediately.

Background task runs the wave loop: foundation -> features (parallel) -> merge -> integration. Spawns Claude Code via asyncio subprocess (using `AgentRunner` protocol for extensibility). Callbacks insert Event rows. State persisted after each task for resume.

### Components to port

| TS source | Python target | Purpose |
|-----------|--------------|---------|
| plan-parser.ts | engine/plan_parser.py | Markdown -> Plan |
| dag.ts | engine/dag.py | Validate, topo-sort, map_concurrent, executeDAG |
| wave-executor.ts | engine/wave_executor.py | 4-phase wave loop |
| feature-executor.ts | engine/feature_executor.py | Feature DAG + sub-worktrees |
| state.ts | engine/state.py | Resume persistence |
| runner/ | engine/runner.py | ClaudeCodeRunner + AgentRunner protocol |
| helpers.ts (enforcement) | engine/enforcement.py | File access rules |

## Dashboard (Next.js + Cloudscape)

Separate deployable. **Cloudscape Design System** for all UI components. Polls REST API every 2-5s.

### Shell
Cloudscape AppLayout with persistent SideNavigation (project list + StatusIndicators: idle/running/failed).

### Pages
/projects, /projects/[id], /projects/[id]/settings, /sequences/[id], /executions/[id]

### Execution view (main feature)
Wave timeline (ProgressBar), task table (Table + StatusIndicator), feature lanes (ColumnLayout + Cards), log tail (CodeView), blocker banner (Flashbar with retry/skip).

## Storage Layout

```
data/
|-- wave-server.db
+-- storage/
    |-- specs/{sequence_id}/spec.md
    |-- plans/{sequence_id}/plan.md
    |-- output/{execution_id}/{task_id}.txt
    +-- logs/{execution_id}/log.txt
```

## Cloud Path (v2)

SQLite -> Supabase Postgres, filesystem -> R2, dashboard -> Vercel, server -> EC2/Fly.io, auth -> single bearer token. Abstraction boundaries (storage.py, db.py, auth.py) make swaps mechanical. Single-user throughout — no RBAC.
