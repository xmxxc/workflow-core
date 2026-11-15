"""Fetch a checksum-pinned Herdr fixture into a new directory, never install globally."""
import argparse
import hashlib
import os
import platform
from pathlib import Path
import urllib.request
import zipfile

ASSETS = {
    'posix': ('herdr-linux-x86_64', '14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d'),
    'nt': ('herdr-windows-x86_64.zip', 'd2c6c9c971c43a0bfdde8c692a9a1a7a45c3e4807cd01ae45662b4734fac1cbf'),
}


def fetch(out):
    if os.name not in ASSETS or platform.machine().lower() not in ("amd64", "x86_64") or (os.name == "posix" and platform.system() != "Linux"):
        raise SystemExit('the fixture supports Linux x86_64 and Windows x86_64')
    name, expected = ASSETS[os.name]
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    archive = out / name
    digest = hashlib.sha256()
    url = 'https://github.com/herdrdev/herdr/releases/download/v0.9.5/' + name
    with urllib.request.urlopen(url, timeout=60) as source, archive.open('xb') as target:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            target.write(block)
    if digest.hexdigest() != expected:
        raise SystemExit('Herdr fixture checksum mismatch; nothing executed')
    if os.name == 'nt':
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                destination = (out / entry.filename).resolve()
                if not destination.is_relative_to(out) or destination == out:
                    raise SystemExit('Herdr archive entry is outside the fixture directory')
            bundle.extractall(out)
        found = list(out.rglob('herdr.exe'))
        if len(found) != 1:
            raise SystemExit('Herdr fixture must contain exactly one herdr.exe')
        return found[0]
    archive.chmod(0o755)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True, type=Path)
    print(fetch(parser.parse_args().out))
