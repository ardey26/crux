# Crux — Claude Code Build Prompt

## What You Are Building

A local daemon called **Crux** — a typed, reactive world-model runtime for AI agents.

Crux externalizes an agent's implicit model of reality (what files exist, what APIs are authenticated, what the user wants) from the context window into a persistent, queryable, reactive belief graph. When a belief becomes stale — because a file changed, an auth token expired, an env var was altered — Crux automatically invalidates every downstream belief that depended on it and surfaces a structured diff to the agent.

The MVP has three components:
1. A Rust daemon (`crux-daemon`)
2. A Python SDK (`crux-py`)
3. A benchmark harness (`crux-bench`)

---

## Project Structure

```
crux/
├── daemon/                  # Rust daemon
│   ├── src/
│   │   ├── main.rs
│   │   ├── ingest.rs        # Observation intake
│   │   ├── graph.rs         # Belief graph engine
│   │   ├── invalidation.rs  # Propagation logic
│   │   ├── log.rs           # Append-only event log
│   │   ├── branch.rs        # Branching / rollback
│   │   ├── query.rs         # Query engine
│   │   └── ipc.rs           # Unix socket server
│   └── Cargo.toml
├── sdk/                     # Python SDK
│   ├── crux/
│   │   ├── __init__.py
│   │   ├── client.py        # Connects to daemon over Unix socket
│   │   ├── world.py         # WorldModel class — main interface
│   │   ├── types.py         # Belief type definitions
│   │   └── interceptor.py   # Claude Code auto-intercept wrapper
│   ├── pyproject.toml
│   └── tests/
│       └── test_world.py
└── bench/                   # Benchmark harness
    ├── runner.py             # Orchestrates benchmark runs
    ├── injector.py           # State change injection
    ├── scorer.py             # Machine-verifiable scoring
    └── tasks/
        ├── task_01_file_refactor/
        ├── task_02_dependency_chain/
        ├── task_03_crash_resume/
        ├── task_04_multi_file_consistency/
        └── task_05_auth_expiry/
```

---

## Component 1: Rust Daemon

### Responsibility
Runs as a background process. Receives Observations from the Python SDK over a Unix socket. Maintains the belief graph. Propagates invalidations. Answers queries.

### Data Structures

```rust
// Every agent action that touches the world
struct Observation {
    id:         String,         // ULID
    timestamp:  u64,            // Unix ms
    action:     String,         // "read_file" | "http_get" | "shell" | "env_read"
    inputs:     serde_json::Value,
    result:     serde_json::Value,
    status:     String,         // "success" | "failure" | "uncertain"
    step:       u32,            // agent step number
}

// A typed belief node in the graph
struct BeliefNode {
    id:           String,       // ULID
    belief_type:  String,       // "file" | "http_endpoint" | "env_var" | "shell_output" | "user_intent"
    payload:      serde_json::Value,
    confidence:   f32,          // 0.0 → 1.0
    source_obs:   String,       // observation that created/last updated this
    status:       String,       // "valid" | "uncertain" | "invalidated"
    created_at:   u64,
    updated_at:   u64,
}

// A causal dependency between two beliefs
struct BeliefEdge {
    from:   String,             // dependent belief id
    to:     String,             // belief it depends on
    weight: f32,                // how strongly this dependency affects confidence
}

// What the daemon sends back when invalidations occur
struct InvalidationDiff {
    invalidated:      Vec<String>,              // belief ids fully invalidated
    uncertain:        Vec<UncertainBelief>,     // beliefs with degraded confidence
    action_required:  String,                  // human-readable summary
}

struct UncertainBelief {
    id:         String,
    was:        f32,
    now:        f32,
}
```

### Belief Types and Their Identity Keys

Each belief type has a set of fields that uniquely identify it — used to find and update existing beliefs on new observations:

| Type | Identity Key Fields | Tracked Payload Fields |
|---|---|---|
| `file` | `path` | `exists`, `hash`, `size` |
| `http_endpoint` | `url` | `status_code`, `auth_valid`, `response_hash` |
| `env_var` | `key` | `present`, `value_hash` |
| `shell_output` | `command` | `stdout_hash`, `exit_code` |
| `user_intent` | `goal_id` | `goal`, `priority`, `last_updated` |

### Ingest Pipeline Logic

When an Observation arrives:

1. Determine which belief type it maps to based on `action`
2. Extract the identity key from `inputs`
3. Look up existing belief node matching that identity key
4. If none exists: create new BeliefNode, status=`valid`, confidence=`1.0`
5. If exists: diff the new `result` payload against stored `payload`
6. If diff detected: mark belief as `invalidated`, trigger Invalidation Propagator
7. Append Observation to event log regardless

### Invalidation Propagator Logic

When a BeliefNode is invalidated:

1. Find all edges where `to == invalidated_belief_id`
2. For each dependent belief:
   ```
   new_confidence = current_confidence * edge.weight * 0.7
   ```
3. If `new_confidence < 0.6`: mark dependent as `uncertain`
4. If `new_confidence < 0.3`: mark dependent as `invalidated`, recurse
5. Collect all affected beliefs into an `InvalidationDiff`
6. Store diff, return it on next query call

### Query Engine

Handle three query types over the Unix socket:

```
// Point query
{ "type": "query", "belief_type": "file", "filters": { "path": "/config.json" } }
→ returns BeliefNode or null

// Pending diff query
{ "type": "diff" }
→ returns InvalidationDiff since last diff query (clears on read)

// Historical query
{ "type": "query_at", "step": 23, "belief_type": "file", "filters": { "path": "/config.json" } }
→ replays event log up to step 23, returns belief state at that point
```

### Branching

```
{ "type": "branch" }
→ returns branch_id (ULID), forks event log at current offset

{ "type": "rollback", "branch_id": "..." }
→ discards branch delta log, restores main log pointer

{ "type": "merge", "branch_id": "..." }
→ applies branch delta to main log (explicit, optional)
```

### Event Log

- Flat binary file: `~/.crux/runs/{run_id}.log`
- Each entry: `[u64 timestamp][u32 length][msgpack bytes]`
- Memory-mapped for reads
- Append-only — never mutate existing entries
- A run starts with `{ "type": "run_start", "run_id": "..." }`
- Index file alongside: `{run_id}.idx` — maps step number to byte offset for O(1) historical queries

### IPC

- Unix socket at `/tmp/crux.sock`
- MessagePack framing: `[u32 length][msgpack bytes]`
- Every message has a `request_id` field; every response echoes it
- Daemon handles one connection per agent run

### Error Handling Rules

- If the daemon receives a malformed message: log it, send `{ "error": "malformed" }`, continue
- If the event log file is corrupt: fail loudly with a clear error message, do not silently recover
- If a query references a belief that doesn't exist: return `null`, not an error

---

## Component 2: Python SDK

### WorldModel Class

```python
class WorldModel:
    def __init__(self, run_id: str = None, socket_path: str = "/tmp/crux.sock"):
        # Connect to daemon, start run, store run_id

    def observe(self, action: str, inputs: dict, result: dict, status: str = "success", step: int = None) -> dict:
        # Send Observation to daemon
        # Returns InvalidationDiff if any invalidations triggered
        # If diff is non-empty, raises BeliefInvalidatedWarning (catchable)

    def query(self, belief_type: str, **filters) -> dict | None:
        # Point query — returns current BeliefNode payload or None

    def diff(self) -> InvalidationDiff:
        # Returns pending invalidations since last diff() call
        # Clears on read

    def snapshot(self) -> str:
        # Returns snapshot_id — a pointer to current log offset

    def branch(self) -> str:
        # Returns branch_id

    def rollback(self, branch_id: str) -> None:
        # Discards branch, restores to pre-branch state

    def at(self, step: int) -> HistoricalWorldModel:
        # Returns a read-only view of world model at given step
        # Supports .query() only
```

### Auto-Interceptor for Claude Code

Wrap the five tool types Claude Code uses most:

```python
class CruxInterceptor:
    def __init__(self, world: WorldModel):
        self.world = world
        self._step = 0

    def read_file(self, path: str) -> str:
        # Execute the actual read
        # Hash the result
        # Call world.observe("read_file", {"path": path}, {"exists": True, "hash": hash, ...})
        # Return file contents

    def write_file(self, path: str, content: str) -> None:
        # Execute write
        # Update belief — hash changes

    def run_shell(self, command: str) -> tuple[str, int]:
        # Execute command
        # world.observe("shell", {"command": command}, {"stdout_hash": hash, "exit_code": code})

    def http_get(self, url: str, headers: dict = None) -> dict:
        # Execute request
        # world.observe("http_get", {"url": url}, {"status_code": ..., "response_hash": ...})

    def read_env(self, key: str) -> str | None:
        # Read env var
        # world.observe("env_read", {"key": key}, {"present": True, "value_hash": hash})
```

Claude Code integration is a one-liner:

```python
from crux import WorldModel, CruxInterceptor

world = WorldModel()
tools = CruxInterceptor(world)
# Use tools.read_file() instead of open()
# Everything else is automatic
```

### World Model Context Injection

A method that serializes current world model state into a compact structured string for injection into the agent's context — replacing the markdown file:

```python
def to_context(self) -> str:
    # Returns a compact structured representation of all valid/uncertain beliefs
    # Format: structured text, not prose
    # Uncertain beliefs are flagged explicitly
    # Total target: under 500 tokens for a typical run
```

Output format:

```
WORLD MODEL [step 31] [run:abc123]

FILES
  /config.json          hash:a3f2  valid
  /src/main.py          hash:b7c1  valid
  /src/utils.py         hash:????  UNCERTAIN (config.json invalidated)

HTTP
  api.github.com        auth:valid  status:200

ENV
  DATABASE_URL          present:true

PENDING INVALIDATIONS: 1
  /src/utils.py — re-observe before acting
```

---

## Component 3: Benchmark Harness

### Runner

```python
# bench/runner.py

class BenchmarkRunner:
    def run_task(self, task_dir: str, with_crux: bool) -> TaskResult:
        # Set up isolated temp environment
        # Spin up agent (Claude via API)
        # If with_crux: wrap tools with CruxInterceptor
        # If not: raw tool calls
        # Inject state changes at configured steps via Injector
        # On completion: run machine verifier
        # Return TaskResult

    def run_suite(self, with_crux: bool) -> SuiteResult:
        # Run all 5 tasks
        # Aggregate scores
        # Return MCI score
```

### TaskResult Schema

```python
@dataclass
class TaskResult:
    task_id:            str
    with_crux:        bool
    completed:          bool        # did it finish?
    verified:           bool        # did machine verifier pass?
    total_tokens:       int
    steps_taken:        int
    steps_repeated:     int         # steps re-done unnecessarily
    stale_belief_errors: int        # actions taken on invalidated beliefs
    crash_recoveries:   int         # successful resumes after kill
```

### MCI Formula

```python
def compute_mci(results: list[TaskResult]) -> float:
    completed = [r for r in results if r.verified]
    completion_rate = len(completed) / len(results)
    
    avg_tokens = sum(r.total_tokens for r in results) / len(results)
    baseline_tokens = 50_000  # establish empirically on first run
    token_overhead = max(0, (avg_tokens - baseline_tokens) / baseline_tokens)
    
    mci = completion_rate * (1 - min(token_overhead, 0.5))
    return round(mci, 3)
```

### The Five Tasks

#### Task 01: File Refactor
```
Setup:    Python project with 3 files, one config.json
Goal:     Refactor auth module to use config values
Inject:   At step 15, silently change a value in config.json
Verify:   pytest passes on final codebase
```

#### Task 02: Dependency Chain
```
Setup:    Empty directory
Goal:     Scaffold project → install deps → write code → write tests
Inject:   At step 10, change the required package version in requirements.txt
Verify:   pip install succeeds + pytest passes
```

#### Task 03: Crash and Resume
```
Setup:    40-step coding task (implement a REST API)
Goal:     Complete the task
Inject:   SIGKILL the agent at step 20
Verify:   Task completes + pytest passes + measure tokens_spent
```

#### Task 04: Multi-File Consistency
```
Setup:    base_schema.py imported by models.py imported by api.py
Goal:     Add a new field to base_schema and propagate it everywhere
Inject:   At step 8, revert base_schema.py to original
Verify:   mypy --strict passes on all three files
```

#### Task 05: Auth Expiry
```
Setup:    Mock API server with expiring tokens (5 min TTL, set to expire at step 12)
Goal:     Authenticate, fetch data, process it, write output.json
Inject:   Token expires at step 12
Verify:   output.json matches expected schema + data values
```

### Machine Verifiers

Each task has a `verify.sh`:

```bash
# task_01/verify.sh
cd $TASK_DIR
python -m pytest tests/ -q --tb=no
echo $?  # 0 = pass, anything else = fail
```

Scorer reads exit code. Binary. No human judgment.

---

## Build Order

Build in this exact sequence. Do not proceed to the next step until the current one has passing tests.

```
Step 1:  Rust daemon — event log only (write + read + replay)
         Test: write 100 observations, replay from offset 50, assert correct

Step 2:  Rust daemon — belief graph (create + update, no invalidation yet)
         Test: observe same file twice with different hash, assert belief updated

Step 3:  Rust daemon — invalidation propagator
         Test: create belief A → belief B depends on A → invalidate A → assert B is uncertain

Step 4:  Rust daemon — branching
         Test: branch, add observations, rollback, assert branch observations gone

Step 5:  Rust daemon — Unix socket IPC + query engine
         Test: connect Python client, send observe + query, assert correct response

Step 6:  Python SDK — WorldModel class
         Test: full observe → query → diff cycle against live daemon

Step 7:  Python SDK — CruxInterceptor
         Test: intercept read_file, assert belief registered automatically

Step 8:  Python SDK — to_context()
         Test: assert output is under 500 tokens for a 30-belief world model

Step 9:  Benchmark harness — runner + injector + scorer
         Test: run Task 01 without Crux, establish baseline MCI

Step 10: Run full benchmark suite with Crux
         Target: MCI improvement >= 0.25 over baseline
```

---

## Constraints

- Daemon must start in under 200ms
- `world.observe()` round-trip must complete in under 5ms
- `world.query()` round-trip must complete in under 2ms
- Event log must survive process kill without corruption (use fsync)
- SDK must work with Python 3.10+
- Zero required external services — runs entirely local
- Single `pip install crux` must be sufficient to install the SDK
- Daemon binary must be distributable as a single static binary

---

## What Success Looks Like

After Step 10, you have:

1. A daemon that runs locally and tracks agent belief state automatically
2. A Python SDK that wraps tool calls in one line of setup
3. A benchmark that produces a single number — MCI — comparing agent performance with and without Crux
4. Machine-verified proof that the system works

The benchmark results ARE the demo. Run it. Show the number going up.
