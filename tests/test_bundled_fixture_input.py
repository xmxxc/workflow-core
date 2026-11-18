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
