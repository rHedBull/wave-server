"""Shared test helpers for simulating pi runtime behavior (rate limits, errors).

Used by test_feature_executor.py and test_e2e_execution.py to avoid
duplicating the rate-limited output builder and mock runner.
"""

from __future__ import annotations

import asyncio
import json

from wave_server.engine.runner import PiRunner, _detect_pi_output_failure
from wave_server.engine.types import RunnerConfig, RunnerResult


def build_rate_limited_pi_output() -> str:
    """Build realistic pi JSON output that simulates a rate-limited task.

    Pi exits 0 even when all retries fail. The output contains:
    - agent_end with stopReason=error and errorMessage=429
    - auto_retry_end with success=false

    This matches the actual output observed from pi during execution
    de49b90b when wave 3 tasks hit account rate limits.
    """
    return "\n".join(
        [
            json.dumps({"type": "session", "version": 3, "id": "rate-limit-test"}),
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "turn_start"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
                    },
                    "toolResults": [],
                }
            ),
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "attempt": 3,
                    "finalError": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
                }
            ),
        ]
    )


class RateLimitPiMockRunner:
    """Mock runner that simulates PiRunner behavior with rate-limited tasks.

    Rate-limited tasks return exit_code=0 (like real pi) with error JSON output.
    Applies _detect_pi_output_failure to override the exit code, just like the
    real PiRunner.spawn() does.
    """

    def __init__(
        self,
        rate_limited_task_ids: set[str],
        delay_s: float = 0.0,
    ):
        self.rate_limited_task_ids = rate_limited_task_ids
        self.delay_s = delay_s
        self.spawned: list[str] = []
        self._pi_runner = PiRunner()

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)

        if config.task_id in self.rate_limited_task_ids:
            stdout = build_rate_limited_pi_output()
            exit_code = 0  # Pi exits 0 despite failure!
            stderr = ""

            # Apply the same detection logic PiRunner.spawn() uses
            detected = _detect_pi_output_failure(stdout)
            rate_limited = False
            if detected:
                exit_code = 1
                stderr = detected.error
                rate_limited = detected.rate_limited

            return RunnerResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                rate_limited=rate_limited,
            )

        # Normal success
        stdout = json.dumps(
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": f"Completed task {config.task_id}"}
                        ],
                        "stopReason": "stop",
                    }
                ],
            }
        )
        return RunnerResult(exit_code=0, stdout=stdout, stderr="")

    def extract_final_output(self, stdout: str) -> str:
        return self._pi_runner.extract_final_output(stdout)
