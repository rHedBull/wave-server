# Spec: Add Pi Runtime Runner to Wave Server

## Goal

Add a `PiRunner` class that implements the `AgentRunner` protocol, using `pi` CLI instead of `claude` CLI as the task execution runtime. This reduces system prompt overhead from ~32K tokens to ~1.3K tokens per turn (23x reduction), which directly cuts input token costs.

## Why

Claude Code CLI loads 45 tools (including Playwright, Chrome, episodic memory), session hooks injecting ~9KB of "superpowers" context, and CLAUDE.md — none of which are needed for wave tasks. Pi can be invoked with only the 4 tools needed (read, write, edit, bash) and zero extensions/hooks.

## Files to modify

- `wave_server/engine/runner.py` — add `PiRunner` class, register in `get_runner()`
- `wave_server/engine/log_parser.py` — add `parse_pi_json()` or extend `parse_stream_json()` to handle pi's format
- `wave_server/engine/execution_manager.py` — update the task log writing to detect runtime and use correct parser
- `tests/test_runner.py` — add tests for PiRunner output parsing

## Detailed Instructions

### 1. Add `PiRunner` to `wave_server/engine/runner.py`

Implement a new class that satisfies the `AgentRunner` protocol:

```python
@runtime_checkable
class AgentRunner(Protocol):
    async def spawn(self, config: RunnerConfig) -> RunnerResult: ...
    def extract_final_output(self, stdout: str) -> str: ...
```

`RunnerConfig` fields (from `wave_server/engine/types.py`):
```python
@dataclass
class RunnerConfig:
    task_id: str
    prompt: str
    cwd: str
    timeout_ms: int | None = None
    env: dict[str, str] | None = None
    model: str | None = None
```

#### Pi CLI invocation

Build the command as:
```python
cmd = [
    "pi",
    "--print",
    "--mode", "json",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-session",
    "--tools", "read,bash,edit,write",
]
if config.model:
    cmd += ["--model", config.model]
cmd.append(config.prompt)
```

Key flags explained:
- `--print` — non-interactive mode, process prompt and exit
- `--mode json` — outputs JSONL events (equivalent to Claude's `--output-format stream-json`)
- `--no-extensions` — disables extension discovery (no MCP servers, no plugins)
- `--no-skills` — disables skill loading
- `--no-prompt-templates` — disables prompt template loading
- `--no-themes` — disables theme loading
- `--no-session` — don't persist session to disk (ephemeral)
- `--tools read,bash,edit,write` — only enable the 4 tools needed for code tasks

Spawn the subprocess identically to `ClaudeCodeRunner` — use `asyncio.create_subprocess_exec`, handle timeout, merge env vars, decode stdout/stderr. The `spawn()` method body is almost identical to `ClaudeCodeRunner.spawn()`, just with different `cmd` construction.

#### `extract_final_output(stdout)` for pi

Pi's JSONL format uses these event types (in order):

```
session          — session metadata (id, cwd, timestamp)
agent_start      — marks agent beginning
turn_start       — marks a new turn
message_end      — completed message with role, content, usage
  role=user        content: [{type: "text", text: "..."}]
  role=assistant   content: [{type: "text", text: "..."}, {type: "toolCall", name: "...", arguments: {...}}]
  role=toolResult  content: [{type: "text", text: "..."}]
tool_execution_start/end — tool execution markers (ignore these)
message_update   — streaming deltas (ignore these)
turn_end         — marks turn completion
agent_end        — final summary with all messages array
```

To extract the final output text:

```python
def extract_final_output(self, stdout: str) -> str:
    result_parts: list[str] = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if msg.get("type") == "agent_end":
                # Get text from the last assistant message
                messages = msg.get("messages", [])
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        for block in m.get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                result_parts.append(block["text"])
                        if result_parts:
                            break
            elif msg.get("type") == "message_end":
                m = msg.get("message", {})
                if m.get("role") == "assistant":
                    for block in m.get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip():
                            result_parts.append(block["text"])
        except (json.JSONDecodeError, KeyError):
            continue

    if result_parts:
        return result_parts[-1]  # Last assistant text is the final output

    lines = [l for l in stdout.split("\n") if l.strip()]
    return "\n".join(lines[-10:]) if lines else "(no output)"
```

#### Register in `get_runner()`

```python
def get_runner(runtime: str = "claude") -> AgentRunner:
    if runtime == "claude":
        return ClaudeCodeRunner()
    if runtime == "pi":
        return PiRunner()
    raise ValueError(f"Unknown runtime: {runtime}. Available: claude, pi")
```

### 2. Add `parse_pi_json()` to `wave_server/engine/log_parser.py`

The existing `parse_stream_json()` parses Claude Code's stream-json format. Add a parallel parser for pi's JSON format that returns the same `ParsedLog` dataclass.

Pi's format differences from Claude Code:

| Concept | Claude Code | Pi |
|---------|------------|-----|
| Assistant message | `{"type": "assistant", "message": {"content": [...]}}` | `{"type": "message_end", "message": {"role": "assistant", "content": [...]}}` |
| Tool call | `content[].type = "tool_use"`, field `input` | `content[].type = "toolCall"`, field `arguments` |
| Tool result | `{"type": "user", "content": [{"type": "tool_result", ...}]}` | `{"type": "message_end", "message": {"role": "toolResult", "content": [...]}}` |
| Final result | `{"type": "result", "result": "...", "total_cost_usd": ...}` | `{"type": "agent_end", "messages": [...]}` — no separate result event |
| Usage per turn | `message.usage.input_tokens` | `message.usage.input` |
| Total cost | `result.total_cost_usd` | Sum of `message.usage.cost.total` across all assistant messages |
| Model | `message.model` | `message.model` (same) |
| Cache tokens | `usage.cache_read_input_tokens` | `usage.cacheRead` |

Implementation:

```python
def parse_pi_json(raw: str) -> ParsedLog:
    """Parse pi's JSON mode JSONL output into a structured log."""
    log = ParsedLog()
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "message_end":
            message = event.get("message", {})
            role = message.get("role", "")

            if not log.model and message.get("model"):
                log.model = message["model"]

            usage = message.get("usage")
            if usage:
                total_input += usage.get("input", 0)
                total_output += usage.get("output", 0)
                total_cache_read += usage.get("cacheRead", 0)
                total_cache_write += usage.get("cacheWrite", 0)
                cost = usage.get("cost", {})
                total_cost += cost.get("total", 0.0)

            if role == "assistant":
                turn = AssistantTurn()
                for block in message.get("content", []):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            turn.text += text
                    elif block_type == "toolCall":
                        tc = ToolCall(
                            name=block.get("name", "unknown"),
                            input_summary=_summarize_tool_input(
                                block.get("name", ""), block.get("arguments", {})
                            ),
                            tool_use_id=block.get("id", ""),
                        )
                        turn.tool_calls.append(tc)
                if turn.text or turn.tool_calls:
                    log.turns.append(turn)

            elif role == "toolResult":
                content_parts = []
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        content_parts.append(block.get("text", ""))
                tr = ToolResult(
                    tool_use_id="",
                    name="",
                    content="\n".join(content_parts),
                    is_error=False,
                )
                log.turns.append(tr)

        elif event_type == "agent_end":
            # Extract final text from last assistant message
            messages = event.get("messages", [])
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    for block in m.get("content", []):
                        if block.get("type") == "text":
                            log.final_result = block.get("text", "")
                            break
                    break
            log.num_turns = sum(1 for m in messages if m.get("role") == "assistant")

    log.total_cost_usd = total_cost
    log.input_tokens = total_input
    log.output_tokens = total_output
    log.cache_read_tokens = total_cache_read
    log.cache_creation_tokens = total_cache_write

    return log
```

### 3. Update `wave_server/engine/execution_manager.py`

In the `on_task_end` callback (~line 427), the code calls `parse_stream_json(result.stdout)`. This needs to detect the runtime and use the correct parser.

Find this block in `_run_execution()`:
```python
parsed = parse_stream_json(result.stdout or "")
```

Change to:
```python
from wave_server.engine.log_parser import parse_stream_json, parse_pi_json

if runtime == "pi":
    parsed = parse_pi_json(result.stdout or "")
else:
    parsed = parse_stream_json(result.stdout or "")
```

The `runtime` variable is already available in `_run_execution()` scope (it's read from execution config at the top of the function, around line 280).

### 4. Tests

Create `tests/test_pi_runner.py` with:

1. **Test `PiRunner.extract_final_output()`** — feed it sample pi JSONL output and verify it extracts the correct final text.

2. **Test `parse_pi_json()`** — feed it sample pi JSONL and verify:
   - `parsed.model` is set correctly
   - `parsed.turns` contains the right tool calls and text
   - `parsed.total_cost_usd` sums correctly
   - `parsed.input_tokens` / `parsed.output_tokens` are correct
   - `parsed.final_result` has the last assistant text

Use this sample JSONL as test fixture (captured from real pi output):

```python
SAMPLE_PI_OUTPUT = '''
{"type":"session","version":3,"id":"test-session","timestamp":"2026-03-07T17:08:16.131Z","cwd":"/tmp/test"}
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"Create hello.txt"}]}}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"tool_1","name":"write","arguments":{"path":"hello.txt","content":"hello world"}}],"model":"claude-sonnet-4-5","usage":{"input":1353,"output":79,"cacheRead":0,"cacheWrite":0,"totalTokens":1432,"cost":{"input":0.004059,"output":0.000395,"cacheRead":0,"cacheWrite":0,"total":0.004454}}}}
{"type":"message_end","message":{"role":"toolResult","content":[{"type":"text","text":"Successfully wrote 11 bytes to hello.txt"}]}}
{"type":"turn_end"}
{"type":"turn_start"}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Created hello.txt with content 'hello world'."}],"model":"claude-sonnet-4-5","usage":{"input":1461,"output":21,"cacheRead":0,"cacheWrite":0,"totalTokens":1482,"cost":{"input":0.004383,"output":0.000105,"cacheRead":0,"cacheWrite":0,"total":0.004488}}}}
{"type":"turn_end"}
{"type":"agent_end","messages":[{"role":"user","content":[{"type":"text","text":"Create hello.txt"}]},{"role":"assistant","content":[{"type":"toolCall","id":"tool_1","name":"write","arguments":{"path":"hello.txt","content":"hello world"}}],"usage":{"input":1353,"output":79,"cost":{"total":0.004454}}},{"role":"toolResult","content":[{"type":"text","text":"Successfully wrote 11 bytes to hello.txt"}]},{"role":"assistant","content":[{"type":"text","text":"Created hello.txt with content 'hello world'."}],"usage":{"input":1461,"output":21,"cost":{"total":0.004488}}}]}
'''.strip()
```

3. **Test `get_runner("pi")`** returns a `PiRunner` instance.
4. **Test `get_runner("invalid")`** raises `ValueError`.

### 5. Do NOT change

- `RunnerConfig` / `RunnerResult` dataclasses — they work as-is
- `_build_task_prompt()` — the prompt format is runtime-agnostic
- `WaveExecutorOptions` — runtime is already handled at a higher level
- `config.py` — already has `runtime: str = "claude"` field

## Expected token savings

| Runtime | System prompt tokens | Tools loaded | Per-turn overhead |
|---------|---------------------|--------------|-------------------|
| Claude Code | ~32,000 | 45 | High (hooks, CLAUDE.md, plugins) |
| Pi | ~1,350 | 4 | Minimal (bare tools only) |

On a 13-turn task at Opus pricing ($5/MTok input cached):
- Claude Code: 13 × 32K = 416K cached input tokens = ~$2.08
- Pi: 13 × 1.35K = 17.5K cached input tokens = ~$0.09
- **Savings: ~$2/task, ~96% reduction in input token costs**

## Verification

After implementation, run the existing plan on the same sequence with `runtime: "pi"` and compare:
1. Input token counts should be ~23x lower per turn
2. Cost per task should drop significantly
3. Task outputs should be equivalent quality
