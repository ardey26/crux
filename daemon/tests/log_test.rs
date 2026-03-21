use crux_daemon::log::EventLog;
use crux_daemon::types::*;
use tempfile::TempDir;

fn make_observation(step: u32) -> Observation {
    Observation {
        id: format!("obs-{}", step),
        timestamp: 1000 + step as u64,
        action: Action::ReadFile,
        inputs: serde_json::json!({"path": format!("/file_{}.txt", step)}),
        result: serde_json::json!({"exists": true, "hash": format!("hash_{}", step), "size": step * 100}),
        status: ObservationStatus::Success,
        step,
    }
}

#[test]
fn write_100_observations_replay_from_offset_50() {
    let tmp = TempDir::new().unwrap();
    let log_path = tmp.path().join("test.log");
    let idx_path = tmp.path().join("test.idx");

    let mut log = EventLog::open_at(log_path, idx_path).unwrap();

    // Write run_start
    let run_start = LogEvent::RunStart {
        run_id: "test-run".to_string(),
    };
    log.append(&run_start, 0).unwrap();

    // Write 100 observations, record offset at step 50
    let mut offset_at_50 = 0u64;
    for step in 0..100u32 {
        let obs = make_observation(step);
        let event = LogEvent::Observe(obs);
        let offset = log.append(&event, 1000 + step as u64).unwrap();
        if step == 50 {
            offset_at_50 = offset;
        }
    }

    // Read all events — should be 1 run_start + 100 observations = 101
    let all_events = log.read_all().unwrap();
    assert_eq!(all_events.len(), 101);

    // Verify first event is run_start
    match &all_events[0].1 {
        LogEvent::RunStart { run_id } => assert_eq!(run_id, "test-run"),
        other => panic!("expected RunStart, got {:?}", other),
    }

    // Verify observations are in order
    for i in 0..100usize {
        match &all_events[i + 1].1 {
            LogEvent::Observe(obs) => {
                assert_eq!(obs.step, i as u32);
                assert_eq!(obs.id, format!("obs-{}", i));
            }
            other => panic!("expected Observe at index {}, got {:?}", i + 1, other),
        }
    }

    // Replay from offset 50 — should get observations from step 50 onward
    let from_50 = log.read_from(offset_at_50).unwrap();
    assert_eq!(from_50.len(), 50);

    // Verify first event in the slice is step 50
    match &from_50[0].1 {
        LogEvent::Observe(obs) => assert_eq!(obs.step, 50),
        other => panic!("expected Observe step 50, got {:?}", other),
    }

    // Verify last event is step 99
    match &from_50[49].1 {
        LogEvent::Observe(obs) => assert_eq!(obs.step, 99),
        other => panic!("expected Observe step 99, got {:?}", other),
    }

    // Verify index has exactly 100 entries (one per observation, not run_start)
    let index = log.read_index().unwrap();
    assert_eq!(index.len(), 100);

    // Verify index entry for step 50 points to the correct offset
    let (step_50, idx_offset_50) = index[50];
    assert_eq!(step_50, 50);
    assert_eq!(idx_offset_50, offset_at_50);

    // Verify we can use the index to seek and read a single event
    let single = log.read_from(idx_offset_50).unwrap();
    assert!(!single.is_empty());
    match &single[0].1 {
        LogEvent::Observe(obs) => assert_eq!(obs.step, 50),
        other => panic!("expected Observe step 50, got {:?}", other),
    }
}

#[test]
fn log_survives_add_edge_and_run_end_events() {
    let tmp = TempDir::new().unwrap();
    let log_path = tmp.path().join("test2.log");
    let idx_path = tmp.path().join("test2.idx");

    let mut log = EventLog::open_at(log_path, idx_path).unwrap();

    log.append(
        &LogEvent::RunStart {
            run_id: "run-2".to_string(),
        },
        0,
    )
    .unwrap();

    // Write an observation
    let obs = make_observation(0);
    log.append(&LogEvent::Observe(obs), 1000).unwrap();

    // Write an add_edge event
    let edge = BeliefEdge {
        from: "belief-a".to_string(),
        to: "belief-b".to_string(),
        weight: 0.9,
    };
    log.append(&LogEvent::AddEdge(edge), 1001).unwrap();

    // Write another observation
    let obs2 = make_observation(1);
    log.append(&LogEvent::Observe(obs2), 1002).unwrap();

    // Write run_end
    log.append(
        &LogEvent::RunEnd {
            run_id: "run-2".to_string(),
            reason: "complete".to_string(),
        },
        1003,
    )
    .unwrap();

    // Read all — should be 5 events
    let all = log.read_all().unwrap();
    assert_eq!(all.len(), 5);

    // Index should only have 2 entries (observations only)
    let index = log.read_index().unwrap();
    assert_eq!(index.len(), 2);

    // has_run_end should return true
    assert!(log.has_run_end().unwrap());
}

#[test]
fn empty_log_reads_empty() {
    let tmp = TempDir::new().unwrap();
    let log_path = tmp.path().join("empty.log");
    let idx_path = tmp.path().join("empty.idx");

    let log = EventLog::open_at(log_path, idx_path).unwrap();
    let events = log.read_all().unwrap();
    assert!(events.is_empty());

    let index = log.read_index().unwrap();
    assert!(index.is_empty());

    assert!(!log.has_run_end().unwrap());
}
