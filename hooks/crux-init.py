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
    sys.exit(0)

DAEMON_BIN = os.environ.get("CRUX_DAEMON", "crux-daemon")
# Socket path is per-session to avoid multi-session collision.
# Set by main() after reading session_id, or from env.
SOCKET_PATH = os.environ.get("CRUX_SOCKET", "")


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

    session_id = data.get("session_id", os.urandom(8).hex())

    # Per-session socket path to avoid multi-session collision
    if not SOCKET_PATH:
        SOCKET_PATH = f"/tmp/crux-{session_id[:16]}.sock"

    # Write socket path to env file so PostToolUse hooks can find it
    env_file = os.environ.get("CLAUDE_ENV_FILE", "")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"CRUX_SOCKET={SOCKET_PATH}\n")

    if not daemon_running():
        if not start_daemon():
            sys.exit(0)

    run_id = f"cc-{session_id[:16]}"
    success = send_run_start(run_id)

    if success:
        print(json.dumps({"reason": f"[crux] world model active (run={run_id[:12]}...)"}))


if __name__ == "__main__":
    main()
