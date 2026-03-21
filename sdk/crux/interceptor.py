from __future__ import annotations

import hashlib
import os
import subprocess

from .world import WorldModel


class CruxInterceptor:
    """Wraps common tool calls and automatically observes them in the world model."""

    def __init__(self, world: WorldModel):
        self.world = world

    def read_file(self, path: str) -> str:
        abspath = os.path.abspath(path)
        try:
            # Read as bytes for hashing (handles binary files)
            with open(abspath, "rb") as f:
                raw = f.read()
            h = hashlib.sha256(raw).hexdigest()[:8]
            size = len(raw)
            self.world.observe(
                "read_file",
                {"path": abspath},
                {"exists": True, "hash": h, "size": size},
            )
            # Return as text (callers expect str)
            return raw.decode("utf-8", errors="replace")
        except FileNotFoundError:
            self.world.observe(
                "read_file",
                {"path": abspath},
                {"exists": False, "hash": "", "size": 0},
                status="failure",
            )
            raise

    def write_file(self, path: str, content: str) -> None:
        abspath = os.path.abspath(path)
        with open(abspath, "w") as f:
            f.write(content)
        h = hashlib.sha256(content.encode()).hexdigest()[:8]
        size = len(content.encode())
        self.world.observe(
            "read_file",
            {"path": abspath},
            {"exists": True, "hash": h, "size": size},
        )

    def run_shell(self, command: str) -> tuple[str, int]:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True
        )
        stdout = result.stdout
        h = hashlib.sha256(stdout.encode()).hexdigest()[:8]
        self.world.observe(
            "shell",
            {"command": command},
            {"stdout_hash": h, "exit_code": result.returncode},
        )
        return stdout, result.returncode

    def http_get(self, url: str, headers: dict | None = None) -> dict:
        import urllib.request

        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                status_code = resp.status
                h = hashlib.sha256(body).hexdigest()[:8]
                self.world.observe(
                    "http_get",
                    {"url": url},
                    {"status_code": status_code, "auth_valid": True, "response_hash": h},
                )
                return {"status_code": status_code, "body": body.decode(errors="replace")}
        except urllib.error.HTTPError as e:
            self.world.observe(
                "http_get",
                {"url": url},
                {"status_code": e.code, "auth_valid": False, "response_hash": ""},
                status="failure",
            )
            raise

    def read_env(self, key: str) -> str | None:
        value = os.environ.get(key)
        if value is not None:
            h = hashlib.sha256(value.encode()).hexdigest()[:8]
            self.world.observe(
                "env_read",
                {"key": key},
                {"present": True, "value_hash": h},
            )
        else:
            self.world.observe(
                "env_read",
                {"key": key},
                {"present": False, "value_hash": ""},
            )
        return value
