"""Package verification controls only; fabricated payloads are not native acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('experiment_package', Path(__file__).resolve().parents[1] / 'tools/package_bundled_experiment.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
COMMIT = 'a' * 40


class ExperimentPackageControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='package with spaces ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        files = dict.fromkeys(('wfc.pyz', 'wfc.cmd', 'wfc-kernel.exe', 'LICENSE', 'BUNDLED-EXPERIMENTAL.md',
                               'WINDOWS-MANUAL.md', 'bundled-notices/provenance/extraction.json',
                               'bundled-notices/provenance/compiled-sources.json'), 'controlled fixture')
        files['COMMIT.txt'] = COMMIT + '\n'
        files['EXPERIMENT.json'] = json.dumps(dict(source_commit=COMMIT, source_tree='tree', target=package.TARGET))
        files['bundled-notices/Cargo.lock'] = 'locked fixture\n'
        files['bundled-notices/sources.json'] = json.dumps(dict(target=package.TARGET,
            cargo_lock_sha256=hashlib.sha256(b'locked fixture\n').hexdigest(),
            cargo=[dict(name='fixture', version='1', notices=['LICENSE'])],
            zig_materialized=[dict(package_hash='fixture-zig-hash', notices=['LICENSE'])]))
        files['bundled-notices/cargo/fixture-1/LICENSE'] = 'fixture license'
        files['bundled-notices/zig/fixture-zig-hash/LICENSE'] = 'fixture Zig license'
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode('utf-8'))
        self.manifest = dict(source_commit=COMMIT, source_tree='tree', upstream_commit=package.PIN,
            files={name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()})
        self.save_manifest()

    def save_manifest(self):
        (self.root / package.MANIFEST).write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_relocated_bundle_requires_expected_source_identity(self):
        self.assertEqual(package.verify_bundle(self.root, COMMIT)['source_commit'], COMMIT)
        with self.assertRaisesRegex(ValueError, 'source identity'):
            package.verify_bundle(self.root, 'b' * 40)

    def test_changed_binary_is_rejected(self):
        (self.root / 'wfc-kernel.exe').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed file'):
            package.verify_bundle(self.root, COMMIT)

    def test_unlisted_file_is_rejected(self):
        (self.root / 'extra.exe').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'file set'):
            package.verify_bundle(self.root, COMMIT)

    def test_missing_notice_is_rejected_even_if_removed_from_manifest(self):
        name = 'bundled-notices/cargo/fixture-1/LICENSE'
        (self.root / name).unlink()
        del self.manifest['files'][name]
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'indexed Cargo notice'):
            package.verify_bundle(self.root, COMMIT)

    def test_paths_cannot_escape_the_bundle(self):
        for name in ('../outside', '/absolute', 'C:/absolute', 'a\\b', 'a/../b', './wfc.pyz'):
            with self.subTest(name=name):
                self.manifest['files'][name] = '0' * 64
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'unsafe manifest path'):
                    package.verify_bundle(self.root, COMMIT)
                del self.manifest['files'][name]

    def test_missing_zig_notice_is_rejected_even_if_removed_from_manifest(self):
        name = 'bundled-notices/zig/fixture-zig-hash/LICENSE'
        (self.root / name).unlink()
        del self.manifest['files'][name]
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'indexed Zig notice'):
            package.verify_bundle(self.root, COMMIT)

    def test_notice_lock_cannot_be_replaced_with_another_target_lock(self):
        path = self.root / 'bundled-notices/Cargo.lock'
        path.write_bytes(b'another lock')
        self.manifest['files']['bundled-notices/Cargo.lock'] = hashlib.sha256(b'another lock').hexdigest()
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'notice target/lock'):
            package.verify_bundle(self.root, COMMIT)


if __name__ == '__main__':
    unittest.main()
