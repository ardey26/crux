import warnings

from crux import BeliefInvalidatedWarning, WorldModel


def test_observe_query_diff_cycle(daemon):
    """Step 6 test: full observe -> query -> diff cycle against live daemon."""
    with WorldModel(socket_path=daemon) as world:
        # Observe a file
        diff = world.observe(
            "read_file",
            {"path": "/config.json"},
            {"exists": True, "hash": "abc123", "size": 100},
        )
        assert diff is None  # first observation, no invalidation

        # Query it back
        belief = world.query("file", path="/config.json")
        assert belief is not None
        assert belief.status == "valid"
        assert belief.confidence == 1.0
        assert belief.payload["hash"] == "abc123"

        # Observe a dependent file
        world.observe(
            "read_file",
            {"path": "/utils.py"},
            {"exists": True, "hash": "xyz789", "size": 200},
        )
        utils = world.query("file", path="/utils.py")
        assert utils is not None

        # Declare dependency: utils depends on config
        config = world.query("file", path="/config.json")
        world.add_edge(utils.id, config.id, weight=0.8)

        # Re-observe config with changed hash — should trigger warning
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            diff = world.observe(
                "read_file",
                {"path": "/config.json"},
                {"exists": True, "hash": "CHANGED", "size": 100},
            )
            assert len(w) == 1
            assert issubclass(w[0].category, BeliefInvalidatedWarning)

        assert diff is not None
        assert len(diff.uncertain) == 1
        assert diff.uncertain[0].id == utils.id

        # utils.py should now be uncertain
        utils_after = world.query("file", path="/utils.py")
        assert utils_after.status == "uncertain"
        assert abs(utils_after.confidence - 0.56) < 0.01

        # Pending diff should have the same info
        pending = world.diff()
        assert len(pending.uncertain) == 1

        # Second diff call should be empty (cleared on read)
        empty = world.diff()
        assert empty.is_empty()

        # Query nonexistent belief returns None
        missing = world.query("file", path="/nonexistent.txt")
        assert missing is None


def test_historical_query(daemon):
    """Step 6 test: query_at returns state at a given step."""
    with WorldModel(socket_path=daemon) as world:
        world.observe(
            "read_file",
            {"path": "/data.json"},
            {"exists": True, "hash": "v1", "size": 50},
            step=1,
        )
        world.observe(
            "read_file",
            {"path": "/data.json"},
            {"exists": True, "hash": "v2", "size": 100},
            step=5,
        )

        # Current state
        current = world.query("file", path="/data.json")
        assert current.payload["hash"] == "v2"

        # Historical: step 1
        past = world.at(1)
        old = past.query("file", path="/data.json")
        assert old.payload["hash"] == "v1"

        # Historical: step 3 (between observations, should show v1)
        mid = world.at(3)
        mid_belief = mid.query("file", path="/data.json")
        assert mid_belief.payload["hash"] == "v1"


def test_branch_rollback(daemon):
    """Step 6 test: branch and rollback via WorldModel."""
    with WorldModel(socket_path=daemon) as world:
        world.observe(
            "read_file",
            {"path": "/a.py"},
            {"exists": True, "hash": "ha", "size": 100},
        )

        branch_id = world.branch()

        world.observe(
            "read_file",
            {"path": "/b.py"},
            {"exists": True, "hash": "hb", "size": 200},
        )
        assert world.query("file", path="/b.py") is not None

        world.rollback(branch_id)

        # b.py should be gone
        assert world.query("file", path="/b.py") is None
        # a.py should still exist
        assert world.query("file", path="/a.py") is not None
