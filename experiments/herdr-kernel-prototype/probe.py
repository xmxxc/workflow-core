#!/usr/bin/env python3
"""Controlled Linux probes, test-only WFC backend seam, optional actual host probe."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
SCRATCH = Path("/tmp/wfc-herdr-kernel-prototype-r1")
RECORDS = []


def record(name, **fields):
    item = {"probe": name, **fields}
    RECORDS.append(item)
    print(json.dumps(item, ensure_ascii=False), flush=True)


class Client:
    def __init__(self, binary):
        self.stderr = open(SCRATCH / "service-stderr.log", "a")
        self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, text=True, bufsize=1)
        self.generation = self.receive()["generation"]
        self.identities = {}
        self.sequence = 0

    def receive(self):
        if not select.select([self.process.stdout], [], [], 10)[0]:
            raise RuntimeError("service response timeout")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("service exited: " + str(self.process.poll()))
        return json.loads(line)

    def call(self, op, unit="probe", **fields):
        self.sequence += 1
        request = {"id": self.sequence, "op": op, "generation": self.generation, "unit": unit,
                   **self.identities.get(unit, {}), **fields}
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        response = self.receive()
        assert response["id"] == self.sequence, response
        return response

    def ok(self, op, unit="probe", **fields):
        result = self.call(op, unit, **fields)
        assert result["ok"], result
        return result["result"]

    def spawn(self, argv, unit="probe", **fields):
        result = self.ok("spawn", unit, argv=argv, cols=100, rows=30, **fields)
        self.identities[unit] = {"nonce": result["nonce"]}
        return result

    def wait(self, predicate, unit="probe", timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.ok("read", unit)
            if predicate(view):
                return view
            time.sleep(0.05)
        raise AssertionError("screen condition timed out: " + json.dumps(view))

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=10)
        self.stderr.close()


def dead(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def controlled(binary, wfc):
    client = Client(binary)
    owned = []
    try:
        argv = [sys.executable, str(HERE / "controlled_host.py"), "space argument 中文", "emoji 🐈"]
        created = client.spawn(argv)
        owned.append(created["pid"])
        view = client.wait(lambda v: "RIGHT" in v["text"])
        assert '"isatty": [true, true, true]' in view["text"], view
        assert "space argument 中文" in view["text"] and "emoji 🐈" in view["text"]
        assert "wrong" not in view["text"]
        record("startup_unicode_spaces_isatty_cursor", identity=created, view=view)
        literal = 'literal ; $(touch /tmp/SHOULD_NOT_EXIST) " 中文 🐈'
        client.ok("input", text=literal + "\r")
        view = client.wait(lambda v: "ECHO " in v["text"])
        assert json.dumps(literal, ensure_ascii=False) in view["text"], view
        record("literal_input", view=view)
        for operation, extra in [("input", {"text": "BAD_REJECTED\r"}), ("resize", {"cols": 70, "rows": 20}), ("close", {})]:
            rejected = client.call(operation, nonce="wrong-nonce", **extra)
            assert not rejected["ok"] and rejected["error"] == "wrong_nonce", rejected
            record("wrong_identity_" + operation, response=rejected)
        view = client.ok("read")
        assert "BAD_REJECTED" not in view["text"] and view["cols"] == 100 and view["exit"] is None
        record("rejected_actions_preserved_live_control", view=view)
        client.ok("input", text="alt\r")
        view = client.wait(lambda v: "ALT_SCREEN" in v["text"] and v["screen"] == "Alternate")
        assert "START " not in view["text"], view
        record("alternate_screen", view=view)
        client.ok("input", text="main\r")
        view = client.wait(lambda v: "MAIN_RESTORED" in v["text"] and v["screen"] == "Primary")
        assert "START " in view["text"], view
        record("primary_screen_restore", view=view)
        client.ok("resize", cols=120, rows=35)
        client.ok("input", text="size\r")
        view = client.wait(lambda v: "columns=120, lines=35" in v["text"])
        record("resize_kernel_and_parser", view=view)
        client.ok("input", text="\x03")
        view = client.wait(lambda v: "CTRL_C_HANDLED" in v["text"])
        record("ctrl_c", view=view)
        client.ok("input", text="exit:7\r")
        view = client.wait(lambda v: v["exit"] is not None and v["eof"])
        assert view["exit"]["code"] == 7, view
        record("natural_exit", view=view)
        old_nonce = client.identities["probe"]["nonce"]
        client.ok("close")
        created = client.spawn(argv)
        owned.append(created["pid"])
        rejected = client.call("close", nonce=old_nonce)
        assert not rejected["ok"] and rejected["error"] == "wrong_nonce"
        record("reused_unit_name_nonce_fence", response=rejected, current=created)
        client.wait(lambda v: "RIGHT" in v["text"])
        client.ok("input", text="tree\r")
        view = client.wait(lambda v: re.search(r"TREE (\d+) (\d+)", v["text"]) is not None)
        children = list(map(int, re.search(r"TREE (\d+) (\d+)", view["text"]).groups()))
        owned.extend(children)
        client.ok("close")
        time.sleep(0.15)
        assert all(dead(p) for p in [created["pid"], *children]), owned
        record("close_child_grandchild_cleanup", pids=[created["pid"], *children], all_terminated=True)
        escape_code = "import os,signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); print('ESCAPED_READY '+str(os.getpid()),flush=True); time.sleep(300)"
        parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]],start_new_session=True); time.sleep(300)"
        escaped_parent = client.spawn([sys.executable, "-c", parent_code, escape_code], "escaped")
        owned.append(escaped_parent["pid"])
        escaped_view = client.wait(lambda v: "ESCAPED_READY " in v["text"], "escaped")
        escaped_pid = int(re.search(r"ESCAPED_READY (\d+)", escaped_view["text"])[1])
        owned.append(escaped_pid)
        client.ok("close", "escaped")
        time.sleep(0.1)
        survived = not dead(escaped_pid)
        record("escaped_process_group_cleanup_gap", parent=escaped_parent["pid"], escaped_pid=escaped_pid,
               escaped_descendant_survived_close=survived, result="unsupported containment; explicit test cleanup follows")
        if survived:
            os.kill(escaped_pid, signal.SIGKILL)
        exited_parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(0.2)"
        exited_parent = client.spawn([sys.executable, "-c", exited_parent_code, escape_code], "exited-parent")
        owned.append(exited_parent["pid"])
        exited_view = client.wait(lambda v: "ESCAPED_READY " in v["text"] and v["exit"] is not None, "exited-parent")
        orphan_pid = int(re.search(r"ESCAPED_READY (\d+)", exited_view["text"])[1])
        owned.append(orphan_pid)
        client.ok("close", "exited-parent")
        survived = not dead(orphan_pid)
        record("natural_parent_exit_descendant_cleanup_gap", parent=exited_parent["pid"], descendant=orphan_pid,
               descendant_survived_close=survived, result="poll reaped leader; close avoids signaling reusable group identity")
        if survived:
            os.kill(orphan_pid, signal.SIGKILL)
        created = client.spawn(argv)
        owned.append(created["pid"])
        client.wait(lambda v: "RIGHT" in v["text"])
        old_generation = client.generation
        marker = SCRATCH / ("unknown-input-" + str(time.time_ns()))
        # Deliver a mutation but deliberately lose its acknowledgement. The marker
        # supplies out-of-band test evidence that the old host applied it once.
        request = {"id": "lost-ack", "op": "input", "generation": old_generation, "unit": "probe",
                   "nonce": created["nonce"], "text": "mark:" + str(marker) + "\r"}
        client.process.stdin.write(json.dumps(request) + "\n")
        client.process.stdin.flush()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.read_text() == "APPLIED_ONCE\n"
        client.process.kill()
        client.process.wait(timeout=5)
        client.stop()
        client = Client(binary)
        fresh = client.spawn(argv)
        owned.append(fresh["pid"])
        rejected = client.call("input", generation=old_generation, nonce=created["nonce"], text="CRASH_REPLAY\r")
        assert not rejected["ok"] and rejected["error"] == "stale_generation"
        for op, extra in [("close", {}), ("resize", {"cols": 70, "rows": 20})]:
            refusal = client.call(op, generation=old_generation, nonce=created["nonce"], **extra)
            assert not refusal["ok"] and refusal["error"] == "stale_generation"
            record("stale_generation_" + op, response=refusal)
        view = client.wait(lambda v: "RIGHT" in v["text"])
        assert "CRASH_REPLAY" not in view["text"]
        assert marker.read_text() == "APPLIED_ONCE\n"
        client.ok("input", text="post_restart_control\r")
        view = client.wait(lambda v: 'ECHO "post_restart_control"' in v["text"])
        record("crash_restart_no_replay", old_generation=old_generation, new_generation=client.generation,
               rejected=rejected, old_pid_terminated=dead(created["pid"]), view=view,
               lost_ack_marker=str(marker), marker_content=marker.read_text(),
               recovery="lost; cannot restore prior PTY")
        client.ok("close")
        crash_code = "import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); print('CRASH_SURVIVOR_READY',flush=True); time.sleep(300)"
        survivor = client.spawn([sys.executable, "-c", crash_code], "crash-survivor")
        owned.append(survivor["pid"])
        client.wait(lambda v: "CRASH_SURVIVOR_READY" in v["text"], "crash-survivor")
        client.process.kill()
        client.process.wait(timeout=5)
        time.sleep(0.1)
        survived = not dead(survivor["pid"])
        record("crash_ignoring_hangup_containment_gap", pid=survivor["pid"], survived_server_sigkill=survived,
               result="no crash-time process ownership guard; explicit test cleanup follows")
        if survived:
            os.kill(survivor["pid"], signal.SIGKILL)
    finally:
        client.stop()
        for pid in owned:
            if not dead(pid):
                os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not all(dead(p) for p in owned) and time.monotonic() < deadline:
            time.sleep(0.02)
        record("controlled_cleanup", owned_pids=owned, all_terminated=all(dead(p) for p in owned))
        assert all(dead(p) for p in owned), "test-owned process remained live after cleanup"

    wfc_contract(binary, wfc)


def wfc_contract(binary, wfc):
    # Import the candidate zipapp's real router, without editing its registry on disk.
    sys.path.insert(0, str(wfc))
    import router
    state = SCRATCH / ("controlled-wfc-" + str(time.time_ns()))
    state.mkdir()
    os.environ["WFC_STATE"] = str(state)
    for key in ("WFC_DISPATCH", "WFC_OWNER_TOKEN", "CODEX_THREAD_ID"):
        os.environ.pop(key, None)
    report = state / "report.md"
    task = state / "task.md"
    task.write_text("Controlled prototype callback probe\n")
    client = Client(binary)

    class KernelBackend(router.ExecutionBackend):
        name = "kernel-prototype"

        def create(self, dispatch_id, cwd, command, extra_env, label=None):
            argv = ["/usr/bin/env", *[f"{k}={v}" for k, v in extra_env.items()], *command]
            client.spawn(argv, dispatch_id, cwd=str(cwd))
            return router.backend_result("accepted")

        def inspect(self, dispatch_id):
            result = client.ok("read", dispatch_id)
            return router.backend_result("accepted", alive=result["exit"] is None, owned=True)

        def read(self, dispatch_id):
            return router.backend_result("accepted", screen=client.ok("read", dispatch_id)["text"])

        def submit(self, dispatch_id, text, host=None):
            client.ok("input", dispatch_id, text=text + "\r")
            return router.backend_result("accepted")

        def interrupt(self, dispatch_id):
            client.ok("input", dispatch_id, text="\x03")
            return router.backend_result("accepted")

        def close(self, dispatch_id):
            client.ok("close", dispatch_id)
            return router.backend_result("completed")

        def resume(self, *args, **kwargs):
            return router.backend_result("uncertain", "PTY lost after restart; explicit new attempt required")

    backend = KernelBackend()
    router.BACKENDS[backend.name] = lambda: backend
    router.LAUNCHERS["codex"] = lambda *args, **kwargs: (
        [sys.executable, str(HERE / "controlled_host.py"), str(report), str(wfc)], {})
    try:
        args = argparse.Namespace(host="codex", backend=backend.name, task=str(task), cwd=str(state),
                                  report=str(report), scope=None, owner_token=None, role="controlled", model=None,
                                  effort=None, label="kernel controlled", git=False, network=False, launch_config=None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            router.cmd_dispatch(args)
        dispatched = json.loads(output.getvalue())
        dispatch_id = dispatched["dispatch"]
        client.wait(lambda v: "RIGHT" in v["text"], dispatch_id)
        assert backend.inspect(dispatch_id)["alive"]
        assert backend.resume()["result"] == "uncertain"
        assert backend.submit(dispatch_id, "WFC_LITERAL 中文")["result"] == "accepted"
        view = client.wait(lambda v: 'ECHO "WFC_LITERAL 中文"' in v["text"], dispatch_id)
        assert backend.interrupt(dispatch_id)["result"] == "accepted"
        client.wait(lambda v: "CTRL_C_HANDLED" in v["text"], dispatch_id)
        backend.submit(dispatch_id, "done:" + dispatched["attempt"])
        view = client.wait(lambda v: "CALLBACK " in v["text"], dispatch_id)
        db = router.connect()
        message = db.execute("SELECT id,type,body,request_key FROM message WHERE dispatch=? AND type='done'", (dispatch_id,)).fetchone()
        assert message is not None, view
        round_state = db.execute("SELECT state FROM dispatch_round WHERE dispatch=?", (dispatch_id,)).fetchone()[0]
        assert round_state == "completed", round_state
        receipt = dict(message)
        backend.close(dispatch_id)
        record("wfc_execution_contract_and_real_done_callback", dispatch=dispatched, state=str(state),
               receipt=receipt, round_state=round_state, backend_close="completed", view=view,
               seam="candidate real router.cmd_dispatch + test-only ExecutionBackend; real child wfc.pyz done")
        db.close()
    finally:
        client.stop()


def actual_host(binary):
    client = Client(binary)
    state = SCRATCH / ("actual-host-" + str(time.time_ns()))
    state.mkdir()
    try:
        # A real TUI with isolated config and data. No agent task/network call is required
        # to measure whether this extracted terminal can start and interpret the host.
        argv = ["/usr/bin/env", "-u", "WFC_DISPATCH", "-u", "WFC_OWNER_TOKEN", "-u", "CODEX_THREAD_ID",
                "XDG_CONFIG_HOME=" + str(state / "config"), "XDG_DATA_HOME=" + str(state / "data"),
                "XDG_STATE_HOME=" + str(state / "state"), "XDG_CACHE_HOME=" + str(state / "cache"),
                "OPENCODE_CONFIG_CONTENT={\"autoupdate\":false}", "opencode", str(state)]
        created = client.spawn(argv, "actual-host", cwd=str(state))
        started = time.monotonic()
        view = client.wait(lambda v: "OpenCode" in v["text"] or "opencode" in v["text"] or v["exit"] is not None,
                           "actual-host", timeout=35)
        startup_seconds = time.monotonic() - started
        startup_view = view
        client.ok("input", "actual-host", text="kernel isolated draft 中文")
        try:
            input_view = client.wait(lambda v: "kernel isolated draft" in v["text"], "actual-host", timeout=10)
            input_observed = True
        except AssertionError:
            input_view = client.ok("read", "actual-host")
            input_observed = False
        client.ok("input", "actual-host", text="\x03")
        time.sleep(0.3)
        interrupted = client.ok("read", "actual-host")
        client.ok("close", "actual-host")
        record("actual_opencode_tui", identity=created, state=str(state), startup_seconds=startup_seconds,
               exercise_seconds=time.monotonic() - started,
               startup=startup_view, input_observed=input_observed, input_view=input_view, interrupted=interrupted,
               closed=True, host_pid_terminated=dead(created["pid"]),
               limitation="isolated unsubmitted draft; no authenticated model turn or WFC callback from actual host")
    finally:
        client.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=SCRATCH / "target/release/herdr-kernel-prototype")
    parser.add_argument("--wfc", type=Path, default=Path("/tmp/wfc-opencode2-candidate-local/wfc.pyz"))
    parser.add_argument("--actual-host", action="store_true")
    args = parser.parse_args()
    try:
        controlled(args.binary, args.wfc)
        if args.actual_host:
            actual_host(args.binary)
    finally:
        (SCRATCH / "probe-results.json").write_text(json.dumps(RECORDS, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
