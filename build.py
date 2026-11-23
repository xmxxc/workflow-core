#!/usr/bin/env python3
"""Build the single-file `wfc` command: a Python zip application with router.py and wfc.py.
Requires Python 3.9+ and the selected external terminal backend at run time.

  python3 build.py [--out dist/wfc]
"""

import argparse
import hashlib
import json
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ("router.py", "wfc.py", "host_launch.py", "wfc_platform.py", "herdr_backend.py", "bundled_backend.py", "bundled_runtime.py")


def build(out):
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "wfc"
        staging.mkdir()
        for module in (*MODULES, "LICENSE"):
            shutil.copy2(ROOT / module, staging / module)
        shutil.copy2(ROOT / "docs/release-install.md", staging / "INSTALL.md")
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
    shutil.copy2(ROOT / "docs/release-install.md", directory / "INSTALL.md")
    return directory


def copy_companion(directory, binary, notices):
    """Package explicitly supplied build/notice outputs; never download a runtime."""
    directory, binary, notices = Path(directory), Path(binary), Path(notices)
    if not binary.is_file() or not notices.is_dir() or not any(p.is_file() for p in notices.rglob('*')):
        raise ValueError('bundled kernel requires a binary and a populated dependency notice directory')
    if any(path.is_symlink() for path in notices.rglob('*')):
        raise ValueError('kernel notice bundle must not contain symlinks')
    suffix = '.exe' if binary.suffix.lower() == '.exe' else ''
    target = directory / ('wfc-kernel' + suffix)
    notice_target = directory / 'bundled-notices'
    if target.exists() or notice_target.exists():
        raise ValueError('bundled companion destination already exists; use a fresh distribution directory')
    shutil.copy2(binary, target)
    shutil.copytree(notices, notice_target)


def add_companion(directory, binary, notices):
    directory = Path(directory)
    copy_companion(directory, binary, notices)
    shutil.copy2(ROOT / 'docs/bundled-backend.md', directory / 'BUNDLED-EXPERIMENTAL.md')
    manifest = {'experiment': 'bundled-kernel',
                'upstream_commit': 'ee5bfb5041bb3e4a49d591c72308aae3c81ecdde',
                'files': {str(path.relative_to(directory)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(directory.rglob('*')) if path.is_file()}}
    (directory / 'BUNDLED-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out", type=Path, default=ROOT / "dist" / "wfc")
    target.add_argument("--windows-dir", type=Path, help="create a new portable directory with wfc.pyz and wfc.cmd")
    parser.add_argument("--bundled-kernel", type=Path, help="optional explicit compiled companion (experimental)")
    parser.add_argument("--kernel-notices", type=Path, help="dependency notice/provenance directory for the companion")
    args = parser.parse_args()
    if bool(args.bundled_kernel) != bool(args.kernel_notices):
        parser.error('--bundled-kernel and --kernel-notices must be supplied together')
    output = build_windows(args.windows_dir) if args.windows_dir else build(args.out.resolve())
    if args.bundled_kernel:
        add_companion(output if args.windows_dir else output.parent, args.bundled_kernel, args.kernel_notices)
    print(output)
