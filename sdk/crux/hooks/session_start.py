#!/usr/bin/env python3
"""Claude Code SessionStart hook — starts a Crux run and daemon if needed.

On session start:
1. Check if crux-daemon is running (socket exists and responds)
2. If not, start it in the background
3. Send run_start to initialize the session's world model
4. Query and display stale beliefs from previous sessions
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

try:
    import msgpack  # noqa: F401
except ImportError:
    print("[crux] msgpack not installed - run: pip install crux-sdk")
    sys.exit(0)

from ._ipc import SOCKET_PATH, daemon_running, send_msg


def _resolve_daemon_bin() -> str:
    """Find the daemon binary: env var, PATH, or auto-download via SDK."""
    env = os.environ.get("CRUX_DAEMON")
    if env:
        return env
    try:
        from ..binary import get_daemon_path
        return get_daemon_path()
    except Exception:
        return "crux-daemon"


def start_daemon() -> bool:
    """Start the daemon in the background."""
    daemon_bin = _resolve_daemon_bin()
    try:
        subprocess.Popen(
            [daemon_bin, "--socket-path", SOCKET_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(20):
            if daemon_running():
                return True
            time.sleep(0.1)
    except FileNotFoundError:
        pass
    return False


def shorten_path(path: str) -> str:
    """Shorten a file path for display."""
    if not path:
        return path
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[-2:])


def format_belief_warning(belief: dict, status_label: str) -> str:
    """Format a single belief into an actionable warning line."""
    belief_type = belief.get("belief_type", "unknown")
    identity_key = belief.get("identity_key")
    dependents = belief.get("dependents", [])

    if not identity_key:
        return ""

    display_key = shorten_path(identity_key) if belief_type == "file" else identity_key

    if belief_type == "file":
        source = f"file:{display_key}"
    elif belief_type == "shell_output":
        cmd = identity_key[:40] + "..." if len(identity_key) > 40 else identity_key
        source = f"cmd:`{cmd}`"
    elif belief_type == "plan":
        source = f"plan:{identity_key}"
    else:
        source = f"{belief_type}:{display_key}"

    if dependents:
        dep_strs = []
        for dep in dependents[:3]:
            dep_type = dep.get("belief_type", "?")
            dep_key = dep.get("identity_key", "?")
            if dep_type == "plan":
                dep_strs.append(f"plan:{dep_key}")
            elif dep_type == "file":
                dep_strs.append(f"file:{shorten_path(dep_key)}")
            else:
                dep_strs.append(f"{dep_type}:{dep_key[:20]}")
        if len(dependents) > 3:
            dep_strs.append(f"+{len(dependents) - 3} more")
        return f"  [{status_label}] {source} -> affects {', '.join(dep_strs)}"
    else:
        return f"  [{status_label}] {source}"


def format_stale_warnings(stale: list) -> str:
    """Format stale beliefs into actionable warnings."""
    invalidated = [b for b in stale if b.get("status") == "invalidated"]
    uncertain = [b for b in stale if b.get("status") == "uncertain"]

    warnings = []
    for b in invalidated:
        w = format_belief_warning(b, "INVALIDATED")
        if w:
            warnings.append(w)
    for b in uncertain:
        w = format_belief_warning(b, "uncertain")
        if w:
            warnings.append(w)

    if not warnings:
        return f"{len(stale)} stale beliefs (no actionable details)"

    max_warnings = 5
    if len(warnings) > max_warnings:
        shown = warnings[:max_warnings]
        shown.append(f"  ... and {len(warnings) - max_warnings} more")
        return "\n".join(shown)

    return "\n".join(warnings)


def get_stale_summary() -> str:
    """Query daemon for all stale beliefs and return actionable warnings."""
    resp = send_msg({"type": "stale", "request_id": "init-stale"})
    if not resp:
        return ""
    stale = resp.get("stale", [])
    if not stale:
        return ""
    return format_stale_warnings(stale)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        data = {}

    cwd = data.get("cwd", os.getcwd())

    project_hash = hashlib.sha256(cwd.encode()).hexdigest()[:12]
    run_id = f"crux-{project_hash}"

    if not daemon_running():
        if not start_daemon():
            print("[crux] daemon unavailable - beliefs not tracked")
            return

    resp = send_msg({"type": "run_start", "run_id": run_id, "request_id": "init"})
    if not resp or not resp.get("ok"):
        print("[crux] failed to connect to daemon")
        return

    stale_summary = get_stale_summary()
    if stale_summary:
        print(f"""[crux] Stale beliefs detected from previous sessions:
{stale_summary}

INVALIDATED = file changed since last read. Re-read before relying on assumptions.
uncertain = may have changed (TTL expired). Re-read if relevant to current task.
If a plan depends on invalidated files, re-read those files and decide if the plan still holds.""")
    else:
        print("[crux] all beliefs valid - no stale context from previous sessions")


if __name__ == "__main__":
    main()
