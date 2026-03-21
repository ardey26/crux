#!/usr/bin/env python3
"""Claude Code PostToolUse hook — bridges tool calls to Crux observations.

Receives tool call data on stdin as JSON, extracts file paths and results,
sends observations to the Crux daemon.

Stdin format (from Claude Code):
{
    "tool_name": "Read" | "Write" | "Edit" | "Bash" | ...,
    "tool_input": { "file_path": "...", "command": "...", ... },
    "tool_result": "...",
    "session_id": "...",
    "cwd": "..."
}

Exit codes:
    0 — success (stdout shown in transcript if non-empty)
    2 — blocking error (stderr fed back to Claude)
    other — non-blocking error (logged)
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
    # If msgpack isn't installed, skip silently — don't break Claude Code
    sys.exit(0)

SOCKET_PATH = os.environ.get("CRUX_SOCKET", "/tmp/crux.sock")


def send_observation(msg: dict) -> dict | None:
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
        # Daemon not running, network error, corrupt response — skip silently
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


def handle_read(tool_input: dict, tool_result: str) -> dict | None:
    """Handle Read tool — register file belief.

    Reads the file from disk to hash it, matching the SDK's interceptor behavior.
    We don't hash tool_result because it includes cat -n line number prefixes.
    """
    path = tool_input.get("file_path", "")
    if not path:
        return None

    path = os.path.abspath(path)

    try:
        with open(path, "rb") as f:
            raw = f.read()
        h = hashlib.sha256(raw).hexdigest()[:8]
        size = len(raw)
        return {
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": path},
            "result": {"exists": True, "hash": h, "size": size},
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


def handle_write(tool_input: dict, tool_result: str) -> dict | None:
    """Handle Write tool — register file belief with new content."""
    path = tool_input.get("file_path", "")
    content = tool_input.get("content", "")
    if not path:
        return None

    path = os.path.abspath(path)
    h = hash8(content)
    size = len(content.encode())

    return {
        "type": "observe",
        "action": "read_file",
        "inputs": {"path": path},
        "result": {"exists": True, "hash": h, "size": size},
        "status": "success",
    }


def handle_edit(tool_input: dict, tool_result: str) -> dict | None:
    """Handle Edit tool — re-read the file to get its new state."""
    path = tool_input.get("file_path", "")
    if not path:
        return None

    path = os.path.abspath(path)

    try:
        with open(path, "rb") as f:
            raw = f.read()
        h = hashlib.sha256(raw).hexdigest()[:8]
        size = len(raw)
        return {
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": path},
            "result": {"exists": True, "hash": h, "size": size},
            "status": "success",
        }
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def handle_bash(tool_input: dict, tool_result: str) -> dict | None:
    """Handle Bash tool — register shell_output belief."""
    command = tool_input.get("command", "")
    if not command:
        return None

    h = hash8(tool_result) if tool_result else ""
    # Infer exit code: if tool_result contains "Exit code" it failed
    exit_code = 0
    if tool_result and "Exit code" in tool_result:
        try:
            # Claude Code format: "Exit code N\n..."
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


def main():
    # Read hook input from stdin
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

    # Convert tool_result to string if it's not already
    if not isinstance(tool_result, str):
        tool_result = json.dumps(tool_result) if tool_result else ""

    # Map tool names to handlers
    handlers = {
        "Read": handle_read,
        "Write": handle_write,
        "Edit": handle_edit,
        "Bash": handle_bash,
    }

    handler = handlers.get(tool_name)
    if handler is None:
        sys.exit(0)

    msg = handler(tool_input, tool_result)
    if msg is None:
        sys.exit(0)

    resp = send_observation(msg)
    if resp is None:
        sys.exit(0)

    # If invalidations were triggered, output a warning for Claude to see
    diff = resp.get("diff")
    if diff and (diff.get("invalidated") or diff.get("uncertain")):
        n_invalid = len(diff.get("invalidated", []))
        n_uncertain = len(diff.get("uncertain", []))
        parts = []
        if n_invalid:
            parts.append(f"{n_invalid} belief(s) invalidated")
        if n_uncertain:
            parts.append(f"{n_uncertain} belief(s) uncertain")
        warning = f"[crux] {', '.join(parts)} — {diff.get('action_required', 'check world model')}"
        print(json.dumps({"reason": warning}))


if __name__ == "__main__":
    main()
