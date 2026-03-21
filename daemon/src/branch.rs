use std::path::PathBuf;

use crate::graph::Graph;
use crate::log::EventLog;
use crate::types::LogEvent;
use crate::ulid::new_ulid;

pub struct Branch {
    pub id: String,
    pub parent_log_offset: u64,
    pub parent_step: u32,
    pub graph_snapshot: Graph,
    pub log: EventLog,
    pub step_counter: u32, // branch-local, starts at 0
}

pub struct BranchManager {
    pub active_branch: Option<Branch>,
    run_id: String,
    base_dir: PathBuf,
}

impl BranchManager {
    pub fn new(run_id: String, base_dir: PathBuf) -> Self {
        Self {
            active_branch: None,
            run_id,
            base_dir,
        }
    }

    /// Create a new branch. Returns branch_id or error if a branch is already active.
    pub fn create_branch(
        &mut self,
        current_graph: &Graph,
        current_log_offset: u64,
        current_step: u32,
    ) -> Result<String, String> {
        if self.active_branch.is_some() {
            return Err("branch_already_active".to_string());
        }

        let branch_id = new_ulid();
        let log_path = self
            .base_dir
            .join(format!("{}.branch.{}.log", self.run_id, branch_id));
        let idx_path = self
            .base_dir
            .join(format!("{}.branch.{}.idx", self.run_id, branch_id));

        let log = EventLog::open_at(log_path, idx_path)
            .map_err(|e| format!("failed to create branch log: {}", e))?;

        self.active_branch = Some(Branch {
            id: branch_id.clone(),
            parent_log_offset: current_log_offset,
            parent_step: current_step,
            graph_snapshot: current_graph.clone(),
            log,
            step_counter: 0,
        });

        Ok(branch_id)
    }

    /// Rollback: restore graph from snapshot, delete branch files.
    pub fn rollback(
        &mut self,
        branch_id: &str,
    ) -> Result<(Graph, u32), String> {
        let branch = self
            .active_branch
            .take()
            .ok_or_else(|| "branch_not_found".to_string())?;

        if branch.id != branch_id {
            // Put it back
            let id = branch.id.clone();
            self.active_branch = Some(branch);
            return Err(format!("branch_not_found: expected {}, got {}", id, branch_id));
        }

        let graph = branch.graph_snapshot;
        let parent_step = branch.parent_step;

        // Clean up branch files
        let _ = std::fs::remove_file(branch.log.log_path());
        let _ = std::fs::remove_file(branch.log.idx_path());

        Ok((graph, parent_step))
    }

    /// Merge: apply branch log entries to main log, return branch graph state.
    pub fn merge(
        &mut self,
        branch_id: &str,
        main_log: &mut EventLog,
    ) -> Result<u32, String> {
        let branch = self
            .active_branch
            .take()
            .ok_or_else(|| "branch_not_found".to_string())?;

        if branch.id != branch_id {
            let id = branch.id.clone();
            self.active_branch = Some(branch);
            return Err(format!("branch_not_found: expected {}, got {}", id, branch_id));
        }

        // Read branch log entries and re-stamp steps to continue from parent_step
        let branch_events = branch
            .log
            .read_all()
            .map_err(|e| format!("failed to read branch log: {}", e))?;

        let mut next_step = branch.parent_step;
        for (timestamp, event) in &branch_events {
            let restamped = match event {
                LogEvent::Observe(obs) => {
                    next_step += 1;
                    let mut new_obs = obs.clone();
                    new_obs.step = next_step;
                    LogEvent::Observe(new_obs)
                }
                other => other.clone(),
            };
            main_log
                .append(&restamped, *timestamp)
                .map_err(|e| format!("failed to append to main log: {}", e))?;
        }

        // The caller's graph (self.graph in ipc.rs) has been receiving branch
        // observations directly — it IS the current state. Don't overwrite it
        // with the stale snapshot taken at branch creation time.

        // Clean up branch files
        let _ = std::fs::remove_file(branch.log.log_path());
        let _ = std::fs::remove_file(branch.log.idx_path());

        Ok(next_step)
    }

    pub fn is_active(&self) -> bool {
        self.active_branch.is_some()
    }

    pub fn active_branch_id(&self) -> Option<&str> {
        self.active_branch.as_ref().map(|b| b.id.as_str())
    }
}
