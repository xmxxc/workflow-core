"""Backend contract tests using a non-terminal controlled execution backend."""

import argparse
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import router


class FakeBackend(router.ExecutionBackend):
    def __init__(self, *, create_result="accepted", submit_result="accepted", inspect_result="accepted"):
        self.create_result = create_result
        self.submit_result = submit_result
        self.inspect_result = inspect_result
        self.submit_category = None
        self.read_calls = 0
        self.resume_calls = 0

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
        return router.backend_result(self.submit_result, "fake submit diagnostic",
                                    **({"category": self.submit_category} if self.submit_category else {}))

    def interrupt(self, dispatch_id):
        return router.backend_result("accepted")

    def resume(self, dispatch_id, cwd, command, extra_env, label=None, text=None, host=None,
               probe_read_only=False):
        self.resume_calls += 1
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

    def test_refused_fault_category_is_preserved(self):
        row = {"id": "d-fake", "backend": "fake", "host": "structured"}
        db = router.connect()
        for category in ("authentication", "policy_rejected"):
            with self.subTest(category=category):
                self.fake.submit_result = "refused"
                self.fake.submit_category = category
                outcome = router.backend_submit(row, "hello")
                router.persist_input_fault(db, row["id"], "submit", router.backend_fault_state(outcome),
                                           outcome["diagnostic"], outcome.get("category"))
                stored = db.execute("SELECT category FROM fault_event WHERE dispatch = ?", (row["id"],)).fetchone()[0]
                self.assertEqual(stored, category)
        db.close()

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

    def test_uncertain_resume_requires_reconcile_before_retry(self):
        self.fake.inspect_result = "uncertain"
        db = router.connect()
        db.execute("INSERT INTO dispatch (id, host, backend, cwd, task, created, started, report_dir, report, "
                   "scope, role, current_round, current_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("d-resume", "structured", "fake", str(self.cwd), "task", 1, 1, str(self.cwd), "report",
                    "s-fake", "worker", 1, "a-fake"))
        db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, state, created) "
                   "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                   ("d-resume", 1, "a-fake", "task", "report", 1))
        db.commit()
        row = db.execute("SELECT * FROM dispatch WHERE id = 'd-resume'").fetchone()

        with self.assertRaises(SystemExit) as raised:
            router.resume_reconcile_required(db, row, self.fake.inspect("d-resume"), None)
        audit = json.loads(str(raised.exception))["audit"]
        router.cmd_reconcile(argparse.Namespace(dispatch="d-resume", audit=audit, result="accepted",
                                                actor="operator", evidence="confirmed-absent", owner_token=None))

        state = db.execute("SELECT state FROM dispatch_round WHERE dispatch = 'd-resume'").fetchone()[0]
        self.assertEqual(state, "running")
        db.close()

    def test_accepted_reconcile_is_consumed_by_resume_override(self):
        self.fake.inspect_result = "uncertain"
        db = router.connect()
        db.execute("INSERT INTO dispatch (id, host, backend, cwd, task, created, started, report_dir, report, "
                   "model, host_session, effort, network, label, git, scope, role, current_round, current_attempt) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("d-release", "codex", "fake", str(self.cwd), "task", 1, 1, str(self.cwd), "report",
                    None, "session", None, 0, "label", 0, "s-fake", "worker", 1, "a-fake"))
        db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, state, created) "
                   "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                   ("d-release", 1, "a-fake", "task", "report", 1))
        db.commit()
        original_find = router.find_host_session
        router.find_host_session = lambda row: "session"
        args = argparse.Namespace(dispatch="d-release", owner_token=None, task=None, report=None)
        try:
            with self.assertRaises(SystemExit) as raised:
                router.cmd_resume(args)
            audit = json.loads(str(raised.exception))["audit"]
            router.cmd_reconcile(argparse.Namespace(dispatch="d-release", audit=audit, result="accepted",
                                                    actor="operator", evidence="confirmed-absent", owner_token=None))
            router.cmd_resume(args)
        finally:
            router.find_host_session = original_find

        self.assertEqual(self.fake.resume_calls, 1)
        current_attempt = db.execute("SELECT current_attempt FROM dispatch WHERE id = 'd-release'").fetchone()[0]
        consumed = db.execute("SELECT consumed_at FROM lifecycle_event WHERE id = ?", (audit,)).fetchone()[0]
        self.assertNotEqual(current_attempt, "a-fake")
        self.assertIsNotNone(consumed)
        db.close()

    def test_stale_override_is_rejected_after_owner_takeover(self):
        self.fake.inspect_result = "uncertain"
        db = router.connect()
        now = time.time()
        db.execute("INSERT INTO controller_owner (scope, owner, token, generation, lease_until, updated) "
                   "VALUES ('s-owner', 'old', 'old-token', 1, ?, ?)", (now + 60, now))
        db.execute("INSERT INTO dispatch (id, host, backend, cwd, task, created, started, report_dir, report, "
                   "model, host_session, effort, network, label, git, scope, role, current_round, current_attempt) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("d-owner", "codex", "fake", str(self.cwd), "task", 1, 1, str(self.cwd), "report",
                    None, "session", None, 0, "label", 0, "s-owner", "worker", 1, "a-owner"))
        db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, state, created) "
                   "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                   ("d-owner", 1, "a-owner", "task", "report", 1))
        event = router.lifecycle_event(db, "d-owner", "resume", round=1, attempt="a-owner",
                                      from_attempt="a-owner", to_attempt="a-owner", from_host="codex",
                                      to_host="codex", result="pending")
        router.finish_lifecycle_event(db, event, "accepted", "confirmed absent", "old", "old-proof")
        db.execute("UPDATE controller_owner SET owner = 'new', token = 'new-token', generation = 2, updated = ?",
                   (now,))
        db.commit()
        args = argparse.Namespace(dispatch="d-owner", owner_token="new-token", task=None, report=None)

        with self.assertRaises(SystemExit) as raised:
            router.cmd_resume(args)

        self.assertIn("lifecycle_reconcile_required", str(raised.exception))
        self.assertEqual(self.fake.resume_calls, 0)
        self.assertEqual(db.execute("SELECT current_attempt FROM dispatch WHERE id = 'd-owner'").fetchone()[0],
                         "a-owner")
        db.close()


if __name__ == "__main__":
    unittest.main()
