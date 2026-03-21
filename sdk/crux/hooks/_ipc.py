"""Lightweight IPC helper for hooks.

Each hook invocation opens a fresh socket, sends one message, and closes.
This is intentional — hooks are short-lived processes that must not hold
connections open across tool calls.
"""

from __future__ import annotations

import os
import socket
import struct

import msgpack

SOCKET_PATH = os.environ.get("CRUX_SOCKET", "/tmp/crux.sock")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def send_msg(msg: dict, timeout: float = 2.0) -> dict | None:
    """Send a msgpack-framed message to the daemon and return the response."""
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
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


def daemon_running() -> bool:
    """Check if the daemon is listening on the socket."""
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
