#!/usr/bin/env python3
"""
Builds a standalone executable from run.py using PyInstaller, for people
who'd rather download a binary than set up a Python environment.

Usage (from repo root):
    pip install -r build/requirements.txt
    python build/build.py

Output lands in dist/ at the repo root.
"""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Run: pip install -r build/requirements.txt")
        sys.exit(1)

    entry = os.path.join(REPO_ROOT, "run.py")
    icon = os.path.join(REPO_ROOT, "icon.png")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "ropysence",
        "--distpath", os.path.join(REPO_ROOT, "dist"),
        "--workpath", os.path.join(REPO_ROOT, "build", "_work"),
        "--specpath", os.path.join(REPO_ROOT, "build", "_work"),
        entry,
    ]
    if os.path.exists(icon):
        cmd.extend(["--add-data", f"{icon}{os.pathsep}."])

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)
    print("\nDone. Executable is in dist/")


if __name__ == "__main__":
    main()
