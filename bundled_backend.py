"""Opt-in bundled kernel adapter; one persistent runtime per execution attempt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import bundled_runtime as runtime
import host_launch
import wfc_platform


def outcome(result, diagnostic=None, **details):
    return {'result': result, **({'diagnostic': diagnostic} if diagnostic else {}), **details}


def companion():
    import router
    explicit = os.environ.get('WFC_BUNDLED_KERNEL')
    path = Path(explicit) if explicit else Path(router.PROGRAM).resolve().parent / (
        'wfc-kernel.exe' if wfc_platform.WINDOWS else 'wfc-kernel')
    path = path.expanduser().resolve()
    if not path.is_file() or (not wfc_platform.WINDOWS and not os.access(path, os.X_OK)):
        raise OSError('bundled kernel companion missing or not executable: co-locate wfc-kernel or set WFC_BUNDLED_KERNEL')
    return str(path)


class BundledBackend:
    name = 'bundled'

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
            raise RuntimeError('bundled lifecycle requires router connection')
        # The caller persisted its lifecycle fence before entering this method.
        # Identity must survive a CLI crash before any external worker is launched.
        if self.db.in_transaction:
            raise RuntimeError('bundled create requires a committed lifecycle fence')
        public = {key: value for key, value in handle.items() if key != 'token'}
        self.db.execute('UPDATE dispatch SET backend_handle = ? WHERE id = ?', (json.dumps(public), dispatch))

    def _snapshot(self, handle):
        try:
            snapshot = runtime.read_json(handle['snapshot'])
            if any(snapshot.get(key) != handle.get(key) for key in ('generation', 'unit', 'nonce', 'attempt')):
                return None
            return snapshot
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _observe(self, handle):
        snapshot = self._snapshot(handle)
        if not snapshot:
            return outcome('uncertain', 'bundled runtime identity is pending; inspect launch snapshot; do not replay')
        if snapshot.get('state') in ('closed', 'exited', 'faulted') and snapshot.get('cleanup') == 'confirmed':
            return outcome('accepted', alive=False, owned=True, confirmedAbsent=True, snapshot=snapshot)
        if not all(snapshot.get(key) for key in ('service', 'kernel', 'endpoint')):
            return outcome('uncertain', 'bundled service identity is incomplete; do not restart or replay')
        service = snapshot['service']
        probe = wfc_platform.process_probe(service['pid'])
        if probe['state'] != 'alive' or probe['identity'] != service['creation']:
            return outcome('uncertain', 'bundled service ended or changed; await guardian cleanup evidence; do not replay')
        return outcome('accepted', snapshot=snapshot)

    def _call(self, dispatch, op, **data):
        handle = self._handle(dispatch)
        if handle.get('state') == 'not-started':
            return outcome('definitely-not-sent', 'bundled worker was not started')
        observed = self._observe(handle)
        if observed['result'] != 'accepted':
            return observed
        if observed.get('confirmedAbsent'):
            if op == 'close':
                return outcome('completed')
            return outcome('definitely-not-sent', 'bundled execution is closed', confirmedAbsent=True)
        return runtime.call(observed['snapshot'], op=op, **data)

    def create(self, dispatch_id, cwd, command, extra_env, label=None):
        import router
        try:
            binary = companion()
            root = None if wfc_platform.WINDOWS else str(runtime.cgroup_root(os.environ.get('WFC_BUNDLED_CGROUP_ROOT')))
            argv = host_launch.prepare(command, extra_env, cwd)
        except host_launch.LaunchError as error:
            return outcome('definitely-not-sent', host_launch.sanitize(error, extra_env), category=error.category)
        except OSError as error:
            return outcome('definitely-not-sent', str(error), category='bundled_platform')
        prior = self._handle(dispatch_id)
        if prior and prior.get('state') != 'not-started':
            observed = self._observe(prior)
            if observed['result'] != 'accepted' or not observed.get('confirmedAbsent'):
                return outcome('refused', 'old bundled execution is not confirmed contained and stopped')
        row = self._load(dispatch_id)
        directory = router.state_dir() / 'bundled'
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        generation = uuid.uuid4().hex
        snapshot = directory / (generation + '.json')
        spec_path = directory / (generation + '.spec')
        handle = {'unit': dispatch_id, 'attempt': row['current_attempt'], 'generation': generation,
                  'nonce': uuid.uuid4().hex, 'snapshot': str(snapshot), 'state': 'pending'}
        receipt = directory / (generation + '.launch.json')
        host_spec = directory / (generation + '.host.spec')
        host_launch.write_private(host_spec, {'argv': argv, 'env': extra_env, 'cwd': str(cwd), 'receipt': str(receipt)})
        host_launch.write_private(receipt, {'category': 'unknown', 'diagnostic': 'bundled host startup not confirmed'})
        handle['receipt'] = str(receipt)
        spec = {**handle, 'host_spec': str(host_spec), 'token': uuid.uuid4().hex + uuid.uuid4().hex, 'binary': binary,
                'argv': [sys.executable, str(router.PROGRAM), '_host-run', str(host_spec)],
                'env': extra_env, 'cwd': str(cwd), 'cgroup_root': root}
        host_launch.write_private(spec_path, spec)
        self._save(dispatch_id, handle)
        try:
            guardian = subprocess.Popen([sys.executable, str(router.PROGRAM), '_bundled-guard', str(spec_path)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        **wfc_platform.detached_options())
        except OSError:
            spec_path.unlink(missing_ok=True)
            host_spec.unlink(missing_ok=True)
            return outcome('definitely-not-sent', 'bundled guardian could not start', category='bundled_start')
        # Never kill using this PID: identity and cleanup belong to the runtime/guardian.
        handle['guardian'] = {'pid': guardian.pid, 'creation': wfc_platform.process_identity(guardian.pid)}
        self._save(dispatch_id, handle)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snapshot_value = self._snapshot(handle)
            if snapshot_value and snapshot_value.get('state') == 'active':
                # Host launcher records exec success through its existing receipt contract.
                self._save(dispatch_id, {**handle, **snapshot_value})
                evidence = host_launch.read_receipt(receipt)
                if evidence and evidence['category'] != 'unknown':
                    if row['host'] == 'opencode' and len(command) > 1 and command[-2] == '--prompt':
                        return {**self._activate_initial_prompt(dispatch_id, command[-1]), 'receipt': str(receipt)}
                    return outcome('accepted', receipt=str(receipt))
            if guardian.poll() is not None:
                guardian.wait()
                break
            time.sleep(.025)
        return outcome('uncertain', 'bundled startup not confirmed; inspect runtime snapshot; no automatic retry',
                       category='unknown', receipt=str(receipt))

    def _activate_initial_prompt(self, dispatch_id, text):
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
                return outcome('refused', notice, category='permission_required')
            box = router.opencode_input_box(screen)
            if box is not None and box == previous:
                shown = ''.join(box.text.split())
                attempt = self._load(dispatch_id)['current_attempt']
                if not shown or attempt not in shown or not ''.join(text.split()).endswith(shown):
                    return outcome('accepted')
                return self._call(dispatch_id, 'input', text='\r')
            previous = box
            time.sleep(.3)
        return outcome('accepted')

    def inspect(self, dispatch_id):
        handle = self._handle(dispatch_id)
        if handle.get('state') == 'not-started':
            return outcome('accepted', alive=False, owned=True, confirmedAbsent=True)
        observed = self._observe(handle)
        if observed['result'] != 'accepted' or observed.get('confirmedAbsent'):
            return {key: value for key, value in observed.items() if key != 'snapshot'}
        result = runtime.call(observed['snapshot'], op='read')
        if result['result'] != 'accepted':
            return {**result, 'result': 'uncertain'}
        # Parent exit remains online until the runtime confirms whole-container
        # cleanup, so close cannot skip living descendants.
        return outcome('accepted', alive=True, owned=True, workerExited=result['data'].get('exit') is not None)

    def read(self, dispatch_id):
        result = self._call(dispatch_id, 'read')
        if result['result'] != 'accepted':
            return result
        data = result['data']
        if data.get('read_error'):
            return outcome('uncertain', 'bundled PTY read failed')
        return outcome('accepted', screen=data['text'], workerExit=data.get('exit'), eof=data['eof'])

    def submit(self, dispatch_id, text, host=None):
        import router
        observed = self.read(dispatch_id)
        if observed['result'] != 'accepted':
            return observed
        notice = router.read_only_notice(observed.get('screen', ''))
        if notice:
            return outcome('refused', notice, category='permission_required')
        # Bracketed paste preserves literal multi-line input. Enter is separate;
        # an unknown paste or key outcome never makes the text replayable.
        pasted = self._call(dispatch_id, 'input', text='\x1b[200~' + text + '\x1b[201~')
        if pasted['result'] != 'accepted':
            return pasted
        entered = self._call(dispatch_id, 'input', text='\r')
        if entered['result'] != 'accepted':
            return {**entered, 'result': 'uncertain'}
        if host == 'codex':
            before = router.screen_notices(observed.get('screen', ''))
            for retry in range(router.ENTER_RETRIES + 1):
                boxes = []
                for _ in range(2):
                    time.sleep(.3)
                    view = self.read(dispatch_id)
                    if view['result'] != 'accepted':
                        return {**view, 'result': 'uncertain'}
                    found = router.screen_notices(view.get('screen', ''))
                    notice = next((text for kind, text in found if kind == 'state'), None)
                    if not notice and len(router.refusals(found)) > len(router.refusals(before)):
                        notice = router.refusals(found)[-1]
                    if notice:
                        return outcome('uncertain', notice, category='permission_required')
                    boxes.append(router.codex_input_box(view.get('screen', ''), require_complete=True))
                box = boxes[-1]
                if (box is None or box != boxes[0] or box.scrolled or not box.text.strip()
                        or ''.join(box.text.split()) != ''.join(text.split())):
                    return outcome('accepted')
                if retry == router.ENTER_RETRIES:
                    return outcome('uncertain', 'typed text remained in the Codex input box')
                entered = self._call(dispatch_id, 'input', text='\r')
                if entered['result'] != 'accepted':
                    return {**entered, 'result': 'uncertain'}
        return outcome('accepted')

    def interrupt(self, dispatch_id):
        return self._call(dispatch_id, 'input', text='\x03')

    def close(self, dispatch_id):
        handle = self._handle(dispatch_id)
        if handle.get('state') == 'not-started':
            return outcome('completed')
        result = self._call(dispatch_id, 'close')
        if result['result'] != 'completed':
            return result
        # A close acknowledgement is issued only after containment is empty.
        return outcome('completed')

    def resume(self, dispatch_id, cwd, command, extra_env, label=None, text=None, host=None, probe_read_only=False):
        # Resume is new execution, never PTY recovery. Close the previous owned
        # container only after the router has authorized a stopped execution.
        created = self.create(dispatch_id, cwd, command, extra_env, label)
        if created['result'] == 'accepted' and host == 'opencode':
            return {**self.submit(dispatch_id, text or '', host), 'receipt': created.get('receipt')}
        return created
