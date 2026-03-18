Output demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1100
Set Height 650
Set Theme "Catppuccin Mocha"
Set Padding 20

Type "# 🌊 Wave Server — Multi-Agent Orchestration Demo"
Enter
Sleep 1s

Type "# Start the server"
Enter
Type "uv run uvicorn wave_server.main:app --port 9722 &"
Enter
Sleep 3s

Type "# Check health"
Enter
Type 'curl -s localhost:9722/api/health | python3 -m json.tool'
Enter
Sleep 2s

Type "# Create a project and register a repo"
Enter
Type 'API=http://localhost:9722/api/v1'
Enter
Type 'PID=$(curl -s -X POST $API/projects -H "Content-Type: application/json" -d \'{"name":"demo"}\' | python3 -c "import sys,json;print(json.load(sys.stdin)[\'id\'])")'
Enter
Sleep 1s
Type 'curl -s -X POST $API/projects/$PID/repositories -H "Content-Type: application/json" -d \'{"path":"/tmp/demo-repo"}\' > /dev/null'
Enter
Sleep 1s

Type "# Create a sequence and upload a plan"
Enter
Type 'SID=$(curl -s -X POST $API/projects/$PID/sequences -H "Content-Type: application/json" -d \'{"name":"hello-world"}\' | python3 -c "import sys,json;print(json.load(sys.stdin)[\'id\'])")'
Enter
Sleep 1s

Type "# Upload execution plan (DAG with foundation + integration)"
Enter
Type "cat plan.md | head -20"
Enter
Sleep 3s

Type 'curl -s -X POST $API/sequences/$SID/plan -H "Content-Type: text/plain" --data-binary @plan.md > /dev/null'
Enter
Sleep 1s

Type "# 🚀 Start execution with Sonnet"
Enter
Type 'EID=$(curl -s -X POST $API/sequences/$SID/executions -H "Content-Type: application/json" -d \'{"runtime":"pi","model":"claude-sonnet-4-20250514"}\' | python3 -c "import sys,json;print(json.load(sys.stdin)[\'id\'])")'
Enter
Type 'echo "Execution started: $EID"'
Enter
Sleep 2s

Type "# ⏳ Poll until complete..."
Enter
Type 'for i in $(seq 1 30); do S=$(curl -s $API/executions/$EID | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"{d[\"status\"]} {d[\"completed_tasks\"]}/{d[\"total_tasks\"]}\")"); echo "  [$i] $S"; [[ "$S" == completed* ]] && break; sleep 5; done'
Enter
Sleep 65s

Type "# ✅ Check results"
Enter
Type 'curl -s $API/executions/$EID/events | python3 -c "import sys,json; [print(f\"{e[\"event_type\"]:25s} task={e[\"task_id\"] or \"-\"}\") for e in json.load(sys.stdin)]"'
Enter
Sleep 2s

Type "# 📄 The agent created the file:"
Enter
Type "git -C /tmp/demo-repo show wave/exec-${EID:0:8}:hello.py"
Enter
Sleep 3s

Type "# 650 tests. Full DAG execution. Git worktree isolation. 🌊"
Enter
Sleep 2s
