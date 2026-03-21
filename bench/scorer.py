"""Machine-verifiable scoring for benchmark tasks."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field


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
    wasted_work: int = 0  # re-derivations that produced no new information


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


def compute_scenario_mci(baseline: TaskResult, crux: TaskResult) -> float:
    """Compute per-scenario MCI comparing Crux vs baseline.

    Returns negative if Crux made things worse (more tokens/steps for same
    or worse correctness), zero if no difference, positive if Crux helped.
    """
    # Correctness delta: +1 if Crux fixed it, -1 if Crux broke it, 0 if same
    if crux.verified and not baseline.verified:
        correctness = 1.0
    elif not crux.verified and baseline.verified:
        correctness = -1.0
    elif crux.verified and baseline.verified:
        correctness = 0.0
    else:
        correctness = 0.0

    # Overhead delta: negative if Crux used more tokens for no correctness gain
    if baseline.total_tokens > 0:
        token_ratio = crux.total_tokens / baseline.total_tokens
    else:
        token_ratio = 1.0

    if correctness > 0:
        # Crux fixed something -- positive MCI, penalized by overhead
        return round(correctness * (1 - min(max(token_ratio - 1, 0), 0.5)), 3)
    elif correctness < 0:
        # Crux broke something -- negative MCI
        return -1.0
    else:
        # Same correctness -- MCI is based on overhead
        if token_ratio > 1.05:
            # Crux added >5% overhead for no benefit
            return round(-(token_ratio - 1), 3)
        elif token_ratio < 0.95:
            # Crux was more efficient
            return round(1 - token_ratio, 3)
        else:
            return 0.0


def format_benchmark_report(
    baseline_results: list[TaskResult],
    crux_results: list[TaskResult],
) -> str:
    """Format a full benchmark report with per-scenario MCI."""
    lines = []
    lines.append("| Scenario | Baseline | Crux | MCI | Interpretation |")
    lines.append("|---|---|---|---|---|")

    baseline_map = {r.task_id: r for r in baseline_results}
    crux_map = {r.task_id: r for r in crux_results}

    for task_id in sorted(set(list(baseline_map.keys()) + list(crux_map.keys()))):
        b = baseline_map.get(task_id)
        c = crux_map.get(task_id)
        if not b or not c:
            continue

        mci = compute_scenario_mci(b, c)

        b_status = "PASS" if b.verified else "FAIL"
        c_status = "PASS" if c.verified else "FAIL"
        b_info = f"{b_status} ({b.total_tokens}tok)"
        c_info = f"{c_status} ({c.total_tokens}tok)"

        if mci > 0.5:
            interp = "Crux core value prop"
        elif mci > 0:
            interp = "Crux helps"
        elif mci == 0:
            interp = "No difference"
        elif mci > -0.2:
            interp = "Minor overhead tax"
        else:
            interp = "Crux adds significant overhead"

        lines.append(f"| {task_id} | {b_info} | {c_info} | {mci:+.3f} | {interp} |")

    return "\n".join(lines)
