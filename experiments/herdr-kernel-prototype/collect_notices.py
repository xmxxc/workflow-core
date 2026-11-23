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
LICENSES = Path(__file__).resolve().parent / 'licenses'
UUCODE = 'uucode-0.2.0-ZZjBPlK5VADj7fdoq7G8LIHzD5o6FSkcBXXrRWr4jnrA'
UUCODE_SOURCE = 'https://github.com/jacobsandlund/uucode/tree/2826a37a4562284fdacd8fa029d49509cc9bffcd'
UUCODE_LICENSES = {
    'licenses/LICENSE_Bjoern_Hoehrmann': 'de219cece932aad5a817bf763393d8d149d378a15d2ad5320e3331eac07626dd',
    'licenses/LICENSE_unicode': '1eda5a3b026870c737b22e8bcd4954338612c790db688242e003f41a4fa95175',
}
RGB_NOTICE = 'xorg-rgb/COPYING'
XORG_RGB = dict(name='X.Org rgb', version='1.1.0', notices=[RGB_NOTICE],
                source='https://www.x.org/releases/individual/app/rgb-1.1.0.tar.xz',
                source_archive_sha256='fc03d7f56e5b2a617668167f8927948cce54f93097e7ccd9f056077f479ed37b')
SUPPLEMENTAL_NOTICES = {
    f'zig/{UUCODE}/{name}': ('uucode/' + name, digest)
    for name, digest in UUCODE_LICENSES.items()
}
SUPPLEMENTAL_NOTICES[RGB_NOTICE] = (RGB_NOTICE, '1d08eb3384f5dedc7735bcfae1aa58cb64643ca9389edce3312b1c3cefbfdaa7')


def checked_notice(path, expected):
    if not path.is_file():
        raise ValueError(f'missing pinned notice: {path.name}')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f'pinned notice digest mismatch: {path.name}')
    return data


def copy_supplemental_notices(out):
    for name, (source, expected) in SUPPLEMENTAL_NOTICES.items():
        data = checked_notice(LICENSES / source, expected)
        destination = out / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def verify_supplemental_notices(directory, index):
    for name, (_, expected) in SUPPLEMENTAL_NOTICES.items():
        checked_notice(directory / name, expected)
    if not any(record['package_hash'] == UUCODE and record.get('source') == UUCODE_SOURCE
               and UUCODE_LICENSES.keys() <= set(record['notices']) for record in index['zig_materialized']):
        raise ValueError('missing pinned uucode notice index')
    if XORG_RGB not in index.get('embedded_data', []):
        raise ValueError('missing pinned X.Org rgb notice index')


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
    if not (cache / UUCODE).is_dir():
        raise ValueError('Expected pinned uucode package at ' + str(cache / UUCODE))
    zig = []
    for package in sorted(p for p in cache.iterdir() if p.is_dir()):
        files = copy_files(notices(package), package, out / 'zig' / package.name)
        record = dict(package_hash=package.name, notices=files)
        if package.name == UUCODE:
            # Zig omits the two licenses referenced by uucode's LICENSE.md.
            record.update(source=UUCODE_SOURCE, notices=sorted(set(files) | UUCODE_LICENSES.keys()))
        zig.append(record)
    # Ghostty embeds the X.Org rgb data in addition to the Zig dependencies.
    copy_supplemental_notices(out)
    shutil.copyfile(vendor / 'LICENSE', out / 'LICENSE-Ghostty')
    shutil.copyfile(prepared / 'LICENSE', out / 'LICENSE-Herdr')
    index = dict(target=target, cargo=records, zig_materialized=zig, embedded_data=[XORG_RGB],
                 cargo_lock_sha256=hashlib.sha256((prepared / 'Cargo.lock').read_bytes()).hexdigest(),
                 scope='Cargo target metadata including build dependencies. Zig package directories describe fetched inputs, not a link map.')
    verify_supplemental_notices(out, index)
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
