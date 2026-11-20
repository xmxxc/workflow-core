"""Native sidecar round trip with a single controlled terminal process."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def run(binary):
    with subprocess.Popen([str(binary)], cwd=binary.parent, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, encoding="utf-8") as kernel:
        try:
            generation = json.loads(kernel.stdout.readline())["generation"]
            unit = dict(unit="native", generation=generation)
            sequence = 0

            def call(operation, **fields):
                nonlocal sequence
                sequence += 1
                request = dict(unit, op=operation, id=sequence, **fields)
                kernel.stdin.write(json.dumps(request) + "\n")
                kernel.stdin.flush()
                reply = json.loads(kernel.stdout.readline())
                assert reply["id"] == sequence and reply["ok"], reply
                return reply["result"]

            script = "print('READY', flush=True); print(input()[::-1], flush=True); input()"
            child = call("spawn", argv=[sys.executable, "-u", "-c", script], cols=100, rows=24)
            unit["nonce"] = child["nonce"]

            def observe(marker):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    view = call("read")
                    if marker in view["text"]:
                        return
                    time.sleep(0.1)
                raise RuntimeError(view)

            observe("READY")
            marker = uuid.uuid4().hex
            call("input", text=marker + "\r")
            observe(marker[::-1])
            assert call("close")["closed"]
            kernel.stdin.close()
            assert kernel.wait(timeout=10) == 0
            print("Native PTY create, submit, observe child response, close passed")
        finally:
            if kernel.poll() is None:
                kernel.kill()


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
