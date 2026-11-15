#!/usr/bin/env python3
"""Deterministic interactive PTY workload; invokes real WFC done on request."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

signal.signal(signal.SIGINT, lambda *_: print("CTRL_C_HANDLED", flush=True))
print("START " + json.dumps({"argv": sys.argv[1:], "isatty": [os.isatty(n) for n in (0, 1, 2)],
                             "pid": os.getpid()}, ensure_ascii=False), flush=True)
print("wrong\rRIGHT\x1b[K", flush=True)
for raw in sys.stdin:
    text = raw.rstrip("\r\n")
    if text == "alt":
        print("\x1b[?1049h\x1b[2J\x1b[HALT_SCREEN", flush=True)
    elif text == "main":
        print("\x1b[?1049lMAIN_RESTORED", flush=True)
    elif text == "size":
        print("SIZE " + str(os.get_terminal_size(0)), flush=True)
    elif text == "tree":
        code = "import subprocess,sys,time,os; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); print('TREE '+str(os.getpid())+' '+str(p.pid),flush=True); time.sleep(300)"
        subprocess.Popen([sys.executable, "-c", code])
    elif text.startswith("done:"):
        report = Path(sys.argv[1])
        report.write_text("# Controlled kernel host completed\n", encoding="utf-8")
        command = [sys.executable, sys.argv[2], "done", "--report", str(report),
                   "--attempt", text[5:], "--message-id", "controlled:" + text[5:]]
        result = subprocess.run(command, capture_output=True, text=True)
        print("CALLBACK " + json.dumps({"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}), flush=True)
    elif text.startswith("exit:"):
        raise SystemExit(int(text[5:]))
    elif text.startswith("mark:"):
        with open(text[5:], "a", encoding="utf-8") as marker:
            marker.write("APPLIED_ONCE\n")
        print("MARK_APPLIED", flush=True)
    else:
        print("ECHO " + json.dumps(text, ensure_ascii=False), flush=True)
