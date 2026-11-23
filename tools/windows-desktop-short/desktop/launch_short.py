"""Resolve the user's two installation paths before starting a single foreground trial."""
import argparse
import hashlib
from pathlib import Path
import json
import re
import runpy
import sys
import tempfile
import zipfile

CANDIDATE = '2409c40b469e0e40bd5c55326df3d5ebb86ca81b'
ZIP_NAME = 'wfc-kernel-experiment-' + CANDIDATE + '-windows-x64.zip'
ZIP_SIZE = 1585670
ZIP_SHA = 'ff7bf5a0fe76297d4ef6040f01bdd7065474a1591af484aff9e60b2965e059ee'
PAYLOAD = {'wfc.pyz':'ba60b239aeb48b6014cf4f7b4925ce2897db4c8d0ad3e131c60603a0ab87d914',
           'wfc-kernel.exe':'ac383ae828e21dc3fa33d306d895ccf188917fccdac3ef2c10b642f3271cfa15',
           'wfc.cmd':'e8f1ce5cda54cb6a40395e1492ea62f98f0827067a15f6ac5a42602f6cf688fa'}


def package_binding(candidate=None, pyz_sha=None):
    if candidate is None and pyz_sha is None:
        return CANDIDATE, PAYLOAD
    if not (re.fullmatch(r'[A-Za-z0-9._-]{1,100}', candidate or '') and
            re.fullmatch(r'[0-9a-f]{64}', pyz_sha or '') and candidate != CANDIDATE):
        raise ValueError('A local candidate requires its distinct build ID and expected pyz SHA-256')
    return candidate, {**PAYLOAD, 'wfc.pyz':pyz_sha}


def package(path, scratch, *, candidate=None, pyz_sha=None):
    identity, payload = package_binding(candidate, pyz_sha)
    if path.is_file() and path.suffix.lower()=='.zip':
        if candidate is not None:
            raise ValueError('Use the extracted wfc.cmd for an explicitly bound local build')
        if path.stat().st_size>8_000_000:
            raise ValueError('ZIP exceeds the fixed candidate artifact size limit')
        blob=path.read_bytes()
        if hashlib.sha256(blob).hexdigest()!=ZIP_SHA:
            # GitHub may wrap the inner, verified distribution in another ZIP.
            with zipfile.ZipFile(path) as outer:
                candidates=[f for f in outer.infolist() if f.filename.rsplit('/',1)[-1]==ZIP_NAME]
                if len(candidates)!=1 or candidates[0].file_size!=ZIP_SIZE:
                    raise ValueError('Expected the archived 2409c40 Windows candidate')
                blob=outer.read(candidates[0])
        if len(blob)!=ZIP_SIZE or hashlib.sha256(blob).hexdigest()!=ZIP_SHA:
            raise ValueError('Candidate ZIP SHA-256 mismatch')
        import io
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            archive.extractall(scratch)
        folder=scratch/'wfc'
    elif path.is_file() and path.name.lower() in ('wfc.pyz','wfc.cmd'):
        folder=path.parent
    elif path.is_dir():
        folder=path if (path/'wfc.cmd').is_file() else path/'wfc'
    else:
        raise ValueError('Supply the existing ZIP, wfc.cmd or extracted package directory')
    if (folder/'COMMIT.txt').read_text().strip()!=identity:
        raise ValueError('Candidate COMMIT.txt mismatch')
    for name, expected in payload.items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Candidate file SHA-256 mismatch: '+name)
    return folder.resolve()


def native_cli(path):
    if path.name.lower()=='codex.exe' and path.is_file():
        return path.resolve()
    if path.name.lower()=='codex.js' and path.parent.name.lower()=='bin':
        root=path.parent.parent
    elif path.name.lower() in ('codex.cmd','codex.ps1'):
        root=path.parent/'node_modules'/'@openai'/'codex'
    else:
        raise ValueError('Supply the independent native codex.exe or its npm codex.js/codex.cmd entry')
    if not path.is_file():
        raise FileNotFoundError(path)
    metadata=json.loads((root/'package.json').read_text(encoding='utf-8'))
    if (metadata.get('name'),metadata.get('version'))!=('@openai/codex','0.164.0'):
        raise ValueError('Expected the verified @openai/codex 0.164.0 installation')
    native=Path('vendor/x86_64-pc-windows-msvc/bin/codex.exe')
    options=[root/'node_modules/@openai/codex-win32-x64'/native,root.parent/'codex-win32-x64'/native,root/native]
    resolved={p.resolve() for p in options if p.is_file()}
    if len(resolved)!=1:
        raise ValueError('Supply the exact installed native codex.exe; the npm entry has no unique native binary')
    return resolved.pop()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('codex',type=Path)
    parser.add_argument('candidate_id',nargs='?')
    parser.add_argument('pyz_sha256',nargs='?')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='wfc-short-package-') as temp:
        wfc=package(args.package.resolve(),Path(temp),candidate=args.candidate_id,pyz_sha=args.pyz_sha256)
        exe=native_cli(args.codex.resolve())
        probe=Path(__file__).with_name('desktop_probe.py')
        sys.argv=[str(probe),'--package',str(wfc),'--codex',str(exe),'--worker-scratch',
                  '--worker-term','xterm-256color','--model','gpt-6.1-sol','--effort','high']
        if args.candidate_id:
            sys.argv.extend(['--candidate-id',args.candidate_id,'--pyz-sha256',args.pyz_sha256])
        if args.package.suffix.lower() == '.zip':
            sys.argv.append('--copy-callback-program')
        runpy.run_path(str(probe),run_name='__main__')
