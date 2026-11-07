#!/usr/bin/env python3
"""wfc: the workflow controller's single command.

  wfc dispatch | done | ask | reply | continue | resume | wait | relay   the router (router.py)
  wfc checks | candidate | transcribe | label                           routine checks (ctl.py)
  wfc install-skills                                                    install the bundled skills
  wfc --version

`build.py` packs this file, router.py, ctl.py and skills/ into one executable file.
"""

import shutil
import sys
import zipfile
from pathlib import Path

import ctl
import router

VERSION = "0.1.0"
CTL_COMMANDS = {"checks", "candidate", "transcribe", "label"}
WORKFLOW_SKILL = "surper-coding"
# The prototypes surper-coding builds on; installed only where the user has none of their own.
PROTOTYPES = ("ai-coding", "live-verify")


def bundled_skill_files(name):
    """Yield (relative path, bytes) for a bundled skill, from the build archive or a source checkout."""
    here = Path(__file__).resolve()
    if here.parent.is_file():  # running from the single-file build
        with zipfile.ZipFile(here.parent) as archive:
            prefix = f"skills/{name}/"
            for entry in archive.namelist():
                if entry.startswith(prefix) and not entry.endswith("/"):
                    yield entry[len(prefix):], archive.read(entry)
    else:
        root = here.parent / "skills" / name
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            yield str(path.relative_to(root)), path.read_bytes()


def install_skill(name, target, replace):
    """Install one bundled skill into `target`. Returns what happened, for the report."""
    destination = target / name
    if destination.is_symlink():
        return f"{destination}: left alone (a linked development install)"
    if destination.exists() and not replace:
        return f"{destination}: left alone (already installed)"
    staging = target / f".{name}.wfc-new"
    shutil.rmtree(staging, ignore_errors=True)
    for relative, data in bundled_skill_files(name):
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return f"{destination}: installed"


def install_skills(argv):
    if argv:
        raise SystemExit("usage: wfc install-skills")
    targets = [Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"]
    found = [t for t in targets if t.parent.is_dir()]
    if not found:
        raise SystemExit("neither ~/.claude nor ~/.codex exists; install Claude Code or Codex first")
    for target in found:
        target.mkdir(exist_ok=True)
        print(install_skill(WORKFLOW_SKILL, target, replace=True))
        for name in PROTOTYPES:
            print(install_skill(name, target, replace=False))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--version"], ["-V"]):
        print(f"wfc {VERSION}")
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "install-skills":
        return install_skills(argv[1:])
    if argv[0] in CTL_COMMANDS:
        return ctl.main(argv)
    return router.main(argv)


if __name__ == "__main__":
    sys.exit(main())
