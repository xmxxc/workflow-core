#!/usr/bin/env python3
"""Build the single-file `wfc` command: a Python zip application with router.py and wfc.py.
It needs only python3 (3.9+) and, at run time, tmux.

  python3 build.py [--out dist/wfc]
"""

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ("router.py", "wfc.py", "host_launch.py")


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "wfc")
    print(build(parser.parse_args().out.resolve()))
