"""Backend contract tests using a non-terminal controlled execution backend."""

import argparse
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import router


class FakeBackend(router.ExecutionBackend):
    def __init__(self, *, create_result="accepted", submit_result="accepted", inspect_result="accepted"):
        self.create_result = create_result
        self.submit_result = submit_result
        self.inspect_result = inspect_result
        self.read_calls = 0

    def create(self, dispatch_id, cwd, command, extra_env, label=None):
        return router.backend_result(self.create_result, "fake create diagnostic")

    def inspect(self, dispatch_id):
        if self.inspect_result == "accepted":
            return router.backend_result("accepted", alive=True, owned=True)
        return router.backend_result(self.inspect_result, "fake inspect timed out")

    def read(self, dispatch_id):
        self.read_calls += 1
        return router.backend_result("accepted", activity="active")

    def submit(self, dispatch_id, text, host=None):
        return router.backend_result(self.submit_result, "fake submit diagnostic")

    def interrupt(self, dispatch_id):
        return router.backend_result("accepted")

    def resume(self, dispatch_id, cwd, command, extra_env, label=None, text=None, host=None,
               probe_read_only=False):
        return router.backend_result("accepted")

    def close(self, dispatch_id):
        return router.backend_result("completed")


class BackendContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.cwd = Path(self.tmp.name) / "cwd"
        self.cwd.mkdir()
        self.previous_state = os.environ.get("WFC_STATE")
        os.environ["WFC_STATE"] = str(self.state)
        self.previous_backend = router.BACKENDS.get("fake")
        self.fake = FakeBackend()
        router.BACKENDS["fake"] = lambda: self.fake

    def tearDown(self):
        if self.previous_backend is None:
            router.BACKENDS.pop("fake", None)
        else:
            router.BACKENDS["fake"] = self.previous_backend
        if self.previous_state is None:
            os.environ.pop("WFC_STATE", None)
        else:
            os.environ["WFC_STATE"] = self.previous_state
        self.tmp.cleanup()

    def test_submit_refused_and_uncertain_stay_generic(self):
        row = {"id": "d-fake", "backend": "fake", "host": "structured"}
        for result in ("refused", "uncertain"):
            with self.subTest(result=result):
                self.fake.submit_result = result
                outcome = router.backend_submit(row, "hello")
                self.assertEqual(outcome["result"], result)
                self.assertEqual(outcome["diagnostic"], "fake submit diagnostic")

    def test_create_refused_is_recorded_without_started_dispatch(self):
        self.fake.create_result = "refused"
        task = Path(self.tmp.name) / "task.md"
        report = Path(self.tmp.name) / "reports" / "report.md"
        task.write_text("task\n")
        args = argparse.Namespace(
            host="codex", backend="fake", task=str(task), cwd=str(self.cwd), report=str(report),
            scope=None, owner_token=None, role=None, model=None, effort=None, label=None,
            git=False, network=False,
        )

        with self.assertRaises(SystemExit):
            router.cmd_dispatch(args)

        db = sqlite3.connect(self.state / "router.db")
        try:
            dispatch = db.execute("SELECT started FROM dispatch").fetchone()
            fault = db.execute("SELECT outcome, diagnostic FROM fault_event WHERE operation = 'create'").fetchone()
        finally:
            db.close()
        self.assertEqual(dispatch[0], None)
        self.assertEqual(tuple(fault), ("refused", "fake create diagnostic"))

    def test_watchdog_inspect_uncertain_posts_unknown_without_reading(self):
        self.fake.inspect_result = "uncertain"
        db = router.connect()
        db.execute("INSERT INTO dispatch (id, host, backend, cwd, task, created, started, report_dir, report, "
                   "scope, role, current_round, current_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("d-fake", "structured", "fake", str(self.cwd), "task", 1, 1, str(self.cwd), "report",
                    "s-fake", "worker", 1, "a-fake"))
        db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, state, created) "
                   "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                   ("d-fake", 1, "a-fake", "task", "report", 1))
        db.commit()

        router.check_workers(db, 60)

        message = db.execute("SELECT type, body FROM message WHERE dispatch = ?", ("d-fake",)).fetchone()
        self.assertEqual(message[0], "unknown")
        self.assertIn("fake inspect timed out", message[1])
        self.assertEqual(self.fake.read_calls, 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
