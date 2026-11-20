"""Account-free PTY worker for standalone WFC lifecycle acceptance."""
import json
from contextlib import nullcontext
from bundled_input import read_utf8_lines, windows_vt_input
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

PROGRAM = os.environ['WFC_TEST_PROGRAM']
REPORT = Path(os.environ['WFC_TEST_REPORT'])
ATTEMPT = re.search(r'a-[0-9a-f]{12}', ' '.join(sys.argv)).group()
MODE = os.environ.get('WFC_TEST_MODE', '')
EVIDENCE = Path(os.environ['WFC_TEST_EVIDENCE'])

# Emit a controlled host transcript in the fixture's isolated CODEX_HOME. This
# lets the real router discover a conversation and enforce its normal resume path.
sessions = Path(os.environ['CODEX_HOME']) / 'sessions'
sessions.mkdir(parents=True, exist_ok=True)
record = sessions / ('rollout-controlled-' + os.environ['WFC_DISPATCH'] + '.jsonl')
if not record.exists():
    import uuid
    record.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(uuid.uuid4()), 'source': 'cli'}}) + '\n'
                      + json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                          'content': [{'type': 'input_text', 'text': sys.argv[-1]}]}}) + '\n', encoding='utf-8')

def callback(*args, attempt=ATTEMPT):
    command = [sys.executable, PROGRAM, *args, '--attempt', attempt]
    result = subprocess.run(command,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    if MODE == 'takeover':
        error = json.loads(result.stderr)['error'] if result.returncode else None
        record = {'pid': os.getpid(), 'exit': result.returncode, 'error': error}
        if not error:
            record['messageId'] = json.loads(result.stdout)['messageId']
            again = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=15)
            duplicate = json.loads(again.stdout)
            record['stableRetry'] = (again.returncode == 0 and duplicate['duplicate']
                                     and duplicate['messageId'] == record['messageId'])
        path = Path(str(EVIDENCE) + '.' + args[0])
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(record), encoding='utf-8')
        temp.replace(path)
    if result.returncode:
        print('CALLBACK_ERROR ' + result.stderr, flush=True)
        return None
    return json.loads(result.stdout)


def descendant():
    # The pid file is only test cleanup evidence, never backend ownership authority.
    source = str(Path(__file__).resolve().parents[2])
    code = ('import os,signal,time,json,sys; sys.path.insert(0,' + repr(source) + '); import wfc_platform; '
            'signal.signal(signal.SIGHUP, signal.SIG_IGN) if hasattr(signal,"SIGHUP") else None; '
            'signal.signal(signal.SIGINT, signal.SIG_IGN); '
            'open(os.environ["WFC_TEST_EVIDENCE"],"w").write(json.dumps({"pid":os.getpid(),"creation":wfc_platform.process_identity(os.getpid())})); time.sleep(120)')
    child = subprocess.Popen([sys.executable, '-c', code], start_new_session=(os.name != 'nt'))
    deadline = time.monotonic() + 5
    while not EVIDENCE.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    return child

signal.signal(signal.SIGINT, lambda *_: print('CTRL_C_HANDLED', flush=True))
if os.name != 'nt':
    import tty
    tty.setraw(0)
input_context = (windows_vt_input() if os.name == 'nt' else
                 nullcontext(read_utf8_lines(lambda: os.read(0, 4096))))
with input_context as lines:
    print('READY ' + json.dumps({'pid': os.getpid(), 'tty': [os.isatty(0), os.isatty(1)]}), flush=True)
    print('wrong\rRIGHT\x1b[K', flush=True)
    if MODE == 'read-only':
        print('This conversation is unavailable. Its cached transcript and draft remain here; input is paused.', flush=True)
    if MODE in ('tree', 'parent-exit'):
        descendant()
        if MODE == 'parent-exit':
            raise SystemExit(0)
    if MODE == 'takeover':
        ready = {'pid': os.getpid(), 'attempt': ATTEMPT,
                 'ownerInHost': any(os.environ.get(k) for k in ('WFC_OWNER_TOKEN', 'WFC_CONTROLLER_TOKEN'))}
        Path(str(EVIDENCE) + '.ready').write_text(json.dumps(ready), encoding='utf-8')
        deadline = time.monotonic() + 30
        while not Path(str(EVIDENCE) + '.release').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('controlled takeover gate expired')
            time.sleep(.02)
    question = callback('ask', 'controlled question', '--options', 'yes,no')
    print('QUESTION ' + json.dumps(question), flush=True)
    if MODE == 'takeover' and question is None:
        REPORT.write_text('controlled worker completed', encoding='utf-8')
        callback('done', '--report', str(REPORT), '--message-id', 'controlled:' + ATTEMPT)
    if MODE == 'pressure':
        gate = Path(str(EVIDENCE) + '.pressure-gate')
        deadline = time.monotonic() + 15
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        if not gate.exists():
            raise TimeoutError('pressure gate not opened')
        os.write(1, b'\x1b[6n')
        chunk = b'PRESSURE' * 1024
        for _ in range(2048):
            os.write(1, chunk)
        print('PRESSURE_FINISHED', flush=True)
        Path(str(EVIDENCE) + '.pressure-written').write_text(str(len(chunk) * 2048))
    for text in lines:
        text = text.rstrip('\r\n').replace('\x1b[200~', '').replace('\x1b[201~', '')
        if 'pressure-input:' in text:
            import hashlib
            payload = text.split('pressure-input:', 1)[1]
            Path(str(EVIDENCE) + '.pressure-input').write_text(hashlib.sha256(payload.encode()).hexdigest())
        elif 'Answer to question' in text:
            print('ANSWER_RECEIVED', flush=True)
            if '复验路径-中文-42 Ω😀' in text:
                print('UNICODE_RECEIVED:复验路径-中文-42 Ω😀', flush=True)
            # Marker proves that dropping a response never replays applied input.
            with open(str(EVIDENCE) + '.applied', 'a', encoding='utf-8') as target:
                target.write('once\n')
            with open(str(EVIDENCE) + '.input.jsonl', 'a', encoding='utf-8') as target:
                target.write(json.dumps(text) + '\n')
            if MODE not in ('record-only', 'continue-callback'):
                REPORT.write_text('controlled bundled worker completed\n', encoding='utf-8')
                done = callback('done', '--report', str(REPORT), '--message-id', 'controlled:' + ATTEMPT)
                print('DONE ' + json.dumps(done), flush=True)
        elif 'Message from the controller' in text:
            print('NUDGE_RECEIVED', flush=True)
        elif '[router] New task for you' in text and MODE in ('record-only', 'continue-callback'):
            if MODE == 'continue-callback':
                attempt = re.search(r'--attempt (a-[0-9a-f]{12})', text).group(1)
                ref = re.search(r'--callback-ref ([0-9a-f]{64})', text).group(1)
                # The initial grant cannot authorize the new attempt or return its old stable receipt.
                for target in (ATTEMPT, attempt):
                    rejected = callback('ask', 'controlled question', '--options', 'yes,no', attempt=target)
                    if rejected is not None:
                        raise RuntimeError('superseded callback accepted')
                accepted = callback('ask', 'controlled next-round question', '--callback-ref', ref, attempt=attempt)
                if accepted is None:
                    raise RuntimeError('continued callback refused')

            Path(str(EVIDENCE) + '.continued').write_text(text, encoding='utf-8')
