"""Thin Herdr 0.9.5 / protocol 22 public JSON API adapter.

The core owns tasks, receipts and owner fencing. Every API call uses a bounded
helper process so a stalled native named pipe cannot block the router forever.
"""
import io
import functools
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

import host_launch
import wfc_platform

LIMIT = 2 * 1024 * 1024
PROTOCOL = 22


def _peer(stream, endpoint):
    if wfc_platform.WINDOWS:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(pid)):
            raise OSError('cannot establish Herdr server process identity')
        pid = pid.value
    else:
        metadata = os.stat(endpoint)
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PermissionError('Herdr endpoint must be an owner-only local socket')
        if sys.platform.startswith('linux'):
            pid, uid, _ = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise PermissionError('Herdr peer belongs to another user')
        elif sys.platform == 'darwin':
            pid = struct.unpack('i', stream.getsockopt(0, 2, 4))[0]  # SOL_LOCAL / LOCAL_PEERPID
        else:
            raise OSError('Herdr peer process identity is unsupported on this platform')
    identity = wfc_platform.process_identity(pid)
    if not identity:
        raise OSError('cannot establish Herdr server creation identity')
    return {'pid': pid, 'creation': identity}


def _snapshot_guard(snapshot, guard):
    workspaces, panes = snapshot['workspaces'], snapshot['panes']
    workspace = next((w for w in workspaces if w.get('workspace_id') == guard['workspace']), None)
    if workspace is None:
        return None
    if workspace.get('tokens', {}).get('wfc_owner') != guard['nonce']:
        raise PermissionError('Herdr workspace ownership does not match this attempt')
    pane = next((p for p in panes if p.get('pane_id') == guard.get('pane')), None)
    if pane is None:
        return None
    if pane['workspace_id'] != guard['workspace'] or pane['terminal_id'] != guard['terminal']:
        raise PermissionError('Herdr pane identity does not match this attempt')
    return pane


def helper(payload):
    """One bounded public-API operation, with identity and optional snapshot fencing."""
    sent = False
    service = None
    try:
        endpoint, method, params = payload['endpoint'], payload['method'], payload.get('params', {})
        if not isinstance(endpoint, str) or not endpoint or '\0' in endpoint:
            raise ValueError('invalid Herdr local endpoint')
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ValueError('invalid Herdr request')
        def request(name, body):
            nonlocal sent, service
            connection = None
            stream = None
            try:
                if wfc_platform.WINDOWS:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            connection = open('\\\\.\\pipe\\' + endpoint, 'r+b', buffering=0)
                            break
                        except OSError as err:
                            if getattr(err, 'winerror', None) != 231 or time.monotonic() >= deadline:
                                raise
                            time.sleep(.02)  # only retry connecting, before any request bytes

                    peer = _peer(connection, endpoint)
                    stream = io.BufferedRWPair(connection, connection)
                else:
                    connection = socket.socket(socket.AF_UNIX)
                    connection.settimeout(5)
                    connection.connect(endpoint)
                    peer = _peer(connection, endpoint)
                    stream = connection.makefile('rwb')
                expected = service or payload.get('service')
                if expected and expected != peer:
                    raise RuntimeError('herdr_service_changed')
                service = peer
                ident = uuid.uuid4().hex
                encoded = (json.dumps({'id': ident, 'method': name, 'params': body}, ensure_ascii=False) + '\n').encode('utf-8')
                if len(encoded) > LIMIT:
                    raise ValueError('Herdr request exceeds the size limit')
                sent = True  # even a partially written request has an uncertain result
                stream.write(encoded)
                stream.flush()
                raw = stream.readline(LIMIT + 1)
                if len(raw) > LIMIT or not raw.endswith(b'\n'):
                    raise OSError('Herdr response is incomplete or exceeds the size limit')
                response = json.loads(raw)
                if response.get('id') != ident:
                    raise ValueError('Herdr response identity mismatch')
                if 'error' in response:
                    error = response['error']
                    code = error.get('code', 'herdr_error') if isinstance(error, dict) else 'herdr_error'
                    raise RuntimeError(code)
                if not isinstance(response.get('result'), dict):
                    raise ValueError('invalid Herdr response result')
                return response['result']
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                if connection is not None:
                    connection.close()

        pong = request('ping', {})
        if pong.get('protocol') != PROTOCOL or pong.get('version') != '0.9.5':
            return {'result': 'refused', 'category': 'herdr_protocol',
                    'diagnostic': 'Herdr adapter requires version 0.9.5 / protocol 22', 'service': service}
        guard = payload.get('guard')
        if guard:
            snapshot = request('session.snapshot', {})['snapshot']
            pane = _snapshot_guard(snapshot, guard)
            if pane is None:
                return {'result': 'accepted', 'service': service, 'absent': True, 'data': {'snapshot': snapshot}}
            if (method == 'pane.send_input' or (method == 'pane.send_keys' and params.get('keys') == ['Enter'])) and pane.get('agent_status') == 'blocked':
                return {'result': 'refused', 'category': 'permission_required',
                        'diagnostic': 'Herdr reports a blocked host; inspect and resolve its prompt'}
        data = request(method, params)
        return {'result': 'accepted', 'service': service, 'data': data}
    except PermissionError:
        return {'result': 'refused', 'category': 'herdr_ownership', 'diagnostic': 'Herdr ownership or access check refused'}
    except RuntimeError as err:
        code = str(err)
        if code == 'herdr_service_changed':
            return {'result': 'uncertain', 'category': 'herdr_restart', 'service': service,
                    'diagnostic': 'Herdr service identity changed; inspect snapshot and explicitly reconcile'}
        definite = code in ('invalid_request', 'invalid_target', 'invalid_key', 'pane_not_found',
                            'workspace_not_found', 'permission_denied', 'confirmation_required')
        return {'result': 'refused' if definite else 'uncertain', 'category': 'herdr_error',
                'diagnostic': 'Herdr API error: ' + code[:100], 'service': service}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'result': 'uncertain' if sent else 'definitely-not-sent', 'category': 'herdr_unavailable',
                'diagnostic': 'Herdr connection, protocol or process-identity observation failed', 'service': service}


def rpc(program, payload):
    try:
        timeout = float(os.environ.get('WFC_HERDR_TIMEOUT', '8'))
        if not 0 < timeout <= 60:
            raise ValueError
    except ValueError:
        return {'result': 'refused', 'category': 'herdr_config', 'diagnostic': 'WFC_HERDR_TIMEOUT must be in (0, 60]'}
    try:
        result = subprocess.run([sys.executable, str(program), '_herdr-call'],
                                input=json.dumps(payload).encode('utf-8'), capture_output=True, timeout=timeout)
        value = json.loads(result.stdout)
        if result.returncode or not isinstance(value, dict) or value.get('result') not in (
                'accepted', 'uncertain', 'refused', 'definitely-not-sent'):
            raise ValueError
        return value
    except OSError:
        return {'result': 'definitely-not-sent', 'category': 'herdr_unavailable', 'diagnostic': 'Herdr helper could not start'}
    except (subprocess.TimeoutExpired, ValueError):
        return {'result': 'uncertain', 'category': 'herdr_timeout',
                'diagnostic': 'Herdr helper did not confirm its result; do not replay input'}


def observed_operation(operation):
    @functools.wraps(operation)
    def observe(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            # Malformed external observations never authorize retry or expose payloads.
            return {'result': 'uncertain', 'category': 'unknown',
                    'diagnostic': 'Herdr operation requires explicit reconciliation'}
    return observe


class HerdrBackend:
    name = 'herdr'

    def __init__(self, db=None, row=None):
        self.db, self.row = db, row

    def _load(self, dispatch):
        if self.db is not None:
            return self.db.execute('SELECT * FROM dispatch WHERE id = ?', (dispatch,)).fetchone()
        return self.row

    def _handle(self, dispatch):
        return json.loads(self._load(dispatch)['backend_handle'] or '{}')

    def _save(self, dispatch, handle):
        if self.db is None:
            raise RuntimeError('Herdr lifecycle requires the router connection')
        self.db.execute('UPDATE dispatch SET backend_handle = ?, launch_receipt = ? WHERE id = ?',
                        (json.dumps(handle), handle.get('receipt'), dispatch))

    def _call(self, handle, method, params=None, guard=False):
        import router
        return rpc(router.PROGRAM, {'endpoint': handle['endpoint'], 'service': handle.get('service'),
                                    'method': method, 'params': params or {},
                                    **({'guard': handle} if guard else {})})

    @observed_operation
    def create(self, dispatch_id, cwd, command, extra_env, label=None):
        import router
        row = self._load(dispatch_id)
        prior = self._handle(dispatch_id)
        if prior.get('pane'):
            observed = self.inspect(dispatch_id)
            if observed['result'] != 'accepted' or observed.get('alive'):
                return {'result': 'refused', 'category': 'herdr_writer',
                        'diagnostic': 'old Herdr writer is not confirmed stopped'}
            closed = self.close(dispatch_id)
            if closed['result'] != 'completed':
                return closed
        try:
            argv = host_launch.prepare(command, extra_env, cwd)
        except host_launch.LaunchError as err:
            return {'result': 'definitely-not-sent', 'category': err.category,
                    'diagnostic': host_launch.sanitize(err, extra_env)}
        handle = {'endpoint': prior.get('endpoint'), 'attempt': row['current_attempt'],
                  'nonce': uuid.uuid4().hex, 'state': 'pending'}
        if not handle['endpoint']:
            return {'result': 'definitely-not-sent', 'category': 'herdr_config',
                    'diagnostic': 'Herdr requires --herdr-endpoint or WFC_HERDR_ENDPOINT'}
        directory = router.state_dir() / 'launches'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt = directory / (dispatch_id + '-' + uuid.uuid4().hex + '.json')
        spec = receipt.with_suffix('.spec')
        handle['receipt'] = str(receipt)
        host_launch.write_private(spec, {'argv': argv, 'env': extra_env, 'cwd': str(cwd), 'receipt': str(receipt)})
        host_launch.write_private(receipt, {'category': 'unknown', 'diagnostic': 'host startup not confirmed', 'at': time.time()})
        self._save(dispatch_id, handle)
        created = self._call(handle, 'workspace.create', {'cwd': str(cwd), 'label': label or dispatch_id, 'focus': False})
        if created['result'] != 'accepted':
            return {**created, 'receipt': str(receipt), 'category': 'unknown'}
        handle.update(service=created['service'], workspace=created['data']['workspace']['workspace_id'])
        self._save(dispatch_id, handle)
        marked = self._call(handle, 'workspace.report_metadata', {'workspace_id': handle['workspace'], 'source': 'wfc',
                              'tokens': {'wfc_owner': handle['nonce'], 'wfc_attempt': handle['attempt']}})
        if marked['result'] != 'accepted':
            return {**marked, 'result': 'uncertain', 'category': 'unknown', 'receipt': str(receipt)}
        launched = self._call(handle, 'layout.apply', {'tab_id': created['data']['tab']['tab_id'], 'focus': False,
                             'root': {'type': 'pane', 'label': label or dispatch_id, 'cwd': str(cwd),
                                      'command': [sys.executable, str(router.PROGRAM), '_host-run', str(spec)],
                                      'env': {'WFC_DISPATCH': dispatch_id, 'WFC_STATE': str(router.state_dir())}}})
        if launched['result'] != 'accepted':
            return {**launched, 'result': 'uncertain', 'category': 'unknown', 'receipt': str(receipt)}
        handle['pane'] = launched['data']['layout']['focused_pane_id']
        self._save(dispatch_id, handle)
        deadline = time.monotonic() + 2
        pane = None
        while True:
            snapshot = self._call(handle, 'session.snapshot')
            if snapshot['result'] != 'accepted':
                return {**snapshot, 'result': 'uncertain', 'category': 'unknown', 'receipt': str(receipt)}
            pane = next((p for p in snapshot['data']['snapshot']['panes'] if p['pane_id'] == handle['pane']), None)
            if pane is not None or time.monotonic() >= deadline:
                break
            time.sleep(.02)  # observe async layout publication; never replay layout.apply
        if pane is None:
            return {'result': 'uncertain', 'category': 'unknown', 'receipt': str(receipt),
                    'diagnostic': 'Herdr started a unit but its identity is not confirmed'}
        handle.update(terminal=pane['terminal_id'], state='active')
        self._save(dispatch_id, handle)
        deadline = time.monotonic() + 2
        evidence = host_launch.read_receipt(receipt)
        while time.monotonic() < deadline and (not evidence or evidence['category'] == 'unknown'):
            time.sleep(.02)
            evidence = host_launch.read_receipt(receipt)
        if evidence and evidence['category'] == 'started':
            if row['host'] == 'opencode' and len(command) > 1 and command[-2] == '--prompt':
                return {**self._activate_initial_prompt(dispatch_id, command[-1]), 'receipt': str(receipt)}
            return {'result': 'accepted', 'receipt': str(receipt)}
        return {'result': 'uncertain', 'category': 'unknown', 'receipt': str(receipt),
                'diagnostic': (evidence or {}).get('diagnostic', 'host startup not confirmed')}

    def _activate_initial_prompt(self, dispatch_id, text):
        """Confirm the argv prefill in two stable composer snapshots; send only Enter once."""
        import router
        previous = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = self.read(dispatch_id)
            if observed['result'] != 'accepted':
                return observed
            screen = observed.get('screen', '')
            notice = router.read_only_notice(screen)
            if notice:
                return {'result': 'refused', 'category': 'permission_required', 'diagnostic': notice}
            box = router.opencode_input_box(screen)
            if box is not None and box == previous:
                shown = ''.join(box.text.split())
                attempt = self._load(dispatch_id)['current_attempt']
                if not shown or attempt not in shown or not ''.join(text.split()).endswith(shown):
                    return {'result': 'accepted'}  # already submitted or another input; do not touch it
                handle = self._handle(dispatch_id)
                result = self._call(handle, 'pane.send_keys', {'pane_id': handle['pane'], 'keys': ['Enter']}, guard=True)
                if result.get('absent'):
                    return {'result': 'uncertain', 'diagnostic': 'Herdr unit disappeared before initial Enter'}
                return result
            previous = box
            time.sleep(.3)
        return {'result': 'accepted'}  # process launch was confirmed; unreadable UI authorizes no extra key

    def _writer_stopped(self, handle):
        evidence = host_launch.read_receipt(handle.get('receipt'))
        if evidence and evidence.get('category') == 'process_exited':
            return True
        if handle.get('writerStoppedEvidence'):
            return True  # explicit operator reconciliation, recorded in lifecycle audit
        if not evidence or not evidence.get('pid') or not evidence.get('processStart'):
            processes = handle.get('writerProcesses', [])
            if not processes:
                return None
            observations = [wfc_platform.process_probe(p['pid']) for p in processes]
            if any(p['state'] == 'unknown' for p in observations):
                return None
            return all(p['state'] == 'exited' or p['identity'] != original['creation']
                       for original, p in zip(processes, observations))
        probe = wfc_platform.process_probe(evidence['pid'])
        if probe['state'] == 'exited':
            return True
        if probe['state'] == 'alive':
            return probe['identity'] != evidence['processStart']
        return None

    @observed_operation
    def inspect(self, dispatch_id):
        handle = self._handle(dispatch_id)
        if handle.get('state') == 'not-started':
            return {'result': 'accepted', 'alive': False, 'owned': True, 'confirmedAbsent': True}
        if not all(handle.get(k) for k in ('service', 'workspace', 'pane', 'terminal', 'nonce')):
            return {'result': 'uncertain', 'diagnostic': 'Herdr unit identity requires explicit reconciliation', 'owned': False}
        observed = self._call(handle, 'session.snapshot', guard=True)
        if observed['result'] != 'accepted':
            return {**observed, 'owned': False}
        stopped = self._writer_stopped(handle)
        if stopped is True:
            return {'result': 'accepted', 'alive': False, 'owned': True,
                    'confirmedAbsent': bool(observed.get('absent'))}
        if observed.get('absent') or stopped is None:
            return {'result': 'uncertain', 'owned': True,
                    'diagnostic': 'Herdr writer liveness is not confirmed; explicit reconciliation required'}
        return {'result': 'accepted', 'alive': True, 'owned': True}

    @observed_operation
    def read(self, dispatch_id):
        handle = self._handle(dispatch_id)
        result = self._call(handle, 'pane.read', {'pane_id': handle.get('pane'), 'source': 'recent',
                             'format': 'text', 'strip_ansi': True, 'lines': 200}, guard=True)
        if result['result'] != 'accepted' or result.get('absent'):
            return {**result, 'result': 'uncertain'}
        read = result['data']['read']
        if not isinstance(read['text'], str) or not isinstance(read['revision'], int) or not isinstance(read['truncated'], bool):
            raise ValueError('invalid Herdr read snapshot')
        return {'result': 'accepted', 'screen': read['text'], 'revision': read['revision'],
                'snapshotTruncated': read['truncated']}

    @observed_operation
    def submit(self, dispatch_id, text, host=None):
        import router
        observed = self.read(dispatch_id)
        if observed['result'] != 'accepted':
            return observed
        notice = router.read_only_notice(observed.get('screen', ''))
        if notice:
            return {'result': 'refused', 'category': 'permission_required', 'diagnostic': notice}
        handle = self._handle(dispatch_id)
        result = self._call(handle, 'pane.send_input', {'pane_id': handle.get('pane'), 'text': text,
                                                       'keys': ['Enter']}, guard=True)
        if result.get('absent'):
            return {'result': 'definitely-not-sent', 'diagnostic': 'Herdr unit is absent'}
        if result['result'] == 'accepted' and host == 'codex':
            return self._submit_codex_draft(dispatch_id, text, handle, router.screen_notices(observed.get('screen', '')))
        return result

    def _submit_codex_draft(self, dispatch_id, text, handle, before):
        """Recover a dropped burst Enter only for a stable, complete Codex draft.

        Acceptance of send_input does not mean the host submitted it. Never replay
        text, or infer a key's outcome from a later screen after an unknown result.
        """
        import router
        for retry in range(router.ENTER_RETRIES + 1):
            boxes = []
            for _ in range(2):
                time.sleep(.3)
                observed = self.read(dispatch_id)
                if observed['result'] != 'accepted':
                    return {**observed, 'result': 'uncertain'}  # text may already have arrived
                found = router.screen_notices(observed.get('screen', ''))
                notice = next((text for kind, text in found if kind == 'state'), None)
                if not notice and len(router.refusals(found)) > len(router.refusals(before)):
                    notice = router.refusals(found)[-1]
                if notice:
                    return {'result': 'uncertain', 'category': 'permission_required', 'diagnostic': notice}
                boxes.append(router.codex_input_box(observed.get('screen', ''), require_complete=True))
            box = boxes[-1]
            # A prefix/suffix can also belong to an overlapping or partially drawn
            # draft. Only the complete visible text authorizes another Enter.
            if (box is None or box != boxes[0] or box.scrolled or not box.text.strip()
                    or ''.join(box.text.split()) != ''.join(text.split())):
                return {'result': 'accepted'}
            if retry == router.ENTER_RETRIES:
                return {'result': 'uncertain', 'diagnostic': 'typed text remained in the Codex input box'}
            entered = self._call(handle, 'pane.send_keys', {'pane_id': handle['pane'], 'keys': ['Enter']}, guard=True)
            if entered['result'] != 'accepted' or entered.get('absent'):
                return {**entered, 'result': 'uncertain'}  # never make the accepted text replayable
        return {'result': 'accepted'}

    @observed_operation
    def interrupt(self, dispatch_id):
        handle = self._handle(dispatch_id)
        result = self._call(handle, 'pane.send_keys', {'pane_id': handle.get('pane'), 'keys': ['C-c']}, guard=True)
        if result.get('absent'):
            return {'result': 'uncertain', 'diagnostic': 'Herdr unit is absent'}
        return result  # acceptance is not evidence that the writer has stopped

    @observed_operation
    def close(self, dispatch_id):
        handle = self._handle(dispatch_id)
        result = self._call(handle, 'pane.close', {'pane_id': handle.get('pane')}, guard=True)
        if result['result'] == 'accepted':
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self._writer_stopped(handle) is True:
                    return {'result': 'completed'}
                time.sleep(.02)
            return {'result': 'uncertain', 'diagnostic': 'Herdr closed the pane but old writer stop is not confirmed'}
        return result

    @observed_operation
    def resume(self, dispatch_id, cwd, command, extra_env, label=None, text=None, host=None, probe_read_only=False):
        created = self.create(dispatch_id, cwd, command, extra_env, label)
        if created['result'] == 'accepted' and host == 'opencode':
            return {**self.submit(dispatch_id, text or '', host), 'receipt': created.get('receipt')}
        return created
