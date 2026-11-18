"""Compare native console input APIs using one real packaged PTY and Unicode marker."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

MARKER = '复验路径-中文-42'
HERE = Path(__file__).resolve()


def child(mode, destination):
    api = ctypes.windll.kernel32
    before = api.GetConsoleCP()
    if mode == 'fd-utf8':
        assert api.SetConsoleCP(65001)
    destination.with_suffix('.ready').touch()
    if mode == 'wide':
        line = sys.stdin.readline().strip()
        raw = None
    else:
        data = os.read(0, 4096)
        raw = data.hex()
        line = data.decode('utf-8', errors='replace').strip()
    destination.write_text(json.dumps(dict(mode=mode, before=before, after=api.GetConsoleCP(),
                                           chars=line, raw=raw)), encoding='utf-8')


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
            call('input', text=MARKER + '\r')
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
            for mode in ['fd', 'wide', 'fd-utf8']:
                dest = Path(tmp) / (mode + '.json')
                exercise(Path(sys.argv[1]).resolve(), mode, dest)
                row = json.loads(dest.read_text())
                row['exact'] = row['chars'] == MARKER
                rows.append(row)
            Path('native-text-result.json').write_text(json.dumps(rows, indent=2))
            print(json.dumps(rows))
