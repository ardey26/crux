use crux_daemon::graph::Graph;
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
fn create_new_belief_on_first_observation() {
    let mut graph = Graph::new();
    let obs = file_observation(0, "/config.json", "abc123");
    let (belief_id, changed) = graph.ingest(&obs);

    assert!(!changed);
    assert_eq!(graph.nodes.len(), 1);

    let node = graph.nodes.get(&belief_id).unwrap();
    assert_eq!(node.belief_type, BeliefType::File);
    assert_eq!(node.status, BeliefStatus::Valid);
    assert_eq!(node.confidence, 1.0);
    assert_eq!(node.payload, serde_json::json!({"exists": true, "hash": "abc123", "size": 100}));
    assert_eq!(node.source_obs, "obs-0");
}

#[test]
fn same_file_same_hash_no_change() {
    let mut graph = Graph::new();
    let obs1 = file_observation(0, "/config.json", "abc123");
    let obs2 = file_observation(1, "/config.json", "abc123");

    let (id1, changed1) = graph.ingest(&obs1);
    let (id2, changed2) = graph.ingest(&obs2);

    assert!(!changed1);
    assert!(!changed2);
    // Same identity key means same belief
    assert_eq!(id1, id2);
    assert_eq!(graph.nodes.len(), 1);

    let node = graph.nodes.get(&id1).unwrap();
    assert_eq!(node.status, BeliefStatus::Valid);
    assert_eq!(node.confidence, 1.0);
    assert_eq!(node.source_obs, "obs-1"); // updated to latest
}

#[test]
fn same_file_different_hash_marks_invalidated() {
    let mut graph = Graph::new();
    let obs1 = file_observation(0, "/config.json", "abc123");
    let obs2 = file_observation(1, "/config.json", "def456");

    let (id1, changed1) = graph.ingest(&obs1);
    let (id2, changed2) = graph.ingest(&obs2);

    assert!(!changed1);
    assert!(changed2); // diff detected
    assert_eq!(id1, id2);
    assert_eq!(graph.nodes.len(), 1);

    let node = graph.nodes.get(&id1).unwrap();
    assert_eq!(node.status, BeliefStatus::Invalidated);
    assert_eq!(node.confidence, 0.0);
    assert_eq!(node.payload, serde_json::json!({"exists": true, "hash": "def456", "size": 100}));
    assert_eq!(node.source_obs, "obs-1");
}

#[test]
fn different_files_create_separate_beliefs() {
    let mut graph = Graph::new();
    let obs1 = file_observation(0, "/config.json", "abc123");
    let obs2 = file_observation(1, "/main.py", "xyz789");

    let (id1, _) = graph.ingest(&obs1);
    let (id2, _) = graph.ingest(&obs2);

    assert_ne!(id1, id2);
    assert_eq!(graph.nodes.len(), 2);
}

#[test]
fn query_returns_existing_belief() {
    let mut graph = Graph::new();
    let obs = file_observation(0, "/config.json", "abc123");
    graph.ingest(&obs);

    let result = graph.query(&BeliefType::File, &serde_json::json!({"path": "/config.json"}));
    assert!(result.is_some());
    assert_eq!(result.unwrap().payload["hash"], "abc123");
}

#[test]
fn query_returns_none_for_missing() {
    let graph = Graph::new();
    let result = graph.query(&BeliefType::File, &serde_json::json!({"path": "/nope.txt"}));
    assert!(result.is_none());
}

#[test]
fn add_edge_validates_beliefs_exist() {
    let mut graph = Graph::new();
    let obs1 = file_observation(0, "/config.json", "abc123");
    let obs2 = file_observation(1, "/main.py", "xyz789");
    let (id1, _) = graph.ingest(&obs1);
    let (id2, _) = graph.ingest(&obs2);

    // Valid edge
    let result = graph.add_edge(BeliefEdge {
        from: id1.clone(),
        to: id2.clone(),
        weight: 0.9,
    });
    assert!(result.is_ok());
    assert_eq!(graph.edges.len(), 1);

    // Invalid edge — nonexistent "from"
    let result = graph.add_edge(BeliefEdge {
        from: "nonexistent".to_string(),
        to: id2.clone(),
        weight: 0.9,
    });
    assert!(result.is_err());
    assert_eq!(result.unwrap_err(), "nonexistent");
}

#[test]
fn all_belief_types_work() {
    let mut graph = Graph::new();

    // HTTP endpoint
    let obs = Observation {
        id: "obs-http".to_string(),
        timestamp: 1000,
        action: Action::HttpGet,
        inputs: serde_json::json!({"url": "https://api.example.com"}),
        result: serde_json::json!({"status_code": 200, "auth_valid": true, "response_hash": "h1"}),
        status: ObservationStatus::Success,
        step: 0,
    };
    graph.ingest(&obs);

    // Env var
    let obs = Observation {
        id: "obs-env".to_string(),
        timestamp: 1001,
        action: Action::EnvRead,
        inputs: serde_json::json!({"key": "DATABASE_URL"}),
        result: serde_json::json!({"present": true, "value_hash": "vh1"}),
        status: ObservationStatus::Success,
        step: 1,
    };
    graph.ingest(&obs);

    // Shell output
    let obs = Observation {
        id: "obs-shell".to_string(),
        timestamp: 1002,
        action: Action::Shell,
        inputs: serde_json::json!({"command": "ls -la"}),
        result: serde_json::json!({"stdout_hash": "sh1", "exit_code": 0}),
        status: ObservationStatus::Success,
        step: 2,
    };
    graph.ingest(&obs);

    assert_eq!(graph.nodes.len(), 3);

    // Query each
    assert!(graph.query(&BeliefType::HttpEndpoint, &serde_json::json!({"url": "https://api.example.com"})).is_some());
    assert!(graph.query(&BeliefType::EnvVar, &serde_json::json!({"key": "DATABASE_URL"})).is_some());
    assert!(graph.query(&BeliefType::ShellOutput, &serde_json::json!({"command": "ls -la"})).is_some());
}
