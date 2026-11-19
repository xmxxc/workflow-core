"""Hosted Windows wrapper for the reviewed local-only foreground probe."""
import json
from pathlib import Path
import subprocess
import sys

from bundled_runtime import WindowsContainment

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence"
DIGEST = "21f1b2358039399397694155af7c995c424a174af4a2c67d4522988d166f186c"


def measure(mode, kernel):
    # The Python child cannot start the probe until assigned to our Job.
    boot = ("import sys,runpy; assert sys.stdin.buffer.read(1)==b'1'; "
            "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')")
    cmd = [sys.executable, "-c", boot, str(ROOT / "tools/probe_windows_foreground.py"),
           "--kernel", kernel, "--kernel-sha256", DIGEST, "--control", mode]
    job = WindowsContainment()
    record = {"mode": mode, "outer_empty": False}
    try:
        with (EVIDENCE / (mode + ".json")).open("wb") as out, (EVIDENCE / (mode + ".stderr")).open("wb") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err)
            try:
                job.attach(proc.pid)
                proc.communicate(b"1", timeout=100)
                record["exit"] = proc.returncode
            finally:
                try:
                    job.stop()
                    record["outer_empty"] = True
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
    finally:
        job.dispose()
        (EVIDENCE / (mode + ".outer.json")).write_text(json.dumps(record))
    return record["exit"], json.loads((EVIDENCE / (mode + ".json")).read_text())


def cleared(trial):
    return (trial.get("root_reaped") and any(e["phase"] == "job_empty" for e in trial["cleanup"])
            and not any(e["phase"] in {"job_stop","job_close","unhook","positive_window_cleanup"} for e in trial["errors"])
            and all(c["destroyed"] for c in trial["window_observation"].get("positive_controls", [])))


if __name__ == "__main__":
    EVIDENCE.mkdir(exist_ok=True)
    code, normal = measure("normal", sys.argv[1])
    if code or len(normal["trials"]) != 2 or not all(cleared(t) for t in normal["trials"]):
        raise SystemExit("Normal comparison incomplete: see evidence; negative control deferred")
    code, negative = measure("hold-guardian-gate", sys.argv[1])
    trial, = negative["trials"]
    if code != 1 or not cleared(trial) or not any("timeout" in e["error_type"].lower() for e in trial["errors"]):
        raise SystemExit("Negative control outcome or cleanup unexpected: see evidence")
