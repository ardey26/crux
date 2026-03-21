use std::collections::HashSet;

use crate::graph::Graph;
use crate::types::*;

const DECAY_FACTOR: f32 = 0.7;

/// Propagate invalidation from a changed belief through its dependents.
/// `changed_fields` lists which payload fields actually changed (if known).
/// Edges with field filters only trigger if their watched fields are in this set.
/// Returns an InvalidationDiff describing all affected beliefs.
pub fn propagate(graph: &mut Graph, source_belief_id: &str) -> InvalidationDiff {
    // Determine which fields changed on the source belief
    let changed_fields = graph.last_changed_fields.take().unwrap_or_default();

    let mut diff = InvalidationDiff::default();
    let mut visited = HashSet::new();

    propagate_recursive(graph, source_belief_id, &changed_fields, &mut diff, &mut visited);

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
    changed_fields: &[String],
    diff: &mut InvalidationDiff,
    visited: &mut HashSet<String>,
) {
    if !visited.insert(belief_id.to_string()) {
        return; // cycle protection
    }

    // Find all edges where `to == belief_id` (dependents of this belief).
    // O(n) edge scan -- known MVP limitation.
    let dependent_edges: Vec<(String, f32, Option<Vec<String>>)> = graph
        .edges
        .iter()
        .filter(|e| e.to == belief_id)
        .map(|e| (e.from.clone(), e.weight, e.fields.clone()))
        .collect();

    for (dep_id, weight, field_filter) in dependent_edges {
        // Field-level filtering: if the edge specifies fields it cares about,
        // only trigger if at least one of those fields actually changed.
        if let Some(ref watched) = field_filter {
            if !changed_fields.is_empty() {
                let any_match = watched.iter().any(|f| changed_fields.contains(f));
                if !any_match {
                    continue; // none of the watched fields changed, skip this edge
                }
            }
            // If changed_fields is empty (unknown), propagate conservatively
        }

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
            // Recurse -- invalidation cascades (propagate all fields downstream)
            propagate_recursive(graph, &dep_id, &[], diff, visited);
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
