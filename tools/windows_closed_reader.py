"""Read one explicitly identified, closed R4 test-worker rollout. Print metadata only."""
import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
from uuid import UUID

DISPATCH = 'd-c929e467'
ATTEMPT = 'a-2a94af6c5892'
EXPECTED = '[router] Answer to question 1: D_GO'
LIMIT = 128 * 1024 * 1024


def read_closed(path, thread, work):
    thread = str(UUID(thread))
    if not path.name.startswith('rollout-') or not path.name.endswith('-' + thread + '.jsonl'):
        raise ValueError('file name does not match the recorded worker session')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('expected a bounded regular test-worker rollout')
    with path.open('rb') as stream:
        first = stream.readline(1024 * 1024)
        row = json.loads(first)
        meta = row['payload']
        if (row['type'] != 'session_meta' or meta['id'] != thread
                or isinstance(meta.get('source'), dict) and 'subagent' in meta['source']
                or not work.is_absolute() or PureWindowsPath(meta['cwd']) != work):
            raise ValueError('worker metadata does not match recorded identity or cwd')
        hasher = hashlib.sha256(first)
        total = len(first)
        brief = []
        canonical = []
        projected = []
        user_rows = []
        event_types = {}
        turns = []
        assistant = []
        index = 1
        while True:
            line = stream.readline(8 * 1024 * 1024)
            if not line:
                break
            total += len(line)
            if total > LIMIT or len(line) == 8 * 1024 * 1024:
                raise ValueError('rollout exceeds byte bound')
            hasher.update(line)
            row = json.loads(line)
            index += 1
            p = row['payload']
            kind = row['type']
            sub = p.get('type', '')
            key = kind + '/' + sub
            event_types[key] = event_types.get(key, 0) + 1
            if kind == 'response_item' and sub == 'message':
                if p.get('role') == 'assistant':
                    assistant.append(index)
                if p.get('role') == 'user':
                    parts = p['content']
                    if not isinstance(parts, list):
                        raise ValueError('unrecognized canonical user content')
                    text = ''.join(v['text'] for v in parts if v.get('type') == 'input_text')
                    types = [v['type'] for v in parts]
                    if (text.startswith(f'You are a worker dispatched by the controller (dispatch {DISPATCH}).')
                            and re.search(r'--attempt\s+' + ATTEMPT + r'\b', text)):
                        brief.append(index)
                    match = text == EXPECTED and all(t == 'input_text' for t in types)
                    if match:
                        canonical.append(index)
                    user_rows.append(dict(line=index, exact=match, bare=text == 'D_GO',
                                          contains_wrapped=EXPECTED in text, contains_marker='D_GO' in text,
                                          text_sha256=hashlib.sha256(text.encode()).hexdigest(), part_types=types))
            if kind == 'event_msg':
                if sub == 'user_message' and p.get('message') == EXPECTED:
                    projected.append(index)
                if sub in ('task_started', 'task_complete', 'turn_aborted', 'error'):
                    turns.append(dict(line=index, type=sub, turn=p.get('turn_id'), at=row.get('timestamp')))
        if not brief:
            raise ValueError('expected dispatch and attempt absent from canonical opening brief')
        if total != path.stat().st_size:
            raise ValueError('rollout size changed during read')
    return dict(file=path.name, session=thread, dispatch=DISPATCH, attempt=ATTEMPT,
                cli_version=meta.get('cli_version'), bytes=total, sha256=hasher.hexdigest(),
                brief_lines=brief, canonical_lines=canonical, projection_lines=projected,
                user_rows=user_rows, turn_events=turns, assistant_lines=assistant, types=event_types)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollout', type=Path, required=True)
    parser.add_argument('--session', required=True, help='worker session ID from the pre-close identity record')
    parser.add_argument('--work', type=PureWindowsPath, required=True)
    args = parser.parse_args()
    print(json.dumps(read_closed(args.rollout, args.session, args.work), ensure_ascii=True, indent=2))
