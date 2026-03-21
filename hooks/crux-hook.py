#!/usr/bin/env python3
"""Claude Code PostToolUse hook -- bridges tool calls to Crux observations.

Auto-creates dependency edges: when a Write/Edit follows Reads, the written
file automatically depends on all recently-read files. No manual wiring.

Stdin format (from Claude Code):
{
    "tool_name": "Read" | "Write" | "Edit" | "Bash" | ...,
    "tool_input": { "file_path": "...", "command": "...", ... },
    "tool_result": "...",
    "session_id": "...",
    "cwd": "..."
}

Exit codes:
    0 -- success (stdout shown in transcript if non-empty)
    2 -- blocking error (stderr fed back to Claude)
    other -- non-blocking error (logged)
"""

import hashlib
import json
import os
import socket
import struct
import sys

try:
    import msgpack
except ImportError:
    sys.exit(0)

SOCKET_PATH = os.environ.get("CRUX_SOCKET", "")

if not SOCKET_PATH:
    import glob
    socks = sorted(glob.glob("/tmp/crux-*.sock"), key=os.path.getmtime, reverse=True)
    SOCKET_PATH = socks[0] if socks else "/tmp/crux.sock"


def send_msg(msg: dict) -> dict | None:
    """Send a msgpack-framed message to the daemon and get the response."""
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)

        payload = msgpack.packb(msg, use_bin_type=True)
        sock.sendall(struct.pack("<I", len(payload)) + payload)

        resp_len = struct.unpack("<I", _recv_exact(sock, 4))[0]
        resp_data = _recv_exact(sock, resp_len)
        return msgpack.unpackb(resp_data, raw=False)
    except Exception:
        return None
    finally:
        if sock:
            sock.close()


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def hash8(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:8]


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
    """Create edges from the written file to all recently-read files.

    Edge direction: written file (from) DEPENDS ON read files (to).
    Skips self-edges (read then edit same file).
    Returns number of edges created.
    """
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

    if is_write and path:
        abs_path = os.path.abspath(path)
        edges_created = create_auto_edges(belief_id, abs_path, recent_reads)
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
    output_parts = []

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
        print(json.dumps({"reason": " | ".join(output_parts)}))


if __name__ == "__main__":
    main()
