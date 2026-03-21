#!/usr/bin/env python3
"""Claude Code SessionStart hook — starts a Crux run and daemon if needed.

On session start:
1. Check if crux-daemon is running (socket exists and responds)
2. If not, start it in the background
3. Send run_start to initialize the session's world model
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

try:
    import msgpack
except ImportError:
    print("[crux] msgpack not installed - run: pip install msgpack")
    sys.exit(0)

def _resolve_daemon_bin() -> str:
    """Find the daemon binary: env var, PATH, or auto-download via SDK."""
    env = os.environ.get("CRUX_DAEMON")
    if env:
        return env
    # Try the SDK's binary resolver (handles PATH check + auto-download)
    try:
        # Add the SDK to the path if we're running from the hooks/ directory
        sdk_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk")
        if os.path.isdir(sdk_path):
            sys.path.insert(0, sdk_path)
        from crux.binary import get_daemon_path
        return get_daemon_path()
    except Exception:
        return "crux-daemon"  # fall back to PATH

DAEMON_BIN = _resolve_daemon_bin()
SOCKET_PATH = os.environ.get("CRUX_SOCKET", "/tmp/crux.sock")


def daemon_running() -> bool:
    """Check if the daemon is listening."""
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False
    finally:
        if s:
            s.close()


def start_daemon():
    """Start the daemon in the background."""
    try:
        subprocess.Popen(
            [DAEMON_BIN, "--socket-path", SOCKET_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for socket
        for _ in range(20):
            if daemon_running():
                return True
            time.sleep(0.1)
    except FileNotFoundError:
        pass
    return False


def send_run_start(run_id: str) -> bool:
    """Send run_start to the daemon."""
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(SOCKET_PATH)

        msg = {"type": "run_start", "run_id": run_id, "request_id": "init"}
        payload = msgpack.packb(msg, use_bin_type=True)
        s.sendall(struct.pack("<I", len(payload)) + payload)

        resp_len_bytes = _recv_exact(s, 4)
        resp_len = struct.unpack("<I", resp_len_bytes)[0]
        resp_data = _recv_exact(s, resp_len)

        resp = msgpack.unpackb(resp_data, raw=False)
        return resp.get("ok", False)
    except Exception:
        return False
    finally:
        if s:
            s.close()


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def main():
    global SOCKET_PATH

    # Read session data from stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        data = {}

    cwd = data.get("cwd", os.getcwd())

    # Run ID is project-scoped: same directory = same run = beliefs persist
    # across sessions. Different sessions in the same project share history.
    import hashlib
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()[:12]
    run_id = f"crux-{project_hash}"

    if not daemon_running():
        if not start_daemon():
            print("[crux] daemon unavailable - beliefs not tracked")
            return

    success = send_run_start(run_id)

    if not success:
        print("[crux] failed to connect to daemon")
        return

    # Query for stale beliefs from previous sessions
    stale_summary = get_stale_summary()
    if stale_summary:
        print(f"""[crux] Stale beliefs detected from previous sessions:
{stale_summary}

INVALIDATED = file changed since last read. Re-read before relying on assumptions.
uncertain = may have changed (TTL expired). Re-read if relevant to current task.
If a plan depends on invalidated files, re-read those files and decide if the plan still holds.""")
    else:
        print("[crux] all beliefs valid - no stale context from previous sessions")


def get_stale_summary() -> str:
    """Query daemon for all stale beliefs and return actionable warnings."""
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(SOCKET_PATH)

        msg = {"type": "stale", "request_id": "init-stale"}
        payload = msgpack.packb(msg, use_bin_type=True)
        s.sendall(struct.pack("<I", len(payload)) + payload)

        resp_len_bytes = _recv_exact(s, 4)
        resp_len = struct.unpack("<I", resp_len_bytes)[0]
        resp_data = _recv_exact(s, resp_len)

        resp = msgpack.unpackb(resp_data, raw=False)
        stale = resp.get("stale", [])
        if not stale:
            return ""

        return format_stale_warnings(stale)
    except Exception:
        return ""
    finally:
        if s:
            s.close()


def format_stale_warnings(stale: list) -> str:
    """Format stale beliefs into actionable warnings."""
    # Separate by status priority: invalidated first, then uncertain
    invalidated = [b for b in stale if b.get("status") == "invalidated"]
    uncertain = [b for b in stale if b.get("status") == "uncertain"]

    warnings = []

    # Format invalidated beliefs (most critical)
    for b in invalidated:
        warning = format_belief_warning(b, "INVALIDATED")
        if warning:
            warnings.append(warning)

    # Format uncertain beliefs (stale but not confirmed changed)
    for b in uncertain:
        warning = format_belief_warning(b, "uncertain")
        if warning:
            warnings.append(warning)

    if not warnings:
        return f"{len(stale)} stale beliefs (no actionable details)"

    # Limit to most important warnings to avoid noise
    max_warnings = 5
    if len(warnings) > max_warnings:
        shown = warnings[:max_warnings]
        remaining = len(warnings) - max_warnings
        shown.append(f"  ... and {remaining} more")
        return "\n".join(shown)

    return "\n".join(warnings)


def format_belief_warning(belief: dict, status_label: str) -> str:
    """Format a single belief into an actionable warning line."""
    belief_type = belief.get("belief_type", "unknown")
    identity_key = belief.get("identity_key")
    dependents = belief.get("dependents", [])

    if not identity_key:
        return ""

    # Shorten file paths for readability
    display_key = shorten_path(identity_key) if belief_type == "file" else identity_key

    # Format the source
    if belief_type == "file":
        source = f"file:{display_key}"
    elif belief_type == "shell_output":
        # Truncate long commands
        cmd = identity_key[:40] + "..." if len(identity_key) > 40 else identity_key
        source = f"cmd:`{cmd}`"
    elif belief_type == "plan":
        source = f"plan:{identity_key}"
    else:
        source = f"{belief_type}:{display_key}"

    # Format dependents
    if dependents:
        dep_strs = []
        for dep in dependents[:3]:  # Limit to 3 dependents
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


def shorten_path(path: str) -> str:
    """Shorten a file path for display."""
    if not path:
        return path
    # If path is long, show just filename or last 2 components
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[-2:])


if __name__ == "__main__":
    main()
