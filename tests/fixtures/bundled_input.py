"""Unicode VT input for the account-free, test-owned terminal fixture."""
import codecs
from contextlib import contextmanager
import ctypes
import re


def read_utf8_lines(read):
    return _read_lines(read, 'utf-8')


def _read_lines(read, encoding):
    decoder = codecs.getincrementaldecoder(encoding)()
    pending = ''
    while True:
        raw = read()
        if not raw:
            decoder.decode(b'', final=True)
            return
        pending += decoder.decode(raw)
        while re.search(r'[\r\n]', pending):
            end = re.search(r'[\r\n]', pending).start()
            line, pending = pending[:end], pending[end + 1:]
            yield line


def _console_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    word, handle = ctypes.c_uint32, ctypes.c_void_p
    api.GetStdHandle.argtypes = [word]
    api.GetStdHandle.restype = handle
    api.GetConsoleMode.argtypes = [handle, ctypes.POINTER(word)]
    api.GetConsoleMode.restype = ctypes.c_int
    api.SetConsoleMode.argtypes = [handle, word]
    api.SetConsoleMode.restype = ctypes.c_int
    api.ReadConsoleW.argtypes = [handle, ctypes.c_void_p, word, ctypes.POINTER(word), ctypes.c_void_p]
    api.ReadConsoleW.restype = ctypes.c_int
    return api


@contextmanager
def windows_vt_input(api=None):
    """Temporarily use raw VT on this fixture's console stdin; no codepage change."""
    api = _console_api() if api is None else api
    handle = api.GetStdHandle(-10)  # STD_INPUT_HANDLE, inherited from our own ConPTY.
    original = ctypes.c_uint32()
    if not api.GetConsoleMode(handle, ctypes.byref(original)):
        raise OSError('controlled fixture stdin is not a readable Windows console')
    # Disable processed, line and echo input; retain unrelated original flags.
    mode = (original.value & ~0x7) | 0x200  # ENABLE_VIRTUAL_TERMINAL_INPUT
    if not api.SetConsoleMode(handle, mode):
        raise OSError('controlled fixture raw VT input mode is unsupported')
    try:
        observed = ctypes.c_uint32()
        if not api.GetConsoleMode(handle, ctypes.byref(observed)) or observed.value != mode:
            raise OSError('controlled fixture raw VT input mode was not applied')

        def read():
            # ReadConsoleW returns UTF-16 code units, independently of the OEM codepage.
            buffer = ctypes.create_string_buffer(2048)
            count = ctypes.c_uint32()
            if not api.ReadConsoleW(handle, buffer, 1024, ctypes.byref(count), None):
                raise OSError('controlled fixture wide console input failed')
            if count.value > 1024:
                raise OSError('controlled fixture wide console count is invalid')
            return buffer.raw[:count.value * 2]

        yield _read_lines(read, 'utf-16-le')
    finally:
        if not api.SetConsoleMode(handle, original.value):
            raise OSError('controlled fixture original console input mode could not be restored')
