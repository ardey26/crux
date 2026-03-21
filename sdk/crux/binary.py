"""Auto-download and cache the crux-daemon binary.

On first use, detects the platform, downloads the matching binary from
the GitHub release, and caches it in ~/.crux/bin/. Subsequent calls
return the cached path.

This follows the same pattern as esbuild, prisma, and lightningcss:
pip install triggers no download, first actual use fetches the binary.
"""

from __future__ import annotations

import os
import platform
import stat
import sys
import urllib.request

# Version must match the GitHub release tag
VERSION = "v0.2.1"
GITHUB_REPO = "ardey26/crux"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".crux", "bin")
BINARY_NAME = "crux-daemon"


def _detect_platform() -> str:
    """Return a platform string like 'darwin-arm64' or 'linux-x86_64'."""
    system = platform.system().lower()  # darwin, linux, windows
    machine = platform.machine().lower()  # arm64, aarch64, x86_64, amd64

    # Normalize machine names
    if machine in ("arm64", "aarch64"):
        machine = "arm64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"

    return f"{system}-{machine}"


def _release_asset_name() -> str:
    """Return the expected asset filename for this platform."""
    plat = _detect_platform()
    return f"crux-daemon-{plat}"


def _download_url() -> str:
    """Return the GitHub release download URL for this platform."""
    asset = _release_asset_name()
    return f"https://github.com/{GITHUB_REPO}/releases/download/{VERSION}/{asset}"


def _cached_path() -> str:
    """Return the path where the binary is cached."""
    return os.path.join(CACHE_DIR, f"{BINARY_NAME}-{VERSION}")


def get_daemon_path() -> str:
    """Return path to the crux-daemon binary, downloading if needed.

    Checks in order:
    1. CRUX_DAEMON env var (explicit override)
    2. crux-daemon on PATH
    3. Cached download in ~/.crux/bin/
    4. Download from GitHub release

    Raises RuntimeError if download fails and no binary is available.
    """
    # 1. Explicit override
    env_path = os.environ.get("CRUX_DAEMON")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Check PATH
    import shutil
    on_path = shutil.which(BINARY_NAME)
    if on_path:
        return on_path

    # 3. Check cache
    cached = _cached_path()
    if os.path.isfile(cached) and os.access(cached, os.X_OK):
        return cached

    # 4. Download
    return _download_binary()


def _download_binary() -> str:
    """Download the daemon binary from GitHub releases."""
    url = _download_url()
    dest = _cached_path()

    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"[crux] Downloading daemon binary for {_detect_platform()}...", file=sys.stderr)
    print(f"[crux] {url}", file=sys.stderr)

    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        raise RuntimeError(
            f"Failed to download crux-daemon from {url}: {e}\n"
            f"You can install manually: download from "
            f"https://github.com/{GITHUB_REPO}/releases and set CRUX_DAEMON=/path/to/binary"
        ) from e

    # Make executable
    st = os.stat(dest)
    os.chmod(dest, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[crux] Cached at {dest}", file=sys.stderr)
    return dest
