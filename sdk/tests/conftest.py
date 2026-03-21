import os
import signal
import subprocess
import tempfile
import time

import pytest


@pytest.fixture
def daemon():
    """Start a crux-daemon in a temp dir and yield (socket_path, home_dir)."""
    tmpdir = tempfile.mkdtemp()
    socket_path = os.path.join(tmpdir, "crux.sock")

    # Find the daemon binary
    daemon_bin = os.path.join(
        os.path.dirname(__file__), "..", "..", "daemon", "target", "release", "crux-daemon"
    )
    if not os.path.exists(daemon_bin):
        daemon_bin = os.path.join(
            os.path.dirname(__file__), "..", "..", "daemon", "target", "debug", "crux-daemon"
        )

    env = {**os.environ, "HOME": tmpdir}
    proc = subprocess.Popen(
        [daemon_bin, "--socket-path", socket_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for socket
    for _ in range(50):
        if os.path.exists(socket_path):
            break
        time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("Daemon socket never appeared")

    yield socket_path

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
