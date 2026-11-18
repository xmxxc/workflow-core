"""Issue #35 opt-in fixture probe; see docs/verification/windows-console-35.md.

No real host, Codex executable, user state, or Herdr service is selected by this tool.
Native window evidence is available only on Windows. Run --help for bounds/STOP.
"""
import argparse
import ctypes as C
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wfc_platform

STAGES = ('idle', 'rpc-fixture', 'queue-fixture', 'observer-selftest')


def emit(root, event, **metadata):
    # One writer per file; no command lines, window titles, environment or RPC bodies.
    record = dict(event=event, utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns(),
                  writer_pid=os.getpid(), writer_creation=wfc_platform.process_identity(os.getpid()), **metadata)
    with (root / ('events-' + str(os.getpid()) + '.jsonl')).open('a', encoding='utf-8') as out:
        out.write(json.dumps(record) + '\n')


def exception_fields(error):
    """Closed built-in categories/codes, without dispatching exception-instance hooks."""
    error_type = type(error)
    fields = dict(category='Exception', errno=None, winerror=None)
    for kind, label, codes in ((PermissionError, 'PermissionError', True), (FileExistsError, 'FileExistsError', True),
                               (FileNotFoundError, 'FileNotFoundError', True), (TimeoutError, 'TimeoutError', True),
                               (OSError, 'OSError', True), (ValueError, 'ValueError', False),
                               (TypeError, 'TypeError', False), (RuntimeError, 'RuntimeError', False)):
        if error_type is kind:
            fields['category'] = label
            if codes:
                for name in ('errno', 'winerror'):
                    try:
                        # Exact built-in types: read native descriptors/storage, bypass instance hooks.
                        value = object.__getattribute__(error, name)
                    except AttributeError:
                        continue
                    fields[name] = value if type(value) is int else None
            break
    return fields


def isolated_env(root):
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'SystemDrive',
           'OS', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS') if key in os.environ}
    env.update(TEMP=str(root), TMP=str(root), PYTHONUTF8='1',
               PATH=str(Path(sys.executable).parent), WFC_STATE=str(root / 'state'),
               HERDR_SOCKET_PATH=('wfc-probe-' + root.name if os.name == 'nt' else str(root / 'api.sock')),
               HERDR_CONFIG_PATH=str(root / 'herdr.toml'), WFC_HERDR_TIMEOUT='2', WFC_QUEUE_TIMEOUT='2')
    env['WFC_HERDR_ENDPOINT'] = env['HERDR_SOCKET_PATH']
    for kind in ('CONFIG', 'STATE', 'DATA', 'CACHE'):
        env['XDG_' + kind + '_HOME'] = str(root / kind.lower())
    return env


def stop_reason(stop, deadline, now=None):
    if stop.exists():
        return 'STOP'
    if (time.monotonic() if now is None else now) >= deadline:
        return 'deadline'
    return None


def wait_file(path, stop, deadline):
    while not path.exists():
        if stop_reason(stop, deadline):
            return False
        time.sleep(.02)
    return not stop_reason(stop, deadline)


def read_events(root):
    events = []
    for path in root.glob('events-*.jsonl'):
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass  # a deadline may interrupt the final write; never infer a quiet run
    return events


def positive_control_seen(events):
    controls = [e for e in events if e['event'] == 'positive_control']
    return bool(controls) and all(
        c['hwnd'] and c.get('owner') and any(
            e['event'] in ('owned_window', 'console_association') and e.get('visible')
            and e.get('hwnd') == c['hwnd'] and e.get('owner') == c['owner']
            and (e['event'] == 'owned_window' or e.get('attached_owned_pid_present'))
            for e in events) for c in controls)


class WatchdogCancellation:
    """Serialize cancellation with callback admission, never with blocking work."""
    def __init__(self):
        self.event = threading.Event()
        self.gate = threading.Lock()

    def set(self):
        with self.gate:
            self.event.set()

    def wait(self, timeout):
        return self.event.wait(timeout)

    def admit(self):
        with self.gate:
            return not self.event.is_set()


def start_watchdog(stop, deadline, terminate, release=lambda: None):
    """Independent of window sampling and phase/RPC progress; owns no PID lookup."""
    finished, outcome = WatchdogCancellation(), []
    def watch():
        try:
            while not finished.wait(.01):
                reason = stop_reason(stop, deadline)
                if reason:
                    if not finished.admit():
                        return  # cancellation also fences an already-running stop_reason
                    terminate()
                    outcome.append(reason)
                    return
        except Exception:
            outcome.append('watchdog_failed')
        finally:
            try:
                release()  # owns a separate handle, valid through an admitted callback
            except Exception:
                outcome.append('watchdog_failed')
    thread = threading.Thread(target=watch, daemon=True)
    try:
        thread.start()
    except BaseException:
        release()
        raise
    return finished, thread, outcome


class Native:
    """Explicit pointer-width-safe Win32 bindings; constructed only on native Windows."""
    def __init__(self):
        from ctypes import wintypes as W
        self.W = W
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.u = C.WinDLL('user32', use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        bindings = [
            (self.k, 'CreateJobObjectW', [C.c_void_p, W.LPCWSTR], W.HANDLE),
            (self.k, 'OpenJobObjectW', [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            (self.k, 'SetInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            (self.k, 'AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL),
            (self.k, 'QueryInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
            (self.k, 'TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL),
            (self.k, 'CloseHandle', [W.HANDLE], W.BOOL),
            (self.k, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.k, 'IsProcessInJob', [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
            (self.k, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
            (self.k, 'GetCurrentProcess', [], W.HANDLE),
            (self.k, 'DuplicateHandle', [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE),
                                       W.DWORD, W.BOOL, W.DWORD], W.BOOL),
            (self.k, 'GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            (self.k, 'QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            (self.k, 'CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE),
            (self.k, 'Process32FirstW', [W.HANDLE, C.c_void_p], W.BOOL),
            (self.k, 'Process32NextW', [W.HANDLE, C.c_void_p], W.BOOL),
            (self.k, 'AttachConsole', [W.DWORD], W.BOOL),
            (self.k, 'FreeConsole', [], W.BOOL),
            (self.k, 'SetConsoleCtrlHandler', [C.c_void_p, W.BOOL], W.BOOL),
            (self.k, 'GetConsoleWindow', [], W.HWND),
            (self.k, 'GetConsoleProcessList', [C.POINTER(W.DWORD), W.DWORD], W.DWORD),
            (self.u, 'EnumWindows', [self.callback, W.LPARAM], W.BOOL),
            (self.u, 'GetWindowThreadProcessId', [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (self.u, 'IsWindowVisible', [W.HWND], W.BOOL),
        ]
        for library, name, args, result in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def check(self, result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    def identity(self, handle):
        values = [self.W.FILETIME() for _ in range(4)]
        self.check(self.k.GetProcessTimes(handle, *(C.byref(v) for v in values)))
        return 'windows:' + str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)

    def process(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            return self.metadata(handle, pid)
        except OSError:
            return None
        finally:
            self.k.CloseHandle(handle)

    def metadata(self, handle, pid, creation=None):
        creation = creation or self.identity(handle)
        image = C.create_unicode_buffer(32768)
        size = self.W.DWORD(len(image))
        self.check(self.k.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)))
        return dict(pid=pid, creation=creation, image=Path(image.value).name)

    def error_code(self):
        return getattr(C, 'get_last_error', lambda: 0)()

    def liveness(self, handle):
        result = self.k.WaitForSingleObject(handle, 0)
        if result == 258:
            return 'alive', {}
        if result == 0:
            return 'exited', {}
        return 'unknown', dict(category='query_failed' if result == 0xffffffff else 'unexpected_result',
            api='WaitForSingleObject', winerror=self.error_code() if result == 0xffffffff else None)

    def retained_state(self, job, pin, expected):
        handle, process = pin
        if process != expected:
            return 'unknown', dict(category='published_identity_changed', api=None, winerror=None)
        state, diagnostic = self.liveness(handle)
        if state != 'alive':
            return state, diagnostic  # only a previously verified, still-retained object can prove exit
        member = self.W.BOOL()
        if not self.k.IsProcessInJob(handle, job, C.byref(member)):
            return 'unknown', dict(category='query_failed', api='IsProcessInJob', winerror=self.error_code())
        if not member.value:
            # The object can exit between wait and membership query; prove that on this handle.
            state, diagnostic = self.liveness(handle)
            if state == 'exited':
                return state, diagnostic
            return 'unknown', diagnostic or dict(category='membership_refused', api='IsProcessInJob', winerror=None)
        return 'alive', {}

    @contextmanager
    def owned_process(self, job, pid, expected=None, diagnostic=None):
        """Acquire an owned object; recover a metadata error only with exact known exit proof."""
        diagnostic = diagnostic if diagnostic is not None else {}
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        api, verified_creation = 'OpenProcess', None
        def refuse(category, operation, winerror=None):
            diagnostic.update(category=category, api=operation, winerror=winerror)
            return None
        def validate():
            nonlocal api, verified_creation
            if not handle:
                return refuse('query_failed', api, self.error_code())
            api = 'IsProcessInJob'
            member = self.W.BOOL()
            if not self.k.IsProcessInJob(handle, job, C.byref(member)):
                return refuse('query_failed', api, self.error_code())
            if not member.value:
                return refuse('membership_refused', api)
            state, detail = self.liveness(handle)
            if state != 'alive':
                diagnostic.update(detail or dict(category='unverified_exit', api='WaitForSingleObject', winerror=None))
                return None
            api = 'GetProcessTimes'
            creation = self.identity(handle)
            if expected is not None and creation != expected.get('creation'):
                return refuse('published_identity_changed', api)
            verified_creation = creation
            api = 'QueryFullProcessImageNameW'
            process = self.metadata(handle, pid, creation=creation)
            if expected is not None and process != expected:
                return refuse('published_identity_changed', api)
            state, detail = self.liveness(handle)
            if state != 'alive':
                diagnostic.update(detail or dict(category='unverified_exit', api='WaitForSingleObject', winerror=None))
                return None
            return handle, process
        try:
            try:
                pin = validate()
            except OSError:
                pin = refuse('query_failed', api, self.error_code())
                if api == 'QueryFullProcessImageNameW':
                    # Preserve the image-query error before any subsequent native call.
                    state, detail = self.liveness(handle)
                    diagnostic.update(post_query_state=state, post_query_api=detail.get('api'),
                                      post_query_winerror=detail.get('winerror'))
                    if (state == 'exited' and expected is not None
                            and verified_creation == expected.get('creation')):
                        try:
                            same_creation = self.identity(handle) == verified_creation
                        except OSError:
                            diagnostic.update(post_query_state='unknown', post_query_api='GetProcessTimes',
                                              post_query_winerror=self.error_code())
                        else:
                            if same_creation:
                                diagnostic['verified_exit'] = True
                                pin = handle, dict(expected)
                            else:
                                diagnostic.update(post_query_state='identity_changed', post_query_api='GetProcessTimes')
            yield pin
        finally:
            if handle:
                self.k.CloseHandle(handle)

    def duplicate_job(self, job):
        duplicate = self.W.HANDLE()
        current = self.k.GetCurrentProcess()
        self.check(self.k.DuplicateHandle(current, job, current, C.byref(duplicate), 0, False, 2))
        return duplicate.value  # DUPLICATE_SAME_ACCESS; watchdog owns lifetime

    def observation_job(self, root):
        return self.check(self.k.OpenJobObjectW(4, False, (root / 'job-name').read_text()))  # QUERY

    def job(self, observe_only=False):
        W = self.W
        class Basic(C.Structure):
            _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', W.DWORD),
                        ('minimum', C.c_size_t), ('maximum', C.c_size_t), ('active', W.DWORD),
                        ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]
        class IO(C.Structure):
            _fields_ = [(name, C.c_uint64) for name in ('read', 'write', 'other', 'rb', 'wb', 'ob')]
        class Limits(C.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', C.c_size_t),
                        ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]
        name = 'Local\\wfc-probe-' + uuid.uuid4().hex if observe_only else None
        handle = self.check(self.k.CreateJobObjectW(None, name))
        if name and C.get_last_error() == 183:  # never adopt a preexisting named object
            self.k.CloseHandle(handle)
            raise OSError('job name collision')
        limits = Limits()
        # Only the unnamed outer job controls cleanup. Query handles for the nested
        # observation job cannot keep outer kill-on-close containment alive.
        limits.basic.flags = 0 if observe_only else 0x2000
        try:
            self.check(self.k.SetInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.k.CloseHandle(handle)
            raise
        if observe_only:
            self.job_name = name
        return handle

    def members(self, job):
        class Pids(C.Structure):
            _fields_ = [('assigned', self.W.DWORD), ('count', self.W.DWORD), ('pids', C.c_size_t * 128)]
        value = Pids()
        self.check(self.k.QueryInformationJobObject(job, 3, C.byref(value), C.sizeof(value), None))
        if value.assigned > 128 or value.count > 128:
            raise RuntimeError('job membership overflow')
        return list(value.pids[:value.count])

    def parents(self):
        W = self.W
        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                        ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                        ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD), ('exe', W.WCHAR * 260)]
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == W.HANDLE(-1).value:
            raise OSError('process snapshot unavailable')
        try:
            entry, parents = Entry(), {}
            entry.size = C.sizeof(entry)
            ok = self.k.Process32FirstW(handle, C.byref(entry))
            while ok:
                parents[entry.pid] = entry.parent
                ok = self.k.Process32NextW(handle, C.byref(entry))
            return parents
        finally:
            self.k.CloseHandle(handle)

    def windows(self, pins):
        samples = []
        def live(pid, check):
            handle, process = pins[pid]
            state, diagnostic = self.liveness(handle)
            if state == 'alive':
                return True
            if state == 'exited':
                samples.append(owned_exit(process))
            else:
                samples.append(dict(event='window_identity_ambiguous', candidate_pid=pid,
                                    check=check, **diagnostic))
            return False
        def sample(hwnd, _):
            pid = self.W.DWORD()
            if (self.u.GetWindowThreadProcessId(hwnd, C.byref(pid)) and pid.value in pins
                    and live(pid.value, 'before_visibility')):
                visible, again = bool(self.u.IsWindowVisible(hwnd)), self.W.DWORD()
                if (self.u.GetWindowThreadProcessId(hwnd, C.byref(again)) and again.value == pid.value
                        and live(pid.value, 'after_visibility')):
                    samples.append(dict(event='owned_window', hwnd=int(hwnd), owner=pins[pid.value][1], visible=visible))
            return True
        callback = self.callback(sample)
        self.check(self.u.EnumWindows(callback, 0))
        return samples

    def console(self, process, handle):
        # Called only for a retained verified object, in the console-free observer.
        def lifecycle_event():
            state, diagnostic = self.liveness(handle)
            if state == 'exited':
                return owned_exit(process)
            if state == 'unknown':
                return dict(event='console_identity_ambiguous', candidate_pid=process['pid'], **diagnostic)
            return None
        event = lifecycle_event()
        if event:
            return event
        if not self.k.AttachConsole(process['pid']):
            code = self.error_code()
            return lifecycle_event() or dict(event='console_unavailable', process=process,
                category='attach_failed', api='AttachConsole', winerror=code)
        try:
            self.k.SetConsoleCtrlHandler(None, True)  # never generate console control events
            event = lifecycle_event()
            if event:
                return event
            hwnd = self.k.GetConsoleWindow()
            pids = (self.W.DWORD * 128)()
            count = self.k.GetConsoleProcessList(pids, 128)
            if not count or count > 128:
                code = self.error_code() if not count else None
                return lifecycle_event() or dict(event='console_membership_unknown', process=process,
                    category='query_failed' if not count else 'membership_limit',
                    api='GetConsoleProcessList', winerror=code)
            owner = self.W.DWORD()
            if hwnd:
                self.u.GetWindowThreadProcessId(hwnd, C.byref(owner))
            # Console host can be outside our job. Query only the API-associated owner.
            host = self.process(owner.value) if owner.value else None
            event = lifecycle_event()
            if event:
                return event
            again = self.W.DWORD()
            if (process['pid'] not in pids[:count]
                    or (hwnd and (not self.u.GetWindowThreadProcessId(hwnd, C.byref(again)) or again.value != owner.value))):
                return dict(event='console_identity_ambiguous', candidate_pid=process['pid'],
                            category='console_identity_changed', api=None, winerror=None)
            return dict(event='console_association', process=process, hwnd=int(hwnd or 0),
                        owner=host, visible=bool(hwnd and self.u.IsWindowVisible(hwnd)),
                        attached_owned_pid_present=process['pid'] in pids[:count])
        finally:
            self.k.FreeConsole()


def spawn(root, role, stage, env, native, job, observation_job):
    # Child gates before any stage/observation work. A retained process HANDLE avoids PID reuse.
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--internal', role,
                              '--run-dir', str(root), '--stage', stage], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    try:
        creation = native.identity(int(child._handle))
        native.check(native.k.AssignProcessToJobObject(job, int(child._handle)))
        native.check(native.k.AssignProcessToJobObject(observation_job, int(child._handle)))
        if native.identity(int(child._handle)) != creation:
            raise RuntimeError('spawn identity changed')
        emit(root, 'owned_root', role=role, pid=child.pid, creation=creation)
        return child
    except BaseException:
        # Popen retains the exact process HANDLE even if PID reuse occurs.
        child.kill()
        child.wait(timeout=1)
        raise


def owned_exit(process):
    return dict(event='owned_process_exited', candidate_pid=process['pid'], creation=process['creation'],
                proof='retained_handle_signaled', api='WaitForSingleObject')


def metadata_exit_samples(pin, diagnostic):
    process = pin[1]
    uncertainty = dict(event='metadata_query_uncertain', candidate_pid=process['pid'],
                       **{k: v for k, v in diagnostic.items() if k != 'verified_exit'})
    exited = owned_exit(process)
    exited['proof'] = 'same_handle_signaled_after_metadata_query'
    return [uncertainty, exited]


def observer_pin(native, job, process, retained, cache):
    pid, diagnostic = process['pid'], {}
    if pid in cache:
        pin = cache[pid]
        state, diagnostic = native.retained_state(job, pin, process)
        return state, pin, diagnostic
    if len(cache) >= 128:
        return 'unknown', None, dict(category='retained_handle_limit', api=None, winerror=None)
    with ExitStack() as acquired:
        pin = acquired.enter_context(native.owned_process(job, pid, process, diagnostic=diagnostic))
        if pin is None:
            return 'unknown', None, diagnostic
        retained.enter_context(acquired.pop_all())
    cache[pid] = pin
    if diagnostic.get('verified_exit'):
        return 'metadata_exit', pin, diagnostic
    return 'alive', pin, {}


def observe(root, stage):
    native, seen = Native(), set()
    def record(samples):
        for sample in samples:
            event = sample['event']
            if event in ('console_identity_ambiguous', 'window_identity_ambiguous'):
                (root / 'STOP').touch()
            key = json.dumps(sample, sort_keys=True)
            if key not in seen:
                metadata = {k: v for k, v in sample.items() if k != 'event'}
                emit(root, event, stage=stage, **metadata)
                seen.add(key)
    native.k.FreeConsole()
    (root / 'observer-ready').touch()
    deadline = time.monotonic() + 10
    # Keep verified objects pinned for this bounded observer lifetime, including after exit.
    # A first unseen/unverifiable candidate still has no exit proof and must stop the capture.
    with ExitStack() as retained:
        cache = {}
        while not stop_reason(root / 'STOP', deadline):
            try:
                candidates = json.loads((root / 'members.json').read_text())
            except (OSError, ValueError):
                time.sleep(.02)
                continue
            pins, samples, job = {}, [], native.observation_job(root)
            try:
                for process in candidates:
                    state, pin, diagnostic = observer_pin(native, job, process, retained, cache)
                    if state == 'metadata_exit':
                        samples.extend(metadata_exit_samples(pin, diagnostic))
                    elif state == 'exited':
                        samples.append(owned_exit(pin[1]))
                    elif state == 'unknown':
                        emit(root, 'membership_identity_gap', stage=stage, observer=True,
                             candidate_pid=process['pid'], **diagnostic)
                        (root / 'STOP').touch()
                    else:
                        pins[process['pid']] = pin
            finally:
                # Query handle is never retained through attachment or sampling/blocking work.
                native.check(native.k.CloseHandle(job))
            samples.extend(native.windows(pins))
            # Persist uncertainty and request STOP before any later console queries.
            record(samples)
            for pid, (handle, process) in pins.items():
                if pid != os.getpid():
                    record([native.console(process, handle)])
            time.sleep(.01)


def fixture_server(root):
    """Only ping and empty guarded snapshot; no user service and no terminal/model."""
    endpoint = os.environ['HERDR_SOCKET_PATH']
    native = Native() if os.name == 'nt' else None
    if native:
        import msvcrt
        W = native.W
        native.k.CreateNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.c_void_p]
        native.k.CreateNamedPipeW.restype = W.HANDLE
        native.k.ConnectNamedPipe.argtypes = [W.HANDLE, C.c_void_p]
        native.k.ConnectNamedPipe.restype = W.BOOL
        native.k.DisconnectNamedPipe.argtypes = [W.HANDLE]
        native.k.DisconnectNamedPipe.restype = W.BOOL
        native.k.FlushFileBuffers.argtypes = [W.HANDLE]
        native.k.FlushFileBuffers.restype = W.BOOL
        handle = native.k.CreateNamedPipeW('\\\\.\\pipe\\' + endpoint,
                    3 | 0x80000, 8, 1, 4096, 4096, 0, None)
        if handle == native.W.HANDLE(-1).value:
            raise OSError('fixture pipe creation failed')
        base_fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        (root / 'server-ready').touch()
        listener = None
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(endpoint)
        os.chmod(endpoint, 0o600)
        listener.listen(1)
        listener.settimeout(2)
    # inspect guard needs three connections: ping, guarded snapshot, requested snapshot.
    for _ in range(3):
        if native:
            if not native.k.ConnectNamedPipe(handle, None) and C.get_last_error() != 535:
                raise OSError('fixture pipe connection failed')
            stream = os.fdopen(os.dup(base_fd), 'r+b', buffering=0)
            connection = None
        else:
            (root / 'server-ready').touch()
            connection, _ = listener.accept()
            stream = connection.makefile('rwb', buffering=0)
        try:
            request = json.loads(stream.readline(8192))
            method = request['method']
            if method == 'ping':
                result = dict(protocol=22, version='0.9.5')
            elif method == 'session.snapshot':
                result = dict(snapshot=dict(workspaces=[dict(workspace_id='fixture-workspace', tokens=dict(wfc_owner='fixture-nonce'))],
                    panes=[dict(pane_id='fixture-pane', workspace_id='fixture-workspace', terminal_id='fixture-terminal')]))
            else:
                raise ValueError('fixture refuses non-observation method')
            # Widen fixture lifetime only; documented perturbation, not timing evidence.
            time.sleep(.08)
            stream.write((json.dumps(dict(id=request['id'], result=result)) + '\n').encode())
            if native:
                native.check(native.k.FlushFileBuffers(handle))
        finally:
            stream.close()
            if native:
                native.check(native.k.DisconnectNamedPipe(handle))
            if connection:
                connection.close()
    if listener:
        listener.close()
    else:
        os.close(base_fd)


def run_stage(root, stage):
    import router
    import herdr_backend
    from unittest.mock import patch
    stop, deadline = root / 'STOP', time.monotonic() + 7
    emit(root, 'phase_begin', stage=stage)
    if stage == 'idle':
        # No rollout search and no controller network delivery: empty fresh scoped DB.
        result = subprocess.run([sys.executable, str(ROOT / 'router.py'), 'relay',
            '--opencode-url', 'http://127.0.0.1:1', '--opencode-session', 'fixture',
            '--scope', 'fixture-scope', '--for', '2', '--detach'], capture_output=True, timeout=3)
        if result.returncode:
            raise RuntimeError('idle launcher failed')
        relay = json.loads(result.stdout)
        emit(root, 'detached_relay', pid=relay['pid'], creation=wfc_platform.process_identity(relay['pid']))
        db = router.connect()
        while not stop_reason(stop, deadline):
            rows = db.execute('SELECT * FROM waiter').fetchall()
            if rows:
                emit(root, 'relay_registered', count=len(rows), scoped=True, owner_count=db.execute('SELECT count(*) FROM controller_owner').fetchone()[0])
                break
            time.sleep(.02)
        if not rows:
            raise RuntimeError('relay registration unobserved')
        time.sleep(2.2)
        if db.execute('SELECT count(*) FROM message').fetchone()[0] or db.execute('SELECT count(*) FROM dispatch').fetchone()[0]:
            raise RuntimeError('idle fixture unexpectedly has work')
        emit(root, 'relay_finished', waiter_count=db.execute('SELECT count(*) FROM waiter').fetchone()[0])
    elif stage == 'rpc-fixture':
        server = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--internal', 'server',
            '--run-dir', str(root), '--stage', stage], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while not (root / 'server-ready').exists():
                if stop_reason(stop, deadline) or server.poll() is not None:
                    raise RuntimeError('fixture server not ready')
                time.sleep(.02)
            identity = wfc_platform.process_identity(os.getpid())
            if not identity:
                raise RuntimeError('testworker identity unavailable')
            handle = dict(endpoint=os.environ['HERDR_SOCKET_PATH'], service=dict(pid=server.pid,
                creation=wfc_platform.process_identity(server.pid)), state='running', workspace='fixture-workspace',
                pane='fixture-pane', terminal='fixture-terminal', nonce='fixture-nonce',
                writerProcesses=[dict(pid=os.getpid(), creation=identity)])
            if not handle['service']['creation']:
                raise RuntimeError('fixture service identity unavailable')
            backend = herdr_backend.HerdrBackend(row=dict(backend_handle=json.dumps(handle)))
            observed = backend.inspect('fixture-worker')
            emit(root, 'rpc_result', result=observed['result'], alive=observed.get('alive'), owned=observed.get('owned'))
            if observed.get('result') != 'accepted' or not observed.get('alive'):
                raise RuntimeError('fixture observation failed')
            server.wait(timeout=1)
        finally:
            if server.poll() is None:
                server.kill()  # exact retained Popen handle; fixture server only
            server.wait(timeout=1)
    elif stage == 'queue-fixture':
        original_run, calls = subprocess.run, []
        def fixture_run(argv, **kwargs):
            if argv[:2] != ['codex', 'queue'] or calls:
                raise RuntimeError('queue fixture refuses extra or unexpected process')
            calls.append(True)
            emit(root, 'queue_launch', fixture=True, count=len(calls))
            return original_run([sys.executable, str(Path(__file__).resolve()), '--internal', 'queue',
                '--run-dir', str(root), '--stage', stage], **kwargs)
        with patch.object(router.subprocess, 'run', side_effect=fixture_run):
            outcome = router.queue_into_codex('fixture-thread', [dict(id=1)])
        emit(root, 'queue_result', result=outcome, launches=len(calls), fixture=True)
        if outcome != 'delivered' or len(calls) != 1:
            raise RuntimeError('queue fixture failed')
    elif stage == 'observer-selftest':
        # Positive control: one new console whose own HWND/owner can be compared to observer output.
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--internal', 'console',
            '--run-dir', str(root), '--stage', stage], creationflags=subprocess.CREATE_NEW_CONSOLE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=3)
    emit(root, 'phase_end', stage=stage)


def internal(args):
    root = args.run_dir.resolve()
    if args.internal in ('observer', 'phase'):
        if not wait_file(root / 'GO', root / 'STOP', time.monotonic() + 2):
            return 2
    if args.internal == 'observer':
        observe(root, args.stage)
    elif args.internal == 'phase':
        if not wait_file(root / 'observer-ready', root / 'STOP', time.monotonic() + 2):
            return 2
        # The subject is detached, matching the relay ancestry. Its children use original source flags.
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--internal', 'subject',
            '--run-dir', str(root), '--stage', args.stage], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **wfc_platform.detached_options())
        child.wait(timeout=8)
        return child.returncode
    elif args.internal == 'subject':
        run_stage(root, args.stage)
    elif args.internal == 'server':
        fixture_server(root)
    elif args.internal == 'queue':
        emit(root, 'queue_fixture_entered')
        time.sleep(.25)
    elif args.internal == 'console':
        n = Native()
        hwnd = n.k.GetConsoleWindow()
        owner = n.W.DWORD()
        n.u.GetWindowThreadProcessId(hwnd, C.byref(owner))
        emit(root, 'positive_control', pid=os.getpid(), creation=wfc_platform.process_identity(os.getpid()),
             hwnd=int(hwnd or 0), owner=n.process(owner.value))
        time.sleep(1.5)
    return 0


def supervise(args):
    if os.name != 'nt':
        raise SystemExit('REFUSED: native Windows is required; Linux is not console evidence')
    operation = 'resolve_run_dir'
    try:
        root = args.run_dir.resolve()
        operation = 'create_run_dir'
        root.mkdir(parents=True, exist_ok=False)  # never attach to existing state
        operation = 'write_fixture_config'
        (root / 'herdr.toml').write_text('[terminal]\ndefault_shell = "cmd.exe"\n', encoding='utf-8')
        operation = 'prepare_environment'
        env = isolated_env(root)
        operation = 'initialize_native_api'
        native = Native()
        operation = 'create_containment_job'
        job, children = native.job(), []
    except Exception as error:
        if args.run_dir.exists():
            emit(args.run_dir, 'supervisor_error', stage=args.stage, supervisor_phase='setup',
                 operation=operation, **exception_fields(error))
        raise  # preserve the entry point's existing failure/exit behavior
    observation_job = None
    deadline, reason = time.monotonic() + args.seconds, 'error'
    finished, watchdog, outcome = None, None, []
    observer_code, shutdown, cleanup = None, 'not_started', 'owned_job_unconfirmed'
    print('STOP now: create ' + str(root / 'STOP'), flush=True)
    operation = 'create_observation_job'
    try:
        observation_job = native.job(observe_only=True)
        operation = 'write_job_name'
        (root / 'job-name').write_text(native.job_name, encoding='utf-8')
        operation = 'duplicate_watchdog_handle'
        callback_job = native.duplicate_job(job)
        operation = 'start_watchdog'
        finished, watchdog, outcome = start_watchdog(root / 'STOP', deadline,
            lambda: native.check(native.k.TerminateJobObject(callback_job, 1)),
            lambda: native.check(native.k.CloseHandle(callback_job)))
        operation = 'check_watchdog_ready'
        if outcome:
            raise RuntimeError('watchdog unavailable before launch')
        operation = 'launch_observer'
        observer = spawn(root, 'observer', args.stage, env, native, job, observation_job)
        children.append(observer)
        operation = 'launch_phase'
        phase = spawn(root, 'phase', args.stage, env, native, job, observation_job)
        children.append(phase)
        operation = 'check_stage_gate'
        if outcome or stop_reason(root / 'STOP', deadline):
            raise RuntimeError('probe stopped before stage authorization')
        operation = 'authorize_stage'
        (root / 'GO').touch()
        known = {}
        while True:
            operation = 'check_stop'
            reason = outcome[0] if outcome else stop_reason(root / 'STOP', deadline)
            if reason:
                break
            with ExitStack() as handles:
                owned, gap = {}, False
                operation = 'enumerate_job_members'
                for pid in native.members(job):
                    diagnostic = {}
                    operation = 'acquire_owned_process'
                    pin = handles.enter_context(native.owned_process(job, pid, known.get(pid), diagnostic=diagnostic))
                    if pin is None:
                        known_pid = dict(candidate_pid=pid) if pid in known else {}
                        operation = 'record_membership_gap'
                        emit(root, 'membership_identity_gap', stage=args.stage, observer=False,
                             **diagnostic, **known_pid)
                        gap = True
                    elif diagnostic.get('verified_exit'):
                        # Only this same-handle, previously published object is skipped.
                        # Its API error remains evidence; it is not retroactive proof of cause.
                        for sample in metadata_exit_samples(pin, diagnostic):
                            event = sample.pop('event')
                            operation = 'record_metadata_exit'
                            emit(root, event, stage=args.stage, observer=False, **sample)
                    else:
                        owned[pid] = pin[1]
                temporary = root / 'members.next'
                operation = 'write_members_snapshot'
                temporary.write_text(json.dumps(list(owned.values())), encoding='utf-8')
                operation = 'publish_members_snapshot'
                os.replace(temporary, root / 'members.json')
                operation = 'query_parent_snapshot'
                parents = native.parents()
                for pid, process in owned.items():
                    if known.get(pid) != process:
                        parent = owned.get(parents.get(pid))
                        # A current pinned candidate is not historical parent proof.
                        if parent and int(parent['creation'].split(':')[1]) > int(process['creation'].split(':')[1]):
                            parent = None
                        operation = 'record_process_observation'
                        emit(root, 'process_observed', stage=args.stage, process=process, parent_candidate=parent)
                        known[pid] = process
                operation = 'release_sample_handles'
            if gap:
                reason = 'membership_identity_gap'
                break
            operation = 'poll_phase'
            phase_code = phase.poll()
            operation = 'poll_observer'
            observer_code = observer.poll()
            if observer_code is not None:  # failed/incomplete capture dominates simultaneous phase success
                reason = 'observer_failed'
                break
            if phase_code is not None:
                operation = 'recheck_observer'
                observer_code = observer.poll()  # recheck before accepting completion
                reason = ('observer_failed' if observer_code is not None else
                          ('completed' if phase_code == 0 else 'phase_failed'))
                operation = 'verify_positive_control'
                if reason == 'completed' and args.stage == 'observer-selftest' and not positive_control_seen(read_events(root)):
                    reason = 'positive_control_unobserved'
                break
            operation = 'observation_pause'
            time.sleep(.02)
    except Exception as error:
        reason = 'probe_error'
        emit(root, 'supervisor_error', stage=args.stage, supervisor_phase='run',
             operation=operation, **exception_fields(error))
    finally:
        # Cancellation fences future admissions. An admitted callback owns a separate handle
        # until its thread's finally, so an unresolved shutdown cannot use our closed handle.
        if finished is not None:
            finished.set()
            watchdog.join(timeout=1)
            shutdown = 'unresolved' if watchdog.is_alive() else 'joined'
            if shutdown == 'unresolved':
                reason = 'watchdog_shutdown_unresolved'
            elif 'watchdog_failed' in outcome:
                reason = 'watchdog_failed'
            elif outcome:
                reason = outcome[0]
        try:
            operation = 'terminate_owned_job'
            native.check(native.k.TerminateJobObject(job, 1))
            cleanup_deadline = time.monotonic() + 1
            operation = 'enumerate_cleanup_members'
            while native.members(job) and time.monotonic() < cleanup_deadline:
                operation = 'cleanup_pause'
                time.sleep(.01)
                operation = 'enumerate_cleanup_members'
            operation = 'verify_job_empty'
            if native.members(job):
                raise RuntimeError('owned job cleanup unconfirmed')
            for child in children:
                operation = 'wait_owned_root'
                child.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            cleanup = 'owned_job_empty'
        except Exception as error:
            reason = 'cleanup_failed'
            emit(root, 'cleanup_error', stage=args.stage, supervisor_phase='cleanup',
                 operation=operation, **exception_fields(error))
        finally:
            if observation_job is not None and not native.k.CloseHandle(observation_job):
                reason = 'cleanup_failed'
            if not native.k.CloseHandle(job):
                reason = 'cleanup_failed'
                cleanup = 'owned_job_unconfirmed'
        # The observer can request STOP after the final phase poll but before cleanup.
        # It is now stopped; a persisted failure marker cannot become completed.
        if reason == 'completed' and (root / 'STOP').exists():
            reason = 'STOP'
        # Choose a return/report only after bounded cancellation, callback join and cleanup.
        emit(root, 'supervisor_end', stage=args.stage, reason=reason, cleanup=cleanup,
             observer_returncode=observer_code, watchdog_shutdown=shutdown, watchdog_outcome=list(outcome))
        print('Probe ended: ' + reason + '; metadata: ' + str(root), flush=True)
    return 0 if reason == 'completed' and shutdown == 'joined' and cleanup == 'owned_job_empty' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--run-dir', type=Path, help='required new scratch directory; STOP file is immediate stop request')
    parser.add_argument('--seconds', type=float, default=6, help='hard supervisor deadline, 1..8 seconds (default 6)')
    parser.add_argument('--internal', choices=('observer', 'phase', 'subject', 'server', 'queue', 'console'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.stage or not args.run_dir or not 1 <= args.seconds <= 8:
        parser.error('--stage and --run-dir required; --seconds must be 1..8')
    try:
        return internal(args) if args.internal else supervise(args)
    except Exception as error:
        # Keep OS messages, arbitrary exception strings and paths out of shared evidence.
        if args.run_dir.exists():
            emit(args.run_dir, 'probe_error', stage=args.stage, supervisor_phase='entry',
                 operation='invoke_command', **exception_fields(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
