# Crux

A local daemon that tracks what an AI agent believes about the world. When reality changes (a file is edited, a token expires, a dependency updates), Crux detects the drift and tells the agent what went stale.

## What it does

Crux sits between an AI agent and the filesystem. Every time the agent reads a file, runs a command, or checks an environment variable, Crux records a "belief" about that resource: its hash, its status, its last-known state. When the resource changes, Crux marks the belief as invalidated and propagates that invalidation to any dependent beliefs.

Without Crux, an agent that read `config.json` at step 5 and wrote code based on it at step 10 has no way to know that `config.json` was changed at step 8 by a git pull. With Crux, the agent gets told immediately.

## Components

- **daemon/** -- Rust binary that maintains the belief graph, event log, and query engine. Communicates over a Unix socket with MessagePack framing.
- **sdk/** -- Python package (`crux`) with `WorldModel`, `CruxInterceptor`, and a `crux-observe` CLI.
- **hooks/** -- Claude Code integration. PostToolUse hooks that automatically register Read/Write/Edit/Bash tool calls as observations.
- **bench/** -- Benchmark harness with 5 tasks that measure agent performance with and without Crux.

## Quick start

### Prerequisites

- Rust 1.75+
- Python 3.10+
- `msgpack` Python package (`pip install msgpack`)

### Build the daemon

```
cd daemon
cargo build --release
```

The binary is at `daemon/target/release/crux-daemon`.

### Install the Python SDK

```
cd sdk
pip install -e .
```

This installs the `crux` package and the `crux-observe` CLI.

### Run the daemon

```
crux-daemon --socket-path /tmp/crux.sock
```

The daemon listens on the given Unix socket. It stores event logs in `~/.crux/runs/`.

### Use with Claude Code

Copy the hooks into your Claude Code plugins directory, or add the hook configuration to your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Read|Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/crux/hooks/crux-hook.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The SessionStart hook (`crux-init.py`) auto-starts the daemon and initializes a run. Add it to your config if you want fully automatic setup:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/crux/hooks/crux-init.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Each Claude Code session gets its own socket (keyed by session ID), so multiple sessions don't interfere with each other.

### Use from Python directly

```python
from crux import WorldModel, CruxInterceptor

world = WorldModel(socket_path="/tmp/crux.sock")
tools = CruxInterceptor(world)

# These automatically register beliefs
content = tools.read_file("config.json")
tools.write_file("output.py", "print('hello')")
stdout, code = tools.run_shell("git status")
value = tools.read_env("DATABASE_URL")

# Query a belief
belief = world.query("file", path="/absolute/path/to/config.json")
print(belief.status)      # "valid", "uncertain", or "invalidated"
print(belief.confidence)   # 0.0 to 1.0

# Check for pending invalidations
diff = world.diff()
if not diff.is_empty():
    print(f"{len(diff.invalidated)} beliefs invalidated")
    print(f"{len(diff.uncertain)} beliefs uncertain")

# Declare dependencies between beliefs
config = world.query("file", path="/abs/path/config.json")
utils = world.query("file", path="/abs/path/utils.py")
world.add_edge(utils.id, config.id, weight=0.8)
# Now if config.json changes, utils.py automatically becomes uncertain

# Historical queries
past = world.at(step=5)
old_belief = past.query("file", path="/abs/path/config.json")

# Compact context for injection into agent prompts
print(world.to_context())

world.close()
```

### Use the CLI

```
# Initialize a run
crux-observe init --run-id my-session

# Observe a file
crux-observe file /path/to/config.json

# Observe a shell command
crux-observe shell "git status" --exit-code 0 --stdout "On branch main"

# Check world model status
crux-observe status
```

Set `CRUX_SOCKET` to point to a non-default socket path.

## How it works

1. The daemon maintains an append-only binary event log and an in-memory belief graph.
2. When an observation arrives (file read, shell command, etc.), the daemon extracts an identity key (file path, command string, env var name) and looks up the existing belief.
3. If the payload differs from the stored belief (different hash, different exit code), the belief is marked as invalidated.
4. Invalidation propagates through declared edges. A belief that depends on an invalidated belief has its confidence decayed by `weight * 0.7` per hop. Below 0.6 it becomes "uncertain", below 0.3 it becomes "invalidated".
5. The daemon returns an inline diff on each observe call and accumulates diffs in a pending queue for polling.

## Belief types

| Type | Identity key | Tracked fields |
|------|-------------|----------------|
| file | path | exists, hash, size |
| http_endpoint | url | status_code, auth_valid, response_hash |
| env_var | key | present, value_hash |
| shell_output | command | stdout_hash, exit_code |

## Branching

Crux supports speculative branching for agent exploration:

```python
branch_id = world.branch()
# ... do speculative work ...
world.rollback(branch_id)  # undo everything since branch point
```

Only one branch can be active at a time.

## Running the benchmark

```
cd bench
python runner.py
```

This runs 5 tasks with and without Crux and computes the Model Coherence Index (MCI). The benchmark uses a scripted agent that simulates realistic tool call patterns. Each task injects a state change mid-run (file edit, file deletion, config change) and measures whether the agent recovers.

## Running tests

```
# Daemon tests (Rust)
cd daemon && cargo test

# SDK tests (Python, requires a built daemon binary)
cd sdk && python -m pytest tests/ -v
```

## Architecture

```
Claude Code                  Crux Daemon
    |                            |
    |-- Read /config.json ------>|
    |                            |-- observe(read_file, path, hash)
    |                            |-- lookup belief by (file, path)
    |                            |-- no change -> status=valid
    |                            |
    |-- Edit /config.json ------>|
    |                            |-- observe(read_file, path, NEW hash)
    |                            |-- diff detected -> status=invalidated
    |                            |-- propagate to dependents
    |                            |-- return diff inline
    |                            |
    |<-- [crux] 1 belief uncertain
    |
    |-- (agent re-reads stale files)
```

The event log survives process crashes (fsync on every write). On restart, the daemon replays the log to reconstruct the belief graph.
