use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    ReadFile,
    HttpGet,
    Shell,
    EnvRead,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum BeliefType {
    File,
    HttpEndpoint,
    EnvVar,
    ShellOutput,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum BeliefStatus {
    Valid,
    Uncertain,
    Invalidated,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ObservationStatus {
    Success,
    Failure,
    Uncertain,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Observation {
    pub id: String,
    pub timestamp: u64,
    pub action: Action,
    pub inputs: serde_json::Value,
    pub result: serde_json::Value,
    pub status: ObservationStatus,
    pub step: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BeliefNode {
    pub id: String,
    pub belief_type: BeliefType,
    pub payload: serde_json::Value,
    pub confidence: f32,
    pub source_obs: String,
    pub status: BeliefStatus,
    pub created_at: u64,
    pub updated_at: u64,
    /// Per-belief TTL override in ms. If None, uses BeliefType::default_ttl_ms().
    pub ttl_ms: Option<u64>,
    /// Recent payload hashes for stability detection. If a belief reverts to
    /// a recently-seen value, the change is suppressed (no invalidation cascade).
    /// Stores up to 4 recent hashes. Not serialized to the log.
    #[serde(skip)]
    pub recent_hashes: Vec<u64>,
    /// Count of changes within the last stability window. Used for GC scoring.
    #[serde(skip)]
    pub change_count: u32,
    /// Last time this belief was queried. Used for GC (evict least-recently-read).
    #[serde(skip)]
    pub last_read_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BeliefEdge {
    pub from: String,
    pub to: String,
    pub weight: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct InvalidationDiff {
    pub invalidated: Vec<String>,
    pub uncertain: Vec<UncertainBelief>,
    pub action_required: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UncertainBelief {
    pub id: String,
    pub was: f32,
    pub now: f32,
}

impl InvalidationDiff {
    pub fn is_empty(&self) -> bool {
        self.invalidated.is_empty() && self.uncertain.is_empty()
    }

    pub fn merge(&mut self, other: InvalidationDiff) {
        self.invalidated.extend(other.invalidated);
        self.uncertain.extend(other.uncertain);
        if !other.action_required.is_empty() {
            if !self.action_required.is_empty() {
                self.action_required.push_str("; ");
            }
            self.action_required.push_str(&other.action_required);
        }
    }
}

/// Event types stored in the log.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum LogEvent {
    RunStart { run_id: String },
    Observe(Observation),
    AddEdge(BeliefEdge),
    RunEnd { run_id: String, reason: String },
}

impl Action {
    pub fn to_belief_type(&self) -> BeliefType {
        match self {
            Action::ReadFile => BeliefType::File,
            Action::HttpGet => BeliefType::HttpEndpoint,
            Action::Shell => BeliefType::ShellOutput,
            Action::EnvRead => BeliefType::EnvVar,
        }
    }
}

impl BeliefType {
    /// Default TTL in milliseconds. Volatile types expire faster.
    pub fn default_ttl_ms(&self) -> u64 {
        match self {
            BeliefType::ShellOutput => 30_000,   // 30s — commands can change fast
            BeliefType::File => 60_000,           // 60s
            BeliefType::HttpEndpoint => 120_000,  // 2min
            BeliefType::EnvVar => 300_000,        // 5min — env vars rarely change
        }
    }
}
