"""Owned, per-execution runtime for the opt-in bundled PTY kernel.

JSON frames over loopback TCP, capability-authenticated before any kernel action.
The guardian owns Linux cgroup cleanup; the runtime owns the native Windows Job.
Neither endpoint nor process IDs alone authorize an operation. No request replay.
"""
import ctypes
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


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def publish(path, value):
    """Atomic, private snapshots; all paths originate in the private launch spec."""
    host_launch.write_private(path, value)


def receive(stream):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = stream.recv(size - len(chunks))
            if not part:
                raise EOFError('incomplete bundled frame')
            chunks.extend(part)
        return chunks
    size = struct.unpack('!I', exact(4))[0]
    if not 0 < size <= LIMIT:
        raise ValueError('bundled frame exceeds size limit')
    value = json.loads(exact(size))
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
        with socket.create_connection(('127.0.0.1', handle['endpoint']), timeout=TIMEOUT) as stream:
            stream.settimeout(TIMEOUT)
            challenge = uuid.uuid4().hex
            stream.sendall(encode({'challenge': challenge}))
            proof = receive(stream).get('proof', '')
            expected = hmac.new(handle['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(proof), expected):
                raise ValueError('bundled server capability proof failed')
            sent = True  # sendall may partially deliver before raising
            stream.sendall(frame)
            response = receive(stream)
        if response.get('id') != ident or response.get('generation') != handle['generation']:
            raise ValueError('bundled response identity mismatch')
        if response.get('result') not in ('accepted', 'completed', 'refused', 'uncertain', 'definitely-not-sent'):
            raise ValueError('invalid bundled response')
        return response
    except (OSError, EOFError, ValueError, KeyError, TypeError):
        return {'result': 'uncertain' if sent else 'definitely-not-sent', 'category': 'bundled_unavailable',
                'diagnostic': 'bundled runtime connection or identity observation failed; no request was replayed'}


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

    def stop(self):
        fd = os.open('cgroup.kill', os.O_WRONLY, dir_fd=self.fd)
        with os.fdopen(fd, 'w') as stream:
            stream.write('1')
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            fd = os.open('cgroup.events', os.O_RDONLY, dir_fd=self.fd)
            with os.fdopen(fd) as stream:
                if 'populated 0' in stream.read().splitlines():
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

    def stop(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs, four DWORDs.
        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_int64 * 4), ('faults', ctypes.c_uint32),
                        ('total', ctypes.c_uint32), ('active', ctypes.c_uint32), ('terminated', ctypes.c_uint32)]
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            info = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.active == 0:
                return
            time.sleep(.02)
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
            if not hello or hello.get('event') != 'hello':
                raise ValueError('invalid bundled kernel hello')
            self.generation = hello['generation']
        except BaseException:
            self.child.kill()
            self.child.wait(timeout=TIMEOUT)
            raise

    def call(self, op, spec, **data):
        ident = uuid.uuid4().hex
        request = {'id': ident, 'generation': self.generation, 'unit': spec['unit'],
                   'nonce': spec.get('kernel_nonce'), 'op': op, **data}
        try:
            self.child.stdin.write((json.dumps(request) + '\n').encode())
            self.child.stdin.flush()
            result = self.responses.get(timeout=TIMEOUT)
        except (OSError, queue.Empty) as error:
            raise RuntimeError('bundled kernel transport became uncertain') from error
        if not result or result.get('id') != ident or result.get('generation') != self.generation:
            raise RuntimeError('bundled kernel response identity lost')
        return result


def run(spec_path):
    spec = read_json(spec_path)
    Path(spec_path).unlink()
    containment = WindowsContainment() if wfc_platform.WINDOWS else LinuxContainment(spec['cgroup'])
    kernel = None
    closed = False
    try:
        kernel = Kernel(spec['binary'], containment, spec['env'])
        spawn = kernel.call('spawn', spec, argv=spec['argv'], cwd=spec['cwd'], cols=120, rows=40)
        if not spawn.get('ok'):
            raise OSError('bundled kernel refused worker creation')
        spec['kernel_nonce'] = spawn['result']['nonce']
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(8)
            listener.settimeout(.2)
            handle = {key: spec[key] for key in ('token', 'generation', 'unit', 'nonce', 'attempt', 'snapshot')}
            identity = {'kind': 'windows-job'} if wfc_platform.WINDOWS else {
                'kind': 'cgroup-v2', 'path': spec['cgroup'],
                'device': os.fstat(containment.fd).st_dev, 'inode': os.fstat(containment.fd).st_ino}
            handle.update(containment=identity, guardian=spec['guardian'], endpoint=listener.getsockname()[1], state='active',
                          service={'pid': os.getpid(), 'creation': wfc_platform.process_identity(os.getpid())},
                          kernel={'pid': kernel.child.pid, 'creation': wfc_platform.process_identity(kernel.child.pid)},
                          kernel_generation=kernel.generation, worker_pid=spawn['result']['pid'])
            publish(spec['snapshot'], handle)
            while True:
                if kernel.child.poll() is not None:
                    raise OSError('bundled kernel exited; live terminal recovery is unavailable')
                try:
                    stream, _ = listener.accept()
                except socket.timeout:
                    view = kernel.call('read', spec)
                    if not view.get('ok'):
                        raise OSError('bundled terminal observation failed')
                    if view['result'].get('exit') is not None:
                        containment.stop()
                        kernel.child.wait(timeout=TIMEOUT)
                        closed = True
                        publish(spec['snapshot'], {**handle, 'state': 'exited', 'cleanup': 'confirmed',
                                                  'final_screen': view['result']['text']})
                        break
                    continue
                with stream:
                    stream.settimeout(2)
                    close_requested = False
                    try:
                        challenge = receive(stream).get('challenge')
                        if not isinstance(challenge, str) or len(challenge) != 32:
                            raise ValueError('invalid bundled challenge')
                        proof = hmac.new(spec['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
                        stream.sendall(encode({'proof': proof}))
                        request = receive(stream)
                        if (not hmac.compare_digest(str(request.get('token', '')), spec['token'])
                                or any(request.get(key) != spec[key] for key in ('generation', 'unit', 'nonce', 'attempt'))):
                            result = {'result': 'refused', 'category': 'bundled_ownership',
                                      'diagnostic': 'bundled execution identity refused'}
                        elif request['op'] == 'close':
                            containment.stop()
                            kernel.child.wait(timeout=TIMEOUT)
                            closed = True
                            close_requested = True
                            publish(spec['snapshot'], {**handle, 'state': 'closed', 'cleanup': 'confirmed'})
                            result = {'result': 'completed'}
                        elif request['op'] in ('read', 'input', 'resize'):
                            extra = {key: request[key] for key in ('text', 'cols', 'rows') if key in request}
                            response = kernel.call(request['op'], spec, **extra)
                            if response.get('ok'):
                                result = {'result': 'accepted', 'data': response['result']}
                            else:
                                # Input errors may follow a partial PTY write. Never permit replay.
                                result = {'result': 'uncertain' if request['op'] == 'input' else 'refused',
                                          'diagnostic': 'bundled kernel operation failed'}
                        else:
                            result = {'result': 'refused', 'diagnostic': 'unknown bundled operation'}
                        stream.sendall(encode({**result, 'id': request.get('id'), 'generation': spec['generation']}))
                    except (OSError, EOFError, ValueError, KeyError, TypeError, queue.Empty):
                        # A disconnected client cannot cancel or replay a request already delivered.
                        pass
                    if close_requested:
                        break
    finally:
        try:
            if not closed:
                containment.stop()
                if kernel:
                    kernel.child.wait(timeout=TIMEOUT)
                publish(spec['snapshot'], {key: spec[key] for key in
                        ('generation', 'unit', 'nonce', 'attempt')} | {'state': 'faulted', 'cleanup': 'confirmed'})
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
        child.wait()
    finally:
        cleaned = False
        if containment:
            try:
                containment.stop()
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
        if snapshot.get('state') not in ('closed', 'exited'):
            publish(spec['snapshot'], {**snapshot, 'state': 'faulted',
                                      'cleanup': 'confirmed' if cleaned else 'unknown'})
        Path(spec_path).unlink(missing_ok=True)
        Path(spec['host_spec']).unlink(missing_ok=True)
