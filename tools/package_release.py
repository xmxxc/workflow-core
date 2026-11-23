"""Assemble a native release from a locked target build, or verify its extracted files."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import zipfile

from package_bundled_experiment import (
    MANIFEST, PIN, ROOT, add_provenance, check_files, git, inventory, verify_notices, write_json,
)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'experiments/herdr-kernel-prototype'))
import build
from collect_notices import verify_supplemental_notices
from wfc import VERSION

TARGETS = {'linux-x64': 'x86_64-unknown-linux-gnu', 'windows-x64': 'x86_64-pc-windows-msvc'}
WINDOWS_GUIDES = {
    'docs/windows-distribution.md': 'README-Windows.md',
    'docs/host-launch.md': 'host-launch.md',
    'docs/herdr-backend.md': 'herdr-backend.md',
    'docs/windows-core.md': 'windows-core.md',
    'docs/verification/windows-opencode-release.md': 'OPENCode-Windows.md',
    'docs/verification/windows-close-rc3.md': 'CLOSE-Windows.md',
    'docs/verification/windows-codex-rc4.md': 'CODEX-Windows.md',
}


def verify(directory, platform, commit):
    manifest = json.loads((directory / MANIFEST).read_text(encoding='utf-8'))
    target = TARGETS[platform]
    if (manifest['version'] != VERSION or manifest['source_commit'] != commit
            or manifest['upstream_commit'] != PIN or manifest['target'] != target
            or (directory / 'COMMIT.txt').read_text().strip() != commit):
        raise ValueError('release identity mismatch')
    files = manifest['files']
    check_files(directory, files)
    index = verify_notices(directory, files, target)
    verify_supplemental_notices(directory / 'bundled-notices', index)
    windows = platform == 'windows-x64'
    program = 'wfc.pyz' if windows else 'wfc'
    required = {program, 'wfc-kernel.exe' if windows else 'wfc-kernel', 'LICENSE', 'INSTALL.md',
                'bundled-notices/LICENSE-Herdr', 'bundled-notices/LICENSE-Ghostty',
                'bundled-notices/provenance/compiled-sources.json', 'bundled-notices/provenance/extraction.json'}
    if windows:
        required.update(('wfc.cmd', *WINDOWS_GUIDES.values()))
    if required - files.keys():
        raise ValueError(f'missing release files: {sorted(required - files.keys())}')
    with zipfile.ZipFile(directory / program) as app:
        for name in ('LICENSE', 'INSTALL.md'):
            if app.read(name) != (directory / name).read_bytes():
                raise ValueError(f'zipapp and native distribution differ: {name}')


def create(prepared, notices, output, platform, commit):
    if git('rev-parse', 'HEAD') != commit or git('diff', 'HEAD', '--name-only'):
        raise ValueError('build requires a clean tracked tree at the release commit')
    windows = platform == 'windows-x64'
    suffix = '.exe' if windows else ''
    target = TARGETS[platform]
    kernel = prepared / 'target' / target / 'release' / ('herdr-kernel-prototype' + suffix)
    output.mkdir(parents=True, exist_ok=True)
    extension = '.zip' if windows else '.tar.gz'
    archive = output / f'wfc-v{VERSION}-{platform}{extension}'
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        directory = Path(tmp) / 'wfc'
        if windows:
            build.build_windows(directory)
            for source, name in WINDOWS_GUIDES.items():
                shutil.copy2(ROOT / source, directory / name)
        else:
            build.build(directory / 'wfc')
            shutil.copy2(ROOT / 'LICENSE', directory)
            shutil.copy2(ROOT / 'docs/release-install.md', directory / 'INSTALL.md')
        build.copy_companion(directory, kernel, notices)
        add_provenance(prepared, notices, directory)
        (directory / 'COMMIT.txt').write_text(commit + '\n', encoding='ascii')
        write_json(directory / MANIFEST, dict(version=VERSION, source_commit=commit,
                   upstream_commit=PIN, target=target, files=inventory(directory)))
        verify(directory, platform, commit)
        staged = Path(tmp) / archive.name
        if windows:
            with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED) as packed:
                for path in sorted(directory.rglob('*')):
                    if path.is_file():
                        packed.write(path, path.relative_to(tmp).as_posix())
        else:
            with tarfile.open(staged, 'w:gz') as packed:
                packed.add(directory, arcname='wfc')
        # Install the completed archive exclusively, preserving any previous output.
        os.link(staged, archive)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('create')
    for name in ('prepared', 'notices', 'output'):
        pack.add_argument('--' + name, type=Path, required=True)
    check = commands.add_parser('verify')
    check.add_argument('--directory', type=Path, required=True)
    for command in (pack, check):
        command.add_argument('--platform', required=True, choices=TARGETS)
        command.add_argument('--commit', required=True)
    args = parser.parse_args()
    if args.command == 'create':
        print(create(args.prepared.resolve(), args.notices.resolve(), args.output.resolve(), args.platform, args.commit))
    else:
        verify(args.directory.resolve(), args.platform, args.commit)


if __name__ == '__main__':
    main()
