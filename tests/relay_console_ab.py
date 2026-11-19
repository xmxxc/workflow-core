"""Use the verified .2 payload for one controlled queue launch-flag differential."""
import os
from pathlib import Path
from zipfile import ZipFile
import tempfile
import subprocess
import sys

original = Path(os.environ['WFC_TEST_PROGRAM'])
with tempfile.TemporaryDirectory(dir=original.parent) as tmp:
    candidate = Path(tmp) / original.name
    with ZipFile(original) as src, ZipFile(candidate, 'w') as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == 'router.py':
                old = b'capture_output=True, text=True, timeout=float(os.environ.get("WFC_QUEUE_TIMEOUT", 60)))'
                assert data.count(old) == 1
                data = data.replace(old,b'creationflags=subprocess.CREATE_NO_WINDOW, '+old)
            out.writestr(info,data)
    # Same released PE, co-located by hard link, fresh private WFC state.
    os.link(original.parent/'wfc-kernel.exe',candidate.parent/'wfc-kernel.exe')
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('windows_relay_probe.py'))],
                            env={**os.environ,'WFC_TEST_PROGRAM':str(candidate)})
    sys.exit(result.returncode)
