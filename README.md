# Wave Server

REST API and execution engine for multi-agent wave orchestration. Parses plan markdown into DAG-scheduled tasks, spawns Claude Code worker subprocesses, and tracks execution state via SQLite and local filesystem.

## Setup

Requires Python 3.12+.

```sh
uv sync
uv sync --group dev  # for tests
```

## Usage

Start the server:

```sh
uv run main.py
# or
uvicorn wave_server.main:app --host 0.0.0.0 --port 8000 --reload
```

Run tests:

```sh
pytest
```

Start the dashboard (separate terminal):

```sh
cd dashboard
cp .env.example .env.local  # sets NEXT_PUBLIC_API_URL=http://localhost:8000/api
npm install
npm run dev
# opens http://localhost:3000
```

### Quick walkthrough

```sh
# Create a project
curl -s -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-project"}' | jq .

# Create a sequence under the project
curl -s -X POST http://localhost:8000/api/projects/{project_id}/sequences \
  -H 'Content-Type: application/json' \
  -d '{"name": "add-oauth"}' | jq .

# Upload a plan (raw markdown body)
curl -s -X POST http://localhost:8000/api/sequences/{sequence_id}/plan \
  -H 'Content-Type: text/plain' \
  --data-binary @plan.md

# Start an execution
curl -s -X POST http://localhost:8000/api/sequences/{sequence_id}/executions \
  -H 'Content-Type: application/json' \
  -d '{}' | jq .

# Poll execution status
curl -s http://localhost:8000/api/executions/{execution_id} | jq .
```

## Configuration

All settings use the `WAVE_` env prefix:

| Variable | Default | Description |
|---|---|---|
| `WAVE_DATA_DIR` | `./data` | SQLite database and storage root |
| `WAVE_DATABASE_URL` | `sqlite+aiosqlite:///./data/wave-server.db` | Database connection string |
| `WAVE_DEFAULT_CONCURRENCY` | `4` | Max parallel tasks per execution |
| `WAVE_DEFAULT_TIMEOUT_MS` | `300000` | Task timeout (5 minutes) |
| `WAVE_RUNTIME` | `claude` | Default agent runtime |
| `WAVE_CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed CORS origins |

## Project structure

```
wave_server/     Python server — FastAPI app, models, routes, execution engine
tests/           pytest test suite (API + engine)
dashboard/       Next.js + Cloudscape monitoring dashboard
docs/            Specs, plans, and architecture docs
```

## Docs

- [Architecture](docs/architecture.md) — system overview, data model, execution engine, data flow
- [API Reference](docs/api.md) — all REST endpoints with request/response shapes
- [Implementation Plan](docs/plans/2026-03-03-wave-server.md) — wave-by-wave build plan
- [Spec](docs/spec/2026-03-03-wave-server.md) — original project specification
- [Deployment Pipeline Spec](docs/spec/2026-03-03-deployment-pipeline.md) — CI/CD and review agent design
- [Dashboard Spec](docs/pi-wave-dashboard-spec.md) — original dashboard spec (Supabase/Vercel design; actual implementation uses local Next.js + Cloudscape polling the REST API)

## License

MIT
