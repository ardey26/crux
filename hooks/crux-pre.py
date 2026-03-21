#!/usr/bin/env python3
"""Claude Code PreToolUse hook -- surfaces stale beliefs before tool calls.

Queries the Crux daemon for pending invalidation diffs and checks whether
the file about to be read/edited is stale. Warnings are injected into the
agent's context so the world model actively influences behavior.

Stdin format (from Claude Code):
{
    "tool_name": "Read" | "Write" | "Edit" | "Bash" | ...,
    "tool_input": { "file_path": "...", "command": "...", ... },
    "session_id": "...",
    "cwd": "..."
}

Exit codes:
    0 -- success (stdout shown in transcript if non-empty)
    2 -- blocking error (stderr fed back to Claude)
    other -- non-blocking error (logged)
"""

import json
import os
import socket
import struct
import sys

try:
    import msgpack
except ImportError:
    sys.exit(0)

SOCKET_PATH = os.environ.get("CRUX_SOCKET", "/tmp/crux.sock")


def send_msg(msg: dict) -> dict | None:
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
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

    warnings = []

    # 1. Check pending diffs (invalidations from recent changes)
    diff_resp = send_msg({"type": "diff", "request_id": "pre-diff"})
    if diff_resp:
        diff = diff_resp.get("diff", {})
        invalidated = diff.get("invalidated", [])
        uncertain = diff.get("uncertain", [])
        if invalidated or uncertain:
            parts = []
            if invalidated:
                parts.append(f"{len(invalidated)} belief(s) invalidated")
            if uncertain:
                parts.append(f"{len(uncertain)} belief(s) now uncertain")
            action = diff.get("action_required", "")
            msg = ", ".join(parts)
            if action:
                msg += f" -- {action}"
            warnings.append(msg)

    # 2. For file operations, check if the target file's belief is stale
    if tool_name in ("Read", "Write", "Edit"):
        path = tool_input.get("file_path", "")
        if path:
            abs_path = os.path.abspath(path)
            resp = send_msg({
                "type": "query",
                "belief_type": "file",
                "filters": {"path": abs_path},
                "request_id": "pre-check",
            })
            if resp:
                belief = resp.get("belief")
                if belief and belief.get("status") != "valid":
                    status = belief.get("status", "unknown")
                    conf = belief.get("confidence", 0)
                    warnings.append(
                        f"{abs_path}: belief is {status} (confidence {conf}), re-read before relying on cached content"
                    )

    # 3. Check for ALL stale beliefs (not just TTL-expired)
    stale_resp = send_msg({"type": "stale", "request_id": "pre-stale"})
    if stale_resp:
        stale = stale_resp.get("stale", [])
        if stale:
            invalidated = [b for b in stale if b.get("status") == "invalidated"]
            uncertain = [b for b in stale if b.get("status") == "uncertain"]
            parts = []
            if invalidated:
                parts.append(f"{len(invalidated)} invalidated")
            if uncertain:
                parts.append(f"{len(uncertain)} uncertain")
            warnings.append(f"{len(stale)} stale belief(s) ({', '.join(parts)}) -- re-observe before acting on them")

    if warnings:
        print("[crux] " + " | ".join(warnings))


if __name__ == "__main__":
    main()
