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


def callback_command(argv):
    """A command for the documented worker shell; argv is never executed here."""
    if WINDOWS:
        # Use one explicit CRT command line: Windows PowerShell 5.1's native
        # invocation would otherwise discard empty arguments and embedded quotes.
        literal = lambda text: "'" + str(text).replace("'", "''") + "'"
        return ("Start-Process -FilePath " + literal(argv[0]) + " -ArgumentList "
                + literal(subprocess.list2cmdline([str(arg) for arg in argv[1:]]))
                + " -NoNewWindow -Wait")
    import shlex
    return shlex.join([str(arg) for arg in argv])


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
