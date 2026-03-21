from __future__ import annotations

import socket
import struct

import msgpack


class CruxClient:
    """Low-level client for communicating with the crux-daemon over Unix socket."""

    def __init__(self, socket_path: str = "/tmp/crux.sock", timeout: float = 5.0):
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._request_counter = 0
        self._timeout = timeout

    def connect(self) -> None:
        if self._sock is not None:
            self._sock.close()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        self._sock.connect(self._socket_path)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def send(self, msg: dict) -> dict:
        if self._sock is None:
            raise RuntimeError("Not connected. Call connect() first.")

        self._request_counter += 1
        if "request_id" not in msg:
            msg["request_id"] = str(self._request_counter)

        payload = msgpack.packb(msg, use_bin_type=True)
        self._sock.sendall(struct.pack("<I", len(payload)) + payload)

        resp_len_bytes = self._recv_exact(4)
        resp_len = struct.unpack("<I", resp_len_bytes)[0]
        resp_data = self._recv_exact(resp_len)
        return msgpack.unpackb(resp_data, raw=False)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Daemon closed connection")
            buf += chunk
        return buf

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
