"""Controlled fixture input must retain Unicode across arbitrary PTY chunks."""
import unittest

from fixtures.bundled_input import read_utf8_lines


class FixtureUnicodeInput(unittest.TestCase):
    def lines(self, chunks):
        chunks = iter([*chunks, b''])
        return list(read_utf8_lines(lambda: next(chunks)))

    def test_unicode_survives_every_utf8_byte_boundary(self):
        marker = '复验路径-中文-42 Ω😀'
        raw = (marker + '\r').encode('utf-8')
        for boundary in range(1, len(raw)):
            with self.subTest(boundary=boundary):
                self.assertEqual(self.lines([raw[:boundary], raw[boundary:]]), [marker])
        self.assertEqual(self.lines([bytes([byte]) for byte in raw]), [marker])

    def test_cr_lf_and_multiple_submissions_keep_their_boundaries(self):
        self.assertEqual(self.lines([b'first\r', b'\nse', b'cond\nthird\r']), ['first', '', 'second', 'third'])

    def test_invalid_utf8_is_not_silently_replaced(self):
        with self.assertRaises(UnicodeDecodeError):
            self.lines([b'bad\xff\r'])

    def test_truncated_utf8_at_eof_is_not_silently_discarded(self):
        with self.assertRaises(UnicodeDecodeError):
            self.lines([b'partial\xe5\xa4'])

    def test_complete_but_unsubmitted_text_at_eof_is_not_applied(self):
        self.assertEqual(self.lines(['复验路径'.encode('utf-8')]), [])


class FakeConsole:
    """Models only the owned console API seam; never opens a native handle."""
    def __init__(self, chunks=()):
        self.original = self.mode = 0x87
        self.chunks = iter([*chunks, b''])
        self.sets = []
        self.handles = []
        self.supported = True
        self.apply = True
        self.readable = True
        self.restore = True
        self.bad_count = False

    def GetStdHandle(self, selector):
        assert selector == -10
        return 123

    def GetConsoleMode(self, handle, mode):
        self.handles.append(handle)
        mode._obj.value = self.mode
        return self.supported

    def SetConsoleMode(self, handle, mode):
        self.handles.append(handle)
        self.sets.append(mode)
        if len(self.sets) == 1 and not self.apply:
            return False
        if len(self.sets) > 1 and not self.restore:
            return False
        self.mode = mode
        return True

    def ReadConsoleW(self, handle, buffer, limit, count, control):
        import ctypes
        self.handles.append(handle)
        assert limit == 1024 and control is None
        chunk = next(self.chunks)
        assert len(chunk) % 2 == 0 and len(chunk) <= limit * 2
        ctypes.memmove(buffer, chunk, len(chunk))
        count._obj.value = limit + 1 if self.bad_count else len(chunk) // 2
        return self.readable


class WindowsVTFixtureInput(unittest.TestCase):
    def test_owned_raw_mode_and_wide_chunks_preserve_vt_and_surrogate_order(self):
        from fixtures.bundled_input import windows_vt_input
        text = '\x1b[200~复验路径-中文-42 Ω😀 ' + 'xyz-123456789 ' * 140 + '\x1b[31mCSI\x1b[0m ATTEMPT-TAIL\x1b[201~\r\n'
        raw = text.encode('utf-16-le')
        api = FakeConsole([raw[n:n + 2] for n in range(0, len(raw), 2)])
        with windows_vt_input(api) as lines:
            self.assertEqual(api.mode & 7, 0)
            self.assertTrue(api.mode & 0x200)
            self.assertEqual(api.mode & 0x80, api.original & 0x80)
            self.assertEqual(list(lines), [text[:-2], ''])
        self.assertEqual(api.mode, api.original)
        self.assertEqual(api.sets, [(api.original & ~7) | 0x200, api.original])
        self.assertTrue(all(handle == 123 for handle in api.handles))

    def test_nonconsole_handle_is_refused_without_mode_changes(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole(); api.supported = False
        with self.assertRaisesRegex(OSError, 'not a readable Windows console'):
            with windows_vt_input(api):
                self.fail('unsupported handle entered the reader')
        self.assertEqual(api.sets, [])

    def test_unsupported_mode_is_refused_without_fallback(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole(); api.apply = False
        with self.assertRaisesRegex(OSError, 'unsupported'):
            with windows_vt_input(api):
                self.fail('unsupported raw mode entered the reader')
        self.assertEqual(api.mode, api.original)

    def test_mode_readback_mismatch_is_refused_and_original_restored(self):
        from fixtures.bundled_input import windows_vt_input
        class IgnoredVTMode(FakeConsole):
            def GetConsoleMode(self, handle, mode):
                result = super().GetConsoleMode(handle, mode)
                if self.sets:
                    mode._obj.value = self.original
                return result
        api = IgnoredVTMode()
        with self.assertRaisesRegex(OSError, 'was not applied'):
            with windows_vt_input(api):
                self.fail('ignored VT mode entered the reader')
        self.assertEqual(api.mode, api.original)

    def test_reader_exception_restores_original_mode(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole(); api.readable = False
        with self.assertRaisesRegex(OSError, 'wide console input failed'):
            with windows_vt_input(api) as lines:
                list(lines)
        self.assertEqual(api.mode, api.original)

    def test_consumer_exception_restores_original_mode(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole()
        with self.assertRaisesRegex(RuntimeError, 'controlled consumer failure'):
            with windows_vt_input(api):
                raise RuntimeError('controlled consumer failure')
        self.assertEqual(api.mode, api.original)

    def test_invalid_wide_count_is_refused_and_mode_restored(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole(); api.bad_count = True
        with self.assertRaisesRegex(OSError, 'count is invalid'):
            with windows_vt_input(api) as lines:
                list(lines)
        self.assertEqual(api.mode, api.original)

    def test_truncated_surrogate_is_refused_and_mode_restored(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole([b'\x3d\xd8'])
        with self.assertRaises(UnicodeDecodeError):
            with windows_vt_input(api) as lines:
                list(lines)
        self.assertEqual(api.mode, api.original)

    def test_restore_failure_is_explicit(self):
        from fixtures.bundled_input import windows_vt_input
        api = FakeConsole(); api.restore = False
        with self.assertRaisesRegex(OSError, 'could not be restored'):
            with windows_vt_input(api) as lines:
                self.assertEqual(list(lines), [])
