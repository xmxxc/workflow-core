"""Observe only this dispatch's prepared host spec, Popen call and durable receipts.

The temporary Python observer surrounds the WFC supervisor. The host receives
the unmodified argv, environment, cwd, streams and child-creation options.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time

ROLES = ('guardian', 'service', 'kernel', 'worker')
FIELDS = ('TERM', 'CODEX_HOME', 'CODEX_SQLITE_HOME', 'HOME', 'USERPROFILE', 'APPDATA',
          'LOCALAPPDATA', 'TEMP', 'WFC_STATE')
TERMS = {'xterm-256color', 'xterm', 'dumb', 'vt100', 'ansi'}
OPTIONS = {'--no-daemon', '--no-alt-screen', '-s', '-a', '-m', '-c', '--add-dir',
           '--model', 'workspace-write', 'never'}
MESSAGES = {
    'extra-roots-denied': 'effective permissions do not allow additional writable roots',
    'add-dir-warning': 'ignoring --add-dir',
    'add-dir-error': 'error adding directories',
    'permission-denied': 'permission denied',
    'access-denied': 'access is denied',
    'config-error': 'error loading config',
    'not-trusted': 'not trusted',
    'model-unavailable': 'model is not available',
    'model-not-found': 'model_not_found',
    'unsupported-model': 'model is not supported',
    'not-configured': 'notconfigured',
    'unrecognized-argument': 'unexpected argument',
    'terminal-query-timeout': 'could not be read within a normal duration',
    'config-parse-error': 'failed to parse',
    'not-logged-in': 'not logged in',
    'terminal-required': 'not a terminal',
    'fixture-startup-sentinel': 'wfc controlled startup sentinel',
}
CATEGORIES = {'started', 'process_exited', 'unknown', 'permission_denied', 'executable_missing',
              'runtime_missing', 'script_missing'}


def sha(value):
    return hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()


def put(path, row):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(row, ensure_ascii=True), encoding='utf-8')
    tmp.replace(path)


def read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def words(text):
    # Never include a free-form fragment, even for unclassified errors.
    if not isinstance(text, str):
        return {'present': False}
    flat = ' '.join(re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', text).lower().split())
    return {'present': True, 'chars': len(text), 'sha256': sha(text),
            'signals': [key for key, phrase in MESSAGES.items() if phrase in flat]}


def env_view(env):
    summary = {}
    for key in FIELDS:
        value = env.get(key)
        item = {'present': value is not None}
        if value is not None:
            item['sha256'] = sha(value)
        if key == 'TERM' and value in TERMS:
            item['value'] = value
        summary[key] = item
    return summary


def entry(argv, cwd, env):
    tokens = []
    for i, value in enumerate(argv):
        role = 'argument'
        if i == 0:
            role = 'executable'
        elif value in OPTIONS:
            role = value
        elif value.startswith('You are a worker dispatched by the controller (dispatch '):
            role = 'dispatch-brief'
        elif argv[i - 1] == '--add-dir':
            role = 'directory'
        elif argv[i - 1] in ('-m', '--model'):
            role = 'model'
        elif value.startswith('model_reasoning_effort='):
            role = 'reasoning-effort'
        tokens.append({'role': role, 'sha256': sha(value)})
    return {'argv': tokens, 'cmdline_sha256': sha(subprocess.list2cmdline(argv)),
            'cwd_sha256': sha(str(cwd)), 'env': env_view(env)}


def compare(plan, actual):
    if not plan or not actual:
        return {'available': False}
    a, b = plan['argv'], actual['argv']
    return {'available': True, 'cmdline_matches': plan['cmdline_sha256'] == actual['cmdline_sha256'],
            'argv_differences': [i for i in range(max(len(a), len(b))) if a[i:i+1] != b[i:i+1]],
            'cwd_matches': plan['cwd_sha256'] == actual['cwd_sha256'],
            'env_differences': [k for k in FIELDS if plan['env'][k] != actual['env'][k]]}


def capture_dir(host_spec):
    host_spec = Path(host_spec)
    gen = host_spec.name.split('.')[0]
    if (not re.fullmatch('[a-f0-9]{32}', gen) or host_spec.name != gen + '.host.spec'
            or host_spec.parent.name != 'bundled'):
        raise ValueError('invalid private generation')
    return host_spec.parent.parent / ('observe-' + gen)


def driver(pyz, args):
    import host_launch
    publish = host_launch.write_private

    def observe(path, value):
        path = Path(path)
        if path.name.endswith('.host.spec'):
            dest = capture_dir(path)
            dest.mkdir(mode=0o700)
            put(dest/'prepared.json', {**entry(value['argv'], value['cwd'], value['env']),
                                       'observed_ns':time.time_ns()})
        elif path.suffix == '.spec' and 'host_spec' in value:
            original = value['argv']
            if original != [sys.executable, pyz, '_host-run', value['host_spec']]:
                raise ValueError('unrecognized supervisor invocation')
            value = {**value, 'argv': [sys.executable, str(Path(__file__).resolve()), 'supervisor',
                                       *original[1:]]}
        return publish(path, value)
    host_launch.write_private = observe
    sys.argv = [pyz, *args]
    runpy.run_path(pyz, run_name='__main__')


def supervisor(pyz, spec):
    import host_launch
    dest = capture_dir(spec)
    publish = host_launch.write_private
    start = subprocess.Popen
    receipt = Path(spec).with_name(Path(spec).name.replace('.host.spec', '.launch.json'))

    def observe_start(path, value):
        publish(path, value)
        if Path(path) == receipt and value.get('category') == 'started':
            row = {'pid': value.get('pid'), 'creation': value.get('processStart'), 'observed_ns':time.time_ns()}
            put(dest/'started.json', row)

    def launch(argv, **kwargs):
        actual = entry(argv, kwargs['cwd'], kwargs['env'])
        put(dest/'spawn.json', {**actual,'observed_ns':time.time_ns()})
        return start(argv, **kwargs)

    def audit(event, args):
        if event == 'subprocess.Popen':
            _, argv, cwd, env = args
            line = argv if isinstance(argv, str) else subprocess.list2cmdline(argv)
            put(dest/'audit.json', {'cmdline_sha256': sha(line), 'cwd_sha256': sha(str(cwd)),
                                    'env': env_view(env), 'observed_ns':time.time_ns()})

    sys.addaudithook(audit)
    host_launch.write_private = observe_start
    subprocess.Popen = launch
    sys.argv = [pyz, '_host-run', spec]
    runpy.run_path(pyz, run_name='__main__')


def sources(state, job, handle):
    gen = handle.get('generation', '')
    if (not re.fullmatch('[a-f0-9]{32}', gen) or handle.get('unit') != job['dispatch']
            or handle.get('attempt') != job['attempt']):
        raise ValueError('generation mismatch')
    folder = state/'bundled'
    snap, receipt = folder/(gen+'.json'), folder/(gen+'.launch.json')
    if Path(handle.get('snapshot','')) != snap or Path(handle.get('receipt','')) != receipt:
        raise ValueError('receipt paths differ from bound generation')
    return snap, receipt, state/('observe-'+gen)


def matching_launch(view):
    comp = view['comparison']
    return (view['spawn_audited'] and comp.get('cmdline_matches') and comp.get('cwd_matches')
            and comp.get('env_differences') == [] and comp.get('argv_differences') == [])


def receipt_view(row):
    out = {'category': row.get('category') if row.get('category') in CATEGORIES else 'unavailable',
           'diagnostic': words(row.get('diagnostic'))}
    if type(row.get('exitCode')) is int:
        out['exit_code'] = row['exitCode']
    return out


def preclose(state, job, handle, runtime, *, startup_failed=False):
    snapfile, receiptfile, dest = sources(state, job, handle)
    receipt = read(receiptfile)
    snap = read(snapfile)

    def bound(row):
        return runtime.capability_matches(handle, row) and all(row.get(k) == handle[k]
                for k in ('unit', 'attempt', 'generation', 'nonce'))

    # The exit receipt is published shortly before the natural final viewport.
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        ended = bound(snap) and snap.get('state') in ('exited','closed','faulted')
        if ended and receipt.get('category') != 'started':
            break
        if not startup_failed and receipt.get('category') != 'process_exited':
            break
        time.sleep(.05)
        receipt, snap = read(receiptfile), read(snapfile)
    signed = bound(snap) and runtime.confirmed_cleanup(handle, snap)
    prepared, spawn, audit, started = [read(dest/(name+'.json'))
                                       for name in ('prepared','spawn','audit','started')]
    comparison = compare(prepared, spawn)
    actual_audited = bool(spawn and audit and all(spawn[k] == audit[k]
                          for k in ('cwd_sha256','cmdline_sha256','env')))
    creation = started.get('creation')
    start_view = {'present': bool(started), 'observed_ns':started.get('observed_ns'), 'pid': started.get('pid') if type(started.get('pid')) is int else None,
                  'creation': creation if isinstance(creation,str) and re.fullmatch(
                        r'windows:[0-9]+|linux:[a-f0-9-]+:[0-9]+',creation) else None}
    state_label = snap.get('state') if bound(snap) else None
    view = {'observed_ns':time.time_ns(), 'generation': handle['generation'], 'prepared': prepared, 'spawn': spawn,
            'started_receipt': start_view, 'snapshot_state': state_label if state_label in
                ('active','exited','closed','faulted') else 'unavailable',
            'popen_audit': audit, 'spawn_audited': actual_audited, 'comparison': comparison,
            'receipt': receipt_view(receipt), 'snapshot_bound': bool(bound(snap)), 'signed_end': signed,
            'natural_final_screen': words(snap.get('final_screen') if signed and state_label == 'exited' else None)}
    roles = {k: snap.get(k) for k in ROLES[:-1]} if bound(snap) else {}
    roles['worker'] = {k:start_view[k] for k in ('pid','creation')} if started else {}
    return view, roles


if __name__ == '__main__':
    mode, pyz, *args = sys.argv[1:]
    sys.path.insert(0, pyz)
    if mode == 'dispatch' and args[:1] == ['dispatch']:
        driver(pyz, args)
    elif mode == 'supervisor' and args[:1] == ['_host-run'] and len(args) == 2:
        supervisor(pyz, args[1])
    else:
        raise SystemExit(2)
