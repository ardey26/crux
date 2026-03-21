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


TASK_AGENTS = {
    "task_01": run_task_01,
    "task_02": run_task_02,
    "task_03": run_task_03,
    "task_04": run_task_04,
    "task_05": run_task_05,
}
