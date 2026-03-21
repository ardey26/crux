#!/usr/bin/env python3
"""Claude Code PostToolUse hook — bridges tool calls to Crux observations.

Auto-creates dependency edges: when a Write/Edit follows Reads, the written
file automatically depends on all recently-read files. No manual wiring.

Auto-registers Plan beliefs when a plan file is written to a /plans/ or /plan/
directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

try:
    import msgpack  # noqa: F401
except ImportError:
    sys.exit(0)

from ._ipc import send_msg


def hash8(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:8]


# -- Plan detection --

PLAN_PATH_MARKERS = ("/plans/", "/plan/")
PLAN_EXTENSION = ".md"


def is_plan_file(path: str) -> bool:
    """Check if a written file looks like a plan document."""
    if not path.endswith(PLAN_EXTENSION):
        return False
    return any(marker in path for marker in PLAN_PATH_MARKERS)


def extract_plan_metadata(path: str, content: str | None = None) -> tuple[str, str]:
    """Derive plan_id and goal from a plan file path and content.

    plan_id: filename stem (e.g. '2026-03-22-http-graph-visualization')
    goal: first markdown heading, or plan_id if none found.
    """
    plan_id = os.path.basename(path).removesuffix(PLAN_EXTENSION)

    goal = plan_id
    text = content
    if text is None:
        try:
            with open(path) as f:
                text = f.read(4096)
        except OSError:
            text = ""
    if text:
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("# ") and not line.startswith("# ["):
                goal = line[2:].strip()
                for suffix in (" — Implementation Plan", " Implementation Plan", " - Implementation Plan"):
                    if goal.endswith(suffix):
                        goal = goal[: -len(suffix)]
                break
    return plan_id, goal


def register_plan(path: str, content: str | None = None) -> dict | None:
    """Send create_plan to the daemon for a plan file write."""
    plan_id, goal = extract_plan_metadata(path, content)
    h = ""
    if content:
        h = hash8(content)
    else:
        try:
            with open(path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()[:8]
        except OSError:
            pass

    return send_msg({
        "type": "create_plan",
        "plan_id": plan_id,
        "goal": goal,
        "payload": {
            "goal": goal,
            "file": os.path.abspath(path),
            "hash": h,
        },
    })


# -- State file for tracking recent reads across hook invocations --

def state_path(session_id: str) -> str:
    return f"/tmp/crux-edges-{session_id[:16]}.json"


def load_recent_reads(session_id: str) -> dict:
    """Load {path: belief_id} map of recently-read files."""
    try:
        with open(state_path(session_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_recent_reads(session_id: str, reads: dict) -> None:
    with open(state_path(session_id), "w") as f:
        json.dump(reads, f)


# -- Observation builders --

def observe_file_read(path: str) -> dict | None:
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        h = hashlib.sha256(raw).hexdigest()[:8]
        return {
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": path},
            "result": {"exists": True, "hash": h, "size": len(raw)},
            "status": "success",
        }
    except (FileNotFoundError, IsADirectoryError):
        return {
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": path},
            "result": {"exists": False, "hash": "", "size": 0},
            "status": "failure",
        }


def observe_file_write(path: str, content: str) -> dict:
    path = os.path.abspath(path)
    h = hash8(content)
    return {
        "type": "observe",
        "action": "read_file",
        "inputs": {"path": path},
        "result": {"exists": True, "hash": h, "size": len(content.encode())},
        "status": "success",
    }


def observe_file_edit(path: str) -> dict | None:
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        h = hashlib.sha256(raw).hexdigest()[:8]
        return {
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": path},
            "result": {"exists": True, "hash": h, "size": len(raw)},
            "status": "success",
        }
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def observe_bash(command: str, tool_result: str) -> dict:
    h = hash8(tool_result) if tool_result else ""
    exit_code = 0
    if tool_result and "Exit code" in tool_result:
        try:
            first_line = tool_result.strip().split("\n")[0]
            if "Exit code" in first_line:
                exit_code = int(first_line.split("Exit code")[1].strip())
        except (ValueError, IndexError):
            pass
    return {
        "type": "observe",
        "action": "shell",
        "inputs": {"command": command},
        "result": {"stdout_hash": h, "exit_code": exit_code},
        "status": "success" if exit_code == 0 else "failure",
    }


# -- Auto-edge creation --

def create_auto_edges(new_belief_id: str, new_path: str, recent_reads: dict) -> int:
    """Create edges from the written file to all recently-read files."""
    created = 0
    for read_path, read_belief_id in recent_reads.items():
        if read_path == new_path:
            continue
        resp = send_msg({
            "type": "add_edge",
            "from": new_belief_id,
            "to": read_belief_id,
            "weight": 0.9,
            "request_id": f"auto-edge-{created}",
        })
        if resp and resp.get("ok"):
            created += 1
    return created


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_result = data.get("tool_result", "")
    session_id = data.get("session_id", "unknown")

    if not isinstance(tool_result, str):
        tool_result = json.dumps(tool_result) if tool_result else ""

    # Build observation message
    msg = None
    path = None
    is_write = False

    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if path:
            msg = observe_file_read(path)
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if path:
            msg = observe_file_write(path, content)
            is_write = True
    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        if path:
            msg = observe_file_edit(path)
            is_write = True
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            msg = observe_bash(command, tool_result)
    else:
        sys.exit(0)

    if msg is None:
        sys.exit(0)

    # Send observation to daemon
    resp = send_msg(msg)
    if resp is None:
        sys.exit(0)

    belief_id = resp.get("belief_id", "")
    if not belief_id:
        sys.exit(0)

    # Auto-edge logic
    recent_reads = load_recent_reads(session_id)
    output_parts = []

    if is_write and path:
        abs_path = os.path.abspath(path)
        edges_created = create_auto_edges(belief_id, abs_path, recent_reads)

        # Auto-register Plan belief when writing a plan file
        if is_plan_file(abs_path):
            content = tool_input.get("content")
            plan_resp = register_plan(abs_path, content)
            if plan_resp and plan_resp.get("ok"):
                plan_edges = plan_resp.get("edges_created", 0)
                output_parts.append(
                    f"[crux] registered plan '{extract_plan_metadata(abs_path, content)[0]}' "
                    f"with {plan_edges} dependency edge(s)"
                )

        # Clear reads after write -- next write starts fresh
        save_recent_reads(session_id, {})
    elif tool_name == "Read" and path:
        abs_path = os.path.abspath(path)
        recent_reads[abs_path] = belief_id
        save_recent_reads(session_id, recent_reads)
        edges_created = 0
    else:
        edges_created = 0

    # Output warnings for invalidations
    diff = resp.get("diff")
    if diff and (diff.get("invalidated") or diff.get("uncertain")):
        n_invalid = len(diff.get("invalidated", []))
        n_uncertain = len(diff.get("uncertain", []))
        parts = []
        if n_invalid:
            parts.append(f"{n_invalid} belief(s) invalidated")
        if n_uncertain:
            parts.append(f"{n_uncertain} belief(s) uncertain")
        output_parts.append(f"[crux] {', '.join(parts)}")

    if edges_created > 0:
        output_parts.append(f"[crux] auto-linked {edges_created} dependency edge(s)")

    if output_parts:
        print(" | ".join(output_parts))


if __name__ == "__main__":
    main()
