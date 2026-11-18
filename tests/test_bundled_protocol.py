"""Wire parser bounds without native sockets; runtime survival is tested natively."""
import json
import struct
import time
import unittest

import bundled_runtime


class FragmentedStream:
    def __init__(self, data, timeout=.03, delay=0):
        self.data, self.timeout, self.delay = data, timeout, delay

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def recv(self, count):
        time.sleep(self.delay)
        part, self.data = self.data[:1], self.data[1:]
        return part


class WireBounds(unittest.TestCase):
    def test_nested_frame_is_a_connection_error(self):
        raw = b'{"x":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'
        stream = FragmentedStream(struct.pack('!I', len(raw)) + raw, timeout=2)
        with self.assertRaises(ValueError):
            bundled_runtime.receive(stream)

    def test_fragment_progress_cannot_reset_frame_deadline(self):
        stream = FragmentedStream(bundled_runtime.encode({'challenge': 'x' * 32}), delay=.005)
        with self.assertRaises(TimeoutError):
            bundled_runtime.receive(stream)

    def test_fragmented_object_preserves_supported_payload(self):
        stream = FragmentedStream(bundled_runtime.encode({'text': '输入', 'op': 'input'}), timeout=2)
        self.assertEqual(bundled_runtime.receive(stream), {'text': '输入', 'op': 'input'})
