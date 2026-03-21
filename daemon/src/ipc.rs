use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};

use crate::branch::BranchManager;
use crate::graph::Graph;
use crate::invalidation;
use crate::log::EventLog;
use crate::types::*;
use crate::ulid::new_ulid;

/// Top-level daemon state.
pub struct Daemon {
    socket_path: PathBuf,
    // Run state — initialized on run_start
    run_id: Option<String>,
    graph: Graph,
    main_log: Option<EventLog>,
    branch_mgr: Option<BranchManager>,
    step_counter: u32,
    pending_diffs: VecDeque<InvalidationDiff>,
}

impl Daemon {
    pub fn new(socket_path: PathBuf) -> Self {
        Self {
            socket_path,
            run_id: None,
            graph: Graph::new(),
            main_log: None,
            branch_mgr: None,
            step_counter: 0,
            pending_diffs: VecDeque::new(),
        }
    }

    pub async fn run(&mut self) -> std::io::Result<()> {
        // Remove stale socket
        let _ = std::fs::remove_file(&self.socket_path);

        let listener = UnixListener::bind(&self.socket_path)?;
        eprintln!("crux-daemon listening on {:?}", self.socket_path);

        // Handle shutdown: remove socket on drop
        let socket_path = self.socket_path.clone();
        let _cleanup = SocketCleanup(socket_path);

        // Accept connections sequentially. Run state persists across connections
        // to support hook-based clients that connect per-message.
        loop {
            let (stream, _) = listener.accept().await?;
            if let Err(e) = self.handle_connection(stream).await {
                eprintln!("connection error: {}", e);
            }
        }
    }

    async fn handle_connection(&mut self, mut stream: UnixStream) -> std::io::Result<()> {
        loop {
            // Read framed message: [u32 length LE][msgpack payload]
            let length = match stream.read_u32_le().await {
                Ok(l) => l as usize,
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    // Client disconnected
                    if self.run_id.is_some() {
                        eprintln!("warning: client disconnected without run_end");
                    }
                    return Ok(());
                }
                Err(e) => return Err(e),
            };

            // Cap message size at 16 MB to prevent OOM from malicious/corrupt clients
            const MAX_MSG_SIZE: usize = 16 * 1024 * 1024;
            if length > MAX_MSG_SIZE {
                eprintln!("rejecting message: {} bytes exceeds {} limit", length, MAX_MSG_SIZE);
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "message too large",
                ));
            }

            let mut buf = vec![0u8; length];
            stream.read_exact(&mut buf).await?;

            let response = self.handle_message(&buf);

            let response_bytes = rmp_serde::to_vec(&response)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
            stream.write_u32_le(response_bytes.len() as u32).await?;
            stream.write_all(&response_bytes).await?;
            stream.flush().await?;
        }
    }

    fn handle_message(&mut self, buf: &[u8]) -> serde_json::Value {
        let msg: serde_json::Value = match rmp_serde::from_slice(buf) {
            Ok(v) => v,
            Err(_) => {
                return serde_json::json!({
                    "error": "malformed",
                    "request_id": null
                });
            }
        };

        let request_id = msg.get("request_id").cloned().unwrap_or(serde_json::Value::Null);
        let msg_type = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");

        let mut response = match msg_type {
            "run_start" => self.handle_run_start(&msg),
            "observe" => self.handle_observe(&msg),
            "query" => self.handle_query(&msg),
            "diff" => self.handle_diff(),
            "query_at" => self.handle_query_at(&msg),
            "branch" => self.handle_branch(),
            "rollback" => self.handle_rollback(&msg),
            "merge" => self.handle_merge(&msg),
            "add_edge" => self.handle_add_edge(&msg),
            "run_end" => self.handle_run_end(&msg),
            _ => serde_json::json!({"error": "malformed"}),
        };

        if let Some(obj) = response.as_object_mut() {
            obj.insert("request_id".to_string(), request_id);
        }
        response
    }

    fn handle_run_start(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        let run_id = match msg.get("run_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };

        // Sanitize run_id — reject path traversal attempts
        if run_id.contains('/') || run_id.contains('\\') || run_id.contains("..") || run_id.is_empty() {
            return serde_json::json!({"error": "invalid_run_id"});
        }

        // Check if log already exists
        let base_dir = runs_dir();
        let log_path = base_dir.join(format!("{}.log", run_id));

        if log_path.exists() {
            // Try to open and check for run_end
            let check_log = match EventLog::open_at(
                log_path.clone(),
                base_dir.join(format!("{}.idx", run_id)),
            ) {
                Ok(l) => l,
                Err(e) => return serde_json::json!({"error": format!("log_open_failed: {}", e)}),
            };

            match check_log.has_run_end() {
                Ok(true) => {
                    return serde_json::json!({"error": "run_already_complete"});
                }
                Ok(false) => {
                    // Incomplete run — replay to reconstruct graph
                    match check_log.read_all() {
                        Ok(events) => {
                            self.graph = Graph::new();
                            self.step_counter = 0;
                            for (_, event) in &events {
                                match event {
                                    LogEvent::Observe(obs) => {
                                        self.graph.ingest(obs);
                                        self.step_counter = obs.step;
                                    }
                                    LogEvent::AddEdge(edge) => {
                                        let _ = self.graph.add_edge(edge.clone());
                                    }
                                    _ => {}
                                }
                            }
                            // Discard orphaned branch files
                            cleanup_branch_files(&base_dir, &run_id);
                        }
                        Err(e) => {
                            return serde_json::json!({"error": format!("corrupt log: {}", e)});
                        }
                    }
                }
                Err(e) => {
                    return serde_json::json!({"error": format!("corrupt log: {}", e)});
                }
            }
        }

        let log = match EventLog::open(&run_id) {
            Ok(l) => l,
            Err(e) => return serde_json::json!({"error": format!("log_create_failed: {}", e)}),
        };

        self.main_log = Some(log);
        self.run_id = Some(run_id.clone());
        self.branch_mgr = Some(BranchManager::new(run_id.clone(), base_dir));

        // Append run_start event (only if log is empty/new)
        let log = self.main_log.as_mut().unwrap();
        let log_size = log.log_size().unwrap_or(0);
        if log_size == 0 {
            if let Err(e) = log.append(&LogEvent::RunStart { run_id }, now_ms()) {
                return serde_json::json!({"error": format!("log_write_failed: {}", e)});
            }
        }

        serde_json::json!({"ok": true})
    }

    fn handle_observe(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        // Parse action
        let action: Action = match msg.get("action").and_then(|v| v.as_str()) {
            Some("read_file") => Action::ReadFile,
            Some("http_get") => Action::HttpGet,
            Some("shell") => Action::Shell,
            Some("env_read") => Action::EnvRead,
            _ => return serde_json::json!({"error": "unknown_action"}),
        };

        let inputs = msg.get("inputs").cloned().unwrap_or(serde_json::Value::Null);
        let result = msg.get("result").cloned().unwrap_or(serde_json::Value::Null);

        let status: ObservationStatus = match msg.get("status").and_then(|v| v.as_str()) {
            Some("success") => ObservationStatus::Success,
            Some("failure") => ObservationStatus::Failure,
            Some("uncertain") => ObservationStatus::Uncertain,
            _ => ObservationStatus::Success, // default
        };

        let step = msg.get("step").and_then(|v| v.as_u64()).map(|v| v as u32);

        // Determine step number
        let obs_step = if let Some(s) = step {
            s
        } else if self.branch_mgr.as_ref().map_or(false, |b| b.is_active()) {
            let branch = self.branch_mgr.as_mut().unwrap().active_branch.as_mut().unwrap();
            branch.step_counter += 1;
            branch.step_counter - 1
        } else {
            self.step_counter += 1;
            self.step_counter
        };

        let obs = Observation {
            id: new_ulid(),
            timestamp: now_ms(),
            action,
            inputs,
            result,
            status,
            step: obs_step,
        };

        // Ingest into graph
        let (belief_id, changed) = self.graph.ingest(&obs);

        // Compute observation-local diff if belief changed
        let obs_diff = if changed {
            let diff = invalidation::propagate(&mut self.graph, &belief_id);
            if !diff.is_empty() {
                // Also add to pending queue
                self.pending_diffs.push_back(diff.clone());
            }
            Some(diff)
        } else {
            None
        };

        // Append to log (branch or main)
        let event = LogEvent::Observe(obs);
        if let Err(e) = self.append_to_active_log(&event) {
            return serde_json::json!({"error": format!("log_write_failed: {}", e)});
        }

        match obs_diff {
            Some(diff) => serde_json::json!({
                "ok": true,
                "diff": {
                    "invalidated": diff.invalidated,
                    "uncertain": diff.uncertain.iter().map(|u| serde_json::json!({
                        "id": u.id,
                        "was": u.was,
                        "now": u.now,
                    })).collect::<Vec<_>>(),
                    "action_required": diff.action_required,
                }
            }),
            None => serde_json::json!({"ok": true, "diff": null}),
        }
    }

    fn handle_query(&self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let belief_type = match parse_belief_type(msg.get("belief_type").and_then(|v| v.as_str())) {
            Some(bt) => bt,
            None => return serde_json::json!({"error": "malformed"}),
        };

        let filters = msg.get("filters").cloned().unwrap_or(serde_json::Value::Null);

        match self.graph.query(&belief_type, &filters) {
            Some(node) => serde_json::json!({
                "belief": {
                    "id": node.id,
                    "belief_type": serde_json::to_value(&node.belief_type).unwrap(),
                    "payload": node.payload,
                    "confidence": node.confidence,
                    "source_obs": node.source_obs,
                    "status": serde_json::to_value(&node.status).unwrap(),
                    "created_at": node.created_at,
                    "updated_at": node.updated_at,
                }
            }),
            None => serde_json::json!({"belief": null}),
        }
    }

    fn handle_diff(&mut self) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        // Drain and merge all pending diffs
        let mut merged = InvalidationDiff::default();
        while let Some(diff) = self.pending_diffs.pop_front() {
            merged.merge(diff);
        }

        serde_json::json!({
            "diff": {
                "invalidated": merged.invalidated,
                "uncertain": merged.uncertain.iter().map(|u| serde_json::json!({
                    "id": u.id,
                    "was": u.was,
                    "now": u.now,
                })).collect::<Vec<_>>(),
                "action_required": merged.action_required,
            }
        })
    }

    fn handle_query_at(&self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let target_step = match msg.get("step").and_then(|v| v.as_u64()) {
            Some(s) => s as u32,
            None => return serde_json::json!({"error": "malformed"}),
        };

        let belief_type = match parse_belief_type(msg.get("belief_type").and_then(|v| v.as_str())) {
            Some(bt) => bt,
            None => return serde_json::json!({"error": "malformed"}),
        };

        let filters = msg.get("filters").cloned().unwrap_or(serde_json::Value::Null);

        // Replay main log from byte 0, processing all events up to target step
        let log = match &self.main_log {
            Some(l) => l,
            None => return serde_json::json!({"belief": null}),
        };

        let events = match log.read_all() {
            Ok(e) => e,
            Err(e) => return serde_json::json!({"error": format!("log_read_failed: {}", e)}),
        };

        let mut temp_graph = Graph::new();
        for (_, event) in &events {
            match event {
                LogEvent::Observe(obs) => {
                    if obs.step > target_step {
                        break;
                    }
                    temp_graph.ingest(obs);
                }
                LogEvent::AddEdge(edge) => {
                    let _ = temp_graph.add_edge(edge.clone());
                }
                _ => {} // skip run_start, run_end
            }
        }

        match temp_graph.query(&belief_type, &filters) {
            Some(node) => serde_json::json!({
                "belief": {
                    "id": node.id,
                    "belief_type": serde_json::to_value(&node.belief_type).unwrap(),
                    "payload": node.payload,
                    "confidence": node.confidence,
                    "source_obs": node.source_obs,
                    "status": serde_json::to_value(&node.status).unwrap(),
                    "created_at": node.created_at,
                    "updated_at": node.updated_at,
                }
            }),
            None => serde_json::json!({"belief": null}),
        }
    }

    fn handle_branch(&mut self) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let log_offset = self.main_log.as_ref().map_or(0, |l| l.log_size().unwrap_or(0));

        match self
            .branch_mgr
            .as_mut()
            .unwrap()
            .create_branch(&self.graph, log_offset, self.step_counter)
        {
            Ok(branch_id) => serde_json::json!({"branch_id": branch_id}),
            Err(e) => serde_json::json!({"error": e}),
        }
    }

    fn handle_rollback(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let branch_id = match msg.get("branch_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };

        match self.branch_mgr.as_mut().unwrap().rollback(&branch_id) {
            Ok((graph, parent_step)) => {
                self.graph = graph;
                self.step_counter = parent_step;
                serde_json::json!({"ok": true})
            }
            Err(e) => serde_json::json!({"error": e}),
        }
    }

    fn handle_merge(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let branch_id = match msg.get("branch_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };

        let main_log = self.main_log.as_mut().unwrap();
        match self
            .branch_mgr
            .as_mut()
            .unwrap()
            .merge(&branch_id, main_log)
        {
            Ok(new_step) => {
                self.step_counter = new_step;
                serde_json::json!({"ok": true})
            }
            Err(e) => serde_json::json!({"error": e}),
        }
    }

    fn handle_add_edge(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let from = match msg.get("from").and_then(|v| v.as_str()) {
            Some(s) => s.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };
        let to = match msg.get("to").and_then(|v| v.as_str()) {
            Some(s) => s.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };
        let weight = msg
            .get("weight")
            .and_then(|v| v.as_f64())
            .unwrap_or(1.0) as f32;

        let edge = BeliefEdge {
            from,
            to,
            weight,
        };

        match self.graph.add_edge(edge.clone()) {
            Ok(()) => {
                let event = LogEvent::AddEdge(edge);
                if let Err(e) = self.append_to_active_log(&event) {
                    return serde_json::json!({"error": format!("log_write_failed: {}", e)});
                }
                serde_json::json!({"ok": true})
            }
            Err(belief_id) => serde_json::json!({
                "error": "belief_not_found",
                "belief_id": belief_id,
            }),
        }
    }

    fn handle_run_end(&mut self, msg: &serde_json::Value) -> serde_json::Value {
        if self.run_id.is_none() {
            return serde_json::json!({"error": "no_active_run"});
        }

        let run_id = match msg.get("run_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => return serde_json::json!({"error": "malformed"}),
        };

        let reason = msg
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("complete")
            .to_string();

        // Warn about unread diffs
        if !self.pending_diffs.is_empty() {
            eprintln!(
                "warning: {} unread diffs discarded on run_end",
                self.pending_diffs.len()
            );
            self.pending_diffs.clear();
        }

        // Append run_end to log
        let event = LogEvent::RunEnd { run_id, reason };
        if let Err(e) = self.append_to_active_log(&event) {
            return serde_json::json!({"error": format!("log_write_failed: {}", e)});
        }

        // Reset state for next run
        self.run_id = None;
        self.graph = Graph::new();
        self.main_log = None;
        self.branch_mgr = None;
        self.step_counter = 0;
        self.pending_diffs.clear();

        serde_json::json!({"ok": true})
    }

    /// Append an event to the active log (branch log if on a branch, main log otherwise).
    fn append_to_active_log(&mut self, event: &LogEvent) -> Result<(), String> {
        if self.branch_mgr.as_ref().map_or(false, |b| b.is_active()) {
            let branch = self.branch_mgr.as_mut().unwrap().active_branch.as_mut().unwrap();
            branch.log.append(event, now_ms())
                .map_err(|e| e.to_string())?;
        } else if let Some(log) = self.main_log.as_mut() {
            log.append(event, now_ms())
                .map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

fn parse_belief_type(s: Option<&str>) -> Option<BeliefType> {
    match s {
        Some("file") => Some(BeliefType::File),
        Some("http_endpoint") => Some(BeliefType::HttpEndpoint),
        Some("env_var") => Some(BeliefType::EnvVar),
        Some("shell_output") => Some(BeliefType::ShellOutput),
        _ => None,
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn runs_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".crux").join("runs")
}

fn cleanup_branch_files(base_dir: &Path, run_id: &str) {
    if let Ok(entries) = std::fs::read_dir(base_dir) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with(&format!("{}.branch.", run_id)) {
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }
}

struct SocketCleanup(PathBuf);

impl Drop for SocketCleanup {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
