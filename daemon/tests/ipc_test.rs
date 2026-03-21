use std::os::unix::net::UnixStream;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::Duration;

use tempfile::TempDir;

/// Send a msgpack-framed message and receive the response.
fn send_recv(stream: &mut UnixStream, msg: &serde_json::Value) -> serde_json::Value {
    let payload = rmp_serde::to_vec(msg).unwrap();
    let len = payload.len() as u32;
    stream.write_all(&len.to_le_bytes()).unwrap();
    stream.write_all(&payload).unwrap();
    stream.flush().unwrap();

    // Read response
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf).unwrap();
    let resp_len = u32::from_le_bytes(len_buf) as usize;

    let mut resp_buf = vec![0u8; resp_len];
    stream.read_exact(&mut resp_buf).unwrap();

    rmp_serde::from_slice(&resp_buf).unwrap()
}

fn start_daemon(socket_path: PathBuf, home_dir: &std::path::Path) -> std::process::Child {
    let bin = env!("CARGO_BIN_EXE_crux-daemon");
    std::process::Command::new(bin)
        .arg("--socket-path")
        .arg(&socket_path)
        .env("HOME", home_dir)
        .spawn()
        .expect("failed to start daemon")
}

fn wait_for_socket(path: &PathBuf) {
    for _ in 0..50 {
        if path.exists() {
            // Try connecting
            if UnixStream::connect(path).is_ok() {
                return;
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("daemon socket never appeared at {:?}", path);
}

#[test]
fn full_observe_query_diff_cycle() {
    let tmp = TempDir::new().unwrap();
    let socket_path = tmp.path().join("crux.sock");

    let mut daemon = start_daemon(socket_path.clone(), tmp.path());
    wait_for_socket(&socket_path);

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    // 1. run_start
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "run_start",
            "run_id": "test-ipc-1",
            "request_id": "req-1"
        }),
    );
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["request_id"], "req-1");

    // 2. observe a file
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/config.json"},
            "result": {"exists": true, "hash": "abc123", "size": 100},
            "status": "success",
            "request_id": "req-2"
        }),
    );
    assert_eq!(resp["ok"], true);
    assert!(resp["diff"].is_null()); // first observation, no diff
    assert_eq!(resp["request_id"], "req-2");

    // 3. query the file
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/config.json"},
            "request_id": "req-3"
        }),
    );
    assert!(!resp["belief"].is_null());
    assert_eq!(resp["belief"]["payload"]["hash"], "abc123");
    assert_eq!(resp["belief"]["status"], "valid");
    assert_eq!(resp["request_id"], "req-3");

    // 4. query nonexistent belief — should return null, not error
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/nonexistent.txt"},
            "request_id": "req-4"
        }),
    );
    assert!(resp["belief"].is_null());

    // 5. observe a second file
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/utils.py"},
            "result": {"exists": true, "hash": "xyz789", "size": 200},
            "status": "success",
            "request_id": "req-5"
        }),
    );
    assert_eq!(resp["ok"], true);

    // 6. Get belief IDs for add_edge
    let resp_config = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/config.json"},
            "request_id": "req-6a"
        }),
    );
    let config_id = resp_config["belief"]["id"].as_str().unwrap().to_string();

    let resp_utils = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/utils.py"},
            "request_id": "req-6b"
        }),
    );
    let utils_id = resp_utils["belief"]["id"].as_str().unwrap().to_string();

    // 7. add_edge: utils depends on config
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "add_edge",
            "from": utils_id,
            "to": config_id,
            "weight": 0.8,
            "request_id": "req-7"
        }),
    );
    assert_eq!(resp["ok"], true);

    // 8. Re-observe config with changed hash — should trigger invalidation
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/config.json"},
            "result": {"exists": true, "hash": "CHANGED", "size": 100},
            "status": "success",
            "request_id": "req-8"
        }),
    );
    assert_eq!(resp["ok"], true);
    // Inline diff should show utils.py as uncertain (1.0 * 0.8 * 0.7 = 0.56)
    assert!(!resp["diff"].is_null());
    let diff = &resp["diff"];
    assert_eq!(diff["uncertain"].as_array().unwrap().len(), 1);
    assert_eq!(diff["uncertain"][0]["id"], utils_id);

    // 9. diff query should also return the pending diff
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "diff",
            "request_id": "req-9"
        }),
    );
    assert!(!resp["diff"].is_null());
    assert_eq!(resp["diff"]["uncertain"].as_array().unwrap().len(), 1);

    // 10. Second diff query should return empty (cleared on read)
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "diff",
            "request_id": "req-10"
        }),
    );
    assert!(resp["diff"]["uncertain"].as_array().unwrap().is_empty());
    assert!(resp["diff"]["invalidated"].as_array().unwrap().is_empty());

    // 11. run_end
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "run_end",
            "run_id": "test-ipc-1",
            "reason": "complete",
            "request_id": "req-11"
        }),
    );
    assert_eq!(resp["ok"], true);

    // Cleanup
    drop(stream);
    daemon.kill().ok();
    daemon.wait().ok();
}

#[test]
fn query_at_replays_historical_state() {
    let tmp = TempDir::new().unwrap();
    let socket_path = tmp.path().join("crux.sock");

    let mut daemon = start_daemon(socket_path.clone(), tmp.path());
    wait_for_socket(&socket_path);

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    // run_start
    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "run_start",
            "run_id": "test-ipc-2",
            "request_id": "r1"
        }),
    );

    // Observe config at step 1 with hash "v1"
    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/config.json"},
            "result": {"exists": true, "hash": "v1", "size": 100},
            "status": "success",
            "step": 1,
            "request_id": "r2"
        }),
    );

    // Observe config at step 5 with hash "v2"
    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/config.json"},
            "result": {"exists": true, "hash": "v2", "size": 200},
            "status": "success",
            "step": 5,
            "request_id": "r3"
        }),
    );

    // query_at step 1 should show hash "v1"
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query_at",
            "step": 1,
            "belief_type": "file",
            "filters": {"path": "/config.json"},
            "request_id": "r4"
        }),
    );
    assert_eq!(resp["belief"]["payload"]["hash"], "v1");

    // query_at step 5 should show hash "v2"
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query_at",
            "step": 5,
            "belief_type": "file",
            "filters": {"path": "/config.json"},
            "request_id": "r5"
        }),
    );
    assert_eq!(resp["belief"]["payload"]["hash"], "v2");

    // query_at step 3 (between observations) should show hash "v1" (last state at step 3)
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query_at",
            "step": 3,
            "belief_type": "file",
            "filters": {"path": "/config.json"},
            "request_id": "r6"
        }),
    );
    assert_eq!(resp["belief"]["payload"]["hash"], "v1");

    drop(stream);
    daemon.kill().ok();
    daemon.wait().ok();
}

#[test]
fn branch_rollback_over_ipc() {
    let tmp = TempDir::new().unwrap();
    let socket_path = tmp.path().join("crux.sock");

    let mut daemon = start_daemon(socket_path.clone(), tmp.path());
    wait_for_socket(&socket_path);

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "run_start",
            "run_id": "test-ipc-3",
            "request_id": "r1"
        }),
    );

    // Observe a file
    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/a.py"},
            "result": {"exists": true, "hash": "ha", "size": 100},
            "status": "success",
            "request_id": "r2"
        }),
    );

    // Branch
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "branch",
            "request_id": "r3"
        }),
    );
    let branch_id = resp["branch_id"].as_str().unwrap().to_string();

    // Second branch should fail
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "branch",
            "request_id": "r3b"
        }),
    );
    assert_eq!(resp["error"], "branch_already_active");

    // Observe on branch
    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/b.py"},
            "result": {"exists": true, "hash": "hb", "size": 200},
            "status": "success",
            "request_id": "r4"
        }),
    );

    // Query b.py — should exist on branch
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/b.py"},
            "request_id": "r5"
        }),
    );
    assert!(!resp["belief"].is_null());

    // Rollback
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "rollback",
            "branch_id": branch_id,
            "request_id": "r6"
        }),
    );
    assert_eq!(resp["ok"], true);

    // Query b.py — should be gone after rollback
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/b.py"},
            "request_id": "r7"
        }),
    );
    assert!(resp["belief"].is_null());

    // a.py should still exist
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "query",
            "belief_type": "file",
            "filters": {"path": "/a.py"},
            "request_id": "r8"
        }),
    );
    assert!(!resp["belief"].is_null());

    drop(stream);
    daemon.kill().ok();
    daemon.wait().ok();
}

#[test]
fn error_before_run_start() {
    let tmp = TempDir::new().unwrap();
    let socket_path = tmp.path().join("crux.sock");

    let mut daemon = start_daemon(socket_path.clone(), tmp.path());
    wait_for_socket(&socket_path);

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    // Send observe before run_start
    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "observe",
            "action": "read_file",
            "inputs": {"path": "/a.py"},
            "result": {"exists": true, "hash": "ha", "size": 100},
            "request_id": "r1"
        }),
    );
    assert_eq!(resp["error"], "no_active_run");

    drop(stream);
    daemon.kill().ok();
    daemon.wait().ok();
}

#[test]
fn add_edge_with_nonexistent_belief_returns_error() {
    let tmp = TempDir::new().unwrap();
    let socket_path = tmp.path().join("crux.sock");

    let mut daemon = start_daemon(socket_path.clone(), tmp.path());
    wait_for_socket(&socket_path);

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "run_start",
            "run_id": "test-ipc-5",
            "request_id": "r1"
        }),
    );

    let resp = send_recv(
        &mut stream,
        &serde_json::json!({
            "type": "add_edge",
            "from": "nonexistent-a",
            "to": "nonexistent-b",
            "weight": 0.9,
            "request_id": "r2"
        }),
    );
    assert_eq!(resp["error"], "belief_not_found");
    assert_eq!(resp["belief_id"], "nonexistent-a");

    drop(stream);
    daemon.kill().ok();
    daemon.wait().ok();
}
