#!/usr/bin/env python3
"""Frozen diagnostic comparison, for a controller-arranged Windows run only.

Runs controlled Python/ConPTY I/O with guardian->runtime flags 0 / CREATE_NO_WINDOW.
Requires an explicitly supplied local kernel and SHA256; never builds/downloads.
Bounded SHOW hook covers new owned/terminal HWNDs with owned positive controls.
No actual dispatch, host account, model, terminal text or environment capture.
"""
import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def clock_anchor():
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetTickCount64.argtypes, kernel.GetTickCount64.restype = [], ctypes.c_uint64
    kernel.GetSystemTimeAdjustment.argtypes = [ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.BOOL)]
    kernel.GetSystemTimeAdjustment.restype = wt.BOOL
    before = now()
    uptime = kernel.GetTickCount64()
    adjustment, increment, disabled = wt.DWORD(), wt.DWORD(), wt.BOOL()
    known = kernel.GetSystemTimeAdjustment(ctypes.byref(adjustment), ctypes.byref(increment), ctypes.byref(disabled))
    padding = max(32, 2 * ((increment.value + 9999) // 10000)) if known and increment.value else None
    return {'utc_before': before, 'uptime_ms': uptime, 'utc_after': now(),
            'system_time_increment_100ns': increment.value if known else None,
            'tick_padding_ms': padding}


def self_identity():
    import wfc_platform
    probe = wfc_platform.windows_process(os.getpid())
    if probe['state'] != 'alive' or not probe['identity']:
        raise RuntimeError('identity unavailable')
    return {'pid': os.getpid(), 'creation': probe['identity']}


def console():
    """Console attachment snapshot, not a conhost/Terminal causal mapping."""
    from ctypes import wintypes as wt
    import wfc_platform
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetConsoleWindow.argtypes, kernel.GetConsoleWindow.restype = [], wt.HWND
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
    kernel.GetConsoleProcessList.restype = wt.DWORD
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user.GetWindowThreadProcessId.restype = wt.DWORD
    user.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    ids = (wt.DWORD * 128)()
    ctypes.set_last_error(0)
    count = kernel.GetConsoleProcessList(ids, 128)
    query_error = ctypes.get_last_error() if count == 0 else None
    hwnd = kernel.GetConsoleWindow()
    owner = wt.DWORD()
    name = ctypes.create_unicode_buffer(129)
    if hwnd:
        if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not user.GetClassNameW(hwnd, name, len(name)):
            raise ctypes.WinError(ctypes.get_last_error())
    return {'at': now(), 'hwnd': hex(hwnd or 0), 'class': name.value if hwnd else None,
            'owner': {'pid': owner.value, 'creation': wfc_platform.process_identity(owner.value)} if hwnd else None,
            'attached': [{'pid': pid, 'creation': wfc_platform.process_identity(pid)} for pid in ids[:count]] if 0 < count <= 128 else [],
            'status': ('buffer_insufficient' if count > 128 else
                       'available' if count else 'no_console_or_query_failed'),
            'query_error': query_error,
            'required_count': count,
            'truncated': count > 128}


def save(path, events):
    # Only controlled metadata is persisted in the unique temporary scope.
    path.write_text(json.dumps(events), encoding='utf-8')


def child(role, spec_path):
    import _winapi
    import wfc_platform
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    events = [{'phase': 'start', 'at': now(), 'identity': self_identity(),
               'basename': Path(sys.executable).name, 'console': console()}]
    destination = spec_path.parent / (role + '.json')
    save(destination, events)
    original = _winapi.CreateProcess

    def observed(*args):
        # Record the CPython _winapi boundary; argv/env/cwd are not recorded.
        # CPython/OS implementation additions still require native API tracing.
        result = original(*args)
        pid = result[2]
        events.append({'phase': 'python_createprocess_return', 'at': now(),
                       'parent': self_identity(), 'child': {'pid': pid, 'creation': wfc_platform.process_identity(pid)},
                       'python_createprocess_flags': args[5], 'inherit_handles': bool(args[4]),
                       'basename': Path(sys.executable).name if role == 'guardian' else Path(spec['kernel']).name,
                       'boundary': 'guardian-runtime' if role == 'guardian' else 'runtime-kernel'})
        save(destination, events)
        return result

    _winapi.CreateProcess = observed
    try:
        if role == 'guardian':
            # Parent registers Job containment before releasing this first action.
            if sys.stdin.buffer.read(1) != b'G':
                raise RuntimeError('unreleased gate')
            with subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                   '_runtime', str(spec_path)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=spec['runtime_flags'], close_fds=True) as process:
                code = process.wait(timeout=30)
            if code:
                raise RuntimeError('controlled runtime failed')
        else:
            # Reuse the existing real kernel protocol/ConPTY round trip. Its input
            # is generated control data and no terminal/model text is retained.
            sys.path.insert(0, str(ROOT / 'experiments/herdr-kernel-prototype'))
            import native_probe
            native_probe.run(Path(spec['kernel']))
            events.append({'phase': 'conpty_io_pass', 'at': now(), 'console': console()})
    except BaseException as error:
        events.append({'phase': 'failed', 'at': now(), 'error_type': type(error).__name__})
        raise
    finally:
        _winapi.CreateProcess = original
        save(destination, events)
    return 0


# Keep callback objects alive until successful unhook or CLI process exit.
_LIVE_HOOKS = []
TERMINAL_CLASSES = {'CASCADIA_HOSTING_WINDOW_CLASS', 'ConsoleWindowClass', 'PseudoConsoleWindow'}


class NativeWindowAPI:
    """Small native boundary: no titles, image paths, argv or environment reads."""
    def __init__(self, job_handle):
        from ctypes import wintypes as wt
        self.wt, self.job = wt, job_handle
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetWindowThreadProcessId': ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            'GetClassNameW': ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            'CreateWindowExW': ([wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p], wt.HWND),
            'ShowWindow': ([wt.HWND, ctypes.c_int], wt.BOOL),
            'DestroyWindow': ([wt.HWND], wt.BOOL),
            'UnhookWinEvent': ([wt.HANDLE], wt.BOOL),
            'PeekMessageW': ([ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL),
            'TranslateMessage': ([ctypes.POINTER(wt.MSG)], wt.BOOL),
            'DispatchMessageW': ([ctypes.POINTER(wt.MSG)], ctypes.c_ssize_t),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        for name, args, result in [
            ('OpenProcess', [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            ('GetProcessTimes', [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4, wt.BOOL),
            ('IsProcessInJob', [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)], wt.BOOL),
            ('WaitForSingleObject', [wt.HANDLE, wt.DWORD], wt.DWORD),
            ('CloseHandle', [wt.HANDLE], wt.BOOL),
            ('GetCurrentThreadId', [], wt.DWORD),
        ]:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.message, self.handle = wt.MSG(), None

    def owner_pid(self, hwnd):
        pid = self.wt.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread or not pid.value:
            raise ctypes.WinError(ctypes.get_last_error())
        return pid.value, thread

    def snapshot(self, hwnd):
        pid, _ = self.owner_pid(hwnd)
        name = ctypes.create_unicode_buffer(129)
        length = self.user.GetClassNameW(hwnd, name, len(name))
        if not length or length >= 128:
            raise RuntimeError('window class unavailable or truncated')
        process = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            times = [self.wt.FILETIME() for _ in range(4)]
            member = self.wt.BOOL()
            if not self.kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.IsProcessInJob(process, self.job, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            if self.owner_pid(hwnd)[0] != pid:
                raise RuntimeError('window owner changed during sample')
            ticks = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
            created, exited = ticks(times[0]), ticks(times[1])
            wait = self.kernel.WaitForSingleObject(process, 0)
            return {'hwnd': hex(hwnd), 'class': name.value,
                    'owner': {'pid': pid, 'creation': 'windows:' + str(created)},
                    'owner_sampled_at': now(), 'identity_binding': 'sampled_after_event',
                    'owner_lifetime': {'creation_filetime': created, 'exit_filetime': exited or None,
                                       'sampled_state': {0: 'exited', 258: 'alive'}.get(wait, 'unknown')},
                    'in_job': bool(member.value)}
        finally:
            if not self.kernel.CloseHandle(process):
                raise ctypes.WinError(ctypes.get_last_error())

    def start(self, callback):
        wt = self.wt
        callback_type = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND,
                                         wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
        self.callback = callback_type(lambda hook, event, hwnd, obj, child, thread, ms:
                                      callback(hwnd, obj, child, ms))
        self.user.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, callback_type,
                                            wt.DWORD, wt.DWORD, wt.DWORD]
        self.user.SetWinEventHook.restype = wt.HANDLE
        # Global SHOW is necessary for new broker HWNDs/owners. Filtering and
        # hard callback/time/storage bounds live in WindowObserver.
        self.handle = self.user.SetWinEventHook(0x8002, 0x8002, None, self.callback, 0, 0, 0)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        _LIVE_HOOKS.append(self)

    def pump(self):
        for _ in range(256):
            if not self.user.PeekMessageW(ctypes.byref(self.message), None, 0, 0, 1):
                break
            self.user.TranslateMessage(ctypes.byref(self.message))
            self.user.DispatchMessageW(ctypes.byref(self.message))

    def close(self):
        if self.handle:
            if not self.user.UnhookWinEvent(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None
            _LIVE_HOOKS.remove(self)

    def create_control(self):
        owner = self_identity()
        thread = self.kernel.GetCurrentThreadId()
        hwnd = self.user.CreateWindowExW(0, 'STATIC', None, 0x80000000,
                                        0, 0, 1, 1, None, None, None, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return {'hwnd': hex(hwnd), 'owner': owner, 'thread': thread}

    def show_control(self, control):
        self.user.ShowWindow(int(control['hwnd'], 16), 4)  # SW_SHOWNOACTIVATE

    def destroy_control(self, control):
        pid, thread = self.owner_pid(int(control['hwnd'], 16))
        if {'pid': pid, 'creation': self_identity()['creation']} != control['owner'] or thread != control['thread']:
            raise RuntimeError('positive window ownership changed')
        if not self.user.DestroyWindow(int(control['hwnd'], 16)):
            raise ctypes.WinError(ctypes.get_last_error())


class WindowObserver:
    """Bounded SHOW metadata, with owned positive controls before and after work."""
    def __init__(self, watch, job_handle):
        self.watch, self.events, self.controls = watch, [], []
        self.errors, self.reasons, self.lost = [], set(), 0
        self.callbacks, self.closed, self.configured = 0, False, False
        self.started = time.monotonic()
        self.clock_start = clock_anchor()
        self.api = NativeWindowAPI(job_handle)
        self.api.start(self.event)
        self.configured = True
        if watch:
            try:
                if self.api.snapshot(watch['hwnd'])['owner'] != {key: watch[key] for key in ('pid', 'creation')}:
                    raise RuntimeError('watch identity changed')
            except BaseException as error:
                self.errors.append({'phase': 'watch_identity', 'error_type': type(error).__name__})

    def timing(self, event_ms):
        anchor = clock_anchor()
        delay = ((anchor['uptime_ms'] & 0xffffffff) - event_ms) & 0xffffffff
        if delay > 40000:
            raise RuntimeError('event clock outside capture')
        before = datetime.fromisoformat(anchor['utc_before'].replace('Z', '+00:00'))
        after = datetime.fromisoformat(anchor['utc_after'].replace('Z', '+00:00'))
        if after < before:
            raise RuntimeError('clock reversed')
        padding = anchor.get('tick_padding_ms')
        if padding is None:
            self.reasons.add('tick_resolution_unknown')
        return {'event_ms_since_boot_low32': event_ms,
                'event_uptime_ms': anchor['uptime_ms'] - delay,
                'clock_anchor': anchor, 'tick_padding_ms': padding,
                'event_utc_earliest': (before - timedelta(milliseconds=delay + padding)).isoformat() if padding is not None else None,
                'event_utc_latest': (after - timedelta(milliseconds=delay - padding)).isoformat() if padding is not None else None,
                'received_at': now()}

    def event(self, hwnd, object_id, child_id, event_ms):
        if object_id != 0 or child_id != 0:
            return
        self.callbacks += 1
        if self.callbacks > 512 or time.monotonic() - self.started > 40:
            self.lost += 1
            self.reasons.add('capture_budget_exhausted')
            return
        try:
            record = self.api.snapshot(hwnd)
            control = next((item for item in reversed(self.controls)
                            if item['hwnd'] == record['hwnd'] and item['owner'] == record['owner']
                            and not item['destroyed']), None)
            watched = bool(self.watch and all(record['owner'][key] == self.watch[key] for key in ('pid', 'creation')))
            if control is None and not record['in_job'] and not watched and record['class'] not in TERMINAL_CLASSES:
                return  # No unrelated identity/class record is retained.
            timing = self.timing(event_ms)
            record.update(timing)
            lifetime = record['owner_lifetime']
            epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
            def ticks(value):
                delta = datetime.fromisoformat(value) - epoch
                return (delta.days * 86400 + delta.seconds) * 10000000 + delta.microseconds * 10
            earliest = ticks(timing['event_utc_earliest']) if timing['event_utc_earliest'] else None
            latest = ticks(timing['event_utc_latest']) if timing['event_utc_latest'] else None
            if (earliest is None or lifetime['sampled_state'] == 'unknown' or lifetime['creation_filetime'] > earliest
                    or (lifetime['exit_filetime'] is not None and lifetime['exit_filetime'] <= latest)):
                self.reasons.add('owner_lifetime_or_clock_uncertain')
            if control is not None:
                if control['show_uptime_ms'] is not None and timing['event_uptime_ms'] >= control['show_uptime_ms']:
                    control['seen'] = True
                    control['event'] = record
                else:
                    self.reasons.add('stale_positive_event')
                return
            if len(self.events) >= 64:
                self.lost += 1
                self.reasons.add('record_limit')
                return
            record['relation'] = ('owned_job' if record['in_job'] else
                                  'watched_owner' if watched else 'unattributed_terminal_candidate')
            self.events.append(record)
        except BaseException:
            self.lost += 1
            self.reasons.add('event_query_or_clock_failed')

    def pump(self):
        self.api.pump()
        if self.callbacks >= 512 or time.monotonic() - self.started > 40:
            self.reasons.add('capture_budget_exhausted')
            self.api.close()
            self.closed = True

    def positive(self, phase):
        control = None
        try:
            control = self.api.create_control()
            control.update(phase=phase, seen=False, destroyed=False, show_uptime_ms=None, created_at=None)
            self.controls.append(control)
            if any(item is not control and item['hwnd'] == control['hwnd'] for item in self.controls):
                raise RuntimeError('positive window handle reused')
            control['created_at'] = now()
            control['show_uptime_ms'] = clock_anchor()['uptime_ms']
            self.api.show_control(control)
            deadline = min(self.started + 40, time.monotonic() + 1)
            while not control['seen'] and time.monotonic() < deadline:
                self.pump()
                time.sleep(.01)
            if not control['seen']:
                self.reasons.add('positive_' + phase + '_missing')
        except BaseException as error:
            self.errors.append({'phase': 'positive_' + phase, 'error_type': type(error).__name__})
        finally:
            if control is not None:
                try:
                    self.api.destroy_control(control)
                    control['destroyed'] = True
                    control['destroyed_at'] = now()
                except BaseException as error:
                    self.errors.append({'phase': 'positive_destroy', 'error_type': type(error).__name__})
                    try:
                        self.api.destroy_control(control)
                        control['destroyed'] = True
                        control['destroyed_at'] = now()
                    except BaseException as retry_error:
                        self.errors.append({'phase': 'positive_destroy_retry', 'error_type': type(retry_error).__name__})

    def close(self):
        try:
            self.pump()
        finally:
            self.api.close()
            self.closed = True
        end = clock_anchor()
        begin = datetime.fromisoformat(self.clock_start['utc_before'].replace('Z', '+00:00'))
        finish = datetime.fromisoformat(end['utc_after'].replace('Z', '+00:00'))
        drift = abs((finish - begin).total_seconds() * 1000 - (end['uptime_ms'] - self.clock_start['uptime_ms']))
        self.clock_drift_ms = drift
        if drift > min(self.clock_start.get('tick_padding_ms') or 0, end.get('tick_padding_ms') or 0):
            self.reasons.add('wall_clock_mapping_uncertain')
        self.clock_end = end

    def summary(self):
        healthy = all(any(item['phase'] == phase and item['seen'] and item['destroyed']
                          for item in self.controls) for phase in ('before', 'after'))
        usable = self.configured and self.closed and healthy and not self.lost and not self.errors and not self.reasons
        return {'status': 'usable' if usable else 'incomplete', 'scope': 'bounded_global_show_filtered_metadata',
                'native_attribution': 'pending', 'configured': self.configured, 'hook_closed': self.closed,
                'positive_controls': self.controls, 'events': self.events, 'callbacks': self.callbacks,
                'lost_events': self.lost, 'reasons': sorted(self.reasons), 'errors': self.errors,
                'observed_clock_drift_ms': getattr(self, 'clock_drift_ms', None),
                'clock_start': self.clock_start, 'clock_end': getattr(self, 'clock_end', None)}


def trial(kernel, runtime_flags, watch, hold_gate=False, result=None):
    if result is None:
        result = new_trial(runtime_flags)
    scope = None
    try:
        import _winapi
        from bundled_runtime import WindowsContainment
        import wfc_platform
        result['entry'] = self_identity()
        result['clock_start'] = clock_anchor()
        scope = tempfile.TemporaryDirectory(prefix='wfc-foreground-')
        directory = Path(scope.name)
        spec = directory / 'controlled.json'
        spec.write_text(json.dumps({'kernel': str(kernel), 'runtime_flags': runtime_flags}), encoding='utf-8')
        job, guardian, observer = None, None, None
        try:
            job = WindowsContainment()
            observer = WindowObserver(watch, job.handle)
            observer.positive('before')
            original = _winapi.CreateProcess
            def observed(*arguments):
                result['entry_guardian_python_flags'] = arguments[5]
                return original(*arguments)
            _winapi.CreateProcess = observed
            try:
                guardian = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_guardian', str(spec)],
                                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            **wfc_platform.detached_options())
                result['root_created'] = True
            finally:
                _winapi.CreateProcess = original
            # Gate ensures descendants cannot start before Job attachment.
            job.attach(guardian.pid)
            result['guardian'] = {'pid': guardian.pid, 'creation': wfc_platform.process_identity(guardian.pid)}
            if not result['guardian']['creation']:
                raise RuntimeError('guardian identity unavailable')
            if not hold_gate:
                guardian.stdin.write(b'G')
                guardian.stdin.close()
            deadline = time.monotonic() + (2 if hold_gate else 35)
            while guardian.poll() is None and time.monotonic() < deadline:
                observer.pump()
                time.sleep(.01)
            if guardian.poll() is None:
                raise TimeoutError('trial deadline')
            result['guardian_exit'] = guardian.returncode
            if guardian.returncode:
                raise RuntimeError('guardian failed')
        except BaseException as error:
            result['errors'].append({'phase': 'trial', 'error_type': type(error).__name__})
        finally:
            if observer:
                try:
                    observer.positive('after')
                except BaseException as error:
                    result['errors'].append({'phase': 'positive_after', 'error_type': type(error).__name__})
                try:
                    observer.close()
                except BaseException as error:
                    result['errors'].append({'phase': 'unhook', 'error_type': type(error).__name__})
                try:
                    result['window_observation'] = observer.summary()
                    if any(error['phase'].startswith('positive_destroy')
                           for error in result['window_observation'].get('errors', [])):
                        result['errors'].append({'phase': 'positive_window_cleanup', 'error_type': 'CleanupError'})
                except BaseException as error:
                    result['errors'].append({'phase': 'observation_report', 'error_type': type(error).__name__})
            if job:
                try:
                    job.stop(lambda phase, **data: result['cleanup'].append({'phase': phase, **data}))
                except BaseException as error:
                    result['errors'].append({'phase': 'job_stop', 'error_type': type(error).__name__})
                try:
                    if not job.kernel.CloseHandle(job.handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                except BaseException as error:
                    result['errors'].append({'phase': 'job_close', 'error_type': type(error).__name__})
            if guardian:
                try:
                    guardian.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    result['errors'].append({'phase': 'root_reap', 'error_type': 'TimeoutExpired'})
                    try:
                        guardian.kill()
                    except BaseException as error:
                        result['errors'].append({'phase': 'root_kill', 'error_type': type(error).__name__})
                    try:
                        guardian.wait(timeout=5)
                    except BaseException as error:
                        result['errors'].append({'phase': 'root_emergency_reap', 'error_type': type(error).__name__})
                except BaseException as error:
                    result['errors'].append({'phase': 'root_reap', 'error_type': type(error).__name__})
                finally:
                    try:
                        guardian.stdin.close()
                    except BaseException as error:
                        result['errors'].append({'phase': 'root_pipe_close', 'error_type': type(error).__name__})
                result['root_reaped'] = guardian.returncode is not None
        result['stages'] = {}
        for role in ('guardian', 'runtime'):
            path = directory / (role + '.json')
            try:
                if path.exists():
                    result['stages'][role] = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as error:
                result['errors'].append({'phase': role + '_evidence_read', 'error_type': type(error).__name__})
    except BaseException as error:
        result['errors'].append({'phase': 'trial_setup_or_evidence', 'error_type': type(error).__name__})
    finally:
        if scope is not None:
            try:
                scope.cleanup()
            except BaseException as error:
                result['errors'].append({'phase': 'temporary_cleanup', 'error_type': type(error).__name__})
        result['end'] = now()
        try:
            result['clock_end'] = clock_anchor()
        except BaseException as error:
            result['errors'].append({'phase': 'clock_end', 'error_type': type(error).__name__})
    return result


def new_trial(runtime_flags):
    return {'runtime_flags_requested': runtime_flags, 'start': now(),
            'errors': [], 'cleanup': [], 'stages': {}, 'root_created': False,
            'window_observation': {'status': 'incomplete', 'native_attribution': 'pending',
                                   'reasons': ['observation_not_initialized']}}


def main():
    if len(sys.argv) == 3 and sys.argv[1] in ('_guardian', '_runtime'):
        return child(sys.argv[1][1:], Path(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', required=True, type=Path)
    parser.add_argument('--kernel-sha256', required=True)
    parser.add_argument('--watch-pid', type=int)
    parser.add_argument('--watch-creation')
    parser.add_argument('--watch-hwnd', type=lambda value: int(value, 16))
    parser.add_argument('--control', choices=('normal', 'hold-guardian-gate'), default='normal')
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'verdict': 'pending', 'reason': 'native_windows_required'}))
        return 2
    results, errors = [], []
    try:
        kernel = args.kernel.resolve(strict=True)
        if hashlib.sha256(kernel.read_bytes()).hexdigest() != args.kernel_sha256:
            raise ValueError('kernel hash mismatch')
        selected = [args.watch_pid is not None, args.watch_creation is not None, args.watch_hwnd is not None]
        if any(selected) and not all(selected):
            raise ValueError('incomplete watch identity')
        watch = {'pid': args.watch_pid, 'creation': args.watch_creation, 'hwnd': args.watch_hwnd} if all(selected) else None
        hold_gate = args.control == 'hold-guardian-gate'
        for flags in ((0,) if hold_gate else (0, 0x08000000)):
            result = new_trial(flags)
            results.append(result)
            if len(results) > 1:
                prior = results[-2]
                uncertain = {'job_stop', 'job_close', 'unhook', 'observation_report', 'positive_window_cleanup'}
                controls = prior['window_observation'].get('positive_controls', [])
                if (any(error['phase'] in uncertain for error in prior['errors'])
                        or (prior.get('root_created') and not prior.get('root_reaped'))
                        or any(not item['destroyed'] for item in controls)):
                    result['errors'].append({'phase': 'previous_cleanup_unconfirmed', 'error_type': 'SkippedTrial'})
                    continue
            try:
                trial(kernel, flags, watch, hold_gate, result=result)
            except BaseException as error:
                result['errors'].append({'phase': 'unexpected_trial', 'error_type': type(error).__name__})
        def complete(result):
            guardian = result['stages'].get('guardian', [])
            runtime = result['stages'].get('runtime', [])
            return (not result['errors'] and result.get('root_reaped')
                    and any(event['phase'] == 'job_empty' for event in result['cleanup'])
                    and any(event['phase'] == 'conpty_io_pass' for event in runtime)
                    and any(event['phase'] == 'python_createprocess_return'
                            and event['python_createprocess_flags'] == result['runtime_flags_requested']
                            and event['child']['creation'] for event in guardian)
                    and any(event['phase'] == 'python_createprocess_return'
                            and event['child']['creation'] for event in runtime))
        io_passed = all(complete(result) for result in results)
        windows_usable = all(not result['errors'] and result['window_observation']['status'] == 'usable'
                             for result in results)
        passed = io_passed and windows_usable
        print(json.dumps({'io_flags_result': 'pass' if io_passed else 'fail',
                          'window_observation_result': 'usable' if windows_usable else 'incomplete',
                          'native_attribution': 'pending', 'trials': results}))
        return 0 if passed else 1
    except BaseException as error:
        errors.append({'phase': 'comparison', 'error_type': type(error).__name__})
        print(json.dumps({'io_flags_result': 'fail', 'window_observation_result': 'incomplete', 'errors': errors,
                          'native_attribution': 'pending', 'trials': results}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
