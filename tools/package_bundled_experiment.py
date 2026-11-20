#!/usr/bin/env python3
"""Add fixed-source evidence to build.py's Windows experiment; verify after relocation."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PIN = 'ee5bfb5041bb3e4a49d591c72308aae3c81ecdde'
TARGET = 'x86_64-pc-windows-msvc'
MANIFEST = 'BUNDLED-MANIFEST.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def inventory(directory):
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in package: ' + str(path))
        if path.is_file() and path != directory / MANIFEST:
            files[path.relative_to(directory).as_posix()] = digest(path)
    return files


def checked_path(directory, name):
    relative = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or relative.is_absolute()
            or '..' in relative.parts or relative.as_posix() != name):
        raise ValueError('unsafe manifest path: ' + name)
    path = directory.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError('manifest path escapes package: ' + name)
    return path


def check_files(directory, files):
    if not files:
        raise ValueError('empty file manifest')
    for name, expected in files.items():
        path = checked_path(directory, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('missing or changed file: ' + name)
    if inventory(directory) != files:
        raise ValueError('package file set differs from manifest')


def verify_bundle(directory, commit):
    manifest = json.loads((directory / MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('source_commit') != commit or manifest.get('upstream_commit') != PIN:
        raise ValueError('package source identity differs from expected commit/pin')
    check_files(directory, manifest['files'])
    required = ('wfc.pyz', 'wfc.cmd', 'wfc-kernel.exe', 'LICENSE', 'COMMIT.txt',
                'EXPERIMENT.json', 'BUNDLED-EXPERIMENTAL.md', 'WINDOWS-MANUAL.md',
                'bundled-notices/sources.json', 'bundled-notices/Cargo.lock',
                'bundled-notices/provenance/extraction.json',
                'bundled-notices/provenance/compiled-sources.json')
    if any(name not in manifest['files'] for name in required):
        raise ValueError('package is missing required payload or provenance')
    metadata = json.loads((directory / 'EXPERIMENT.json').read_text(encoding='utf-8'))
    if ((directory / 'COMMIT.txt').read_text().strip() != commit
            or metadata['source_commit'] != commit or metadata['target'] != TARGET
            or metadata['source_tree'] != manifest['source_tree']):
        raise ValueError('inconsistent package identity')
    index = json.loads((directory / 'bundled-notices/sources.json').read_text(encoding='utf-8'))
    if (index['target'] != TARGET or index['cargo_lock_sha256'] != digest(directory / 'bundled-notices/Cargo.lock')
            or not index['cargo']):
        raise ValueError('notice target/lock differs from native build')
    for record in index['cargo']:
        if not record['notices']:
            raise ValueError('Cargo dependency has no notices')
        for name in record['notices']:
            path = 'bundled-notices/cargo/' + record['name'] + '-' + record['version'] + '/' + name
            if path not in manifest['files']:
                raise ValueError('missing indexed Cargo notice: ' + path)
    for record in index['zig_materialized']:
        for name in record['notices']:
            path = 'bundled-notices/zig/' + record['package_hash'] + '/' + name
            if path not in manifest['files']:
                raise ValueError('missing indexed Zig notice: ' + path)
    return metadata


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()


def create_bundle(prepared, notices, kernel, output, commit):
    if git('rev-parse', 'HEAD') != commit or git('diff', 'HEAD', '--name-only'):
        raise ValueError('build requires a clean tracked tree at the candidate commit')
    tree = git('rev-parse', 'HEAD^{tree}')
    authored = ROOT / 'experiments/herdr-kernel-prototype'
    extraction = json.loads((prepared / 'extraction.json').read_text(encoding='utf-8'))
    if extraction['source_commit'] != PIN:
        raise ValueError('incorrect upstream extraction pin')
    compiled = {}
    for record in extraction['files']:
        path = checked_path(prepared, record['path'])
        if digest(path) != record['sha256']:
            raise ValueError('extracted source changed: ' + record['path'])
        compiled[record['path']] = record['sha256']
    for path in [authored / 'Cargo.toml', authored / 'Cargo.lock', *sorted((authored / 'src').rglob('*'))]:
        if path.is_file():
            name = path.relative_to(authored).as_posix()
            if digest(prepared / name) != digest(path):
                raise ValueError('compiled authored source differs from checkout: ' + name)
            compiled[name] = digest(path)
    if (not compiled or (notices / 'Cargo.lock').read_bytes() != (prepared / 'Cargo.lock').read_bytes()):
        raise ValueError('notice lock differs from compiled source')
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / 'wfc'
    subprocess.run([sys.executable, str(ROOT / 'build.py'), '--windows-dir', str(bundle),
                    '--bundled-kernel', str(kernel), '--kernel-notices', str(notices)], check=True, timeout=120)
    manifest = json.loads((bundle / MANIFEST).read_text(encoding='utf-8'))
    check_files(bundle, manifest['files'])
    provenance = bundle / 'bundled-notices/provenance'
    provenance.mkdir()
    shutil.copy2(prepared / 'extraction.json', provenance / 'extraction.json')
    write_json(provenance / 'compiled-sources.json', compiled)
    for name in ('vendor/portable-pty.patches.md', 'vendor/libghostty-vt.patches.md',
                 'vendor/libghostty-vt.vendor.json', 'vendor/patches/portable-pty', 'vendor/patches/libghostty-vt'):
        source, destination = prepared / name, provenance / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    shutil.copy2(ROOT / 'docs/verification/bundled-kernel-experiment.md', bundle / 'WINDOWS-MANUAL.md')
    shutil.copy2(ROOT / 'docs/verification/windows-bundled-takeover.zh-CN.md', bundle / 'WINDOWS-TAKEOVER.zh-CN.md')
    (bundle / 'COMMIT.txt').write_text(commit + '\n', encoding='ascii')
    index = json.loads((notices / 'sources.json').read_text(encoding='utf-8'))
    metadata = dict(experiment='bundled-kernel', source_commit=commit, source_tree=tree,
                    upstream_commit=PIN, target=TARGET, kernel_sha256=digest(kernel), kernel_bytes=kernel.stat().st_size,
                    cargo_packages=len(index['cargo']), zig_materialized=len(index['zig_materialized']),
                    zig_without_notices=[p['package_hash'] for p in index['zig_materialized'] if not p['notices']],
                    notice_scope=index['scope'],
                    coverage_bounds='Materialized Zig inputs are not a link map. Theme input N-V-__8 has no top-level notice; linkage/license review remains separate. Controlled CI is not real-host manual acceptance.')
    write_json(bundle / 'EXPERIMENT.json', metadata)
    manifest.update(source_commit=commit, source_tree=tree, files=inventory(bundle))
    write_json(bundle / MANIFEST, manifest)
    verify_bundle(bundle, commit)
    archive = output / ('wfc-kernel-experiment-' + commit + '-windows-x64.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                target.write(path, 'wfc/' + path.relative_to(bundle).as_posix())
    (output / 'SHA256SUMS').write_text(digest(archive) + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps(dict(zip=str(archive), zip_bytes=archive.stat().st_size, **metadata), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    for name in ('prepared', 'notices', 'kernel', 'output'):
        create.add_argument('--' + name, type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--directory', type=Path, required=True)
    for command in (create, verify):
        command.add_argument('--commit', required=True)
    args = parser.parse_args()
    if args.command == 'create':
        create_bundle(args.prepared.resolve(), args.notices.resolve(), args.kernel.resolve(), args.output.resolve(), args.commit)
    else:
        print(json.dumps(verify_bundle(args.directory.resolve(), args.commit), indent=2))


if __name__ == '__main__':
    main()
