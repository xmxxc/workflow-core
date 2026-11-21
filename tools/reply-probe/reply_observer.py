"""Imported by sitecustomize only for the single public reply subprocess."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

EXPECTED = '[router] Answer to question 1: D_GO'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def bottom(data, router):
    # Synthetic provider, synthetic cwd. Preserve the final lines and positions
    # without exporting arbitrary path/credential-like values or the initial brief.
    lines = data.get('text', '').splitlines()
    start = max((i + 1 for i, line in enumerate(lines) if 'interactive question tool' in line), default=0)
    lines = lines[max(start, len(lines)-16):]
    lines = [re.sub(r'(?i)\b[0-9a-f]{48,}\b', '<redacted>', line)[:200] for line in lines]
    box = router.codex_input_box(data.get('text', ''), require_complete=True)
    return dict(bottom=lines, composer=None if box is None else {
        'empty': not bool(box.text.strip()), 'exact': box.text == EXPECTED, 'scrolled': box.scrolled},
        queue_hints=[line for line in lines if re.search(r'(?i)queued|queue|steer|interrupt|working', line)],
        terminal={k: data.get(k) for k in ('cols','rows','screen','eof','exit','read_error')})


def install():
    sys.path.insert(0, os.environ['PROBE_PYZ'])
    import bundled_runtime
    import router
    call = bundled_runtime.call
    def observed(handle, op, **data):
        event = dict(op=op, start=time.monotonic())
        if op == 'input':
            raw = data['text']
            paste = raw.startswith('\x1b[200~') and raw.endswith('\x1b[201~')
            event.update(kind='paste' if paste else 'enter' if raw == '\r' else 'other', sha256=sha(raw))
            if paste:
                event.update(payload_sha256=sha(raw[6:-6]), exact=raw[6:-6] == EXPECTED)
        result = call(handle, op, **data)
        event.update(result=result['result'], end=time.monotonic())
        if op == 'read' and 'data' in result:
            event.update(bottom(result['data'], router))
        with Path(os.environ['PROBE_TRACE']).open('a', encoding='utf-8') as sink:
            sink.write(json.dumps(event) + '\n')
        return result
    bundled_runtime.call = observed
