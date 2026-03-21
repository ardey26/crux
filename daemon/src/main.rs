use std::path::PathBuf;

use crux_daemon::ipc::Daemon;

#[tokio::main]
async fn main() {
    let socket_path = std::env::args()
        .skip_while(|a| a != "--socket-path")
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp/crux.sock"));

    let mut daemon = Daemon::new(socket_path.clone());

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
