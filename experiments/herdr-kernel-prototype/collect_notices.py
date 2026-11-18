"""Collect build-target Cargo metadata and materialized Zig package notices."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

NOTICE = re.compile(r'^(licen[cs]e|unlicense|copying|copyright|notices?)(?:$|[-. _])', re.I)


def notices(directory):
    result = []
    for path in directory.iterdir():
        if path.is_file() and NOTICE.match(path.name):
            result.append(path)
        if path.is_dir() and path.name.lower() in ('licenses', 'licences', 'notices'):
            result.extend(p for p in path.rglob('*') if p.is_file())
    return sorted(result)


def copy_files(paths, source, destination):
    names = []
    for path in sorted(set(paths)):
        relative = path.relative_to(source)
        dest = destination / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        names.append(relative.as_posix())
    return names


def collect(prepared, target, out, wfc_license):
    metadata = json.loads(subprocess.check_output([
        'cargo', 'metadata', '--format-version', '1', '--locked', '--offline',
        '--manifest-path', str(prepared / 'Cargo.toml'), '--filter-platform', target,
    ]))
    records = []
    for package in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        name, version = package['name'], package['version']
        source = Path(package['manifest_path']).parent
        dest = out / 'cargo' / f'{name}-{version}'
        if source == prepared:
            dest.mkdir(parents=True)
            shutil.copyfile(wfc_license, dest / 'LICENSE-WFC')
            files = ['LICENSE-WFC']
        elif source == prepared / 'crates' / 'ghostty-vt':
            dest.mkdir(parents=True)
            shutil.copyfile(prepared / 'LICENSE', dest / 'LICENSE-Herdr')
            files = ['LICENSE-Herdr']
        else:
            paths = notices(source)
            if package['license_file']:
                paths.append(source / package['license_file'])
            if not paths:
                raise ValueError(f'Inspect missing notices for {name} {version}')
            files = copy_files(paths, source, dest)
        records.append(dict(name=name, version=version, license=package['license'],
                            repository=package['repository'], source=package['source'], notices=files))
    shutil.copyfile(prepared / 'Cargo.lock', out / 'Cargo.lock')
    vendor = prepared / 'vendor/libghostty-vt'
    cache = vendor / 'zig-pkg'
    if not cache.is_dir():
        raise ValueError('Expected materialized Zig packages at ' + str(cache))
    zig = []
    for package in sorted(p for p in cache.iterdir() if p.is_dir()):
        files = copy_files(notices(package), package, out / 'zig' / package.name)
        zig.append(dict(package_hash=package.name, notices=files))
    shutil.copyfile(vendor / 'LICENSE', out / 'LICENSE-Ghostty')
    shutil.copyfile(prepared / 'LICENSE', out / 'LICENSE-Herdr')
    index = dict(target=target, cargo=records, zig_materialized=zig,
                 cargo_lock_sha256=hashlib.sha256((prepared / 'Cargo.lock').read_bytes()).hexdigest(),
                 scope='Cargo target metadata including build dependencies. Zig package directories describe fetched inputs, not a link map.')
    (out / 'sources.json').write_text(json.dumps(index, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prepared', type=Path)
    parser.add_argument('target')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be a new directory')
    with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
        stage = Path(tmp) / 'notices'
        stage.mkdir()
        collect(args.prepared.resolve(), args.target, stage, Path(__file__).resolve().parents[2] / 'LICENSE')
        stage.rename(args.output)
