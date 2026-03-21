"""Benchmark runner — orchestrates task runs with and without Crux."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import warnings

from agent import AgentTools, TASK_AGENTS
from injector import inject, load_task_config
from scorer import TaskResult, verify_task, compute_mci, format_benchmark_report

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(BENCH_DIR, "tasks")
DAEMON_BIN = os.path.join(BENCH_DIR, "..", "daemon", "target", "release", "crux-daemon")


class BenchmarkRunner:
    def run_task(self, task_dir: str, with_crux: bool) -> TaskResult:
        task_config = load_task_config(task_dir)
        task_id = task_config["id"]
        agent_fn = TASK_AGENTS.get(task_id)
        if agent_fn is None:
            return TaskResult(
                task_id=task_id, with_crux=with_crux, completed=False, verified=False,
                total_tokens=0, steps_taken=0, steps_repeated=0,
                stale_belief_errors=0, crash_recoveries=0,
            )

        # Create isolated working directory
        work_dir = tempfile.mkdtemp(prefix=f"crux-bench-{task_id}-")
        setup_dir = os.path.join(task_dir, "setup")
        if os.path.exists(setup_dir):
            for item in os.listdir(setup_dir):
                src = os.path.join(setup_dir, item)
                dst = os.path.join(work_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        daemon_proc = None
        interceptor = None
        world = None
        socket_path = None

        try:
            if with_crux:
                # Start daemon
                socket_path = os.path.join(work_dir, "crux.sock")
                home_dir = tempfile.mkdtemp()
                daemon_proc = subprocess.Popen(
                    [DAEMON_BIN, "--socket-path", socket_path],
                    env={**os.environ, "HOME": home_dir},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                # Wait for socket
                for _ in range(50):
                    if os.path.exists(socket_path):
                        break
                    time.sleep(0.05)

                import sys
                sys.path.insert(0, os.path.join(BENCH_DIR, "..", "sdk"))
                from crux import WorldModel, CruxInterceptor

                world = WorldModel(socket_path=socket_path)
                interceptor = CruxInterceptor(world)

            tools = AgentTools(work_dir, interceptor=interceptor)

            # Define injection function
            def do_inject():
                inject(work_dir, task_config)
                if interceptor and task_config.get("inject_file"):
                    # Re-observe the changed file so Crux detects it.
                    # The interceptor handles FileNotFoundError internally
                    # (observes exists=False then re-raises).
                    fpath = os.path.join(work_dir, task_config["inject_file"])
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        try:
                            interceptor.read_file(fpath)
                        except FileNotFoundError:
                            pass  # interceptor already observed it

            # Run the agent
            agent_fn(tools, inject_fn=do_inject)
            completed = True

        except Exception as e:
            print(f"  Task {task_id} error: {e}")
            completed = False
        finally:
            if world:
                try:
                    world.close()
                except Exception:
                    pass
            if daemon_proc:
                daemon_proc.send_signal(signal.SIGTERM)
                daemon_proc.wait(timeout=5)

        # Verify
        verify_script = os.path.join(task_dir, "verify.sh")
        verified = False
        if completed and os.path.exists(verify_script):
            verified = verify_task(work_dir, verify_script)

        tokens = tools._token_count if completed else 0

        return TaskResult(
            task_id=task_id,
            with_crux=with_crux,
            completed=completed,
            verified=verified,
            total_tokens=tokens,
            steps_taken=tools.steps_taken,
            steps_repeated=tools.steps_repeated,
            stale_belief_errors=tools.stale_belief_errors,
            crash_recoveries=0,
        )

    def run_suite(self, with_crux: bool, task_ids: list[str] | None = None) -> list[TaskResult]:
        results = []
        task_dirs = sorted([
            os.path.join(TASKS_DIR, d)
            for d in os.listdir(TASKS_DIR)
            if os.path.isdir(os.path.join(TASKS_DIR, d))
        ])

        for task_dir in task_dirs:
            task_config = load_task_config(task_dir)
            if task_ids and task_config["id"] not in task_ids:
                continue

            mode = "with Crux" if with_crux else "without Crux"
            print(f"Running {task_config['id']} ({task_config['name']}) {mode}...")
            result = self.run_task(task_dir, with_crux=with_crux)
            status = "PASS" if result.verified else "FAIL"
            print(f"  {status} | steps={result.steps_taken} tokens={result.total_tokens}")
            results.append(result)

        return results


def main():
    runner = BenchmarkRunner()

    print("=" * 60)
    print("CRUX BENCHMARK SUITE")
    print("=" * 60)

    # Run without Crux
    print("\n--- Baseline (without Crux) ---")
    baseline_results = runner.run_suite(with_crux=False)
    baseline_mci = compute_mci(baseline_results)
    print(f"\nBaseline MCI: {baseline_mci}")

    # Run with Crux
    print("\n--- With Crux ---")
    crux_results = runner.run_suite(with_crux=True)
    crux_mci = compute_mci(crux_results)
    print(f"\nCrux MCI: {crux_mci}")

    # Summary
    improvement = crux_mci - baseline_mci
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Baseline MCI: {baseline_mci}")
    print(f"Crux MCI:     {crux_mci}")
    print(f"Improvement:  {improvement:+.3f}")
    print(f"Target:       >= 0.250")
    print(f"Status:       {'PASS' if improvement >= 0.25 else 'BELOW TARGET'}")

    # Per-scenario comparison table
    print("\n" + "=" * 60)
    print("PER-SCENARIO BREAKDOWN")
    print("=" * 60)
    print(format_benchmark_report(baseline_results, crux_results))


if __name__ == "__main__":
    main()
