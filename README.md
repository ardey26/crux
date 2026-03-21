# Crux

Agentic knowledge versioning. `git` for what an AI agent believes about the world.

## What it does

An agent reads files, runs commands, checks APIs. Each of those creates a belief -- a versioned snapshot of what the agent thinks reality looks like. When reality changes out from under it, Crux diffs the versions and tells the agent what went stale.

Git tracks file versions across commits. Crux tracks belief versions across agent steps. Git gives you `diff` to see what changed between two points. Crux gives you `query_at` to see what the agent believed at step 5 vs step 20. Git gives you branches for speculative work. Crux gives you the same thing.

The part that goes beyond versioning is the dependency graph. Git doesn't know that `utils.py` depends on `config.json`. Crux does. So when `config.json` changes, it doesn't just tell you that file changed -- it tells you every downstream belief that's now suspect.

Without Crux, an agent that read `config.json` at step 5 and wrote code based on it at step 10 has no way to know that `config.json` was changed at step 8 by a git pull. With Crux, the agent gets told immediately.

## Philosophy

Crux is built on Peirce's pragmatist epistemology. Beliefs are working hypotheses about reality. They're valid until falsified by a new observation. When falsified, you update and move on. No synthesis, no transcendence. Just ground truth.

An agent reads a file and forms a belief: "config.json contains client_id app-12345." That belief is not a fact. It's a hypothesis that holds until the next observation contradicts it. When someone edits config.json and the agent re-reads it, the old belief is falsified. Crux marks it as such and propagates the doubt to everything that depended on it.

This is the entire model. Observe, believe, falsify, update. The belief graph is not a knowledge base. It's a map of working assumptions, each one carrying a confidence score that decays the further it sits from direct observation. Crux doesn't try to reason about what's true. It just tracks what was observed, when, and what depends on what.

## Components

- **daemon/** -- Rust binary that maintains the belief graph, event log, and query engine. Communicates over a Unix socket with MessagePack framing.
- **sdk/** -- Python package (`crux`) with `WorldModel`, `CruxInterceptor`, and a `crux-observe` CLI.
- **hooks/** -- Claude Code integration. PostToolUse hooks that automatically register Read/Write/Edit/Bash tool calls as observations.
- **bench/** -- Benchmark harness with 17 scenarios (8 core + 9 adversarial) that measure agent performance with and without Crux.

## Quick start

### Install

```
pip install crux-sdk
```

Or build from source:

```
make build && make install
```

### Set up Claude Code integration

Add the following to your project's `.claude/settings.json`. Replace `/path/to/crux` with the absolute path to your Crux checkout.

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
    ],
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

The SessionStart hook auto-starts the daemon and initializes a run. The PostToolUse hook registers every file read, write, edit, and shell command as an observation. Each Claude Code session gets its own socket (keyed by session ID), so multiple sessions don't interfere with each other.

That's it. Start a Claude Code session and Crux runs in the background.

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

# Field-level filtering: only cascade when specific payload fields change
world.add_edge(utils.id, config.id, weight=0.8, fields=["hash"])

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

### Branching

Crux supports speculative branching for agent exploration:

```python
branch_id = world.branch()
# ... do speculative work ...
world.rollback(branch_id)  # undo everything since branch point
```

Only one branch can be active at a time.

## How it works

1. The daemon maintains an append-only binary event log and an in-memory belief graph.
2. When an observation arrives (file read, shell command, etc.), the daemon extracts an identity key (file path, command string, env var name) and looks up the existing belief.
3. If the payload differs from the stored belief (different hash, different exit code), the belief is marked as invalidated.
4. Invalidation propagates through dependency edges. Confidence decays by `weight * 0.7` per hop. Below 0.6 it becomes "uncertain", below 0.3 it becomes "invalidated".
5. The daemon returns an inline diff on each observe call and accumulates diffs in a pending queue for polling.
6. **Auto-edges:** When using the hooks or interceptor, writes automatically depend on all files read since the last write. No manual `add_edge` calls needed for the common read-then-write pattern.
7. **TTL expiry:** Beliefs expire after a type-specific TTL (shell: 30s, file: 60s, http: 2min, env: 5min). Expired beliefs are marked uncertain on the next query. Per-belief override via `ttl_ms` on observe.
8. **Stability filter:** If a belief oscillates (3+ changes and reverts to a recently-seen value), the cascade is suppressed. Prevents thrashing from triggering unnecessary re-derivation.
9. **Schema evolution:** If a belief's payload keys change (fields added or removed), it always invalidates regardless of the stability filter.
10. **Field-level edges:** Edges can specify which payload fields they depend on. Invalidation only cascades if one of the watched fields actually changed.
11. **GC:** The `gc` IPC message evicts least-recently-read beliefs when the graph exceeds a configurable max size.

## Belief types

| Type | Identity key | Tracked fields |
|------|-------------|----------------|
| file | path | exists, hash, size |
| http_endpoint | url | status_code, auth_valid, response_hash |
| env_var | key | present, value_hash |
| shell_output | command | stdout_hash, exit_code |

## Running tests

```
make test
```

Or manually:

```
cd daemon && cargo test
cd sdk && python -m pytest tests/ -v
```

## Running the benchmark

```
make bench
```

Or manually:

```
cd bench && python runner.py
```

This runs 17 scenarios with and without Crux and computes the Model Coherence Index (MCI). The benchmark uses a scripted agent that simulates realistic tool call patterns. Each task injects a state change mid-run and measures whether the agent recovers.

The benchmark includes adversarial scenarios that show where Crux hurts or can't help:

| Scenario | MCI | What it tests |
|---|---|---|
| task_01-08 | +0.5 to +0.9 | Core value prop: drift detection, crash recovery |
| a1: Static overhead | 0.0 | Minimum tax on simple tasks |
| a3: False cascade | -0.57 | Cost of coarse-grained hashing |
| a4: Thrashing | -1.0 | Oscillating beliefs without damping |
| b1: Semantic equivalence | -0.32 | Hash differs but meaning is identical |
| b4: Latent dependency | -0.37 | Semantic dependencies invisible to graph |
| b5: Stale plan | -0.10 | Plans aren't first-class beliefs |

Overall: Crux MCI 0.882, baseline 0.353, improvement +0.529.

## Architecture

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant Hook as PostToolUse Hook
    participant D as Crux Daemon

    CC->>CC: Read /config.json
    Hook->>D: observe(read_file, path, hash=a3f2)
    D->>D: Create belief: file /config.json, valid, 1.0
    D-->>Hook: ok, diff=null

    CC->>CC: Read /utils.py
    Hook->>D: observe(read_file, path, hash=b7c1)
    D->>D: Create belief: file /utils.py, valid, 1.0

    Note over D: Client declares: utils.py depends on config.json

    CC->>CC: External process edits config.json
    CC->>CC: Read /config.json (again)
    Hook->>D: observe(read_file, path, hash=CHANGED)
    D->>D: Diff detected, invalidate config.json
    D->>D: Propagate: utils.py confidence 1.0 * 0.8 * 0.7 = 0.56
    D->>D: utils.py status -> uncertain
    D-->>Hook: diff: 1 belief uncertain

    Hook-->>CC: [crux] 1 belief(s) uncertain
    CC->>CC: Agent re-reads stale files
```

### Belief graph

```mermaid
graph LR
    A[config.json<br/>hash: a3f2<br/>status: valid] --> B[utils.py<br/>hash: b7c1<br/>status: valid]
    A --> C[main.py<br/>hash: c9d0<br/>status: valid]

    style A fill:#4a9,stroke:#333
    style B fill:#4a9,stroke:#333
    style C fill:#4a9,stroke:#333
```

When config.json changes:

```mermaid
graph LR
    A[config.json<br/>hash: CHANGED<br/>status: invalidated] --> B[utils.py<br/>hash: b7c1<br/>status: uncertain]
    A --> C[main.py<br/>hash: c9d0<br/>status: uncertain]

    style A fill:#d44,stroke:#333,color:#fff
    style B fill:#da4,stroke:#333
    style C fill:#da4,stroke:#333
```

### Component layout

```mermaid
graph TB
    CC[Claude Code] -->|PostToolUse hook| H[crux-hook.py]
    H -->|msgpack over Unix socket| D[crux-daemon]
    D --> L[(Event Log<br/>~/.crux/runs/)]
    D --> G[Belief Graph<br/>in memory]

    SDK[Python SDK] -->|WorldModel API| D
    CLI[crux-observe CLI] -->|single messages| D

    subgraph Daemon Internals
        D --> INV[Invalidation Propagator]
        D --> BR[Branch Manager]
        D --> QE[Query Engine]
    end
```

The event log survives process crashes (fsync on every write). On restart, the daemon replays the log to reconstruct the belief graph.
