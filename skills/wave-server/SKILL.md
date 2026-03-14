---
name: wave-server
description: Interact with a Wave Server to manage projects, sequences, specs, plans, and executions. Upload plans and specs, start and monitor executions, check task results, and resolve blockers — all via the Wave Server REST API.
---

# Wave Server Skill

Interact with a running Wave Server instance to orchestrate multi-agent plan executions.

## Prerequisites

- Wave Server running (default: `http://localhost:9718`)
- `curl` and `jq` available

Verify the server is up before doing anything:

```bash
curl -sf http://localhost:9718/api/health | jq .
```

If the server is not running, tell the user and stop.

## API Base

All endpoints use the prefix `/api` (health) or `/api/v1` (everything else).

Default base URL: `http://localhost:9718`

If the user specifies a different host/port, use that instead.

## Core Concepts

- **Project** — a workspace that groups related sequences. Has an auto-generated `api_key`.
- **Sequence** — a lifecycle unit (e.g. "add-oauth"). Holds a spec (requirements markdown) and a plan (implementation markdown). Status: `drafting` → `planned` → `executing` → `completed` | `failed`.
- **Execution** — a run of a sequence's plan. The server parses the plan into a DAG and spawns Claude Code workers to execute tasks wave-by-wave. Status: `queued` → `running` → `completed` | `failed` | `cancelled`.
- **Event** — append-only log entries for an execution (task_started, task_completed, task_failed, etc.).
- **Command/Blocker** — when a task fails and needs human input, a blocker is created. Resolve it with "retry" or "skip".

## Workflow

### Step 1: Find or create a project

List existing projects:

```bash
curl -s http://localhost:9718/api/v1/projects | jq .
```

If the user's project exists, use its `id`. Otherwise create one:

```bash
curl -s -X POST http://localhost:9718/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"name": "<project-name>", "description": "<optional>"}' | jq .
```

Save the returned `id` — you'll need it for all subsequent calls.

### Step 2: Create a sequence

```bash
curl -s -X POST http://localhost:9718/api/v1/projects/<project_id>/sequences \
  -H 'Content-Type: application/json' \
  -d '{"name": "<sequence-name>", "description": "<optional>"}' | jq .
```

Save the returned sequence `id`.

### Step 3: Upload spec and/or plan

Upload a spec (requirements markdown):

```bash
curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/spec \
  -H 'Content-Type: text/plain' \
  --data-binary @path/to/spec.md
```

Upload a plan (implementation markdown):

```bash
curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/plan \
  -H 'Content-Type: text/plain' \
  --data-binary @path/to/plan.md
```

You can also upload from stdin or a string:

```bash
curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/plan \
  -H 'Content-Type: text/plain' \
  -d '<raw markdown content>'
```

### Step 4: Start an execution

```bash
curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/executions \
  -H 'Content-Type: application/json' \
  -d '{}' | jq .
```

Optional config fields:

```json
{
  "runtime": "claude",
  "concurrency": 4,
  "timeout_ms": 300000
}
```

The execution starts immediately in the background. Save the returned execution `id`.

### Step 5: Monitor execution

**Check execution status:**

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id> | jq '{status, current_wave, completed_tasks, total_tasks}'
```

**Get task summary:**

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id>/tasks | jq .
```

Each task has: `task_id`, `status` (pending|running|completed|failed|skipped), `phase`, `has_output`, `has_transcript`.

**Get recent events:**

```bash
curl -s "http://localhost:9718/api/v1/executions/<execution_id>/events?limit=20" | jq '.[] | {event_type, task_id, phase, created_at}'
```

To get only new events since a timestamp:

```bash
curl -s "http://localhost:9718/api/v1/executions/<execution_id>/events?since=<ISO-timestamp>&limit=50" | jq .
```

**Get task output:**

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id>/output/<task_id>
```

**Get task transcript:**

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id>/transcript/<task_id>
```

**Get full execution log:**

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id>/log
```

### Step 6: Handle blockers

Check for unresolved blockers:

```bash
curl -s http://localhost:9718/api/v1/executions/<execution_id>/blockers | jq .
```

Resolve a blocker (retry the failed task or skip it):

```bash
curl -s -X POST http://localhost:9718/api/v1/executions/<execution_id>/blockers/<command_id> \
  -H 'Content-Type: application/json' \
  -d '{"action": "retry", "message": "optional note"}' | jq .
```

Action must be `"retry"` or `"skip"`.

### Step 7: Cancel or continue

**Cancel a running execution:**

```bash
curl -s -X POST http://localhost:9718/api/v1/executions/<execution_id>/cancel
```

**Continue from failure** (creates a new execution that resumes where the previous one stopped):

```bash
curl -s -X POST http://localhost:9718/api/v1/executions/<execution_id>/continue | jq .
```

Only works when execution status is `failed` or `cancelled`.

## Plan Format

Plans are markdown documents parsed by the server into a DAG. Two formats are supported:

### Feature-based format (preferred)

```markdown
# Implementation Plan

## Goal
One-line description of what this plan achieves.

## Wave 1: Setup
Wave description.

### Foundation
#### Task 1a: Create database schema
- **Agent**: worker
- **Files**: `src/db/schema.ts`, `src/db/migrations/001.sql`
- **Depends**: (none)
- **Tests**: `tests/db/schema.test.ts`
- **Description**: Create the initial database schema...

### Feature: Auth
Files: `src/auth/login.ts`, `src/auth/register.ts`

#### Task 1b: Implement login endpoint
- **Agent**: worker
- **Files**: `src/auth/login.ts`
- **Depends**: 1a
- **Tests**: `tests/auth/login.test.ts`
- **Description**: Implement the login endpoint...

#### Task 1c: Implement registration
- **Agent**: worker
- **Files**: `src/auth/register.ts`
- **Depends**: 1a
- **Description**: Implement user registration...

### Feature: API
Files: `src/api/routes.ts`

#### Task 1d: Setup API routes
- **Agent**: worker
- **Files**: `src/api/routes.ts`
- **Depends**: 1a
- **Description**: Set up API routing...

### Integration
#### Task 1e: Verify auth + API integration
- **Agent**: wave-verifier
- **Files**: `src/auth/login.ts`, `src/api/routes.ts`
- **Depends**: 1b, 1d
- **Description**: Verify the auth and API integration...

## Wave 2: Features
...
```

### Legacy flat format

```markdown
## Wave 1: Setup

### Task 1a: Create schema
- **Agent**: worker
- **Files**: `src/schema.ts`
- **Depends**: (none)
- **Description**: ...

### Task 1b: Add routes
- **Agent**: worker
- **Files**: `src/routes.ts`
- **Depends**: 1a
- **Description**: ...
```

### Task metadata fields

| Field | Required | Description |
|---|---|---|
| **Agent** | No (default: `worker`) | Agent type: `worker`, `test-writer`, `wave-verifier` |
| **Files** | No | Comma-separated file paths this task creates/modifies |
| **Depends** | No | Comma-separated task IDs this task depends on. Use `(none)` or `-` for no dependencies |
| **Tests** | No | Comma-separated test file paths |
| **Spec refs** | No | Comma-separated spec section references |
| **Description** | No | Multi-line description of what the task should do |

### Wave execution order

Within each wave, the server executes in phases:
1. **Foundation** — shared setup tasks (run via DAG, respecting dependencies)
2. **Features** — independent feature groups (run in parallel across features, sequential within)
3. **Integration** — cross-feature verification (run via DAG after all features complete)

If foundation fails, features and integration are skipped. If any feature fails, integration is skipped.

## Other Useful Endpoints

**List sequences for a project:**

```bash
curl -s http://localhost:9718/api/v1/projects/<project_id>/sequences | jq '.[] | {id, name, status}'
```

**List executions for a sequence:**

```bash
curl -s http://localhost:9718/api/v1/sequences/<sequence_id>/executions | jq '.[] | {id, status, trigger, completed_tasks, total_tasks}'
```

**Get spec or plan back:**

```bash
curl -s http://localhost:9718/api/v1/sequences/<sequence_id>/spec
curl -s http://localhost:9718/api/v1/sequences/<sequence_id>/plan
```

**Update a project:**

```bash
curl -s -X PATCH http://localhost:9718/api/v1/projects/<project_id> \
  -H 'Content-Type: application/json' \
  -d '{"name": "new name"}' | jq .
```

**Update a sequence:**

```bash
curl -s -X PATCH http://localhost:9718/api/v1/sequences/<sequence_id> \
  -H 'Content-Type: application/json' \
  -d '{"status": "planned"}' | jq .
```

**Delete a project** (cascades to all sequences, executions, events):

```bash
curl -s -X DELETE http://localhost:9718/api/v1/projects/<project_id>
```

**Regenerate API key:**

```bash
curl -s -X POST http://localhost:9718/api/v1/projects/<project_id>/regenerate-key | jq .
```

## Tips

- Always verify the server is healthy before starting a workflow.
- Save IDs (project, sequence, execution) as you go — you need them for subsequent calls.
- When monitoring a long execution, poll status every 5-10 seconds rather than hammering the API.
- Use `jq` filters to extract just the fields you need from responses.
- The `events` endpoint supports `since` for efficient incremental polling — save the last event's `created_at` and pass it on the next poll.
- If an execution fails, check task output and the execution log to understand why before deciding to retry or skip.
- Plans uploaded to a sequence can be updated by re-uploading — the server uses the latest version when starting a new execution.
