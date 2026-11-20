"""Exercise the co-located experimental kernel with a controlled worker on Linux."""
import hashlib
import json
from pathlib import Path
import tempfile

import probe

root = Path(__file__).resolve().parent
if __name__ == "__main__":
    for name, expected in json.loads((root / "manifest.json").read_text()).items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    with tempfile.TemporaryDirectory(prefix="wfc-kernel-smoke-") as scratch:
        probe.SCRATCH = Path(scratch)
        probe.wfc_contract(root / "wfc-pty", root / "wfc.pyz")
