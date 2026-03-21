#!/usr/bin/env python3
"""Claude Code PreToolUse hook — surfaces stale beliefs before tool calls.

Queries the Crux daemon for pending invalidation diffs and checks whether
the file about to be read/edited is stale. Warnings are injected into the
agent's context so the world model actively influences behavior.
"""

from __future__ import annotations

import json
import os
import sys

try:
    import msgpack  # noqa: F401
except ImportError:
    sys.exit(0)

from ._ipc import send_msg


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
    diff_resp = send_msg({"type": "diff", "request_id": "pre-diff"}, timeout=1.0)
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
            }, timeout=1.0)
            if resp:
                belief = resp.get("belief")
                if belief and belief.get("status") != "valid":
                    status = belief.get("status", "unknown")
                    conf = belief.get("confidence", 0)
                    warnings.append(
                        f"{abs_path}: belief is {status} (confidence {conf}), re-read before relying on cached content"
                    )

    # 3. Check for ALL stale beliefs
    stale_resp = send_msg({"type": "stale", "request_id": "pre-stale"}, timeout=1.0)
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
