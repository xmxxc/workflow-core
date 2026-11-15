#!/usr/bin/env python3
"""Build in scratch; bounded curl fallback for Zig HTTP proxy failures."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--scratch", type=Path, default=Path("/tmp/wfc-herdr-kernel-prototype-r1"))
args = parser.parse_args()
scratch = args.scratch.resolve()
tools = Path("/tmp/wfc-kernel-tools")
zig = tools / "zig-x86_64-linux-0.16.0/zig"
env = {**os.environ, "CARGO_HOME": str(tools / "cargo"), "RUSTUP_HOME": str(tools / "rustup"),
       "PATH": str(tools / "cargo/bin") + os.pathsep + os.environ["PATH"], "ZIG": str(zig),
       "ZIG_GLOBAL_CACHE_DIR": str(tools / "zig-cache")}
command = ["cargo", "+1.96.1", "build", "--release", "--locked", "--manifest-path", str(scratch / "Cargo.toml")]
records = []
for attempt in range(1, 13):
    start = time.monotonic()
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = scratch / f"build-probe-{attempt}.log"
    log.write_bytes(result.stdout)
    record = {"command": command, "exit": result.returncode, "seconds": time.monotonic() - start, "log": str(log)}
    records.append(record)
    print(json.dumps(record), flush=True)
    (scratch / "build-results.json").write_text(json.dumps(records, indent=2) + "\n")
    if result.returncode == 0:
        raise SystemExit(0)
    output = result.stdout.decode(errors="replace")
    if "bad HTTP response" not in output and "Connection" not in output:
        raise SystemExit(result.returncode)
    urls = set(re.findall(r'https://[^\s"<>]+', output))
    fetched = False
    seen = set()
    # Only fetch URLs whose expected content hash exists in selected pinned sources.
    for zon in (scratch / "vendor/libghostty-vt").rglob("build.zig.zon"):
        content = zon.read_text()
        for url, expected in re.findall(r'\.url\s*=\s*"([^"]+)"\s*,\s*\.hash\s*=\s*"([^"]+)"', content):
            if url not in urls or url in seen:
                continue
            seen.add(url)
            archive = tools / ("fallback-" + url.rsplit("/", 1)[-1])
            subprocess.run(["curl", "-fL", "--retry", "2", "--max-time", "60", "-o", str(archive), url], check=True)
            hashed = subprocess.check_output([str(zig), "fetch", str(archive)], cwd=scratch / "vendor/libghostty-vt", env=env).decode().strip()
            if hashed != expected:
                raise SystemExit(f"Zig hash mismatch: {hashed} != {expected}")
            record.setdefault("curl_fallbacks", []).append({"url": url, "verified_zig_hash": hashed})
            fetched = True
    (scratch / "build-results.json").write_text(json.dumps(records, indent=2) + "\n")
    if not fetched:
        raise SystemExit("no pinned-source URL matched build failure; inspect log")
raise SystemExit("bounded build retries exhausted")
