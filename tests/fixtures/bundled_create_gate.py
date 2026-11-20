"""Run the candidate CLI with a file barrier around a real bundled create and relay scan."""
import json
from pathlib import Path
import runpy
import sys
import time
import zipfile

program, directory = map(Path, sys.argv[1:3])
sys.argv = [str(program), *sys.argv[3:]]
sys.path.insert(0, str(program if zipfile.is_zipfile(program) else program.parent))
import router

pending = directory / 'create-pending'
release = directory / 'scan-complete'
if sys.argv[1] == 'dispatch':
    save = router.bundled_backend.BundledBackend._save

    def gated_save(self, dispatch, handle):
        save(self, dispatch, handle)
        if handle['state'] == 'pending' and 'guardian' not in handle:
            pending.touch()
            deadline = time.monotonic() + 15
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('test did not release the pending create')
                time.sleep(.02)

    router.bundled_backend.BundledBackend._save = gated_save
elif sys.argv[1] == 'relay':
    check_workers = router.check_workers
    scanned = False

    def witnessed_scan(*args, **kwargs):
        global scanned
        if scanned:
            # The previous full iteration includes the message claim and settlement.
            release.touch()
        started_pending = pending.exists()
        check_workers(*args, **kwargs)
        scanned = started_pending

    def record_queue(thread, batch):
        with (directory / 'queue.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(batch) + '\n')
        return 'delivered'

    router.check_workers = witnessed_scan
    router.queue_into_codex = record_queue

runpy.run_path(str(program), run_name='__main__')
