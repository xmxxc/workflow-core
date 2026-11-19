"""Bounded, account-free test of the released bundled worker and detached relay."""
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_bundled import BundledNative
import bundled_runtime as runtime
import wfc_platform


class Relay(BundledNative):
    def test_relay(self):
        thread = str(uuid.uuid4())
        sessions = Path(self.env['CODEX_HOME']) / 'sessions'
        sessions.mkdir(parents=True)
        (sessions / ('rollout-probe-' + thread + '.jsonl')).write_text(json.dumps(
            {'type':'session_meta','payload':{'id':thread,'source':'cli'}}) + '\n')
        capture = self.root / 'queue'
        capture.mkdir()
        self.env.update(CODEX_THREAD_ID=thread, QUEUE_CAPTURE=str(capture))
        value = self.dispatch()
        d, attempt = value['dispatch'], value['attempt']
        self.handle(d)
        relay = self.cli('relay','--codex','--scope',value['scope'],'--for','16','--stall-after','60','--detach')
        creation = wfc_platform.process_identity(relay['pid'])
        self.assertTrue(creation)
        def delivered(kind):
            for path in capture.glob('*.received'):
                window, line = path.read_text(encoding='utf-8-sig').splitlines()
                messages = json.loads(line.split(': ',1)[1])['messages']
                for msg in messages:
                    if msg['type'] == kind:
                        self.assertEqual((msg['dispatch'],msg['attempt']), (d,attempt))
                        row = next(m for m in self.status(d)['messages'] if m['messageId']==msg['messageId'])
                        if row['state']=='received':
                            return msg, int(window)
        question, w1 = self.until(lambda: delivered('question'))
        self.cli('reply',str(question['id']),'finish now')
        self.assertEqual(self.cli('ack',question['messageId'])['state'],'processed')
        done, w2 = self.until(lambda: delivered('done'))
        self.assertEqual(self.report.read_text().strip(),'controlled bundled worker completed')
        self.assertEqual(self.cli('ack',done['messageId'])['state'],'processed')
        self.assertEqual(self.cli('close',d)['result'],'completed')
        self.until(lambda: wfc_platform.process_identity(relay['pid']) != creation, timeout=25)
        self.assertEqual(len(list(capture.glob('*.received'))),2)
        print(json.dumps(dict(scope=value['scope'],attempt=attempt,queueConsoleHWND=[w1,w2],
                              ack='processed',queueCalls=2,relayExited=True)), flush=True)
        self.assertEqual([w1,w2],[0,0], 'queue helper acquired a console')


if __name__ == '__main__':
    if '--child' not in sys.argv:
        job = runtime.WindowsContainment()
        try:
            with subprocess.Popen([sys.executable,__file__,'--child'],stdin=subprocess.PIPE) as proc:
                try:
                    job.attach(proc.pid)
                    proc.communicate(b'1', timeout=90)
                finally:
                    job.stop(lambda phase, **fields: print(phase,fields,flush=True))
        finally:
            job.dispose()
        sys.exit(proc.returncode)
    if sys.stdin.buffer.read(1) != b'1':
        sys.exit('outer Job required')
    unittest.main(argv=[sys.argv[0],'Relay.test_relay'],verbosity=2)
