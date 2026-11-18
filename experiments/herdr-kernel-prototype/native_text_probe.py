"""Compare native console input APIs using one real packaged PTY and Unicode marker."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

MARKER = '复验路径-中文-42' + (' xyz-123456789' * 130) + ' ATTEMPT-TAIL'
HERE = Path(__file__).resolve()


def child(mode, destination):
    api = ctypes.windll.kernel32
    import ctypes.wintypes as wt
    api.GetStdHandle.restype = wt.HANDLE
    handle = wt.HANDLE(api.GetStdHandle(-10))
    flags = wt.DWORD()
    assert api.GetConsoleMode(handle, ctypes.byref(flags))
    if mode == 'raw-wide':
        assert api.SetConsoleMode(handle, (flags.value & ~7) | 0x200)
        sys.stdout.write('\x1b[?2004h')
        sys.stdout.flush()
    destination.with_suffix('.ready').touch()
    if mode == 'raw-wide':
        chunks = []
        while True:
            chars = ctypes.create_unicode_buffer(1024)
            count = wt.DWORD()
            assert api.ReadConsoleW(handle, chars, len(chars), ctypes.byref(count), None)
            text = chars[:count.value]
            chunks.append(text)
            if '\r' in text or '\n' in text:
                break
        line = ''.join(chunks)
    else:
        line = sys.stdin.readline()
    normalized = line.rstrip('\r\n').removeprefix('\x1b[200~').removesuffix('\x1b[201~')
    row = dict(mode=mode, expected=len(MARKER), received=len(normalized),
               exact=normalized == MARKER, raw_length=len(line),
               head=repr(line[:24]), tail=repr(line[-55:]))
    destination.write_text(json.dumps(row), encoding='utf-8')


def wait(path):
    for _ in range(200):
        if path.is_file():
            return
        time.sleep(.05)
    raise TimeoutError(path)


def exercise(binary, mode, destination):
    with subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          encoding='utf-8') as svc:
        try:
            scope = {'unit': 'test', 'generation': json.loads(svc.stdout.readline())['generation']}
            def call(op, **fields):
                request = {**scope, 'op': op, 'id': 1, **fields}
                svc.stdin.write(json.dumps(request) + '\n')
                svc.stdin.flush()
                reply = json.loads(svc.stdout.readline())
                assert reply['ok'], reply
                return reply['result']
            launch = call('spawn', cols=100, rows=30,
                          argv=[sys.executable, '-u', str(HERE), '--child', mode, str(destination)])
            scope['nonce'] = launch['nonce']
            wait(destination.with_suffix('.ready'))
            call('input', text='\x1b[200~' + MARKER + '\x1b[201~' if mode != 'plain' else MARKER)
            call('input', text='\r')
            wait(destination)
            assert call('close')['closed']
            svc.stdin.close()
            assert svc.wait(timeout=8) == 0
        finally:
            if svc.poll() is None:
                svc.kill()


if __name__ == '__main__':
    if sys.argv[1] == '--child':
        child(sys.argv[2], Path(sys.argv[3]))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for mode in ['plain', 'cooked-bracket', 'raw-wide']:
                dest = Path(tmp) / (mode + '.json')
                exercise(Path(sys.argv[1]).resolve(), mode, dest)
                row = json.loads(dest.read_text())
                rows.append(row)
            Path('native-text-result.json').write_text(json.dumps(rows, indent=2))
            print(json.dumps(rows))
