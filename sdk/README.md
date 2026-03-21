# crux-sdk

Python SDK for [Crux](https://github.com/ardey26/crux) -- agentic knowledge versioning.

Crux tracks what an AI agent believes about the world and tells it when those beliefs go stale. This SDK connects to the Crux daemon over a Unix socket.

## Install

```
pip install crux-sdk
```

You also need the [crux-daemon binary](https://github.com/ardey26/crux/releases).

## Quick start

```python
from crux import WorldModel, CruxInterceptor

world = WorldModel(socket_path="/tmp/crux.sock")
tools = CruxInterceptor(world)

# These automatically register beliefs
content = tools.read_file("config.json")
tools.write_file("output.py", "print('hello')")
stdout, code = tools.run_shell("git status")

# Query a belief
belief = world.query("file", path="/absolute/path/to/config.json")
print(belief.status)      # "valid", "uncertain", or "invalidated"
print(belief.confidence)   # 0.0 to 1.0

# Check for pending invalidations
diff = world.diff()
if not diff.is_empty():
    print(f"{len(diff.invalidated)} beliefs invalidated")

# Plans as beliefs -- fence pattern
world.create_plan("refactor-auth", goal="Refactor auth module")
# Plan auto-depends on all beliefs read since the last plan

world.close()
```

## Claude Code integration

Add hooks to `.claude/settings.json` to auto-register every Read/Write/Edit/Bash tool call. See the [full README](https://github.com/ardey26/crux) for setup.

## Features

- Typed belief graph with causal dependency edges
- Convention-based auto-edges (writes depend on recent reads)
- Plans as first-class beliefs with fence-pattern auto-edges
- TTL-based belief expiry (shell: 30s, file: 60s, http: 2min, env: 5min)
- Stability filter for oscillating beliefs
- Field-level edge filtering for selective invalidation
- Schema evolution detection
- Append-only event log with crash recovery
- Branching for speculative agent work
- Historical queries (query_at)

## Links

- [GitHub](https://github.com/ardey26/crux)
- [Daemon releases](https://github.com/ardey26/crux/releases)
