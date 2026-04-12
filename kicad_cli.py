"""Helpers for locating the kicad-cli executable across platforms.

Path resolution logic adapted from PR #730 by z2amiller
(https://github.com/Bouni/kicad-jlcpcb-tools/pull/730).
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any


def resolve_kicad_cli_path(pcbnew_module: Any = None) -> str | None:
    """Locate the kicad-cli executable, returning its full path or None.

    Resolution order:
    1. ``KICAD_CLI`` environment variable (full path to executable)
    2. ``kicad-cli`` on the system PATH
    3. Platform-specific default install locations
    4. Derive the app-bundle path from the pcbnew module location (macOS)
    """
    # 1. Explicit env-var override — useful for non-standard installs
    env_cli = os.getenv("KICAD_CLI", "").strip()
    if env_cli and os.path.isfile(env_cli):
        return env_cli

    # 2. System PATH
    if cli := shutil.which("kicad-cli"):
        return cli

    # 3. Platform-specific candidate paths
    candidates: list[str] = []

    if sys.platform.startswith("win"):
        base = os.environ.get("KICAD_PATH", r"C:\Program Files\KiCad")
        for version in ("10.0", "9.0", "8.0", ""):
            sub = os.path.join(base, version, "bin") if version else os.path.join(base, "bin")
            candidates.append(os.path.join(sub, "kicad-cli.exe"))
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "/Applications/KiCad/KiCad Nightly.app/Contents/MacOS/kicad-cli",
        ])
    else:
        # Linux / other POSIX
        candidates.extend([
            "/usr/bin/kicad-cli",
            "/usr/local/bin/kicad-cli",
        ])

    # 4. Derive from pcbnew module location (catches non-standard macOS installs)
    pcbnew_file = getattr(pcbnew_module, "__file__", "") or ""
    if "/Contents/" in pcbnew_file:
        contents_end = pcbnew_file.find("/Contents/") + len("/Contents")
        candidates.append(os.path.join(pcbnew_file[:contents_end], "MacOS", "kicad-cli"))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    return None
