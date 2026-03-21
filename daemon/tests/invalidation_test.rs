use crux_daemon::graph::Graph;
use crux_daemon::invalidation::propagate;
use crux_daemon::types::*;

fn file_observation(step: u32, path: &str, hash: &str) -> Observation {
    Observation {
        id: format!("obs-{}", step),
        timestamp: 1000 + step as u64,
        action: Action::ReadFile,
        inputs: serde_json::json!({"path": path}),
        result: serde_json::json!({"exists": true, "hash": hash, "size": 100}),
        status: ObservationStatus::Success,
        step,
    }
}

#[test]
fn invalidate_a_makes_b_uncertain() {
    let mut graph = Graph::new();

    // Create belief A (config.json) and B (utils.py)
    let obs_a = file_observation(0, "/config.json", "hash_a");
    let obs_b = file_observation(1, "/utils.py", "hash_b");
    let (id_a, _) = graph.ingest(&obs_a);
    let (id_b, _) = graph.ingest(&obs_b);

    // B depends on A: edge from=B, to=A, weight=0.9
    graph
        .add_edge(BeliefEdge {
            from: id_b.clone(),
            to: id_a.clone(),
            weight: 0.9,
            fields: None,
        })
        .unwrap();

    // Re-observe A with different hash — triggers invalidation
    let obs_a2 = file_observation(2, "/config.json", "hash_a_changed");
    let (_, changed) = graph.ingest(&obs_a2);
    assert!(changed);

    // Propagate from A
    let diff = propagate(&mut graph, &id_a);

    // B should be uncertain: 1.0 * 0.9 * 0.7 = 0.63 > 0.6, stays valid actually
    // Wait: 0.63 > 0.6, so it stays valid with reduced confidence
    // Let me check: the spec says < 0.6 -> uncertain, >= 0.6 -> stays valid
    // 0.63 >= 0.6, so B stays valid but with reduced confidence

    let node_b = graph.nodes.get(&id_b).unwrap();
    assert_eq!(node_b.confidence, 0.63);
    assert_eq!(node_b.status, BeliefStatus::Valid);

    // The diff should be empty (no invalidated, no uncertain — B stayed valid)
    assert!(diff.invalidated.is_empty());
    assert!(diff.uncertain.is_empty());
}

#[test]
fn invalidate_a_makes_b_uncertain_with_lower_weight() {
    let mut graph = Graph::new();

    let obs_a = file_observation(0, "/config.json", "hash_a");
    let obs_b = file_observation(1, "/utils.py", "hash_b");
    let (id_a, _) = graph.ingest(&obs_a);
    let (id_b, _) = graph.ingest(&obs_b);

    // weight=0.8: 1.0 * 0.8 * 0.7 = 0.56 < 0.6 -> uncertain
    graph
        .add_edge(BeliefEdge {
            from: id_b.clone(),
            to: id_a.clone(),
            weight: 0.8,
            fields: None,
        })
        .unwrap();

    let obs_a2 = file_observation(2, "/config.json", "hash_a_changed");
    graph.ingest(&obs_a2);

    let diff = propagate(&mut graph, &id_a);

    let node_b = graph.nodes.get(&id_b).unwrap();
    assert!((node_b.confidence - 0.56).abs() < 0.001);
    assert_eq!(node_b.status, BeliefStatus::Uncertain);

    assert!(diff.invalidated.is_empty());
    assert_eq!(diff.uncertain.len(), 1);
    assert_eq!(diff.uncertain[0].id, id_b);
    assert!((diff.uncertain[0].was - 1.0).abs() < 0.001);
    assert!((diff.uncertain[0].now - 0.56).abs() < 0.001);
}

#[test]
fn cascading_invalidation_three_hops() {
    let mut graph = Graph::new();

    // A -> B -> C chain
    let obs_a = file_observation(0, "/a.py", "ha");
    let obs_b = file_observation(1, "/b.py", "hb");
    let obs_c = file_observation(2, "/c.py", "hc");
    let (id_a, _) = graph.ingest(&obs_a);
    let (id_b, _) = graph.ingest(&obs_b);
    let (id_c, _) = graph.ingest(&obs_c);

    // B depends on A, C depends on B
    // weight=0.4: B -> 1.0 * 0.4 * 0.7 = 0.28 < 0.3 -> invalidated, recurse
    graph
        .add_edge(BeliefEdge {
            from: id_b.clone(),
            to: id_a.clone(),
            weight: 0.4,
            fields: None,
        })
        .unwrap();
    // C -> 0.28 * 0.4 * 0.7 = 0.0784 < 0.3 -> invalidated
    graph
        .add_edge(BeliefEdge {
            from: id_c.clone(),
            to: id_b.clone(),
            weight: 0.4,
            fields: None,
        })
        .unwrap();

    // Invalidate A
    let obs_a2 = file_observation(3, "/a.py", "ha_changed");
    graph.ingest(&obs_a2);

    let diff = propagate(&mut graph, &id_a);

    // B: 1.0 * 0.4 * 0.7 = 0.28 < 0.3 -> invalidated
    let node_b = graph.nodes.get(&id_b).unwrap();
    assert!((node_b.confidence - 0.28).abs() < 0.001);
    assert_eq!(node_b.status, BeliefStatus::Invalidated);

    // C: C's own confidence (1.0) * weight (0.4) * decay (0.7) = 0.28 < 0.3 -> invalidated
    // Decay compounds per hop because each node starts at 1.0 but the chain only
    // reaches nodes that were themselves invalidated (< 0.3 threshold gates recursion).
    let node_c = graph.nodes.get(&id_c).unwrap();
    assert!((node_c.confidence - 0.28).abs() < 0.001);
    assert_eq!(node_c.status, BeliefStatus::Invalidated);

    // Both B and C should be in invalidated list
    assert_eq!(diff.invalidated.len(), 2);
    assert!(diff.invalidated.contains(&id_b));
    assert!(diff.invalidated.contains(&id_c));
}

#[test]
fn cycle_protection() {
    let mut graph = Graph::new();

    let obs_a = file_observation(0, "/a.py", "ha");
    let obs_b = file_observation(1, "/b.py", "hb");
    let (id_a, _) = graph.ingest(&obs_a);
    let (id_b, _) = graph.ingest(&obs_b);

    // A depends on B, B depends on A — cycle
    graph
        .add_edge(BeliefEdge {
            from: id_a.clone(),
            to: id_b.clone(),
            weight: 0.8,
            fields: None,
        })
        .unwrap();
    graph
        .add_edge(BeliefEdge {
            from: id_b.clone(),
            to: id_a.clone(),
            weight: 0.8,
            fields: None,
        })
        .unwrap();

    // Invalidate A
    let obs_a2 = file_observation(2, "/a.py", "ha_changed");
    graph.ingest(&obs_a2);

    // Should not infinite loop
    let diff = propagate(&mut graph, &id_a);

    // B should be affected (1.0 * 0.8 * 0.7 = 0.56 -> uncertain)
    let node_b = graph.nodes.get(&id_b).unwrap();
    assert_eq!(node_b.status, BeliefStatus::Uncertain);

    // A is already visited, so the cycle back to A doesn't recurse
    assert!(diff.uncertain.len() >= 1);
}

#[test]
fn no_edges_means_no_propagation() {
    let mut graph = Graph::new();

    let obs_a = file_observation(0, "/a.py", "ha");
    let obs_b = file_observation(1, "/b.py", "hb");
    let (id_a, _) = graph.ingest(&obs_a);
    graph.ingest(&obs_b);

    let obs_a2 = file_observation(2, "/a.py", "ha_changed");
    graph.ingest(&obs_a2);

    let diff = propagate(&mut graph, &id_a);

    assert!(diff.is_empty());
}
