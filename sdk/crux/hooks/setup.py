#!/usr/bin/env python3
"""crux-setup: One-command setup for Claude Code + Crux integration.

Writes the Claude Code hooks configuration so that every tool call flows
through the Crux world model. Idempotent — safe to run multiple times.

Usage:
    crux-setup            # setup in current directory
    crux-setup /path/to   # setup in a specific project
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HOOKS_CONFIG = {
    "hooks": {
        "SessionStart": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "crux-hook-init",
                        "timeout": 10,
                    }
                ],
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Read|Write|Edit|Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "crux-hook-pre",
                        "timeout": 3,
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Read|Write|Edit|Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "crux-hook-post",
                        "timeout": 5,
                    }
                ],
            }
        ],
    }
}


def setup_hooks(project_dir: str) -> None:
    """Write Claude Code hook config to .claude/settings.json."""
    claude_dir = os.path.join(project_dir, ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    os.makedirs(claude_dir, exist_ok=True)

    # Load existing settings if present
    existing = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Merge: our hooks config replaces the hooks key, preserves everything else
    existing["hooks"] = HOOKS_CONFIG["hooks"]

    with open(settings_path, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")

    print(f"[crux] hooks configured in {settings_path}")
    print("[crux] Claude Code will now track beliefs automatically.")
    print("[crux] Restart Claude Code or start a new session to activate.")


def main():
    parser = argparse.ArgumentParser(
        prog="crux-setup",
        description="Configure Claude Code hooks for Crux belief tracking",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=os.getcwd(),
        help="Project directory (default: current directory)",
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    setup_hooks(project_dir)


if __name__ == "__main__":
    main()
