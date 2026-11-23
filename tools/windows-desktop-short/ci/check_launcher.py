"""Check package discovery, integrity failure and npm native resolution without launching a host."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import zipfile

p=argparse.ArgumentParser()
p.add_argument('--desktop-tools', type=Path, required=True)
p.add_argument('--candidate', type=Path, required=True)
args=p.parse_args()
sys.path.insert(0,str(args.desktop_tools.resolve()))
from launch_short import package, native_cli, ZIP_NAME

with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    inner=args.candidate.resolve()
    if not inner.is_file() or inner.suffix.lower() != '.zip':
        raise ValueError('launcher self-test requires the fixed inner distribution ZIP')
    out=package(inner,root/'inner')
    assert package(out/'wfc.cmd',root/'unused')==out
    outer=root/'outer.zip'
    with zipfile.ZipFile(outer,'w') as archive:
        archive.write(inner,ZIP_NAME)
    assert (package(outer,root/'outer')/'wfc.cmd').is_file()
    original=(out/'wfc.cmd').read_bytes()
    (out/'wfc.cmd').write_bytes(original+b' ')
    try: package(out,root/'unused')
    except ValueError: pass
    else: raise AssertionError('tampered launcher accepted')
    prefix=root/'npm'
    mod=prefix/'node_modules'/'@openai'/'codex'
    (mod/'bin').mkdir(parents=True)
    (mod/'package.json').write_text(json.dumps({'name':'@openai/codex','version':'0.164.0'}))
    (mod/'bin'/'codex.js').touch()
    (prefix/'codex.cmd').touch()
    native=Path('vendor/x86_64-pc-windows-msvc/bin/codex.exe')
    exe=mod.parent/'codex-win32-x64'/native
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert all(native_cli(arg)==exe.resolve() for arg in (mod/'bin'/'codex.js',exe,prefix/'codex.cmd'))
    other=mod/native
    other.parent.mkdir(parents=True)
    other.touch()
    try: native_cli(prefix/'codex.cmd')
    except ValueError: pass
    else: raise AssertionError('ambiguous installed native chosen')
print('PASS fixed candidate ZIP, download ZIP, extracted entry, tamper refusal, unique installed CLI')
