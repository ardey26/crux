import warnings

from crux import CruxInterceptor, WorldModel


def test_to_context_under_500_tokens_for_30_beliefs(daemon):
    """Step 8 test: to_context output is under 500 tokens for a 30-belief world model."""
    with WorldModel(socket_path=daemon) as world:
        # Generate 30 beliefs across different types
        for i in range(20):
            world.observe(
                "read_file",
                {"path": f"/src/file_{i:02d}.py"},
                {"exists": True, "hash": f"h{i:04x}", "size": 100 + i * 10},
            )

        for i in range(5):
            world.observe(
                "env_read",
                {"key": f"VAR_{i}"},
                {"present": True, "value_hash": f"vh{i:02x}"},
            )

        for i in range(3):
            world.observe(
                "http_get",
                {"url": f"https://api{i}.example.com/v1"},
                {"status_code": 200, "auth_valid": True, "response_hash": f"rh{i:02x}"},
            )

        for i in range(2):
            world.observe(
                "shell",
                {"command": f"make target_{i}"},
                {"stdout_hash": f"sh{i:02x}", "exit_code": 0},
            )

        ctx = world.to_context()

        # Rough token estimate: ~1 token per 4 chars for structured text
        estimated_tokens = len(ctx) / 4
        print(f"\n--- to_context output ({len(ctx)} chars, ~{estimated_tokens:.0f} tokens) ---")
        print(ctx)
        print("--- end ---")

        assert estimated_tokens < 500, f"to_context too large: ~{estimated_tokens:.0f} tokens"
        assert "WORLD MODEL" in ctx
        assert "FILES" in ctx
        assert "ENV" in ctx
        assert "HTTP" in ctx
        assert "SHELL" in ctx
        assert "!!" not in ctx  # no pending issues


def test_to_context_shows_uncertain_beliefs(daemon):
    """Step 8 test: uncertain beliefs are flagged in to_context output."""
    with WorldModel(socket_path=daemon) as world:
        world.observe(
            "read_file",
            {"path": "/config.json"},
            {"exists": True, "hash": "abc123", "size": 100},
        )
        world.observe(
            "read_file",
            {"path": "/utils.py"},
            {"exists": True, "hash": "xyz789", "size": 200},
        )

        config = world.query("file", path="/config.json")
        utils = world.query("file", path="/utils.py")
        world.add_edge(utils.id, config.id, weight=0.8)

        # Change config — utils becomes uncertain
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            world.observe(
                "read_file",
                {"path": "/config.json"},
                {"exists": True, "hash": "CHANGED", "size": 100},
            )

        ctx = world.to_context()
        print(f"\n--- to_context with uncertain ---")
        print(ctx)

        assert "conf=" in ctx  # uncertain belief flagged
        assert "re-observation" in ctx
        assert "/utils.py" in ctx
