"""Test the hook scripts end-to-end: simulate Claude Code tool calls."""

import json
import os
import subprocess
import tempfile

# Use the SDK's packaged hooks via entry points (falls back to module invocation)
import shutil

HOOK_CMD = shutil.which("crux-hook-post") or "crux-hook-post"
INIT_CMD = shutil.which("crux-hook-init") or "crux-hook-init"


def run_hook(hook_input: dict, socket_path: str) -> str:
    """Run the hook script with given input, return stdout."""
    result = subprocess.run(
        [HOOK_CMD],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env={**os.environ, "CRUX_SOCKET": socket_path},
        timeout=5,
    )
    return result.stdout.strip()


def test_hook_detects_file_change(daemon):
    """Simulate: Read config.json → external change → Read again → Crux detects drift."""
    # Create a test file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"version": 1}')
        fpath = f.name

    try:
        # 1. Init a run
        result = subprocess.run(
            [INIT_CMD],
            input=json.dumps({"session_id": "hook-test-1"}),
            capture_output=True,
            text=True,
            env={**os.environ, "CRUX_SOCKET": daemon},
            timeout=5,
        )
        assert "[crux]" in result.stdout

        # 2. Simulate Read tool call
        output = run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": fpath},
            "tool_result": '     1\t{"version": 1}\n',
        }, daemon)
        # No invalidation on first read
        assert "invalidated" not in output
        assert "uncertain" not in output

        # 3. Externally change the file (simulates another process, git pull, etc.)
        with open(fpath, "w") as f:
            f.write('{"version": 2}')

        # 4. Simulate another Read — Crux should detect the hash changed
        output = run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": fpath},
            "tool_result": '     1\t{"version": 2}\n',
        }, daemon)
        # The file belief itself changes, but no dependent beliefs → no cascade warning
        # (The belief is just updated, not "invalidated" in the cascading sense)
        # This is correct — the value just got refreshed

        # 5. Now test with edges — add a dependent belief
        # Read a second file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f2:
            f2.write("import config")
            fpath2 = f2.name

        run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": fpath2},
            "tool_result": "     1\timport config\n",
        }, daemon)

        # Add edge: fpath2 depends on fpath (via the SDK client directly)
        from crux import WorldModel
        # Connect as a second client to add the edge
        from crux.client import CruxClient
        client = CruxClient(daemon)
        client.connect()

        # Get belief IDs
        r1 = client.send({"type": "query", "belief_type": "file", "filters": {"path": os.path.abspath(fpath)}})
        r2 = client.send({"type": "query", "belief_type": "file", "filters": {"path": os.path.abspath(fpath2)}})
        id1 = r1["belief"]["id"]
        id2 = r2["belief"]["id"]

        client.send({"type": "add_edge", "from": id2, "to": id1, "weight": 0.8})
        client.close()

        # 6. Change the config file again
        with open(fpath, "w") as f:
            f.write('{"version": 3}')

        # 7. Read it — now the dependent belief should become uncertain
        output = run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": fpath},
            "tool_result": '     1\t{"version": 3}\n',
        }, daemon)

        assert "uncertain" in output, f"Expected uncertainty warning, got: {output}"

        os.unlink(fpath2)
    finally:
        os.unlink(fpath)


def test_hook_bash_observation(daemon):
    """Simulate Bash tool call and verify shell_output belief is created."""
    # Init run
    subprocess.run(
        [INIT_CMD],
        input=json.dumps({"session_id": "hook-test-2"}),
        capture_output=True,
        text=True,
        env={**os.environ, "CRUX_SOCKET": daemon},
        timeout=5,
    )

    # Simulate bash
    run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_result": "On branch main\nnothing to commit\n",
    }, daemon)

    # Query the belief
    from crux.client import CruxClient
    client = CruxClient(daemon)
    client.connect()
    resp = client.send({
        "type": "query",
        "belief_type": "shell_output",
        "filters": {"command": "git status"},
    })
    client.close()

    assert resp["belief"] is not None
    assert resp["belief"]["status"] == "valid"


def test_hook_silent_when_no_daemon(tmp_path):
    """Hook should exit 0 silently when daemon isn't running."""
    result = subprocess.run(
        [HOOK_CMD],
        input=json.dumps({
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/nonexistent"},
            "tool_result": "",
        }),
        capture_output=True,
        text=True,
        env={**os.environ, "CRUX_SOCKET": str(tmp_path / "nope.sock")},
        timeout=5,
    )
    assert result.returncode == 0
