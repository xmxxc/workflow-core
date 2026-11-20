#!/usr/bin/env python3
"""Materialize a pinned, unmodified extraction; third-party bulk stays in scratch."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

PIN = "ee5bfb5041bb3e4a49d591c72308aae3c81ecdde"
SELECTED = [
    "crates/ghostty-vt", "vendor/portable-pty", "vendor/libghostty-vt",
    "vendor/portable-pty.patches.md", "vendor/patches/portable-pty",
    "vendor/libghostty-vt.vendor.json", "vendor/libghostty-vt.patches.md",
    "vendor/patches/libghostty-vt", "LICENSE",
]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/tmp/wfc-herdr-source"))
    parser.add_argument("--scratch", type=Path, default=Path("/tmp/wfc-herdr-kernel-prototype-r1"))
    args = parser.parse_args()
    source, scratch = args.source.resolve(), args.scratch.resolve()
    authored = Path(__file__).resolve().parent
    if git(source, "rev-parse", "HEAD").decode().strip() != PIN:
        raise SystemExit("source commit differs from fixed Herdr pin")
    dirty = git(source, "status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", *SELECTED, "Cargo.lock", "Cargo.toml")
    if dirty:
        raise SystemExit("dirty selected source paths: " + dirty.decode())
    if scratch == source or scratch == authored or source in scratch.parents or authored in scratch.parents:
        raise SystemExit("scratch must be separate from source and authored tree")
    marker = scratch / ".wfc-kernel-prototype"
    if scratch.exists() and not marker.exists():
        raise SystemExit("refusing an existing unmarked scratch directory")
    scratch.mkdir(parents=True, exist_ok=True)
    marker.write_text(PIN + "\n")
    manifest = []
    files = git(source, "ls-files", "-z", "--", *SELECTED).split(b"\0")
    for raw in files:
        if not raw:
            continue
        rel = raw.decode()
        src = source / rel
        if src.is_symlink():
            raise SystemExit("unexpected selected symlink: " + rel)
        dst = scratch / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Hash and materialize actual pinned Git bytes, including on autocrlf hosts.
        data = git(source, "cat-file", "blob", f"{PIN}:{rel}")
        dst.write_bytes(data)
        shutil.copymode(src, dst)
        manifest.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    for name in ["Cargo.toml", "src"]:
        if (authored / name).is_dir():
            shutil.copytree(authored / name, scratch / name, dirs_exist_ok=True)
        else:
            shutil.copy2(authored / name, scratch / name)
    # Seed with upstream's exact dependency versions until our own lock exists.
    lock = authored / "Cargo.lock"
    shutil.copy2(lock if lock.exists() else source / "Cargo.lock", scratch / "Cargo.lock")
    provenance = {"source_commit": PIN, "source_tree": git(source, "rev-parse", "HEAD^{tree}").decode().strip(),
                  "selected": SELECTED, "local_upstream_edits": [], "files": manifest}
    (scratch / "extraction.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps({"scratch": str(scratch), "commit": PIN, "files": len(manifest),
                      "bytes": sum(f["bytes"] for f in manifest), "copied": SELECTED}, indent=2))


if __name__ == "__main__":
    main()
