"""Machine-verifiable scoring for benchmark tasks."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass
class TaskResult:
    task_id: str
    with_crux: bool
    completed: bool
    verified: bool
    total_tokens: int
    steps_taken: int
    steps_repeated: int
    stale_belief_errors: int
    crash_recoveries: int


def verify_task(task_dir: str, verify_script: str) -> bool:
    """Run verify.sh and return True if exit code is 0."""
    env = {**os.environ, "TASK_DIR": task_dir}
    result = subprocess.run(
        ["bash", verify_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def compute_mci(results: list[TaskResult]) -> float:
    """Compute Model Coherence Index from task results."""
    if not results:
        return 0.0

    completed = [r for r in results if r.verified]
    completion_rate = len(completed) / len(results)

    avg_tokens = sum(r.total_tokens for r in results) / len(results)
    baseline_tokens = 50_000
    token_overhead = max(0, (avg_tokens - baseline_tokens) / baseline_tokens)

    mci = completion_rate * (1 - min(token_overhead, 0.5))
    return round(mci, 3)
