use crux_daemon::branch::BranchManager;
use crux_daemon::graph::Graph;
use crux_daemon::log::EventLog;
use crux_daemon::types::*;
use tempfile::TempDir;

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
fn branch_rollback_restores_graph() {
    let tmp = TempDir::new().unwrap();
    let log_path = tmp.path().join("test.log");
    let idx_path = tmp.path().join("test.idx");

    let mut main_log = EventLog::open_at(log_path, idx_path).unwrap();
    let mut graph = Graph::new();
    let mut branch_mgr = BranchManager::new("test-run".to_string(), tmp.path().to_path_buf());

    // Write run_start + 2 observations to main log
    main_log
        .append(
            &LogEvent::RunStart {
                run_id: "test-run".to_string(),
            },
            0,
        )
        .unwrap();

    let obs1 = file_observation(0, "/a.py", "ha");
    graph.ingest(&obs1);
    main_log.append(&LogEvent::Observe(obs1), 1000).unwrap();

    let obs2 = file_observation(1, "/b.py", "hb");
    graph.ingest(&obs2);
    main_log.append(&LogEvent::Observe(obs2), 1001).unwrap();

    assert_eq!(graph.nodes.len(), 2);
    let main_log_offset = main_log.log_size().unwrap();

    // Create branch
    let branch_id = branch_mgr
        .create_branch(&graph, main_log_offset, 1)
        .unwrap();

    // Add observation on branch
    let obs3 = file_observation(0, "/c.py", "hc"); // branch-local step 0
    graph.ingest(&obs3);

    let branch = branch_mgr.active_branch.as_mut().unwrap();
    branch
        .log
        .append(&LogEvent::Observe(obs3), 1002)
        .unwrap();
    branch.step_counter = 1;

    assert_eq!(graph.nodes.len(), 3); // a, b, c

    // Rollback — graph should go back to 2 nodes
    let (restored_graph, parent_step) = branch_mgr.rollback(&branch_id).unwrap();
    assert_eq!(restored_graph.nodes.len(), 2);
    assert_eq!(parent_step, 1);
    assert!(!branch_mgr.is_active());

    // Branch log files should be cleaned up
    let branch_files: Vec<_> = std::fs::read_dir(tmp.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().contains("branch"))
        .collect();
    assert!(branch_files.is_empty());
}

#[test]
fn cannot_create_second_branch() {
    let tmp = TempDir::new().unwrap();
    let graph = Graph::new();
    let mut branch_mgr = BranchManager::new("test-run".to_string(), tmp.path().to_path_buf());

    branch_mgr.create_branch(&graph, 0, 0).unwrap();
    let result = branch_mgr.create_branch(&graph, 0, 0);
    assert!(result.is_err());
    assert_eq!(result.unwrap_err(), "branch_already_active");
}

#[test]
fn rollback_wrong_branch_id_fails() {
    let tmp = TempDir::new().unwrap();
    let graph = Graph::new();
    let mut branch_mgr = BranchManager::new("test-run".to_string(), tmp.path().to_path_buf());

    branch_mgr.create_branch(&graph, 0, 0).unwrap();
    let result = branch_mgr.rollback("wrong-id");
    assert!(result.is_err());
    assert!(branch_mgr.is_active()); // branch still active
}

#[test]
fn merge_appends_to_main_log() {
    let tmp = TempDir::new().unwrap();
    let log_path = tmp.path().join("main.log");
    let idx_path = tmp.path().join("main.idx");

    let mut main_log = EventLog::open_at(log_path, idx_path).unwrap();
    let mut graph = Graph::new();
    let mut branch_mgr = BranchManager::new("test-run".to_string(), tmp.path().to_path_buf());

    // Write initial observation
    main_log
        .append(
            &LogEvent::RunStart {
                run_id: "test-run".to_string(),
            },
            0,
        )
        .unwrap();
    let obs1 = file_observation(0, "/a.py", "ha");
    graph.ingest(&obs1);
    main_log.append(&LogEvent::Observe(obs1), 1000).unwrap();

    let main_offset = main_log.log_size().unwrap();

    // Branch
    let branch_id = branch_mgr.create_branch(&graph, main_offset, 0).unwrap();

    // Add 2 observations to branch
    let obs_b1 = file_observation(0, "/b.py", "hb"); // branch-local step 0
    graph.ingest(&obs_b1);
    let branch = branch_mgr.active_branch.as_mut().unwrap();
    branch
        .log
        .append(&LogEvent::Observe(obs_b1), 1001)
        .unwrap();

    let obs_b2 = file_observation(1, "/c.py", "hc"); // branch-local step 1
    graph.ingest(&obs_b2);
    let branch = branch_mgr.active_branch.as_mut().unwrap();
    branch
        .log
        .append(&LogEvent::Observe(obs_b2), 1002)
        .unwrap();

    // Merge
    let new_step = branch_mgr
        .merge(&branch_id, &mut main_log)
        .unwrap();

    // Steps re-stamped: parent_step=0, so branch obs get steps 1 and 2
    assert_eq!(new_step, 2);
    assert!(!branch_mgr.is_active());

    // Main log should have: run_start + obs(step=0) + obs(step=1) + obs(step=2) = 4 events
    let all_events = main_log.read_all().unwrap();
    assert_eq!(all_events.len(), 4);

    // Verify re-stamped steps
    match &all_events[2].1 {
        LogEvent::Observe(obs) => assert_eq!(obs.step, 1),
        other => panic!("expected Observe, got {:?}", other),
    }
    match &all_events[3].1 {
        LogEvent::Observe(obs) => assert_eq!(obs.step, 2),
        other => panic!("expected Observe, got {:?}", other),
    }

    // Branch files cleaned up
    let branch_files: Vec<_> = std::fs::read_dir(tmp.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().contains("branch"))
        .collect();
    assert!(branch_files.is_empty());
}
