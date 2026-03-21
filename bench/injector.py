"""State change injection for benchmark tasks."""

from __future__ import annotations

import json
import os


def load_task_config(task_dir: str) -> dict:
    """Load task.json from a task directory."""
    with open(os.path.join(task_dir, "task.json")) as f:
        return json.load(f)


def inject(task_work_dir: str, task_config: dict) -> None:
    """Apply the configured state change to the working directory."""
    inject_file = task_config.get("inject_file")
    inject_content = task_config.get("inject_content")
    inject_type = task_config.get("inject_type", "file_change")

    if inject_type == "delete_file" and inject_file:
        target = os.path.join(task_work_dir, inject_file)
        if os.path.exists(target):
            os.remove(target)
        return

    if inject_type == "double_change" and inject_file:
        # Write intermediate value first, then final value
        target = os.path.join(task_work_dir, inject_file)
        intermediate = task_config.get("inject_intermediate", "")
        if intermediate:
            with open(target, "w") as f:
                f.write(intermediate)
        if inject_content is not None:
            with open(target, "w") as f:
                f.write(inject_content)
        return

    if inject_type == "rapid_churn" and inject_file:
        target = os.path.join(task_work_dir, inject_file)
        count = task_config.get("inject_count", 50)
        for i in range(1, count + 1):
            with open(target, "w") as f:
                f.write(f"{i}\n")
        return

    if inject_file and inject_content is not None:
        target = os.path.join(task_work_dir, inject_file)
        with open(target, "w") as f:
            f.write(inject_content)
