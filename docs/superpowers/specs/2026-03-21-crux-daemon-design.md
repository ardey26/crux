# Crux Daemon Design — Steps 1-5

**Scope:** Rust daemon only (crux-daemon). Python SDK and benchmark harness are out of scope until all five steps have passing tests.

**Architecture:** Single-threaded tokio, in-memory belief graph, append-only mmap'd event log. Graph state is reconstructed from the log on restart.

**Dependencies:** tokio (async runtime), rmp-serde (MessagePack), memmap2 (memory-mapped reads), ulid (unique IDs), serde/serde_json (serialization). Standard dynamic linking; static/musl deferred to later (original requirement: single static binary — will be addressed before release).

**Known deviations from full spec:** Static binary distribution deferred. `snapshot` IPC message deferred to Step 6 (Python SDK). Composite identity key serialization specified but untestable until a multi-field belief type is added.

---

## 1. Event Log

The append-only binary log is the authoritative source of truth. All other state is derived from it.

### File Layout

- `~/.crux/runs/{run_id}.log` — binary event log
- `~/.crux/runs/{run_id}.idx` — step-to-offset index

### Log Entry Format

```
[u64 timestamp (8 bytes, little-endian)]
[u32 length (4 bytes, little-endian)]
[msgpack payload (variable)]
```

- First entry is always `{"type": "run_start", "run_id": "..."}`
- Every observation is appended as a serialized msgpack blob

### Index File Format

Array of fixed-size entries:

```
[u32 step (4 bytes, little-endian)]
[u64 offset (8 bytes, little-endian)]
```

Append-only. Written alongside each log append.

### Durability

`fsync` after every write to both `.log` and `.idx`. The event log must survive process kill without corruption.

### Reads

`memmap2` maps the log file read-only. Historical queries seek via the index to the correct offset, then deserialize forward.

### Branch Log Files

A branch creates a separate log segment: `{run_id}.branch.{branch_id}.log` with its own index `{run_id}.branch.{branch_id}.idx`. Branch index uses branch-local step numbers starting at 0 — not the global step counter.

---

## 2. Belief Graph

In-memory directed graph built from the log. Serves as a fast query cache over the authoritative log.

### Storage

- `nodes: HashMap<String, BeliefNode>` — keyed by belief ID (ULID)
- `edges: Vec<BeliefEdge>` — flat list; O(n) scan per invalidation query. **Known MVP limitation** — replace with adjacency list indexed by `to` field if performance becomes an issue.
- `identity_index: HashMap<(String, String), String>` — maps `(belief_type, identity_key_value)` to belief ID for O(1) upsert lookups

### Belief Types and Identity Keys

| Type | Identity Key Fields | Tracked Payload Fields |
|---|---|---|
| `file` | `path` | `exists`, `hash`, `size` |
| `http_endpoint` | `url` | `status_code`, `auth_valid`, `response_hash` |
| `env_var` | `key` | `present`, `value_hash` |
| `shell_output` | `command` | `stdout_hash`, `exit_code` |
| `user_intent` | `goal_id` | `goal`, `priority`, `last_updated` |

### Composite Identity Key Serialization

For belief types with multiple identity key fields, the identity key value is built by sorting the key field names alphabetically and joining as `key1=val1,key2=val2` with no whitespace.

### Ingest Flow

When an Observation arrives:

1. Map `action` to `belief_type` (`"read_file"` -> `"file"`, `"http_get"` -> `"http_endpoint"`, `"shell"` -> `"shell_output"`, `"env_read"` -> `"env_var"`, `"set_intent"` -> `"user_intent"`)
2. Extract identity key from `inputs`
3. Look up existing belief via `identity_index`
4. If not found: create new `BeliefNode` with `status="valid"`, `confidence=1.0`, insert into both maps
5. If found: diff new `result` payload against stored `payload`
6. If diff detected: mark as `invalidated`, trigger invalidation propagator
7. Append observation to event log regardless

### Edge Creation

Edges are created explicitly via the `add_edge` IPC message. The client (SDK/interceptor) is responsible for declaring dependencies between beliefs. For example, when the interceptor observes that `utils.py` imports from `config.json`, it sends:

```
{"type": "add_edge", "from": "<utils.py belief id>", "to": "<config.json belief id>", "weight": 0.9, "request_id": "..."}
```

The daemon validates that both belief IDs exist and appends the edge to the log for replay. Response: `{"ok": true, "request_id": "..."}` or `{"error": "belief_not_found", "belief_id": "...", "request_id": "..."}`.

Edges are also logged as events so they are reconstructed on startup replay.

**MVP scope:** No automatic edge inference in the daemon. The SDK layer (Step 7) will provide convenience methods for common patterns. For Step 3 testing, edges are created manually via `add_edge`.

### Reconstruction on Startup

Replay the entire log from offset 0, running each observation and `add_edge` event through the ingest flow (minus re-appending to the log). Any branch log files found on disk are discarded — branches do not survive restart. This is the simplest recovery strategy and avoids the complexity of reconstructing in-flight branch state.

---

## 3. Invalidation Propagator

Triggered when a belief's payload changes on re-observation.

### Algorithm

1. Mark the changed belief as `invalidated`, set `confidence = 0.0`
2. Find all edges where `to == invalidated_belief_id` (O(n) edge scan — MVP limitation)
3. For each dependent belief:
   - `new_confidence = current_confidence * edge.weight * 0.7`
   - If `new_confidence < 0.3`: mark as `invalidated`, recurse (depth-first)
   - Else if `new_confidence < 0.6`: mark as `uncertain`
   - Else: update confidence, keep `valid`
4. Collect all affected beliefs into an `InvalidationDiff`

### Decay Behavior

The 0.7 decay factor compounds multiplicatively per hop. Two hops from source: `confidence * weight1 * 0.7 * weight2 * 0.7`. This is intentionally conservative to avoid false negatives — better to flag something as uncertain than to miss a stale belief. **v2:** The decay factor becomes a configurable parameter.

### Cycle Protection

Track visited belief IDs during recursion. Skip any already-visited node. The graph should be a DAG in practice, but this guards against bugs.

### InvalidationDiff

```rust
struct InvalidationDiff {
    invalidated:     Vec<String>,          // belief IDs fully invalidated (< 0.3)
    uncertain:       Vec<UncertainBelief>, // beliefs with degraded confidence (0.3-0.6)
    action_required: String,               // e.g. "config.json changed — 2 beliefs need re-observation: /src/utils.py, /src/main.py"
}

struct UncertainBelief {
    id:  String,
    was: f32,
    now: f32,
}
```

### Diff Delivery: Two Channels

- **Observe-local diff:** The `observe` response contains the diff caused by that specific observation only.
- **Pending diff queue:** Accumulates all diffs since the last `diff` call. The `diff` query type drains and returns it (clear-on-read).

A client that reads inline diffs on every `observe` misses nothing. A client that polls `diff` periodically gets the full accumulated picture. Both patterns work.

---

## 4. Branching

Fork-and-rollback for speculative agent work.

### Data Model

```rust
struct Branch {
    id: String,                  // ULID
    parent_log_offset: u64,      // byte offset in main log at branch point
    parent_step: u32,            // global step at branch point
    graph_snapshot: Graph,       // clone of in-memory graph at branch time
}
```

Top-level daemon state: `active_branch: Option<String>` — routes observations and queries when `Some`.

### Branch Operation

1. If `active_branch.is_some()`: return error `"branch_already_active"`
2. Generate branch ULID
3. Clone current graph state
4. Create branch log + index files (branch-local steps start at 0)
5. Set `active_branch = Some(branch_id)`
6. New observations go to branch log, applied to branch's graph clone

### Rollback Operation

1. Restore main graph from `graph_snapshot`
2. Delete branch log and index files
3. Remove branch from map, set `active_branch = None`
4. Main log step counter resumes from `parent_step`

### Merge Operation

1. Append branch log entries to main log, re-stamping steps to continue from `parent_step`
2. Replace main graph with the branch's graph state
3. Delete branch files, remove from map, set `active_branch = None`
4. **Note:** After merge, any references to branch-local step numbers are invalidated — they have been re-stamped to global steps.

### Constraint

Only one active branch at a time for MVP. No nested branches.

---

## 5. IPC and Query Engine

Unix socket server and message handling.

### Socket

`/tmp/crux.sock` — removed on startup if stale, created fresh. Single tokio `UnixListener`, one connection at a time. Optional `--socket-path` CLI flag to override.

### Framing

```
[u32 length (4 bytes, little-endian)]
[msgpack payload]
```

Every request includes a `request_id` field. Every response echoes it.

### Message Types

| Request | Response |
|---------|----------|
| `run_start {run_id}` | `{ok: true}` |
| `observe {...observation}` | `{ok: true, diff: InvalidationDiff or null}` — diff is observation-local only |
| `query {belief_type, filters}` | `{belief: BeliefNode or null}` |
| `diff` | `{diff: InvalidationDiff}` — drains pending queue |
| `query_at {step, belief_type, filters}` | `{belief: BeliefNode or null}` |
| `branch` | `{branch_id}` or error if branch active |
| `rollback {branch_id}` | `{ok: true}` or error if branch not found |
| `merge {branch_id}` | `{ok: true}` or error if branch not found |
| `add_edge {from, to, weight}` | `{ok: true}` or error if belief not found |
| `snapshot` | `{snapshot_id, offset}` — pointer to current log offset |

All responses include the echoed `request_id`.

### Error Handling

- Malformed message: `{"error": "malformed", "request_id": ... or null}`, continue listening
- Query for nonexistent belief: return `null`, not error
- Corrupt event log: fail loudly with clear error, do not silently recover
- Branch already active: `{"error": "branch_already_active"}`
- Wrong branch_id on rollback/merge: `{"error": "branch_not_found"}`
- Any message before `run_start` (except `run_start` itself): `{"error": "no_active_run"}`
- `add_edge` with nonexistent belief ID: `{"error": "belief_not_found", "belief_id": "..."}`

### `query_at` Behavior

Replays the main log up to the given step into a temporary in-memory graph, then runs the query against it. O(n) in log entries — acceptable for MVP since historical queries are infrequent. **`query_at` always replays the main log only, regardless of active branch state.** It is a historical view of the main timeline. There is no way to do historical queries against branch state in MVP — this is a known user-facing limitation.

### Startup Sequence

1. Parse CLI args (optional `--socket-path`)
2. Remove stale socket file if present (stale = file exists but no process is listening)
3. Bind `UnixListener`
4. Wait for `run_start` message to initialize run state (reject other messages with `no_active_run`)
5. If run log exists for the given `run_id`, replay it to reconstruct graph state; discard any orphaned branch log files
6. Begin processing messages

### Shutdown

On SIGTERM/SIGINT:
1. Finish processing the current message (if any)
2. Close the Unix socket connection
3. Remove the socket file
4. Exit

---

## Data Structures Reference

```rust
struct Observation {
    id:        String,              // ULID
    timestamp: u64,                 // Unix ms
    action:    String,              // "read_file" | "http_get" | "shell" | "env_read" | "set_intent"
    inputs:    serde_json::Value,
    result:    serde_json::Value,
    status:    String,              // "success" | "failure" | "uncertain"
    step:      u32,                 // agent step number
}

struct BeliefNode {
    id:          String,            // ULID
    belief_type: String,            // "file" | "http_endpoint" | "env_var" | "shell_output" | "user_intent"
    payload:     serde_json::Value,
    confidence:  f32,               // 0.0 - 1.0
    source_obs:  String,            // observation that created/last updated this
    status:      String,            // "valid" | "uncertain" | "invalidated"
    created_at:  u64,
    updated_at:  u64,
}

struct BeliefEdge {
    from:   String,                 // dependent belief id (the one that breaks when `to` changes)
    to:     String,                 // dependency (the belief depended upon — invalidation starts here)
    weight: f32,                    // dependency strength (0.0-1.0)
}
// Edge direction: from DEPENDS ON to. Invalidation flows: to -> from.
// "Find dependents of X" = find edges where to == X.
```

---

## Performance Constraints

- Daemon startup: < 200ms
- `observe` round-trip: < 5ms
- `query` round-trip: < 2ms
- Event log: survives process kill (fsync on every write)

---

## Build Steps and Tests

| Step | What | Test |
|------|------|------|
| 1 | Event log (write + read + replay) | Write 100 observations, replay from offset 50, assert correct |
| 2 | Belief graph (create + update, no invalidation) | Observe same file twice with different hash, assert belief updated |
| 3 | Invalidation propagator | A -> B dependency, invalidate A, assert B uncertain |
| 4 | Branching | Branch, add observations, rollback, assert branch observations gone |
| 5 | Unix socket IPC + query engine | Connect raw socket client (Python script, not SDK), send observe + query, assert correct response |
