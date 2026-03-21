use std::collections::HashMap;
use std::hash::{Hash, Hasher};

use crate::types::*;
use crate::ulid::new_ulid;

/// Hash a serde_json::Value for stability detection.
fn hash_payload(payload: &serde_json::Value) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    let s = payload.to_string();
    s.hash(&mut hasher);
    hasher.finish()
}

/// Max recent hashes to keep for stability detection.
const STABILITY_WINDOW: usize = 4;

/// In-memory belief graph. Serves as a fast query cache over the event log.
#[derive(Debug, Clone)]
pub struct Graph {
    pub nodes: HashMap<String, BeliefNode>,
    pub edges: Vec<BeliefEdge>,
    // Maps (belief_type, identity_key_value) -> belief_id for O(1) upsert lookups.
    pub identity_index: HashMap<(BeliefType, String), String>,
}

impl Graph {
    pub fn new() -> Self {
        Self {
            nodes: HashMap::new(),
            edges: Vec::new(),
            identity_index: HashMap::new(),
        }
    }

    /// Ingest an observation. Returns the belief ID and whether the belief changed (diff detected).
    pub fn ingest(&mut self, obs: &Observation) -> (String, bool) {
        let belief_type = obs.action.to_belief_type();
        let identity_key = extract_identity_key(&belief_type, &obs.inputs);
        let now = obs.timestamp;

        let lookup_key = (belief_type.clone(), identity_key);

        if let Some(belief_id) = self.identity_index.get(&lookup_key).cloned() {
            // Existing belief — check for diff
            let node = self.nodes.get_mut(&belief_id).unwrap();
            let changed = node.payload != obs.result;
            if changed {
                let new_hash = hash_payload(&obs.result);

                // Stability filter: suppress cascade only if the belief is
                // oscillating (3+ changes AND reverting to a recent value).
                // A single revert is legitimate -- only rapid oscillation is thrashing.
                let is_revert = node.change_count >= 3 && node.recent_hashes.contains(&new_hash);

                // Track the old hash before replacing
                let old_hash = hash_payload(&node.payload);
                if node.recent_hashes.len() >= STABILITY_WINDOW {
                    node.recent_hashes.remove(0);
                }
                node.recent_hashes.push(old_hash);
                node.change_count += 1;

                node.payload = obs.result.clone();

                if is_revert {
                    // Revert detected — update payload but don't cascade
                    node.status = BeliefStatus::Valid;
                    node.confidence = 1.0;
                    node.source_obs = obs.id.clone();
                    node.updated_at = now;
                    return (belief_id, false); // false = no cascade
                }

                node.status = BeliefStatus::Invalidated;
                node.confidence = 0.0;
            } else {
                // Same payload — refresh to valid
                node.status = BeliefStatus::Valid;
                node.confidence = 1.0;
            }
            node.source_obs = obs.id.clone();
            node.updated_at = now;
            (belief_id, changed)
        } else {
            // New belief
            let belief_id = new_ulid();
            let node = BeliefNode {
                id: belief_id.clone(),
                belief_type: lookup_key.0.clone(),
                payload: obs.result.clone(),
                confidence: 1.0,
                source_obs: obs.id.clone(),
                status: BeliefStatus::Valid,
                created_at: now,
                updated_at: now,
                ttl_ms: None,
                recent_hashes: Vec::new(),
                change_count: 0,
                last_read_at: now,
            };
            self.nodes.insert(belief_id.clone(), node);
            self.identity_index.insert(lookup_key, belief_id.clone());
            (belief_id, false)
        }
    }

    /// Add an edge. Returns error string if either belief doesn't exist.
    /// Deduplicates: if an edge with the same (from, to) already exists, updates its weight.
    pub fn add_edge(&mut self, edge: BeliefEdge) -> Result<(), String> {
        if !self.nodes.contains_key(&edge.from) {
            return Err(edge.from.clone());
        }
        if !self.nodes.contains_key(&edge.to) {
            return Err(edge.to.clone());
        }
        // Update weight if edge already exists, otherwise push new
        if let Some(existing) = self.edges.iter_mut().find(|e| e.from == edge.from && e.to == edge.to) {
            existing.weight = edge.weight;
        } else {
            self.edges.push(edge);
        }
        Ok(())
    }

    /// Find all valid beliefs that have exceeded their TTL.
    /// Returns belief IDs that should be marked uncertain.
    pub fn find_stale(&self, now: u64) -> Vec<String> {
        self.nodes
            .values()
            .filter(|node| {
                if node.status != BeliefStatus::Valid {
                    return false;
                }
                let ttl = node.ttl_ms.unwrap_or_else(|| node.belief_type.default_ttl_ms());
                now.saturating_sub(node.updated_at) > ttl
            })
            .map(|node| node.id.clone())
            .collect()
    }

    /// Mark a belief as uncertain due to TTL expiry. Returns true if the belief was valid.
    pub fn mark_stale(&mut self, belief_id: &str) -> bool {
        if let Some(node) = self.nodes.get_mut(belief_id) {
            if node.status == BeliefStatus::Valid {
                node.status = BeliefStatus::Uncertain;
                node.confidence = 0.5;
                return true;
            }
        }
        false
    }

    /// Point query: find a belief by type and filters. Updates last_read_at for GC.
    pub fn query(&mut self, belief_type: &BeliefType, filters: &serde_json::Value) -> Option<&BeliefNode> {
        let identity_key = extract_identity_key_from_filters(belief_type, filters);
        let belief_id = self.identity_index.get(&(belief_type.clone(), identity_key))?.clone();
        if let Some(node) = self.nodes.get_mut(&belief_id) {
            node.last_read_at = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
        }
        self.nodes.get(&belief_id)
    }

    /// Immutable query (for historical replay, no side effects).
    pub fn query_readonly(&self, belief_type: &BeliefType, filters: &serde_json::Value) -> Option<&BeliefNode> {
        let identity_key = extract_identity_key_from_filters(belief_type, filters);
        let belief_id = self.identity_index.get(&(belief_type.clone(), identity_key))?;
        self.nodes.get(belief_id)
    }

    /// Garbage collect beliefs not read in `max_idle_ms` and exceeding `max_nodes`.
    /// Returns the number of evicted beliefs.
    pub fn gc(&mut self, now: u64, max_nodes: usize, max_idle_ms: u64) -> usize {
        if self.nodes.len() <= max_nodes {
            return 0;
        }

        // Find candidates: beliefs not read within max_idle_ms
        let mut candidates: Vec<(String, u64)> = self.nodes
            .iter()
            .filter(|(_, node)| now.saturating_sub(node.last_read_at) > max_idle_ms)
            .map(|(id, node)| (id.clone(), node.last_read_at))
            .collect();

        // Sort by last_read_at ascending (oldest first)
        candidates.sort_by_key(|(_, t)| *t);

        let to_evict = self.nodes.len() - max_nodes;
        let evict_count = to_evict.min(candidates.len());

        for (id, _) in candidates.into_iter().take(evict_count) {
            self.nodes.remove(&id);
            // Remove edges referencing this belief
            self.edges.retain(|e| e.from != id && e.to != id);
            // Remove from identity index
            self.identity_index.retain(|_, v| v != &id);
        }

        evict_count
    }

    /// Returns the number of beliefs in the graph.
    pub fn len(&self) -> usize {
        self.nodes.len()
    }
}

/// Extract the identity key value from observation inputs.
/// For single-key types, this is just the value of that field.
/// For composite keys, fields are sorted alphabetically and joined as key1=val1,key2=val2.
fn extract_identity_key(belief_type: &BeliefType, inputs: &serde_json::Value) -> String {
    match belief_type {
        BeliefType::File => inputs["path"].as_str().unwrap_or("").to_string(),
        BeliefType::HttpEndpoint => inputs["url"].as_str().unwrap_or("").to_string(),
        BeliefType::EnvVar => inputs["key"].as_str().unwrap_or("").to_string(),
        BeliefType::ShellOutput => inputs["command"].as_str().unwrap_or("").to_string(),
    }
}

/// Extract identity key from query filters (same logic, different source).
fn extract_identity_key_from_filters(belief_type: &BeliefType, filters: &serde_json::Value) -> String {
    extract_identity_key(belief_type, filters)
}
