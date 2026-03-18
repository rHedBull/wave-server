#!/bin/bash
# Wave Server Demo — run this to see the full flow
# Requires: uv, pi or claude CLI, git
set -euo pipefail

API=http://localhost:9722/api/v1
GREEN='\033[0;32m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

say() { echo -e "\n${CYAN}${BOLD}▸ $1${NC}"; }
run() { echo -e "${DIM}\$ $1${NC}"; eval "$1"; }

# Setup
say "Setting up test repo..."
rm -rf /tmp/demo-repo
mkdir -p /tmp/demo-repo && cd /tmp/demo-repo
git init -q && echo "# Demo" > README.md && git add . && git commit -q -m "init"

say "Starting wave-server on port 9722..."
cd "$(dirname "$0")"
uv run uvicorn wave_server.main:app --port 9722 &>/dev/null &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; rm -rf /tmp/demo-repo" EXIT
sleep 2

say "Health check"
run 'curl -s localhost:9722/api/health | python3 -m json.tool'

say "Create project + register repo"
PID=$(curl -s -X POST $API/projects -H "Content-Type: application/json" \
  -d '{"name":"demo"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST "$API/projects/$PID/repositories" \
  -H "Content-Type: application/json" \
  -d '{"path":"/tmp/demo-repo"}' > /dev/null
echo -e "${GREEN}✓ Project: $PID${NC}"

say "Create sequence + upload plan"
SID=$(curl -s -X POST "$API/projects/$PID/sequences" \
  -H "Content-Type: application/json" \
  -d '{"name":"hello-world"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

PLAN='# Implementation Plan
<!-- format: v2 -->

## Goal
Create a hello world Python script

## Project Structure
```
/
  hello.py
```

## Data Schemas
N/A

---

## Wave 1: Hello World

### Foundation
#### Task w1-found-t1: Create hello.py
- **Agent**: worker
- **Files**: `hello.py`
- **Description**: Create hello.py that prints "Hello World"'

curl -s -X POST "$API/sequences/$SID/plan" \
  -H "Content-Type: text/plain" -d "$PLAN" > /dev/null
echo -e "${GREEN}✓ Sequence: $SID${NC}"

say "🚀 Start execution (pi runtime, Sonnet)"
EID=$(curl -s -X POST "$API/sequences/$SID/executions" \
  -H "Content-Type: application/json" \
  -d '{"runtime":"pi","model":"claude-sonnet-4-20250514"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo -e "${GREEN}✓ Execution: $EID${NC}"

say "⏳ Waiting for completion..."
for i in $(seq 1 60); do
  STATUS=$(curl -s "$API/executions/$EID" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f\"{d['status']} ({d['completed_tasks']}/{d['total_tasks']} tasks)\")")
  echo -e "  ${DIM}[$i]${NC} $STATUS"
  [[ "$STATUS" == completed* ]] && break
  sleep 3
done

say "📋 Event stream"
curl -s "$API/executions/$EID/events" | python3 -c "
import sys,json
for e in json.load(sys.stdin):
    p = json.loads(e['payload']) if e['payload'] else {}
    extra = ''
    for k in ['exit_code','passed','wave_name']:
        if k in p: extra += f' {k}={p[k]}'
    print(f'  {e[\"event_type\"]:25s} task={e[\"task_id\"] or \"-\":15s}{extra}')"

say "📄 Result — hello.py on work branch"
BRANCH=$(curl -s "$API/executions/$EID" | python3 -c "import sys,json;print(json.load(sys.stdin)['work_branch'])")
run "git -C /tmp/demo-repo show $BRANCH:hello.py"

echo -e "\n${GREEN}${BOLD}✅ Done — full DAG execution with git worktree isolation${NC}"
echo -e "${DIM}650 tests • REST API • real-time events • verify-fix loops${NC}\n"
