"""Owned, per-execution runtime for the opt-in bundled PTY kernel.

JSON frames over loopback TCP, capability-authenticated before any kernel action.
The guardian owns Linux cgroup cleanup; the runtime owns the native Windows Job.
Neither endpoint nor process IDs alone authorize an operation. No request replay.
"""
import ctypes
from collections import deque
import hmac
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

import host_launch
import wfc_platform

LIMIT = 2 * 1024 * 1024
TIMEOUT = 6
PREAUTH_LIMIT = 256
PREAUTH_TIMEOUT = .5
FRAME_TIMEOUT = 2


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def publish(path, value):
    """Atomic, private snapshots; all paths originate in the private launch spec."""
    host_launch.write_private(path, value)


def capability_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def capability_matches(handle, snapshot):
    token, expected = snapshot.get('token'), handle.get('capabilityHash')
    return (isinstance(token, str) and isinstance(expected, str)
            and hmac.compare_digest(capability_digest(token), expected))


def receipt_payload(snapshot):
    return json.dumps({key: value for key, value in snapshot.items()
                       if key not in ('token', 'receiptSignature')},
                      sort_keys=True, separators=(',', ':')).encode()


def publish_cleanup(spec, snapshot):
    # The private capability is established before launch; its hash stays bound
    # in the router handle. A trace or a missing PID never supplies this proof.
    snapshot = {**snapshot, 'token': spec['token'], 'capabilityHash': capability_digest(spec['token'])}
    snapshot['receiptSignature'] = hmac.new(spec['token'].encode(), receipt_payload(snapshot), hashlib.sha256).hexdigest()
    publish(spec['snapshot'], snapshot)


def confirmed_cleanup(handle, snapshot):
    try:
        if (snapshot.get('state') not in ('closed', 'exited', 'faulted') or snapshot.get('cleanup') != 'confirmed'
                or not capability_matches(handle, snapshot)):
            return False
        if any(not handle.get(key) or snapshot.get(key) != handle[key]
               for key in ('unit', 'generation', 'attempt', 'nonce')):
            return False
        for key in ('service', 'kernel', 'guardian', 'containment'):
            if handle.get(key) and snapshot.get(key) != handle[key]:
                return False
        signature = snapshot.get('receiptSignature')
        expected = hmac.new(snapshot['token'].encode(), receipt_payload(snapshot), hashlib.sha256).hexdigest()
        return isinstance(signature, str) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


class LifecycleTrace:
    """Opt-in bounded diagnostics. Never read by ownership/cleanup decisions."""
    FIELDS = {'pid', 'creation', 'code', 'error_type', 'errno', 'winerror', 'active', 'total', 'terminated'}

    def __init__(self, spec, role):
        self.path = Path(spec['snapshot']).with_suffix('.' + role + '.trace.json') if spec.get('diagnostics') else None
        self.identity = {key: spec[key] for key in ('unit', 'generation', 'attempt')}
        self.role = role
        self.events = deque(maxlen=48)
        self.started = time.monotonic()

    def record(self, phase, **fields):
        if self.path is None:
            return
        safe = {key: value[:128] if isinstance(value, str) else value for key, value in fields.items()
                if key in self.FIELDS and (value is None or isinstance(value, (str, int, float, bool)))}
        self.events.append({'phase': phase[:64], 'elapsed': round(time.monotonic() - self.started, 6), **safe})
        try:
            publish(self.path, {'version': 1, **self.identity, 'role': self.role, 'events': list(self.events)})
        except OSError:
            pass  # Evidence loss must not change lifecycle semantics.


def trace_error(trace, phase, error):
    trace.record(phase, error_type=type(error).__name__, errno=getattr(error, 'errno', None),
                 winerror=getattr(error, 'winerror', None))


def budget(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('bundled connection deadline exceeded')
    return seconds


def remaining(stream, deadline):
    stream.settimeout(budget(deadline))


def receive(stream, *, limit=LIMIT, deadline=None):
    # A complete frame, including its header, shares one absolute deadline.
    if deadline is None:
        deadline = time.monotonic() + (stream.gettimeout() or FRAME_TIMEOUT)
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            remaining(stream, deadline)
            part = stream.recv(size - len(chunks))
            if not part:
                raise EOFError('incomplete bundled frame')
            chunks.extend(part)
        remaining(stream, deadline)
        return chunks
    size = struct.unpack('!I', exact(4))[0]
    if not 0 < size <= limit:
        raise ValueError('bundled frame exceeds size limit')
    try:
        value = json.loads(exact(size).decode('utf-8'))
    except RecursionError:
        raise ValueError('bundled JSON nesting exceeds parser bounds') from None
    if not isinstance(value, dict):
        raise ValueError('bundled frame must be an object')
    return value


def encode(value):
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(raw) > LIMIT:
        raise ValueError('bundled frame exceeds size limit')
    return struct.pack('!I', len(raw)) + raw


def call(handle, op, **data):
    """Single bounded attempt. A failed send or response is never made replayable."""
    sent = False
    phase = 'identity'
    try:
        if not handle.get('service') or not handle.get('endpoint'):
            raise ValueError('bundled runtime identity is incomplete')
        peer = handle['service']
        if wfc_platform.process_identity(peer['pid']) != peer['creation']:
            return {'result': 'uncertain', 'category': 'bundled_restart',
                    'diagnostic': 'bundled runtime creation identity changed; explicit reconciliation required'}
        ident = uuid.uuid4().hex
        payload = {key: handle[key] for key in ('token', 'generation', 'unit', 'nonce', 'attempt')}
        payload.update(id=ident, op=op, **data)
        frame = encode(payload)
        deadline = time.monotonic() + TIMEOUT
        phase = 'connect'
        with socket.create_connection(('127.0.0.1', handle['endpoint']), timeout=TIMEOUT) as stream:
            remaining(stream, deadline)
            phase = 'challenge'
            challenge = uuid.uuid4().hex
            stream.sendall(encode({'challenge': challenge}))
            proof = receive(stream, limit=PREAUTH_LIMIT, deadline=deadline).get('proof', '')
            expected = hmac.new(handle['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(proof), expected):
                raise ValueError('bundled server capability proof failed')
            remaining(stream, deadline)
            phase = 'authenticate'
            stream.sendall(encode({'token': handle['token']}))
            remaining(stream, deadline)
            phase = 'request'
            sent = True  # sendall may partially deliver before raising
            stream.sendall(frame)
            phase = 'response'
            response = receive(stream, deadline=deadline)
        phase = 'response_identity'
        if response.get('id') != ident or response.get('generation') != handle['generation']:
            raise ValueError('bundled response identity mismatch')
        if response.get('result') not in ('accepted', 'completed', 'refused', 'uncertain', 'definitely-not-sent'):
            raise ValueError('invalid bundled response')
        return response
    except (OSError, EOFError, ValueError, KeyError, TypeError) as error:
        return {'result': 'uncertain' if sent else 'definitely-not-sent', 'category': 'bundled_unavailable',
                'diagnostic': 'bundled runtime observation failed at ' + phase + ' (' + type(error).__name__ + '); no request was replayed'}


def cgroup_root(value):
    """Fail before launching if Linux cannot contain arbitrary process descendants."""
    if not sys.platform.startswith('linux'):
        raise OSError('bundled containment is supported only on Linux cgroup v2 and native Windows')
    if not value:
        raise OSError('bundled Linux requires WFC_BUNDLED_CGROUP_ROOT: a delegated cgroup v2 subtree with cgroup.kill')
    path = Path(value).resolve(strict=True)
    # Resolve the actual cgroup filesystem mount, not an arbitrary lookalike directory.
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, separator, after = line.partition(' - ')
        if separator and after.split()[0] == 'cgroup2':
            mount = before.split()[4].replace('\\040', ' ').replace('\\134', '\\')
            mounts.append(Path(mount).resolve())
    if not any(path == mount or mount in path.parents for mount in mounts):
        raise OSError('bundled cgroup root must be on the cgroup v2 filesystem')
    if not (path / 'cgroup.kill').is_file() or not os.access(path, os.W_OK):
        raise OSError('bundled requires writable delegated cgroup v2 with cgroup.kill; process groups are insufficient')
    return path


class LinuxContainment:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            os.close(fd)
        except BaseException:
            os.close(self.fd)
            raise

    def attach(self, pid):
        # The kernel has produced no children before receiving its first spawn command.
        fd = os.open('cgroup.procs', os.O_WRONLY, dir_fd=self.fd)
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(pid))

    def stop(self, trace=None):
        record = trace or (lambda *args, **kwargs: None)
        record('cgroup_kill_begin')
        fd = os.open('cgroup.kill', os.O_WRONLY, dir_fd=self.fd)
        with os.fdopen(fd, 'w') as stream:
            stream.write('1')
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            fd = os.open('cgroup.events', os.O_RDONLY, dir_fd=self.fd)
            with os.fdopen(fd) as stream:
                if 'populated 0' in stream.read().splitlines():
                    record('cgroup_empty', active=0)
                    return
            time.sleep(.02)
        raise TimeoutError('bundled cgroup remained populated after kill')

    def dispose(self):
        os.close(self.fd)


class WindowsContainment:
    """Assign the idle kernel before spawn; the sole Job handle stays in the runtime.

    TerminateJobObject includes descendants after a PTY parent exits. OS handle
    closure on runtime death enforces KILL_ON_JOB_CLOSE, including forced death.
    Native ConPTY/Job nesting still requires the experimental CI acceptance suite.
    """
    def __init__(self):
        from ctypes import wintypes as wt
        class Basic(ctypes.Structure):
            _fields_ = [('process', ctypes.c_int64), ('job', ctypes.c_int64), ('flags', wt.DWORD),
                        ('min', ctypes.c_size_t), ('max', ctypes.c_size_t), ('active', wt.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wt.DWORD), ('scheduling', wt.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ro', 'wo', 'oo', 'rt', 'wt', 'ot')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE),
            'SetInformationJobObject': ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL),
            'AssignProcessToJobObject': ([wt.HANDLE, wt.HANDLE], wt.BOOL),
            'OpenProcess': ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            'CloseHandle': ([wt.HANDLE], wt.BOOL),
            'TerminateJobObject': ([wt.HANDLE, wt.UINT], wt.BOOL),
            'QueryInformationJobObject': ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p], wt.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.dispose()
            raise error

    def attach(self, pid):
        process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def stop(self, trace=None):
        record = trace or (lambda *args, **kwargs: None)
        record('job_terminate_begin')
        if not self.kernel.TerminateJobObject(self.handle, 1):
            code = ctypes.get_last_error()
            record('job_terminate_failed', winerror=code)
            raise ctypes.WinError(code)
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs, four DWORDs.
        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_int64 * 4), ('faults', ctypes.c_uint32),
                        ('total', ctypes.c_uint32), ('active', ctypes.c_uint32), ('terminated', ctypes.c_uint32)]
        deadline = time.monotonic() + TIMEOUT
        previous = None
        while time.monotonic() < deadline:
            info = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                code = ctypes.get_last_error()
                record('job_query_failed', winerror=code)
                raise ctypes.WinError(code)
            if info.active != previous:
                record('job_accounting', active=info.active, total=info.total, terminated=info.terminated)
                previous = info.active
            if info.active == 0:
                record('job_empty', active=0)
                return
            time.sleep(.02)
        record('job_cleanup_timeout', active=previous)
        raise TimeoutError('bundled Job remained populated after termination')

    def dispose(self):
        self.kernel.CloseHandle(self.handle)


class Kernel:
    def __init__(self, binary, containment, env):
        self.child = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, close_fds=True)
        self.responses = queue.Queue(maxsize=2)
        def drain():
            try:
                while True:
                    raw = self.child.stdout.readline(LIMIT + 1)
                    if not raw or len(raw) > LIMIT:
                        break
                    self.responses.put(json.loads(raw), timeout=TIMEOUT)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    self.responses.put(None, timeout=TIMEOUT)
                except queue.Full:
                    pass
        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()
        try:
            containment.attach(self.child.pid)
            hello = self.responses.get(timeout=TIMEOUT)
            if (not isinstance(hello, dict) or hello.get('event') != 'hello' or hello.get('protocol') != 1
                    or not isinstance(hello.get('generation'), str) or not hello['generation']):
                raise ValueError('invalid bundled kernel hello')
            self.generation = hello['generation']
        except BaseException:
            self.child.kill()
            self.child.wait(timeout=TIMEOUT)
            raise

    def call(self, op, spec, *, deadline=None, **data):
        ident = uuid.uuid4().hex
        request = {'id': ident, 'generation': self.generation, 'unit': spec['unit'],
                   'nonce': spec.get('kernel_nonce'), 'op': op, **data}
        try:
            self.child.stdin.write((json.dumps(request) + '\n').encode())
            self.child.stdin.flush()
            result = self.responses.get(timeout=min(TIMEOUT, budget(deadline)) if deadline else TIMEOUT)
        except (OSError, queue.Empty) as error:
            raise RuntimeError('bundled kernel transport became uncertain') from error
        if not result or result.get('id') != ident or result.get('generation') != self.generation:
            raise RuntimeError('bundled kernel response identity lost')
        return result


def run(spec_path):
    spec = read_json(spec_path)
    trace = LifecycleTrace(spec, 'runtime')
    trace.record('runtime_start', pid=os.getpid(), creation=wfc_platform.process_identity(os.getpid()))
    Path(spec_path).unlink()
    containment = WindowsContainment() if wfc_platform.WINDOWS else LinuxContainment(spec['cgroup'])
    kernel = None
    closed = False
    try:
        kernel = Kernel(spec['binary'], containment, spec['env'])
        trace.record('kernel_ready', pid=kernel.child.pid, creation=wfc_platform.process_identity(kernel.child.pid))
        spawn = kernel.call('spawn', spec, argv=spec['argv'], cwd=spec['cwd'], cols=120, rows=40)
        if not spawn.get('ok'):
            raise OSError('bundled kernel refused worker creation')
        spec['kernel_nonce'] = spawn['result']['nonce']
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(8)
            listener.settimeout(.2)
            handle = {key: spec[key] for key in ('token', 'generation', 'unit', 'nonce', 'attempt', 'snapshot', 'capabilityHash')}
            identity = {'kind': 'windows-job'} if wfc_platform.WINDOWS else {
                'kind': 'cgroup-v2', 'path': spec['cgroup'],
                'device': os.fstat(containment.fd).st_dev, 'inode': os.fstat(containment.fd).st_ino}
            handle.update(containment=identity, guardian=spec['guardian'], endpoint=listener.getsockname()[1], state='active',
                          service={'pid': os.getpid(), 'creation': wfc_platform.process_identity(os.getpid())},
                          kernel={'pid': kernel.child.pid, 'creation': wfc_platform.process_identity(kernel.child.pid)},
                          kernel_generation=kernel.generation, worker_pid=spawn['result']['pid'])
            publish(spec['snapshot'], handle)
            next_observation = 0.0
            while True:
                if kernel.child.poll() is not None:
                    trace.record('kernel_ended', code=kernel.child.returncode)
                    raise OSError('bundled kernel exited; live terminal recovery is unavailable')
                if time.monotonic() >= next_observation:
                    view = kernel.call('read', spec)
                    next_observation = time.monotonic() + .2
                    if not view.get('ok') or view['result'].get('read_error'):
                        raise OSError('bundled terminal observation failed')
                    if view['result'].get('exit') is not None:
                        trace.record('host_exit_observed', code=view['result']['exit'].get('code'))
                        containment.stop(trace=trace.record)
                        kernel.child.wait(timeout=TIMEOUT)
                        closed = True
                        publish_cleanup(spec, {**handle, 'state': 'exited', 'cleanup': 'confirmed',
                                                  'final_screen': view['result']['text']})
                        trace.record('exit_cleanup_published', code=kernel.child.returncode)
                        break
                try:
                    stream, _ = listener.accept()
                except socket.timeout:
                    continue
                with stream:
                    connection_deadline = time.monotonic() + TIMEOUT
                    preauth_deadline = min(connection_deadline, time.monotonic() + PREAUTH_TIMEOUT)
                    close_requested = False
                    try:
                        challenge = receive(stream, limit=PREAUTH_LIMIT, deadline=preauth_deadline).get('challenge')
                        if not isinstance(challenge, str) or len(challenge) != 32:
                            raise ValueError('invalid bundled challenge')
                        proof = hmac.new(spec['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
                        remaining(stream, preauth_deadline)
                        stream.sendall(encode({'proof': proof}))
                        # Authenticate a small capability frame before accepting a large operation.
                        auth = receive(stream, limit=PREAUTH_LIMIT, deadline=preauth_deadline)
                        if not hmac.compare_digest(str(auth.get('token', '')), spec['token']):
                            continue
                        request = receive(stream, deadline=min(connection_deadline, time.monotonic() + FRAME_TIMEOUT))
                        if (not hmac.compare_digest(str(request.get('token', '')), spec['token'])
                                or any(request.get(key) != spec[key] for key in ('generation', 'unit', 'nonce', 'attempt'))):
                            result = {'result': 'refused', 'category': 'bundled_ownership',
                                      'diagnostic': 'bundled execution identity refused'}
                        elif request['op'] == 'close':
                            trace.record('close_received')
                            containment.stop(trace=trace.record)
                            kernel.child.wait(timeout=TIMEOUT)
                            trace.record('close_kernel_waited', code=kernel.child.returncode)
                            closed = True
                            close_requested = True
                            publish_cleanup(spec, {**handle, 'state': 'closed', 'cleanup': 'confirmed'})
                            trace.record('close_cleanup_published')
                            result = {'result': 'completed'}
                        elif request['op'] in ('read', 'input', 'resize'):
                            extra = {key: request[key] for key in ('text', 'cols', 'rows') if key in request}
                            if request['op'] == 'input' and request.get('text') == '\x03':
                                trace.record('interrupt_received')
                            response = kernel.call(request['op'], spec, deadline=connection_deadline, **extra)
                            if request['op'] == 'input' and request.get('text') == '\x03':
                                trace.record('interrupt_kernel_replied', code=int(bool(response.get('ok'))))
                            if response.get('ok'):
                                result = {'result': 'accepted', 'data': response['result']}
                            else:
                                # Input errors may follow a partial PTY write. Never permit replay.
                                result = {'result': 'uncertain' if request['op'] == 'input' else 'refused',
                                          'diagnostic': 'bundled kernel operation failed'}
                        else:
                            result = {'result': 'refused', 'diagnostic': 'unknown bundled operation'}
                        if close_requested:
                            trace.record('close_reply_begin')
                        remaining(stream, connection_deadline)
                        stream.sendall(encode({**result, 'id': request.get('id'), 'generation': spec['generation']}))
                        if close_requested:
                            trace.record('close_reply_sent')
                    except (OSError, EOFError, ValueError, KeyError, TypeError, queue.Empty) as error:
                        trace_error(trace, 'connection_error', error)
                        # A disconnected client cannot cancel or replay a request already delivered.
                        pass
                    if close_requested:
                        break
    except BaseException as error:
        trace_error(trace, 'runtime_error', error)
        raise
    finally:
        try:
            if not closed:
                trace.record('final_cleanup_begin')
                containment.stop(trace=trace.record)
                if kernel:
                    kernel.child.wait(timeout=TIMEOUT)
                publish_cleanup(spec, {**(handle if 'handle' in locals() else {key: spec[key] for key in
                        ('generation', 'unit', 'nonce', 'attempt', 'guardian')}),
                        'state': 'faulted', 'cleanup': 'confirmed'})
                trace.record('fault_cleanup_published')
        finally:
            containment.dispose()
            Path(spec_path).unlink(missing_ok=True)
            Path(spec['host_spec']).unlink(missing_ok=True)


def guard(spec_path):
    """Linux guardian survives runtime/kernel death and kills the owned cgroup.

    Killing the guardian itself AND runtime is outside this userspace guarantee.
    A privileged process can move itself out of a delegated subtree; this is process
    lifecycle containment, not a security sandbox for a hostile same-user worker.
    """
    spec = read_json(spec_path)
    trace = LifecycleTrace(spec, 'guardian')
    trace.record('guardian_start', pid=os.getpid(), creation=wfc_platform.process_identity(os.getpid()))
    containment = None
    try:
        if not wfc_platform.WINDOWS:
            root = cgroup_root(spec['cgroup_root'])
            path = root / ('wfc-' + spec['generation'])
            path.mkdir(mode=0o700)
            containment = LinuxContainment(path)
            spec['cgroup'] = str(path)
            publish(spec_path, spec)
        spec['guardian'] = {'pid': os.getpid(), 'creation': wfc_platform.process_identity(os.getpid())}
        publish(spec_path, spec)
        import router
        child = subprocess.Popen([sys.executable, str(router.PROGRAM), '_bundled-runtime', str(spec_path)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
        trace.record('runtime_started', pid=child.pid, creation=wfc_platform.process_identity(child.pid))
        code = child.wait()
        trace.record('runtime_ended', code=code)
    finally:
        cleaned = False
        if containment:
            try:
                containment.stop(trace=trace.record)
                cleaned = True
            finally:
                containment.dispose()
                try:
                    Path(spec['cgroup']).rmdir()
                except OSError:
                    pass
        try:
            snapshot = read_json(spec['snapshot'])
        except (OSError, ValueError):
            snapshot = {key: spec[key] for key in ('generation', 'unit', 'nonce', 'attempt')}
        if not confirmed_cleanup({**spec, 'capabilityHash': capability_digest(spec['token'])}, snapshot):
            snapshot = {**snapshot, 'state': 'faulted', 'cleanup': 'confirmed' if cleaned else 'unknown'}
            if cleaned:
                publish_cleanup(spec, snapshot)
            else:
                snapshot.pop('receiptSignature', None)
                publish(spec['snapshot'], snapshot)
        trace.record('guardian_finished', code=int(cleaned))
        Path(spec_path).unlink(missing_ok=True)
        Path(spec['host_spec']).unlink(missing_ok=True)
