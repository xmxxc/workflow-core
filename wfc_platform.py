"""Small native boundaries for locking, process identity and detached children."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'


def default_state_dir():
    if WINDOWS:
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'workflow-core'
    return Path.home() / '.local' / 'state' / 'workflow-core'


def configure_stdio():
    if WINDOWS:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')


def acquire_lock(fd, timeout=30):
    """Lock byte zero (Windows) or the file (Unix), with a monotonic deadline.

    Closing the descriptor or process releases the lock. Always acquire this before
    beginning a database write transaction; no unlocked fallback is permitted.
    """
    if WINDOWS:
        import msvcrt
    else:
        import fcntl
    deadline = time.monotonic() + timeout
    while True:
        try:
            if WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as err:
            if err.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('file lock deadline exceeded') from None
            time.sleep(min(0.05, remaining))


def detached_options():
    if WINDOWS:
        return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                'close_fds': True}
    return {'start_new_session': True, 'close_fds': True}


def callback_command(argv, *, dispatch=None, state=None):
    """A command for the documented worker shell; argv is never executed here."""
    context = {key: str(value) for key, value in
               (("WFC_DISPATCH", dispatch), ("WFC_STATE", state)) if value is not None}
    if WINDOWS:
        # Use one explicit CRT command line: Windows PowerShell 5.1's native
        # invocation would otherwise discard empty arguments and embedded quotes.
        literal = lambda text: "'" + str(text).replace("'", "''") + "'"
        prefix = "".join("$env:" + key + " = " + literal(value) + "; " for key, value in context.items())
        return (prefix + "Start-Process -FilePath " + literal(argv[0]) + " -ArgumentList "
                + literal(subprocess.list2cmdline([str(arg) for arg in argv[1:]]))
                + " -NoNewWindow -Wait")
    import shlex
    values = [str(arg) for arg in argv]
    if context:
        values = ["env", *[key + "=" + value for key, value in context.items()], *values]
    return shlex.join(values)


def windows_process(pid):
    """Read liveness and creation FILETIME through one read-only process handle.

    Access denial is unknown, never proof of exit. No signal or termination API is
    used. Creation identity and liveness come from the same handle, avoiding reuse
    between two PID lookups.
    """
    import ctypes
    from ctypes import wintypes
    if not WINDOWS:
        raise OSError('Windows process queries require native Windows')
    if not isinstance(pid, int) or not 0 < pid <= 0xffffffff:
        return {'state': 'unknown', 'identity': None, 'diagnostic': 'invalid process id'}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        code = ctypes.get_last_error()
        return {'state': 'exited' if code == 87 else 'unknown', 'identity': None,
                'diagnostic': f'OpenProcess failed ({code})'}
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return {'state': 'exited', 'identity': None, 'diagnostic': None}
        if wait != 258:  # WAIT_TIMEOUT means still running; WAIT_FAILED is unknown.
            return {'state': 'unknown', 'identity': None,
                    'diagnostic': f'process query failed ({ctypes.get_last_error()})'}
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in (created, exited, system, user))):
            return {'state': 'unknown', 'identity': None,
                    'diagnostic': f'creation time query failed ({ctypes.get_last_error()})'}
        return {'state': 'alive', 'identity': f'windows:{(created.dwHighDateTime << 32) | created.dwLowDateTime}',
                'diagnostic': None}
    finally:
        kernel.CloseHandle(handle)


def process_identity(pid):
    if WINDOWS:
        return windows_process(pid)['identity']
    if Path('/proc/self/stat').exists():
        try:
            value = Path(f'/proc/{pid}/stat').read_text()
        except OSError:
            return None
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            boot = ''
        return f"linux:{boot}:{value[value.rindex(')') + 2:].split()[19]}"
    try:
        result = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                                env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
        return result.stdout.strip() or None
    except OSError:
        return None


def open_private(path):
    """Create a new private file; Windows DACL is present from creation time."""
    if not WINDOWS:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', wintypes.LPVOID), ('inherit', wintypes.BOOL)]

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(SecurityAttributes), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    descriptor = wintypes.LPVOID()
    # Same protected SYSTEM/owner-rights DACL used by Herdr's native private pipe.
    if not convert('D:P(A;;GA;;;SY)(A;;GA;;;OW)', 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    finally:
        kernel.LocalFree(descriptor)


def process_probe(pid):
    """Local read-only observation; inaccessible identity remains unknown."""
    if WINDOWS:
        return windows_process(pid)
    if Path('/proc/self/stat').exists():
        try:
            value = Path(f'/proc/{pid}/stat').read_text()
            if value[value.rindex(')') + 2:].split()[0] in ('Z', 'X'):
                return {'state': 'exited', 'identity': None}
        except FileNotFoundError:
            return {'state': 'exited', 'identity': None}
        except OSError:
            return {'state': 'unknown', 'identity': None}
    identity = process_identity(pid)
    if identity:
        return {'state': 'alive', 'identity': identity}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'state': 'exited', 'identity': None}
    except PermissionError:
        pass
    return {'state': 'unknown', 'identity': None}
