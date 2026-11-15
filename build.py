#!/usr/bin/env python3
"""Build the single-file `wfc` command: a Python zip application with router.py and wfc.py.
Requires Python 3.9+ and the selected external terminal backend at run time.

  python3 build.py [--out dist/wfc]
"""

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ("router.py", "wfc.py", "host_launch.py", "wfc_platform.py", "herdr_backend.py")


def build(out):
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "wfc"
        staging.mkdir()
        for module in MODULES:
            shutil.copy2(ROOT / module, staging / module)
        (staging / "__main__.py").write_text("import sys\nimport wfc\n\nsys.exit(wfc.main())\n")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    out.chmod(0o755)
    return out


def build_windows(directory):
    """Same Python payload, with a native cmd entrypoint; no global installation."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    build(directory / "wfc.pyz")
    (directory / "wfc.cmd").write_bytes(
        b'@echo off\r\npython "%~dp0wfc.pyz" %*\r\nexit /b %errorlevel%\r\n')
    shutil.copy2(ROOT / "LICENSE", directory / "LICENSE")
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out", type=Path, default=ROOT / "dist" / "wfc")
    target.add_argument("--windows-dir", type=Path, help="create a new portable directory with wfc.pyz and wfc.cmd")
    args = parser.parse_args()
    print(build_windows(args.windows_dir) if args.windows_dir else build(args.out.resolve()))
