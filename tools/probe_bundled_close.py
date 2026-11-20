#!/usr/bin/env python3
"""Bounded, account-free native close probe; writes allowlisted evidence only."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from bundled_diagnostics import close_evidence, safe_outcome
from test_bundled import BundledNative
import wfc_platform


def run(attempts, output):
    if not os.environ.get('WFC_BUNDLED_TEST_KERNEL'):
        raise SystemExit('WFC_BUNDLED_TEST_KERNEL must select the exact native test companion')
    records = []
    started = time.monotonic()
    for index in range(attempts):
        if time.monotonic() - started > 180:
            break
        mode = ('immediate', 'question', 'parent-exit')[index % 3]
        case = BundledNative('test_interrupt_close_resume_replaces_execution_identity')
        case.setUp()
        row = {'index': index, 'mode': mode}
        handle = None
        dispatch = None
        # Capture final evidence just before test-owned files are removed.
        cleanup = case.tmp.cleanup
        def capture_and_cleanup():
            if handle is not None:
                row['after_cleanup'] = close_evidence(dispatch, handle)
            cleanup()
        case.tmp.cleanup = capture_and_cleanup
        try:
            value = case.dispatch()
            dispatch = value['dispatch']
            handle = case.handle(dispatch)
            if mode == 'question':
                case.question()
            row['before'] = close_evidence(dispatch, handle)
            if mode == 'parent-exit':
                pid = handle['worker_pid']
                creation = wfc_platform.process_identity(pid)
                case.assertTrue(creation)
                case.assertEqual(wfc_platform.process_identity(pid), creation)
                row['controlled_parent'] = {'pid': pid, 'creation': creation}
                os.kill(pid, signal.SIGTERM)
            else:
                row['interrupt'] = safe_outcome(case.cli('interrupt', dispatch))
            closed = case.cli('close', dispatch)
            row['close'] = safe_outcome(closed)
            row['immediate'] = close_evidence(dispatch, handle)
            # Bounded read-only samples distinguish receipt publication from RPC loss.
            row['samples'] = []
            for _ in range(3):
                time.sleep(.05)
                row['samples'].append(close_evidence(dispatch, handle))
            row['passed'] = closed.get('result') == 'completed'
        except BaseException as error:
            row['error_type'] = type(error).__name__
            row['passed'] = False
            if handle is not None:
                row['failure'] = close_evidence(dispatch, handle)
        finally:
            try:
                case.tearDown()
                row['cleanup'] = 'test-owned processes stopped'
            except BaseException as error:
                row['cleanup_error_type'] = type(error).__name__
                row['passed'] = False
            records.append(row)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'version': 1, 'records': records}, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'index': index, 'mode': mode, 'passed': row['passed'],
                          'close': row.get('close'), 'cleanup': row.get('cleanup')}), flush=True)
    return 0 if len(records) == attempts and all(row['passed'] for row in records) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--attempts', type=int, default=12, choices=range(1, 21))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.attempts, args.out))
