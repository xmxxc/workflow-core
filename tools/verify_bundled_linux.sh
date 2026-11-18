#!/bin/sh
# Run inside a uniquely named disposable systemd --user Delegate=yes unit.
set -eu
repo=$(realpath "$1")
kernel=$(realpath "$2")
logs=$(realpath -m "$3")
mkdir -p "$logs"
cd "$repo"
cgroup=$(cut -d: -f3 /proc/self/cgroup)
export WFC_BUNDLED_CGROUP_ROOT="/sys/fs/cgroup$cgroup"
export WFC_BUNDLED_TEST_KERNEL="$kernel"
unset WFC_DISPATCH WFC_OWNER_TOKEN WFC_LAUNCH_CONFIG WFC_STATE
{
    git rev-parse HEAD 'HEAD^{tree}'
    git diff --binary HEAD
    sha256sum "$kernel" bundled_backend.py bundled_runtime.py tests/test_bundled.py
    cat /proc/self/cgroup
    python3 -c 'import os,wfc_platform; print({"pid":os.getpid(),"creation":wfc_platform.process_identity(os.getpid())})'
} > "$logs/identity.txt"
python3 -m unittest discover -s tests -p test_bundled.py -v > "$logs/source.log" 2>&1
python3 build.py --out "$logs/relocated payload with spaces/wfc.pyz" > "$logs/build.log" 2>&1
cp "$kernel" "$logs/relocated payload with spaces/wfc-kernel"
WFC_TEST_PROGRAM="$logs/relocated payload with spaces/wfc.pyz" WFC_BUNDLED_TEST_DISCOVERY=1 \
    python3 -m unittest discover -s tests -p test_bundled.py -v > "$logs/packaged.log" 2>&1
# The guardian may have just received runtime exit; allow its bounded cleanup.
python3 - <<'PY'
import os
from pathlib import Path
import time
root = Path(os.environ['WFC_BUNDLED_CGROUP_ROOT'])
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    remaining = list(root.glob('wfc-*'))
    if not remaining:
        break
    time.sleep(.05)
else:
    raise SystemExit('test-owned cgroup directories remain: ' + str(remaining))
print('No test-owned child cgroups remain')
PY
