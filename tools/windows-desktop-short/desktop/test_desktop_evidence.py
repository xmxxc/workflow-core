"""Offline regression tests. Run with --package <verified package directory or wfc.pyz>."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from desktop_evidence import EvidenceError, check_question, check_reply, input_observation, read_rollout, sha, snapshot

SESSION = "01a1fc69-d822-71b3-8755-aae2c1ead4aa"
ASSIGN = dict(dispatch="d-12345678", attempt="a-123456789abc", scope="probe", round=1)
EXPECTED = "[router] Answer to question 1: D_GO"
BRIEF = ("You are a worker dispatched by the controller (dispatch d-12345678). Read and do the task. "
         "Run PowerShell: ... --attempt a-123456789abc")
SECRET = "synthetic-secret-never-export"
CANDIDATE_PYZ = "ba60b239aeb48b6014cf4f7b4925ce2897db4c8d0ad3e131c60603a0ab87d914"


def canonical(text, role="user"):
    return {"type":"response_item","payload":{"type":"message","role":role,
            "content":[{"type":"input_text","text":text}]}}


def projected():
    return {"type":"event_msg","payload":{"type":"user_message","message":EXPECTED}}


class BoundRollout(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd=Path(self.tmp.name).resolve()
        self.root=self.cwd/"sessions"
        self.root.mkdir()
        self.file=self.root/("rollout-2026-11-20T22-00-00-"+SESSION+".jsonl")
        self.meta={"type":"session_meta","payload":{"id":SESSION,"cwd":str(self.cwd),"source":"cli","cli_version":"0.164.0"}}
        self.rows=[self.meta,canonical("<environment_context>"),canonical(BRIEF)]

    def save(self):
        self.file.write_bytes(b"".join(json.dumps(r).encode()+b"\n" for r in self.rows))

    def read(self, **kw):
        args=dict(session=SESSION,cwd=self.cwd,dispatch=ASSIGN["dispatch"],attempt=ASSIGN["attempt"],
                  expected=EXPECTED,closed=True,sessions_root=self.root)
        return read_rollout(self.file, **{**args,**kw})

    def test_exact_projection_and_duplicate_are_independent(self):
        self.rows.extend([projected(),canonical("D_GO"),canonical(EXPECTED+" "),canonical("quote "+EXPECTED),
                          canonical(EXPECTED,"assistant")])
        self.save()
        r=self.read()
        self.assertEqual((r["brief"],r["exact"],r["projected"]),([3],[],[4]))
        self.rows.extend([canonical(EXPECTED),canonical(EXPECTED)])
        self.save()
        self.assertEqual(self.read()["exact"],[9,10])

    def test_bound_turn_context_preserves_only_known_fields(self):
        self.rows.append({"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"high",
            "approval_policy":"never","sandbox_policy":{"type":"workspace-write","writable_roots":[SECRET]},
            "instructions":SECRET}})
        self.rows.append({"type":"turn_context","payload":{"model":SECRET}})
        self.save()
        got=self.read()
        self.assertEqual(got["turn_context"][0],{"line":4,"model":"gpt-6.1-sol","effort":"high",
                         "approval_policy":"never","sandbox_type":"workspace-write"})
        self.assertEqual(got["turn_context"][1]["model"],{"sha256":sha(SECRET)})
        self.assertNotIn(SECRET,json.dumps(got))

    def test_completed_user_item_is_a_projection_only(self):
        for role in ('UserMessage','AgentMessage'):
            self.rows.append({'type':'event_msg','payload':{'type':'item_completed',
                'item':{'type':role,'content':[{'type':'text','text':EXPECTED}]}}})
        self.save()
        result = self.read()
        self.assertEqual(result['projected'], [4])
        self.assertEqual(result['exact'], [])

    def test_multipart_and_nontext_user_content(self):
        self.rows.extend([canonical(EXPECTED),canonical(EXPECTED)])
        self.rows[-2]["payload"]["content"]=[{"type":"input_text","text":s} for s in [EXPECTED[:8],EXPECTED[8:]]]
        self.rows[-1]["payload"]["content"].append({"type":"input_image","image_url":SECRET})
        self.save()
        got=self.read()
        self.assertEqual(got["exact"],[4])
        self.assertNotIn(SECRET,json.dumps(got))
        self.assertNotIn(BRIEF,json.dumps(got))

    def test_refuses_wrong_worker_or_inherited_controller_context(self):
        self.save()
        for delta in (dict(session="11111111-1111-1111-1111-111111111111"),
                      dict(cwd=self.root),dict(dispatch="d-89abcdef"),dict(attempt="a-123456789abd"),
                      dict(sessions_root=self.cwd/"other")):
            with self.subTest(delta=delta), self.assertRaises((OSError,EvidenceError)):
                self.read(**delta)
        for k,bad in [("id","11111111-1111-1111-1111-111111111111"),("source",{"subagent":{}}),
                      ("source","desktop"),("cli_version","0.169.1")]:
            with self.subTest(k=k):
                original=self.meta["payload"][k]
                self.meta["payload"][k]=bad
                self.save()
                with self.assertRaises(EvidenceError):
                    self.read()
                self.meta["payload"][k]=original

    def test_refuses_unterminated_live_last_line_and_corrupt_closed_row(self):
        self.save()
        self.file.write_bytes(self.file.read_bytes().rstrip(b'\n'))
        with self.assertRaisesRegex(EvidenceError,"partial"):
            self.read(closed=False)
        self.assertEqual(self.read()["exact"],[])
        with self.file.open("ab") as f:
            f.write(b'\n{"payload":')
        with self.assertRaises(EvidenceError):
            self.read()

    def test_refuses_wrong_brief_and_second_session_header(self):
        for rows in ([self.meta,canonical("citation "+BRIEF)], [self.meta,canonical(BRIEF),self.meta]):
            self.rows=rows
            self.save()
            with self.assertRaises(EvidenceError):
                self.read()

    def test_ignores_all_other_files(self):
        other=self.root/"rollout-unrelated.jsonl"
        if hasattr(os, "mkfifo"):
            os.mkfifo(other)
        else:
            other.write_bytes(b'not a valid record')
        self.save()
        self.assertEqual(self.read()["brief"],[3])


class Signals(unittest.TestCase):
    def capture(self, screen, **kw):
        import router
        data=dict(text=screen,cols=120,rows=40,eof=False,exit=None,read_error=None,screen="Primary")
        return snapshot({**data,**kw},router,EXPECTED)

    def test_rendered_echo_is_distinct_from_composer(self):
        for body, expected_draft in [
                ("› "+EXPECTED+"\n\n• Done\n\n› Ask Codex to do anything",False),
                ("› "+EXPECTED,True)]:
            screen=SECRET+"\n"+body+"\n\n  controlled · sandbox\n  ? for shortcuts · current model"
            result=self.capture(screen)
            self.assertNotIn(SECRET,json.dumps(result))
            self.assertTrue(result["usable"])
            self.assertEqual(result["composer"]["exact"],expected_draft)
            self.assertEqual(result["ready_to_reply"],not expected_draft)
            self.assertTrue(any("wrapped-reply" in line["labels"] for line in result["bottom"]))

    def test_processing_queue_and_unreadable_stop(self):
        body="› Ask Codex to do anything\n\n  controlled · sandbox"
        for banner in ["• Working (2s • esc to interrupt)","› Queued message "+SECRET]:
            r=self.capture(banner+"\n\n"+body)
            self.assertFalse(r["ready_to_reply"])
            self.assertTrue(r["queue_hints"] or r["activity_hints"])
        self.assertFalse(self.capture(body,eof=True)["usable"])
        self.assertFalse(self.capture(body,read_error=SECRET)["usable"])
        self.assertIsNone(self.capture("unframed "+SECRET)["composer"])

    def test_input_wire_and_payload_hashes(self):
        framed="\x1b[200~"+EXPECTED+"\x1b[201~"
        r=input_observation(framed,EXPECTED)
        self.assertEqual(r,dict(kind="paste",sha256=sha(framed),payload_sha256=sha(EXPECTED),exact=True))
        self.assertFalse(input_observation("\x1b[200~D_GO\x1b[201~",EXPECTED)["exact"])
        self.assertEqual(input_observation("\r",EXPECTED)["kind"],"enter")
        self.assertEqual(input_observation(framed+"\r",EXPECTED)["kind"],"other")

    def test_question_and_reply_receipts(self):
        q=dict(**ASSIGN,question="D_GO?",type="question",id=1,messageId="m-"+"a"*32,
               state="received",statusVersion=2)
        self.assertEqual(check_question(q,ASSIGN)["id"],1)
        for bad in [dict(dispatch='d-00000000'),dict(attempt='a-000000000000'),dict(round=2),
                    dict(scope='elsewhere'),dict(state='unknown'),dict(id=True),
                    dict(messageId=None),dict(statusVersion=None),dict(question='D_GO')]:
            with self.assertRaises(EvidenceError):
                check_question({**q,**bad},ASSIGN)
        ack=dict(replied=1,dispatch=ASSIGN["dispatch"])
        self.assertEqual(check_reply(ack,ASSIGN,1),ack)
        for bad in [dict(replied=True),dict(replied=2),dict(dispatch='wrong')]:
            with self.assertRaises(EvidenceError):
                check_reply({**ack,**bad},ASSIGN,1)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package",type=Path,required=True)
    args,rest=parser.parse_known_args()
    path=args.package/"wfc.pyz" if args.package.is_dir() else args.package
    if hashlib.sha256(path.read_bytes()).hexdigest()!=CANDIDATE_PYZ:
        raise SystemExit("Unexpected candidate pyz hash")
    sys.path.insert(0,str(path.resolve()))
    unittest.main(argv=[sys.argv[0],*rest],verbosity=2)
