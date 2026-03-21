# Crux - Agent Instructions

## What is Crux

Crux tracks what an AI agent believes about the world and tells it when those beliefs go stale. This repo contains:
- `daemon/` - Rust binary that maintains the belief graph
- `sdk/` - Python package (`crux-sdk`) with WorldModel API
- `hooks/` - Claude Code integration (SessionStart, PreToolUse, PostToolUse)

## Release Process

When making changes that affect the daemon or SDK, follow this checklist:

### Version Bumps Required

| Change Type | Files to Update |
|-------------|-----------------|
| Daemon IPC changes | `daemon/Cargo.toml`, `sdk/crux/binary.py` (VERSION) |
| SDK code changes | `sdk/pyproject.toml`, `sdk/crux/binary.py` (VERSION) |
| Any release | All three should be in sync |

### Release Steps

1. **Bump versions** in all relevant files (keep daemon, SDK, and binary.py VERSION in sync)

2. **Build and test**:
   ```bash
   cd daemon && cargo build --release && cargo test
   cd sdk && python -m pytest tests/ -v
   ```

3. **Commit and push**:
   ```bash
   git add -A && git commit -m "feat: description"
   git push origin main
   ```

4. **Tag and create GitHub release**:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z \
     --title "vX.Y.Z" \
     --notes "Release notes here" \
     daemon/target/release/crux-daemon#crux-daemon-darwin-arm64
   ```

5. **Publish SDK to PyPI**:
   ```bash
   make publish
   ```

### Pre-commit Checklist

Before committing changes, verify:
- [ ] Version bumped in all required files
- [ ] `cargo test` passes
- [ ] `pytest` passes
- [ ] README updated if user-facing behavior changed

## Architecture Notes

- The daemon communicates via Unix socket (`/tmp/crux.sock`) using MessagePack
- Beliefs have types: file, shell_output, http_endpoint, env_var, plan
- Invalidation cascades through dependency edges with confidence decay
- TTLs: shell 30s, file 60s, http 2min, env 5min, plan 10min

## Testing the Hooks

To test SessionStart hook output:
```bash
echo '{"cwd": "/path/to/project"}' | python3 hooks/crux-init.py
```

To test stale belief surfacing in a new session, ask: "What context did you receive at session start?"
