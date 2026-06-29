# /// script
# requires-python = ">=3.11"
# ///
"""Build ``dist/opcua-client-gui.exe`` from ``app.py`` using the project venv's pyinstaller."""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    entry = Path("app.py")
    icon = Path("app.ico")
    if not entry.exists():
        raise SystemExit(f"{entry} not found")

    cmd = [
        "pyinstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "opcua-client-gui",
        "--icon", str(icon),
        str(entry),
    ]
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)

    exe = Path("dist") / "opcua-client-gui.exe"
    if exe.exists():
        print(f"build complete, {exe} created.")
    else:
        print("pyinstaller exited cleanly; check dist/ for the binary.")


if __name__ == "__main__":
    main()
