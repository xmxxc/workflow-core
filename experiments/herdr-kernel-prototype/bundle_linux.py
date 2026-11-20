"""Pack a local Linux experiment from a prepared kernel build and WFC zipapp."""
import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(prepared, wfc, output):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wfc-kernel-trial"
        root.mkdir()
        for rel in ("src/main.rs", "Cargo.toml", "Cargo.lock"):
            if digest(prepared / rel) != digest(HERE / rel):
                raise ValueError(f"Unexpected prepared source: {rel}")
        shutil.copy2(prepared / "target/release/herdr-kernel-prototype", root / "wfc-pty")
        shutil.copy2(wfc, root / "wfc.pyz")
        for name in ("probe.py", "controlled_host.py", "smoke.py"):
            shutil.copy2(HERE / name, root / name)
        shutil.copy2(HERE.parents[1] / "LICENSE", root / "LICENSE-WFC")
        for entry in json.loads((HERE / "artifacts/licenses.json").read_text()):
            src = prepared / entry["path"]
            if digest(src) != entry["sha256"]:
                raise ValueError(f"Unexpected license: {src}")
            dst = root / "licenses" / entry["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for name in ("extraction.json", "source-summary.json"):
            shutil.copy2(HERE / "artifacts" / name, root / name)
        files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}
        (root / "manifest.json").write_text(json.dumps(files, indent=2) + "\n")
        with tarfile.open(output, "x:gz") as archive:
            archive.add(root, arcname=root.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared", type=Path)
    parser.add_argument("wfc", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.prepared, args.wfc, args.output)
