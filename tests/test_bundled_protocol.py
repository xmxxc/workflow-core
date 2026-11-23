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
        if self.delay:
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


class DiagnosticBounds(unittest.TestCase):
    def test_opt_in_trace_bounds_events_and_omits_capabilities_and_payloads(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            secret = 'private-capability-canary'
            spec = {'unit': 'd-test', 'generation': 'g-test', 'attempt': 'a-test',
                    'snapshot': str(Path(tmp) / 'snapshot.json'), 'diagnostics': True,
                    'token': secret, 'nonce': secret, 'env': {'KEY': secret}}
            trace = bundled_runtime.LifecycleTrace(spec, 'runtime')
            for index in range(100):
                trace.record('test_phase', code=index, token=secret, nonce=secret, text=secret, env=secret)
            data = json.loads(trace.path.read_text())
            self.assertEqual(len(data['events']), 48)
            self.assertEqual(data['events'][0]['code'], 52)
            self.assertNotIn(secret, trace.path.read_text())
            error = OSError(5, secret)
            bundled_runtime.trace_error(trace, 'test_error', error)
            self.assertEqual(json.loads(trace.path.read_text())['events'][-1]['error_type'], 'OSError')
            self.assertNotIn(secret, trace.path.read_text())

    def test_diagnostics_default_to_no_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            trace = bundled_runtime.LifecycleTrace({'unit': 'd', 'generation': 'g', 'attempt': 'a',
                                                    'snapshot': str(Path(tmp) / 'snapshot.json')}, 'runtime')
            trace.record('test')
            self.assertFalse(list(Path(tmp).iterdir()))
