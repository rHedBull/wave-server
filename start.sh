#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$PROJECT_DIR/dashboard"
LOG_DIR="$PROJECT_DIR/data/logs"

mkdir -p "$LOG_DIR"

cleanup() {
    echo "Shutting down Wave Server..."
    [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null
    [[ -n "${DASHBOARD_PID:-}" ]] && kill "$DASHBOARD_PID" 2>/dev/null
    wait 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

# Start backend
echo "Starting backend (http://localhost:8000) ..."
cd "$PROJECT_DIR"
uv run main.py > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!

# Start dashboard
echo "Starting dashboard (http://localhost:3000) ..."
cd "$DASHBOARD_DIR"
npm run dev > "$LOG_DIR/dashboard.log" 2>&1 &
DASHBOARD_PID=$!

# Wait for backend to be ready
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/health > /dev/null 2>&1; then
        echo "Backend ready."
        break
    fi
    sleep 1
done

# Open dashboard in default browser
if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000 &
fi

echo "Wave Server running. Press Ctrl+C to stop."
echo "  Backend log:   $LOG_DIR/backend.log"
echo "  Dashboard log: $LOG_DIR/dashboard.log"

wait
