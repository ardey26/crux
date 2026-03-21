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
