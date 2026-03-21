"""Scripted agent for benchmark testing.

Simulates an AI agent completing each task via tool calls.
Designed to be swapped for a real Claude agent later.
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Callable


class AgentTools:
    """Tool interface for the agent — raw or Crux-intercepted."""

    def __init__(self, work_dir: str, interceptor=None):
        self.work_dir = work_dir
        self._interceptor = interceptor
        self.steps_taken = 0
        self.steps_repeated = 0
        self.stale_belief_errors = 0
        self._read_cache: dict[str, str] = {}
        self._token_count = 0

    def read_file(self, path: str) -> str:
        self.steps_taken += 1
        self._token_count += 500
        abspath = os.path.join(self.work_dir, path)
        if self._interceptor:
            content = self._interceptor.read_file(abspath)
        else:
            with open(abspath) as f:
                content = f.read()
        old = self._read_cache.get(path)
        if old is not None and old == content:
            self.steps_repeated += 1
        self._read_cache[path] = content
        return content

    def write_file(self, path: str, content: str) -> None:
        self.steps_taken += 1
        self._token_count += 800
        abspath = os.path.join(self.work_dir, path)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        if self._interceptor:
            self._interceptor.write_file(abspath, content)
        else:
            with open(abspath, "w") as f:
                f.write(content)
        self._read_cache[path] = content

    def run_shell(self, command: str) -> tuple[str, int]:
        self.steps_taken += 1
        self._token_count += 300
        if self._interceptor:
            return self._interceptor.run_shell(f"cd {self.work_dir} && {command}")
        else:
            import subprocess
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, cwd=self.work_dir
            )
            return result.stdout + result.stderr, result.returncode

    def check_stale(self, path: str) -> bool:
        """Check if a file has changed since last read. Without Crux, always returns False."""
        if self._interceptor:
            abspath = os.path.join(self.work_dir, path)
            belief = self._interceptor.world.query("file", path=abspath)
            if belief and belief.status in ("uncertain", "invalidated"):
                return True
        return False


# ---------------------------------------------------------------------------
# Task 01: File Refactor
# Agent reads config.json, embeds its values into auth.py as constants.
# Injection changes config.json. Tests compare auth.py constants to config.json.
# Without Crux: stale constants → tests fail.
# With Crux: detects change, re-reads config, rewrites auth.py → tests pass.
# ---------------------------------------------------------------------------

def run_task_01(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    config_raw = tools.read_file("config.json")
    config = json.loads(config_raw)
    tools.read_file("auth.py")
    tools.read_file("app.py")
    tools.read_file("tests/test_auth.py")

    tools.write_file("auth.py", _make_auth_py(config))

    for i in range(6, 16):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("config.json"):
        config_raw = tools.read_file("config.json")
        config = json.loads(config_raw)
        tools.write_file("auth.py", _make_auth_py(config))

    tools.run_shell("python -m pytest tests/ -q --tb=short")


def _make_auth_py(config: dict) -> str:
    auth = config["auth"]
    scopes_repr = repr(auth["scopes"])
    return f'''"""Auth module — refactored to use config values."""

PROVIDER = "{auth["provider"]}"
CLIENT_ID = "{auth["client_id"]}"
REDIRECT_URI = "{auth["redirect_uri"]}"
SCOPES = {scopes_repr}


def get_auth_url():
    return f"https://auth.example.com/{{PROVIDER}}?client_id={{CLIENT_ID}}&redirect_uri={{REDIRECT_URI}}"


def get_scopes():
    return SCOPES
'''


# ---------------------------------------------------------------------------
# Task 02: Dependency Chain
# Agent reads settings.py, embeds values into db.py and query.py.
# Injection changes settings.py. Tests verify embedded values match settings.
# ---------------------------------------------------------------------------

def run_task_02(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    settings_raw = tools.read_file("settings.py")
    settings = _parse_settings(settings_raw)

    tools.write_file("db.py", _make_db_py(settings))
    tools.write_file("query.py", _make_query_py(settings))

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_chain.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def _load_settings():
    d = {}
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.py")) as f:
        exec(f.read(), d)
    return d

def test_db_matches_settings():
    from db import DB_HOST, DB_PORT, DB_NAME
    s = _load_settings()
    assert DB_HOST == s["DB_HOST"]
    assert DB_PORT == s["DB_PORT"]
    assert DB_NAME == s["DB_NAME"]

def test_query_uses_correct_dsn():
    from query import get_dsn
    s = _load_settings()
    dsn = get_dsn()
    assert s["DB_HOST"] in dsn
    assert str(s["DB_PORT"]) in dsn
    assert s["DB_NAME"] in dsn
''')

    for i in range(6, 11):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("settings.py"):
        settings_raw = tools.read_file("settings.py")
        settings = _parse_settings(settings_raw)
        tools.write_file("db.py", _make_db_py(settings))
        tools.write_file("query.py", _make_query_py(settings))

    tools.run_shell("python -m pytest tests/ -q --tb=short")


def _parse_settings(raw: str) -> dict:
    d: dict = {}
    exec(raw, d)
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _make_db_py(settings: dict) -> str:
    return f'''"""Database connection module — values from settings.py."""

DB_HOST = "{settings["DB_HOST"]}"
DB_PORT = {settings["DB_PORT"]}
DB_NAME = "{settings["DB_NAME"]}"

def connect():
    return f"mysql://{{DB_HOST}}:{{DB_PORT}}/{{DB_NAME}}"
'''


def _make_query_py(settings: dict) -> str:
    return f'''"""Query module — depends on db settings."""

DB_HOST = "{settings["DB_HOST"]}"
DB_PORT = {settings["DB_PORT"]}
DB_NAME = "{settings["DB_NAME"]}"

def get_dsn():
    return f"mysql://{{DB_HOST}}:{{DB_PORT}}/{{DB_NAME}}"
'''


# ---------------------------------------------------------------------------
# Task 03: Crash and Resume
# Agent writes a multi-file project. "Crash" at step 20 deletes a source file.
# Without Crux: agent doesn't notice, tests fail (missing file).
# With Crux: detects the file is gone, rewrites it.
# ---------------------------------------------------------------------------

def run_task_03(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    tools.write_file("models.py", '''"""Data models."""

class Item:
    def __init__(self, id: int, name: str, price: float):
        self.id = id
        self.name = name
        self.price = price

    def to_dict(self):
        return {"id": self.id, "name": self.name, "price": self.price}
''')

    tools.write_file("store.py", '''"""Store — manages items."""
from models import Item

class Store:
    def __init__(self):
        self.items = {}
        self.next_id = 1

    def add(self, name: str, price: float) -> Item:
        item = Item(self.next_id, name, price)
        self.items[self.next_id] = item
        self.next_id += 1
        return item

    def get(self, id: int) -> Item | None:
        return self.items.get(id)

    def delete(self, id: int) -> bool:
        return self.items.pop(id, None) is not None

    def list_all(self) -> list[Item]:
        return list(self.items.values())
''')

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_store.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from store import Store

def test_add_and_get():
    s = Store()
    item = s.add("widget", 9.99)
    assert s.get(item.id).name == "widget"

def test_delete():
    s = Store()
    item = s.add("widget", 9.99)
    assert s.delete(item.id)
    assert s.get(item.id) is None

def test_list_all():
    s = Store()
    s.add("a", 1.0)
    s.add("b", 2.0)
    assert len(s.list_all()) == 2
''')

    for i in range(5, 21):
        tools.run_shell(f"echo step {i}")

    # "Crash" — injection deletes models.py
    if inject_fn:
        inject_fn()

    if tools.check_stale("models.py"):
        # Crux detects models.py is gone — rewrite it
        tools.write_file("models.py", '''"""Data models."""

class Item:
    def __init__(self, id: int, name: str, price: float):
        self.id = id
        self.name = name
        self.price = price

    def to_dict(self):
        return {"id": self.id, "name": self.name, "price": self.price}
''')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# Task 04: Multi-File Consistency
# Agent adds `age` field to base_schema and propagates to models and api.
# Injection reverts base_schema.py to original (no age).
# Without Crux: models/api reference age but base_schema doesn't have it → fail.
# With Crux: detects revert, re-adds age → pass.
# ---------------------------------------------------------------------------

def run_task_04(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    tools.read_file("base_schema.py")
    tools.read_file("models.py")
    tools.read_file("api.py")

    tools.write_file("base_schema.py", '''"""Base schema definition."""

from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str
    age: int
''')

    tools.write_file("models.py", '''"""Models that depend on base_schema."""

from base_schema import User


def create_user(id: int, name: str, email: str, age: int) -> User:
    return User(id=id, name=name, email=email, age=age)


def user_to_dict(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "age": user.age}
''')

    tools.write_file("api.py", '''"""API layer that depends on models."""

from models import create_user, user_to_dict


def handle_create_user(data: dict) -> dict:
    user = create_user(
        id=data["id"],
        name=data["name"],
        email=data["email"],
        age=data["age"],
    )
    return user_to_dict(user)
''')

    for i in range(7, 9):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("base_schema.py"):
        tools.read_file("base_schema.py")
        tools.write_file("base_schema.py", '''"""Base schema definition."""

from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str
    age: int
''')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# Task 05: Auth Expiry
# Agent reads auth_token.txt, embeds token into a client script.
# Injection changes the token. Tests verify the client uses the current token.
# ---------------------------------------------------------------------------

def run_task_05(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    token = tools.read_file("auth_token.txt").strip()

    tools.write_file("client.py", f'''"""API client with embedded auth token."""

AUTH_TOKEN = "{token}"

def fetch_data():
    """Simulates fetching data with the token."""
    return {{"items": [{{"id": 1, "value": "alpha"}}, {{"id": 2, "value": "beta"}}], "token_used": AUTH_TOKEN}}

def write_output():
    import json
    data = fetch_data()
    with open("output.json", "w") as f:
        json.dump(data, f, indent=2)
    return data
''')

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_client.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_token_matches():
    from client import AUTH_TOKEN
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth_token.txt")) as f:
        expected = f.read().strip()
    assert AUTH_TOKEN == expected, f"stale token: got {AUTH_TOKEN}, expected {expected}"

def test_output_valid():
    from client import write_output
    data = write_output()
    assert "items" in data
    assert len(data["items"]) > 0
''')

    for i in range(4, 13):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("auth_token.txt"):
        token = tools.read_file("auth_token.txt").strip()
        tools.write_file("client.py", f'''"""API client with embedded auth token."""

AUTH_TOKEN = "{token}"

def fetch_data():
    """Simulates fetching data with the token."""
    return {{"items": [{{"id": 1, "value": "alpha"}}, {{"id": 2, "value": "beta"}}], "token_used": AUTH_TOKEN}}

def write_output():
    import json
    data = fetch_data()
    with open("output.json", "w") as f:
        json.dump(data, f, indent=2)
    return data
''')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# Task 06: Deep Cascade
# 10-file pipeline: stage_00 -> stage_01 -> ... -> stage_09.
# Each file imports VALUE from the previous and re-exports it.
# Injection changes stage_00. All downstream must update.
# Tests verify stage_09 has the correct value.
# Crux must propagate invalidation through 9 hops of edges.
# ---------------------------------------------------------------------------

def run_task_06(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    # Read the root value
    root_raw = tools.read_file("stage_00.py")
    root_d: dict = {}
    exec(root_raw, root_d)
    value = root_d["VALUE"]

    # Build 10-stage pipeline: each embeds the value as a constant
    for i in range(1, 10):
        tools.write_file(f"stage_{i:02d}.py", f'VALUE = "{value}"\n')

    # Write test -- checks stage_09 matches stage_00
    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_pipeline.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_pipeline_value():
    """stage_09.VALUE must match current stage_00.VALUE."""
    d = {}
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "stage_00.py")) as f:
        exec(f.read(), d)
    expected = d["VALUE"]

    d2 = {}
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "stage_09.py")) as f:
        exec(f.read(), d2)
    got = d2["VALUE"]

    assert got == expected, f"pipeline stale: stage_09 has {got!r}, stage_00 has {expected!r}"
''')

    for i in range(5, 13):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("stage_00.py"):
        # Re-read root and rebuild entire pipeline with new value
        root_raw = tools.read_file("stage_00.py")
        root_d = {}
        exec(root_raw, root_d)
        value = root_d["VALUE"]
        for i in range(1, 10):
            tools.write_file(f"stage_{i:02d}.py", f'VALUE = "{value}"\n')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# Task 07: Conflicting Observations
# Config changes twice rapidly. Agent must use the final value.
# Without Crux: agent uses the original value.
# With Crux: detects staleness, re-reads, gets the final value.
# ---------------------------------------------------------------------------

def run_task_07(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    config_raw = tools.read_file("config.py")
    config = _parse_settings(config_raw)

    tools.write_file("server.py", _make_server_py(config))
    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_server.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def _load_config():
    d = {}
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.py")) as f:
        exec(f.read(), d)
    return d

def test_host_matches():
    from server import HOST
    c = _load_config()
    assert HOST == c["HOST"], f"stale host: got {HOST}, expected {c['HOST']}"

def test_port_matches():
    from server import PORT
    c = _load_config()
    assert PORT == c["PORT"], f"stale port: got {PORT}, expected {c['PORT']}"
''')

    for i in range(4, 9):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("config.py"):
        config_raw = tools.read_file("config.py")
        config = _parse_settings(config_raw)
        tools.write_file("server.py", _make_server_py(config))

    tools.run_shell("python -m pytest tests/ -q --tb=short")


def _make_server_py(config: dict) -> str:
    return f'''"""Server config -- embedded from config.py."""

HOST = "{config["HOST"]}"
PORT = {config["PORT"]}

def get_bind_address():
    return f"{{HOST}}:{{PORT}}"
'''


# ---------------------------------------------------------------------------
# Task 08: Rapid Churn
# Counter file changes 50 times. Agent must use the final value (50).
# Tests that the system stays coherent under high-frequency updates.
# ---------------------------------------------------------------------------

def run_task_08(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    counter = tools.read_file("counter.txt").strip()

    tools.write_file("display.py", f'''"""Display module -- embeds counter value."""

COUNTER = {counter}

def get_counter():
    return COUNTER
''')

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_counter.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_counter_matches():
    from display import COUNTER
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "counter.txt")) as f:
        expected = int(f.read().strip())
    assert COUNTER == expected, f"stale counter: got {COUNTER}, expected {expected}"
''')

    for i in range(3, 6):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("counter.txt"):
        counter = tools.read_file("counter.txt").strip()
        tools.write_file("display.py", f'''"""Display module -- embeds counter value."""

COUNTER = {counter}

def get_counter():
    return COUNTER
''')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ===========================================================================
# ADVERSARIAL SCENARIOS — Category A: Crux Makes Things Worse
# ===========================================================================

# ---------------------------------------------------------------------------
# A1: Static Task Overhead
# Zero drift, zero dependencies. Measures Crux's minimum tax.
# ---------------------------------------------------------------------------

def run_a1(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    content = tools.read_file("article.md")
    # Write a simple summary
    lines = content.strip().split("\n")
    summary_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    summary = " ".join(summary_lines[:3])
    tools.write_file("summary.txt", summary)


# ---------------------------------------------------------------------------
# A2: Write-Heavy Logging
# 50 sequential writes to a single file. Pure write-path overhead.
# ---------------------------------------------------------------------------

def run_a2(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    entries = []
    for i in range(50):
        entries.append(f"[{i:04d}] Log entry {i}: operation completed successfully")
        tools.write_file("log.txt", "\n".join(entries) + "\n")


# ---------------------------------------------------------------------------
# A3: False Cascade
# Config has 20 fields. 5 downstream beliefs depend on specific fields.
# Injection changes an unrelated field ("description"). Hash changes.
# All 5 downstream beliefs invalidate and re-derive identically.
# ---------------------------------------------------------------------------

def run_a3(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    config_raw = tools.read_file("config.json")
    config = json.loads(config_raw)

    # Write 5 downstream files that depend on specific config fields
    tools.write_file("db_conn.py", f'HOST = "{config["host"]}"\nPORT = {config["port"]}\n')
    tools.write_file("retry_policy.py", f'MAX_RETRIES = {config["retries"]}\n')
    tools.write_file("timeout_config.py", f'TIMEOUT = {config["timeout"]}\n')
    tools.write_file("connection_string.py",
        f'DSN = "postgres://{config["host"]}:{config["port"]}/mydb"\n')
    tools.write_file("health_check.py",
        f'CHECK_INTERVAL = {config["timeout"]} * 2\nHOST = "{config["host"]}"\n')

    # With Crux: declare field-level edges so only relevant changes cascade
    if tools._interceptor:
        abspath = os.path.join(tools.work_dir, "config.json")
        config_belief = tools._interceptor.world.query("file", path=abspath)
        if config_belief:
            cid = config_belief.id
            # Each downstream file depends on specific fields of config.json's result payload.
            # The result payload has: exists, hash, size. But the actual data fields are
            # in the file content, not the belief payload. Field-level filtering works on
            # the belief payload (hash/size/exists), not the file content.
            #
            # For this scenario, we use the auto-edges (which have no field filter).
            # The test demonstrates that without field-level content awareness,
            # Crux can't distinguish meaningful from superficial changes.
            pass

    # Write tests that verify downstream values match config
    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_config.py", '''import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def _load_config():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")) as f:
        return json.load(f)

def test_db_conn():
    from db_conn import HOST, PORT
    c = _load_config()
    assert HOST == c["host"]
    assert PORT == c["port"]

def test_retry():
    from retry_policy import MAX_RETRIES
    c = _load_config()
    assert MAX_RETRIES == c["retries"]

def test_timeout():
    from timeout_config import TIMEOUT
    c = _load_config()
    assert TIMEOUT == c["timeout"]

def test_connection_string():
    from connection_string import DSN
    c = _load_config()
    assert c["host"] in DSN
    assert str(c["port"]) in DSN

def test_health_check():
    from health_check import HOST
    c = _load_config()
    assert HOST == c["host"]
''')

    for i in range(6, 11):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("config.json"):
        config_raw = tools.read_file("config.json")
        config = json.loads(config_raw)
        tools.write_file("db_conn.py", f'HOST = "{config["host"]}"\nPORT = {config["port"]}\n')
        tools.write_file("retry_policy.py", f'MAX_RETRIES = {config["retries"]}\n')
        tools.write_file("timeout_config.py", f'TIMEOUT = {config["timeout"]}\n')
        tools.write_file("connection_string.py",
            f'DSN = "postgres://{config["host"]}:{config["port"]}/mydb"\n')
        tools.write_file("health_check.py",
            f'CHECK_INTERVAL = {config["timeout"]} * 2\nHOST = "{config["host"]}"\n')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# A4: Thrashing Belief
# Status alternates up/down 10 times. 3 downstream beliefs re-derive each time.
# Final state = initial state. Maximum work, zero net progress.
# ---------------------------------------------------------------------------

def run_a4(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    status = tools.read_file("status.txt").strip()

    tools.write_file("report.txt", f"Service status: {status}\n")
    tools.write_file("alert.py", f'STATUS = "{status}"\nALERT = STATUS == "down"\n')
    tools.write_file("dashboard.py", f'CURRENT_STATUS = "{status}"\n')

    if inject_fn:
        inject_fn()

    # After thrashing, re-read and re-derive
    if tools.check_stale("status.txt"):
        status = tools.read_file("status.txt").strip()
        tools.write_file("report.txt", f"Service status: {status}\n")
        tools.write_file("alert.py", f'STATUS = "{status}"\nALERT = STATUS == "down"\n')
        tools.write_file("dashboard.py", f'CURRENT_STATUS = "{status}"\n')


# ===========================================================================
# ADVERSARIAL SCENARIOS — Category B: Crux Can't Help
# ===========================================================================

# ---------------------------------------------------------------------------
# B1: Semantic Equivalence, Different Hash
# "timeout: 30" -> "timeout: 30s". Functionally identical, hash differs.
# Downstream re-derives for no reason.
# ---------------------------------------------------------------------------

def run_b1(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    config_raw = tools.read_file("config.yaml")
    # Parse YAML-like config (simple key: value)
    config = {}
    for line in config_raw.strip().split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            # Strip units for numeric parsing
            numeric = ''.join(c for c in v if c.isdigit())
            try:
                config[k.strip()] = int(numeric) if numeric else v
            except ValueError:
                config[k.strip()] = v

    tools.write_file("settings.py",
        f'TIMEOUT = {config.get("timeout", 30)}\nRETRIES = {config.get("retries", 3)}\n')

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_settings.py", '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def _parse_config():
    d = {}
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")) as f:
        for line in f:
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                numeric = "".join(c for c in v if c.isdigit())
                try:
                    d[k.strip()] = int(numeric) if numeric else v
                except ValueError:
                    d[k.strip()] = v
    return d

def test_timeout():
    from settings import TIMEOUT
    c = _parse_config()
    assert TIMEOUT == c["timeout"]

def test_retries():
    from settings import RETRIES
    c = _parse_config()
    assert RETRIES == c["retries"]
''')

    for i in range(3, 6):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("config.yaml"):
        config_raw = tools.read_file("config.yaml")
        config = {}
        for line in config_raw.strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                numeric = ''.join(c for c in v if c.isdigit())
                try:
                    config[k.strip()] = int(numeric) if numeric else v
                except ValueError:
                    config[k.strip()] = v
        tools.write_file("settings.py",
            f'TIMEOUT = {config.get("timeout", 30)}\nRETRIES = {config.get("retries", 3)}\n')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# B2: Unmodeled External Dependency
# Agent calls an API that starts returning 429 at step 8.
# No belief tracks rate limit state. Crux can't help.
# ---------------------------------------------------------------------------

def run_b2(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    # Simulate API calls
    api_available = True

    tools.write_file("api_client.py", '''def fetch_data():
    return {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
''')

    for i in range(1, 9):
        tools.run_shell(f"echo 'API call {i}'")

    if inject_fn:
        inject_fn()
        api_available = False

    # After "rate limit", agent tries to use API
    if api_available:
        tools.write_file("output.json", '{"items": [{"id": 1}, {"id": 2}, {"id": 3}]}')
    else:
        # Agent doesn't know about rate limit, tries anyway
        # Simulate: write partial/error output
        tools.write_file("output.json", '{"error": "rate_limited", "items": []}')


# ---------------------------------------------------------------------------
# B3: Conflicting Sources
# File says stock=5, API says stock=0. Last-write-wins is wrong.
# ---------------------------------------------------------------------------

def run_b3(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    inv_raw = tools.read_file("inventory.json")
    inv = json.loads(inv_raw)
    stock = inv["stock"]

    tools.write_file("reorder.py", f'''STOCK = {stock}
REORDER_THRESHOLD = 3
SHOULD_REORDER = STOCK < REORDER_THRESHOLD
''')

    tools.write_file("tests/__init__.py", "")
    tools.write_file("tests/test_reorder.py", '''import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_reorder_matches_inventory():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "inventory.json")) as f:
        inv = json.load(f)
    from reorder import STOCK
    assert STOCK == inv["stock"], f"stale stock: got {STOCK}, expected {inv['stock']}"
''')

    for i in range(3, 7):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    if tools.check_stale("inventory.json"):
        inv_raw = tools.read_file("inventory.json")
        inv = json.loads(inv_raw)
        stock = inv["stock"]
        tools.write_file("reorder.py", f'''STOCK = {stock}
REORDER_THRESHOLD = 3
SHOULD_REORDER = STOCK < REORDER_THRESHOLD
''')

    tools.run_shell("python -m pytest tests/ -q --tb=short")


# ---------------------------------------------------------------------------
# B4: Latent Dependency Discovery
# README and CHANGELOG are semantically linked but Crux sees no edge.
# README changes to reference CHANGELOG content. Summary becomes stale.
# ---------------------------------------------------------------------------

def run_b4(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    readme = tools.read_file("README.md")
    changelog = tools.read_file("CHANGELOG.md")

    # Write summaries based on each file independently
    tools.write_file("summary.txt", f"Project summary: {readme.strip().split(chr(10))[0]}\n")
    tools.write_file("release_notes.txt",
        f"Release notes: {changelog.strip()}\n")

    for i in range(3, 6):
        tools.run_shell(f"echo step {i}")

    if inject_fn:
        inject_fn()

    # Only README changed. Crux flags README as stale.
    # But summary.txt and release_notes.txt are independent beliefs.
    # The semantic dependency (README now references CHANGELOG) is invisible.
    if tools.check_stale("README.md"):
        readme = tools.read_file("README.md")
        tools.write_file("summary.txt", f"Project summary: {readme.strip().split(chr(10))[0]}\n")
        # Agent does NOT re-read CHANGELOG or update release_notes.txt
        # because Crux sees no edge between them.
        # The summary now mentions v2 but release_notes was already correct.


# ---------------------------------------------------------------------------
# B5: Stale Plan, Fresh Beliefs
# Agent makes a plan based on requirements.txt. Requirements change.
# Crux updates the belief but the plan (steps 3-5) is already committed.
# ---------------------------------------------------------------------------

def run_b5(tools: AgentTools, inject_fn: Callable | None = None) -> None:
    reqs_raw = tools.read_file("requirements.txt")
    packages = [l.strip() for l in reqs_raw.strip().split("\n") if l.strip()]

    # Register the plan as a belief (with Crux).
    # The plan depends on requirements.txt via the fence pattern.
    plan_belief_id = None
    if tools._interceptor:
        plan_belief_id = tools._interceptor.world.create_plan(
            plan_id="install-deps",
            goal="Install packages and write app.py referencing all of them",
            payload={"packages": packages},
        )

    plan = packages[:]

    # Execute plan steps 1-2: write setup
    tools.write_file("app.py", "# App module\n" +
        "\n".join(f"# Uses {p.split('==')[0]}" for p in plan) + "\n")

    tools.write_file("tests/__init__.py", "")

    for i in range(3, 9):
        tools.run_shell(f"echo 'plan step {i}'")

    if inject_fn:
        inject_fn()

    # With plan beliefs: check if the PLAN is stale (not just requirements.txt).
    # If the plan is stale, re-read requirements AND re-plan.
    plan_is_stale = False
    if tools._interceptor and plan_belief_id:
        plan_belief = tools._interceptor.world.query("plan", plan_id="install-deps")
        if plan_belief and (plan_belief.status in ("uncertain", "invalidated") or plan_belief.confidence < 1.0):
            plan_is_stale = True
    elif tools.check_stale("requirements.txt"):
        # Without plan beliefs, fall back to checking requirements.txt directly.
        # This was the old behavior -- re-read data but don't re-plan.
        plan_is_stale = True

    if plan_is_stale:
        reqs_raw = tools.read_file("requirements.txt")
        packages = [l.strip() for l in reqs_raw.strip().split("\n") if l.strip()]
        plan = packages[:]  # re-plan: update the plan variable
        # Re-execute the plan with new packages
        tools.write_file("app.py", "# App module\n" +
            "\n".join(f"# Uses {p.split('==')[0]}" for p in plan) + "\n")

    # Execute remaining plan steps
    test_code = '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_all_packages_referenced():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "requirements.txt")) as f:
        packages = [l.strip().split("==")[0] for l in f if l.strip()]
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")) as f:
        app_content = f.read()
    for pkg in packages:
        assert pkg in app_content, f"app.py missing reference to {pkg}"
'''
    tools.write_file("tests/test_plan.py", test_code)
    tools.run_shell("python -m pytest tests/ -q --tb=short")


TASK_AGENTS = {
    "task_01": run_task_01,
    "task_02": run_task_02,
    "task_03": run_task_03,
    "task_04": run_task_04,
    "task_05": run_task_05,
    "task_06": run_task_06,
    "task_07": run_task_07,
    "task_08": run_task_08,
    "a1": run_a1,
    "a2": run_a2,
    "a3": run_a3,
    "a4": run_a4,
    "b1": run_b1,
    "b2": run_b2,
    "b3": run_b3,
    "b4": run_b4,
    "b5": run_b5,
}
