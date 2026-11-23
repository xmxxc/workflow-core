"""Verify stable archives and their recursive license inventory after extraction."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import package_release as release
from collect_notices import UUCODE, UUCODE_LICENSES, UUCODE_SOURCE, XORG_RGB, copy_supplemental_notices
from package_bundled_experiment import digest


class StableArchives(unittest.TestCase):
    def test_target_archives_preserve_nested_notices_and_relocatable_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            prepared = trial / 'prepared'
            source = ROOT / 'experiments/herdr-kernel-prototype'
            prepared.mkdir()
            for name in ('Cargo.lock', 'Cargo.toml'):
                shutil.copy2(source / name, prepared)
            shutil.copytree(source / 'src', prepared / 'src')
            for name in ('portable-pty.patches.md', 'libghostty-vt.patches.md', 'libghostty-vt.vendor.json',
                         'patches/libghostty-vt/one.patch', 'patches/portable-pty/two.patch'):
                path = prepared / 'vendor' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            sample = 'vendor/portable-pty.patches.md'
            (prepared / 'extraction.json').write_text(json.dumps(dict(source_commit=release.PIN,
                files=[dict(path=sample, sha256=digest(prepared / sample))])))
            notices = trial / 'notices'
            nested = 'cargo/demo-1/LICENSES/sub/MIT.txt'
            for name in (nested, 'zig/example/LICENSE', 'LICENSE-Ghostty', 'LICENSE-Herdr'):
                path = notices / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('license fixture', encoding='utf-8')
            shutil.copy2(source / 'Cargo.lock', notices)
            copy_supplemental_notices(notices)
            commit = 'a' * 40
            for platform, target in release.TARGETS.items():
                with self.subTest(platform=platform):
                    windows = platform == 'windows-x64'
                    exe = '.exe' if windows else ''
                    kernel = prepared / 'target' / target / 'release' / ('herdr-kernel-prototype' + exe)
                    kernel.parent.mkdir(parents=True)
                    kernel.write_bytes(b'native fixture')
                    kernel.chmod(0o755)
                    (notices / 'sources.json').write_text(json.dumps(dict(target=target,
                        cargo_lock_sha256=digest(source / 'Cargo.lock'),
                        cargo=[dict(name='demo', version='1', notices=['LICENSES/sub/MIT.txt'])],
                        zig_materialized=[dict(package_hash='example', notices=['LICENSE']),
                                          dict(package_hash=UUCODE, source=UUCODE_SOURCE,
                                               notices=list(UUCODE_LICENSES))],
                        embedded_data=[XORG_RGB])))
                    with patch.object(release, 'git', side_effect=lambda *args: commit if args[0] == 'rev-parse' else ''):
                        archive = release.create(prepared, notices, trial / 'dist', platform, commit)
                        checksum = digest(archive)
                        with self.assertRaises(FileExistsError):
                            release.create(prepared, notices, trial / 'dist', platform, commit)
                        self.assertEqual(digest(archive), checksum)
                    extracted = trial / ('已解压 ' + platform)
                    shutil.unpack_archive(str(archive), str(extracted))
                    directory = extracted / 'wfc'
                    release.verify(directory, platform, commit)
                    program = directory / ('wfc.pyz' if windows else 'wfc')
                    self.assertEqual(subprocess.check_output([sys.executable, str(program), '--version'],
                                     text=True).strip(), 'wfc ' + release.VERSION)
                    self.assertEqual((directory / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())
                    self.assertEqual((directory / ('wfc-kernel' + exe)).read_bytes(), kernel.read_bytes())
                    if not windows and os.name != 'nt':
                        self.assertTrue(os.access(program, os.X_OK))
                        self.assertTrue(os.access(directory / 'wfc-kernel', os.X_OK))
                    self.check_pinned_notices(directory, platform, commit)
                    (directory / 'bundled-notices' / nested).unlink()
                    with self.assertRaisesRegex(ValueError, 'missing or changed file'):
                        release.verify(directory, platform, commit)
                    manifest_path = directory / release.MANIFEST
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                    del manifest['files']['bundled-notices/' + nested]
                    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'missing indexed Cargo notice'):
                        release.verify(directory, platform, commit)

    def check_pinned_notices(self, directory, platform, commit):
        manifest_path = directory / release.MANIFEST
        original_manifest = manifest_path.read_bytes()
        index_path = directory / 'bundled-notices/sources.json'
        original_index = index_path.read_bytes()
        for name in ('licenses/LICENSE_Bjoern_Hoehrmann', 'licenses/LICENSE_unicode', 'xorg-rgb/COPYING'):
            with self.subTest(notice=name):
                index = json.loads(original_index)
                if name.startswith('licenses/'):
                    index['zig_materialized'][1]['notices'].remove(name)
                    relative = f'zig/{UUCODE}/{name}'
                else:
                    index.pop('embedded_data')
                    relative = name
                notice = directory / 'bundled-notices' / relative
                contents = notice.read_bytes()
                notice.unlink()
                release.write_json(index_path, index)
                manifest = json.loads(original_manifest)
                manifest['files'] = release.inventory(directory)
                release.write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ValueError, 'missing pinned notice'):
                    release.verify(directory, platform, commit)

                index_path.write_bytes(original_index)
                notice.write_bytes(b'replacement notice')
                manifest['files'] = release.inventory(directory)
                release.write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ValueError, 'pinned notice digest mismatch'):
                    release.verify(directory, platform, commit)
                notice.write_bytes(contents)
                manifest_path.write_bytes(original_manifest)
