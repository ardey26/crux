use std::collections::HashSet;

use crate::graph::Graph;
use crate::types::*;

const DECAY_FACTOR: f32 = 0.7;

/// Propagate invalidation from a changed belief through its dependents.
/// Returns an InvalidationDiff describing all affected beliefs.
pub fn propagate(graph: &mut Graph, source_belief_id: &str) -> InvalidationDiff {
    let mut diff = InvalidationDiff::default();
    let mut visited = HashSet::new();

    propagate_recursive(graph, source_belief_id, &mut diff, &mut visited);

    if !diff.invalidated.is_empty() || !diff.uncertain.is_empty() {
        let count = diff.invalidated.len() + diff.uncertain.len();
        diff.action_required = format!(
            "{} changed — {} belief{} need re-observation",
            source_belief_id,
            count,
            if count == 1 { "" } else { "s" }
        );
    }

    diff
}

fn propagate_recursive(
    graph: &mut Graph,
    belief_id: &str,
    diff: &mut InvalidationDiff,
    visited: &mut HashSet<String>,
) {
    if !visited.insert(belief_id.to_string()) {
        return; // cycle protection
    }

    // Find all edges where `to == belief_id` (dependents of this belief).
    // O(n) edge scan — known MVP limitation.
    let dependent_edges: Vec<(String, f32)> = graph
        .edges
        .iter()
        .filter(|e| e.to == belief_id)
        .map(|e| (e.from.clone(), e.weight))
        .collect();

    for (dep_id, weight) in dependent_edges {
        let node = match graph.nodes.get_mut(&dep_id) {
            Some(n) => n,
            None => continue,
        };

        let old_confidence = node.confidence;
        // Decay compounds per hop: new = current * weight * 0.7
        let new_confidence = old_confidence * weight * DECAY_FACTOR;

        if new_confidence < 0.3 {
            node.status = BeliefStatus::Invalidated;
            node.confidence = new_confidence;
            diff.invalidated.push(dep_id.clone());
            // Recurse — invalidation cascades
            propagate_recursive(graph, &dep_id, diff, visited);
        } else if new_confidence < 0.6 {
            node.status = BeliefStatus::Uncertain;
            node.confidence = new_confidence;
            diff.uncertain.push(UncertainBelief {
                id: dep_id.clone(),
                was: old_confidence,
                now: new_confidence,
            });
        } else {
            node.confidence = new_confidence;
            // Stays valid, but with reduced confidence
        }
    }
}
