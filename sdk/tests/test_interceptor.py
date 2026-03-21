import os
import tempfile

from crux import CruxInterceptor, WorldModel


def test_read_file_registers_belief(daemon):
    """Step 7 test: intercept read_file, assert belief registered automatically."""
    with WorldModel(socket_path=daemon) as world:
        tools = CruxInterceptor(world)

        # Create a temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"key": "value"}')
            fpath = f.name

        try:
            content = tools.read_file(fpath)
            assert content == '{"key": "value"}'

            # Belief should be registered automatically
            belief = world.query("file", path=fpath)
            assert belief is not None
            assert belief.status == "valid"
            assert belief.confidence == 1.0
            assert belief.payload["exists"] is True
            assert belief.payload["size"] == 16
        finally:
            os.unlink(fpath)


def test_write_file_updates_belief(daemon):
    """Step 7 test: write_file updates the belief hash."""
    with WorldModel(socket_path=daemon) as world:
        tools = CruxInterceptor(world)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original")
            fpath = f.name

        try:
            tools.read_file(fpath)
            b1 = world.query("file", path=fpath)
            old_hash = b1.payload["hash"]

            tools.write_file(fpath, "modified content")
            b2 = world.query("file", path=fpath)
            assert b2.payload["hash"] != old_hash
            assert b2.payload["size"] == len("modified content".encode())
        finally:
            os.unlink(fpath)


def test_run_shell_registers_belief(daemon):
    """Step 7 test: run_shell registers a shell_output belief."""
    with WorldModel(socket_path=daemon) as world:
        tools = CruxInterceptor(world)

        stdout, exit_code = tools.run_shell("echo hello")
        assert "hello" in stdout
        assert exit_code == 0

        belief = world.query("shell_output", command="echo hello")
        assert belief is not None
        assert belief.status == "valid"
        assert belief.payload["exit_code"] == 0


def test_read_env_registers_belief(daemon):
    """Step 7 test: read_env registers an env_var belief."""
    with WorldModel(socket_path=daemon) as world:
        tools = CruxInterceptor(world)

        os.environ["CRUX_TEST_VAR"] = "test_value"
        try:
            value = tools.read_env("CRUX_TEST_VAR")
            assert value == "test_value"

            belief = world.query("env_var", key="CRUX_TEST_VAR")
            assert belief is not None
            assert belief.status == "valid"
            assert belief.payload["present"] is True
        finally:
            del os.environ["CRUX_TEST_VAR"]


def test_read_env_missing_var(daemon):
    """Step 7 test: read_env with missing var registers as not present."""
    with WorldModel(socket_path=daemon) as world:
        tools = CruxInterceptor(world)

        value = tools.read_env("CRUX_DEFINITELY_NOT_SET_12345")
        assert value is None

        belief = world.query("env_var", key="CRUX_DEFINITELY_NOT_SET_12345")
        assert belief is not None
        assert belief.payload["present"] is False
