#!/usr/bin/env python3
"""Count retained bytes/physical nonblank source lines; preserve dependency graphs."""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
SCRATCH = Path("/tmp/wfc-herdr-kernel-prototype-r1")
SOURCE_SUFFIXES = {".rs", ".zig", ".c", ".h", ".cc", ".cpp", ".hpp", ".py", ".sh", ".ps1"}
env = {**os.environ, "CARGO_HOME": "/tmp/wfc-kernel-tools/cargo", "RUSTUP_HOME": "/tmp/wfc-kernel-tools/rustup"}
cargo = "/tmp/wfc-kernel-tools/cargo/bin/cargo"
base = [cargo, "+1.96.1"]
manifest = ["--manifest-path", str(SCRATCH / "Cargo.toml"), "--locked", "--offline"]
metadata = json.loads(subprocess.check_output(base + ["metadata", "--format-version", "1", *manifest], env=env))
(SCRATCH / "cargo-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
for name, extra in [("linux", []), ("all-targets", ["--target", "all"])]:
    result = subprocess.check_output(base + ["tree", *manifest, *extra, "--prefix", "none", "--format", "{p}", "-e", "normal,build", "--no-dedupe"], env=env)
    (SCRATCH / ("cargo-tree-" + name + ".txt")).write_bytes(result)
groups = defaultdict(lambda: {"files": 0, "bytes": 0, "source_nonblank_lines": 0, "inline_test_nonblank_lines": 0, "inline_test_blocks": 0})
rows = []
extraction = json.loads((SCRATCH / "extraction.json").read_text())
for entry in extraction["files"]:
    path = Path(entry["path"])
    data = (SCRATCH / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"], path
    parts = path.parts
    dedicated_test = any(p in ("test", "tests", "testdata", "test_data") for p in parts) or "_tests" in path.stem or path.stem == "test_allocations"
    generated = path.name == "bindings.rs"
    component = "ghostty-rust" if parts[0] == "crates" else "portable-pty-vendor" if "portable-pty" in parts else "ghostty-zig-vendor" if "libghostty-vt" in parts else "licenses-patches-provenance"
    category = component + ("/tests-fixtures" if dedicated_test else "/generated-bindings" if generated else "/retained")
    group = groups[category]
    group["files"] += 1
    group["bytes"] += len(data)
    nonblank = inline = blocks = 0
    if path.suffix in SOURCE_SUFFIXES:
        text = data.decode(errors="replace")
        nonblank = sum(bool(line.strip()) for line in text.splitlines())
        # Format-based inventory, not a language semantic SLOC parser. The selected
        # sources put top-level test closers at column zero. Inline tests remain
        # included in total source lines; this field is a subset, never additive.
        pattern = r'^test\s+"[^\n]*\{.*?^\}' if path.suffix == ".zig" else r'^#\[cfg\(test\)\]\s*\nmod\s+\w+\s*\{.*?^\}'
        for match in re.finditer(pattern, text, re.MULTILINE | re.DOTALL):
            inline += sum(bool(line.strip()) for line in match.group().splitlines())
            blocks += 1
    group["source_nonblank_lines"] += nonblank
    group["inline_test_nonblank_lines"] += inline
    group["inline_test_blocks"] += blocks
    rows.append({**entry, "category": category, "source_nonblank_lines": nonblank, "inline_test_nonblank_lines": inline})
authored = []
for path in sorted(HERE.rglob("*")):
    if path.is_file() and path.suffix in SOURCE_SUFFIXES and "__pycache__" not in path.parts:
        data = path.read_bytes()
        authored.append({"path": str(path.relative_to(HERE)), "bytes": len(data), "nonblank_lines": sum(bool(line.strip()) for line in data.decode().splitlines()), "sha256": hashlib.sha256(data).hexdigest()})
trees = {}
for name in ("linux", "all-targets"):
    lines = (SCRATCH / ("cargo-tree-" + name + ".txt")).read_text().splitlines()
    trees[name] = sorted(set(lines))
binary = SCRATCH / "target/release/herdr-kernel-prototype"
full = Path("/tmp/wfc-herdr-0.9.5")
result = {"counting": "physical nonblank source lines including comments; inline tests are a format-based subset; all retained files count toward bytes; generated bindings explicitly separated",
          "retained_groups": dict(groups), "authored": authored, "retained_total_files": len(rows),
          "retained_total_bytes": sum(r["bytes"] for r in rows), "rust_dependency_closure_including_root": trees,
          "metadata_packages": [{"name": p["name"], "version": p["version"], "license": p["license"], "source": p["source"]} for p in metadata["packages"]],
          "binary": {"bytes": binary.stat().st_size, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()},
          "full_herdr": {"bytes": full.stat().st_size, "sha256": hashlib.sha256(full.read_bytes()).hexdigest()}}
(SCRATCH / "metrics.json").write_text(json.dumps(result, indent=2) + "\n")
(SCRATCH / "source-inventory.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps({k: v for k, v in result.items() if k not in ("metadata_packages", "rust_dependency_closure_including_root")}, indent=2))
