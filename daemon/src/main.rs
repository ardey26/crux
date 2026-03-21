use std::path::PathBuf;

use crux_daemon::ipc::Daemon;

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();

    let socket_path = args.iter()
        .position(|a| a == "--socket-path")
        .and_then(|i| args.get(i + 1))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp/crux.sock"));

    let idle_timeout: u64 = args.iter()
        .position(|a| a == "--idle-timeout")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(1800);

    let mut daemon = Daemon::new(socket_path.clone());
    daemon.set_idle_timeout(idle_timeout);

    // Spawn signal handler to clean up socket on shutdown
    let sp = socket_path.clone();
    tokio::spawn(async move {
        tokio::signal::ctrl_c().await.ok();
        eprintln!("received signal, shutting down");
        let _ = std::fs::remove_file(&sp);
        std::process::exit(0);
    });

    if let Err(e) = daemon.run().await {
        eprintln!("daemon error: {}", e);
        let _ = std::fs::remove_file(&socket_path);
        std::process::exit(1);
    }
}
