"""Completion recognition must use authenticated owned receipts, never PID disappearance."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bundled_backend
import bundled_runtime
import wfc_platform


class CompletionReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = 'controlled-test-capability-256-bits'
        self.handle = {'unit': 'd-test', 'generation': 'g-test', 'attempt': 'a-test', 'nonce': 'n-test',
                       'snapshot': str(Path(self.tmp.name) / 'snapshot.json'), 'state': 'active',
                       'endpoint': 1, 'service': {'pid': os.getpid(), 'creation': wfc_platform.process_identity(os.getpid())},
                       'capabilityHash': hashlib.sha256(self.token.encode()).hexdigest()}
        self.active = {**self.handle, 'token': self.token}
        self.active['kernel'] = dict(self.handle['service'])
        bundled_runtime.publish(self.handle['snapshot'], self.active)
        self.backend = bundled_backend.BundledBackend(row={'backend_handle': json.dumps(self.handle)})

    def receipt(self, **changes):
        data = {**self.active, 'state': 'exited', 'cleanup': 'confirmed', **changes}
        # Independent fixture signing of the canonical wire document.
        payload = {key: value for key, value in data.items() if key not in ('token', 'receiptSignature')}
        data['receiptSignature'] = hmac.new(data['token'].encode(), json.dumps(
            payload, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
        return data

    def lost(self, receipt):
        def applied(*args, **kwargs):
            bundled_runtime.publish(self.handle['snapshot'], receipt)
            return {'result': 'uncertain', 'diagnostic': 'controlled lost transport response'}
        return applied

    def test_inspect_recognizes_matching_receipt_after_read_transport_loss(self):
        with patch.object(bundled_runtime, 'call', side_effect=self.lost(self.receipt())) as call:
            observed = self.backend.inspect('d-test')
        self.assertEqual(observed['result'], 'accepted')
        self.assertTrue(observed['confirmedAbsent'])
        self.assertFalse(observed['alive'])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs['op'], 'read')

    def test_close_recognizes_matching_receipt_without_replaying_close(self):
        with patch.object(bundled_runtime, 'call', side_effect=self.lost(self.receipt(state='closed'))) as call:
            closed = self.backend.close('d-test')
        self.assertEqual(closed['result'], 'completed')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs['op'], 'close')

    def test_preflight_identity_loss_uses_receipt_without_any_rpc(self):
        def ended(pid):
            bundled_runtime.publish(self.handle['snapshot'], self.receipt())
            return {'state': 'exited', 'identity': None}
        with patch.object(wfc_platform, 'process_probe', side_effect=ended), patch.object(bundled_runtime, 'call') as call:
            observed = self.backend.inspect('d-test')
        self.assertEqual(observed['result'], 'accepted')
        self.assertTrue(observed['confirmedAbsent'])
        call.assert_not_called()

    def test_missing_stale_or_tampered_receipt_never_completes(self):
        variants = []
        variants.append(('missing', None))
        unsigned = self.receipt();unsigned.pop('receiptSignature');variants.append(('unsigned', unsigned))
        for field in ('generation', 'attempt', 'nonce', 'unit'):
            variants.append((field, self.receipt(**{field: 'other'})))
        variants.append(('service', self.receipt(service={'pid': os.getpid(), 'creation': 'other'})))
        body = self.receipt();body['final_screen'] = 'tampered';variants.append(('body', body))
        key = self.receipt(token='other-capability', capabilityHash=hashlib.sha256(b'other-capability').hexdigest())
        variants.append(('key substitution', key))
        pending = self.receipt(cleanup='unknown');variants.append(('unconfirmed', pending))
        for label, receipt in variants:
            with self.subTest(label=label):
                bundled_runtime.publish(self.handle['snapshot'], self.active)
                def failed(*args, **kwargs):
                    if receipt is None:
                        Path(self.handle['snapshot']).unlink()
                    else:
                        bundled_runtime.publish(self.handle['snapshot'], receipt)
                    return {'result': 'uncertain', 'diagnostic': 'controlled response loss'}
                with patch.object(bundled_runtime, 'call', side_effect=failed) as call:
                    result = self.backend.close('d-test')
                self.assertEqual(result['result'], 'uncertain')
                self.assertEqual(call.call_count, 1)

    def test_unsigned_prior_cleanup_cannot_authorize_completion(self):
        unsigned = self.receipt();unsigned.pop('receiptSignature')
        bundled_runtime.publish(self.handle['snapshot'], unsigned)
        with patch.object(bundled_runtime, 'call') as call:
            result = self.backend.close('d-test')
        self.assertEqual(result['result'], 'uncertain')
        call.assert_not_called()

    def test_explicit_refusal_is_not_overridden_by_completion_observation(self):
        def refused(*args, **kwargs):
            bundled_runtime.publish(self.handle['snapshot'], self.receipt())
            return {'result': 'refused', 'diagnostic': 'controlled identity refusal'}
        with patch.object(bundled_runtime, 'call', side_effect=refused) as call:
            self.assertEqual(self.backend.close('d-test')['result'], 'refused')
        self.assertEqual(call.call_count, 1)

    def test_cleanup_proof_is_read_only_and_does_not_claim_create_acceptance(self):
        receipt = self.receipt()
        bundled_runtime.publish(self.handle['snapshot'], receipt)
        with patch.object(bundled_runtime, 'call') as call:
            result = self.backend.cleanup_receipt('d-test')
        self.assertEqual(result['result'], 'accepted')
        self.assertEqual(result['proof']['proves'], 'execution-empty')
        self.assertEqual(result['proof']['attempt'], self.handle['attempt'])
        self.assertNotIn('token', json.dumps(result))
        self.assertNotIn('receiptSignature', json.dumps(result))
        self.assertNotIn('createAccepted', result)
        call.assert_not_called()

    def test_non_object_receipt_remains_unconfirmed(self):
        bundled_runtime.publish(self.handle['snapshot'], [])
        with patch.object(bundled_runtime, 'call') as call:
            result = self.backend.cleanup_receipt('d-test')
        self.assertEqual(result['result'], 'refused')
        call.assert_not_called()
