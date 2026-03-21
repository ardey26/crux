#!/usr/bin/env python3
"""crux-observe: CLI for sending observations to the Crux daemon.

Used by Claude Code hooks to bridge tool calls to Crux beliefs.

Usage:
    crux-observe read_file --path /foo.py
    crux-observe write_file --path /foo.py
    crux-observe bash --command "pip install requests"
    crux-observe status
    crux-observe context

Reads file content and hashes it automatically. Connects to the daemon
at /tmp/crux.sock (or $CRUX_SOCKET).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

from .client import CruxClient
from .types import BeliefNode, InvalidationDiff


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def _connect() -> CruxClient:
    socket_path = os.environ.get("CRUX_SOCKET", "/tmp/crux.sock")
    client = CruxClient(socket_path)
    client.connect()
    return client


def cmd_observe_file(args):
    """Observe a file read or write."""
    client = _connect()
    path = os.path.abspath(args.path)

    try:
        with open(path, "r") as f:
            content = f.read()
        h = _hash(content)
        size = len(content.encode())
        result = {"exists": True, "hash": h, "size": size}
        status = "success"
    except FileNotFoundError:
        result = {"exists": False, "hash": "", "size": 0}
        status = "failure"
    except Exception:
        result = {"exists": False, "hash": "", "size": 0}
        status = "failure"

    resp = client.send({
        "type": "observe",
        "action": "read_file",
        "inputs": {"path": path},
        "result": result,
        "status": status,
    })

    if resp.get("diff") and resp["diff"].get("uncertain"):
        for u in resp["diff"]["uncertain"]:
            print(f"!! uncertain: {u['id']} (was={u['was']:.2f} now={u['now']:.2f})", file=sys.stderr)
    if resp.get("diff") and resp["diff"].get("invalidated"):
        for bid in resp["diff"]["invalidated"]:
            print(f"!! invalidated: {bid}", file=sys.stderr)

    client.close()


def cmd_observe_shell(args):
    """Observe a shell command execution."""
    client = _connect()
    command = args.command
    exit_code = args.exit_code
    stdout_hash = _hash(args.stdout) if args.stdout else ""

    resp = client.send({
        "type": "observe",
        "action": "shell",
        "inputs": {"command": command},
        "result": {"stdout_hash": stdout_hash, "exit_code": exit_code},
        "status": "success" if exit_code == 0 else "failure",
    })

    client.close()


def cmd_status(args):
    """Print current world model status."""
    client = _connect()

    diff_resp = client.send({"type": "diff"})
    diff = diff_resp.get("diff", {})

    n_invalidated = len(diff.get("invalidated", []))
    n_uncertain = len(diff.get("uncertain", []))

    if n_invalidated == 0 and n_uncertain == 0:
        print("all beliefs valid")
    else:
        if n_invalidated:
            print(f"{n_invalidated} invalidated belief(s)")
        if n_uncertain:
            print(f"{n_uncertain} uncertain belief(s)")
            for u in diff["uncertain"]:
                print(f"  {u['id']} was={u['was']:.2f} now={u['now']:.2f}")

    client.close()


def cmd_init(args):
    """Initialize a new Crux run."""
    client = _connect()
    run_id = args.run_id or os.urandom(8).hex()

    resp = client.send({"type": "run_start", "run_id": run_id})
    if "error" in resp:
        print(f"error: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(run_id)
    client.close()


def main():
    parser = argparse.ArgumentParser(prog="crux-observe", description="Send observations to Crux daemon")
    sub = parser.add_subparsers(dest="command")

    p_file = sub.add_parser("file", help="Observe a file read/write")
    p_file.add_argument("path", help="File path")
    p_file.set_defaults(func=cmd_observe_file)

    p_shell = sub.add_parser("shell", help="Observe a shell command")
    p_shell.add_argument("command", help="The command that was run")
    p_shell.add_argument("--exit-code", type=int, default=0, help="Exit code")
    p_shell.add_argument("--stdout", default="", help="Stdout content")
    p_shell.set_defaults(func=cmd_observe_shell)

    p_status = sub.add_parser("status", help="Print world model status")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init", help="Start a new run")
    p_init.add_argument("--run-id", default=None, help="Run ID (generated if omitted)")
    p_init.set_defaults(func=cmd_init)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
