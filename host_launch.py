"""Standard-library host launch configuration, resolution and durable startup evidence."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

DIAGNOSTIC_LIMIT = 4096
_SECRET_NAME = re.compile(r'token|secret|password|passwd|credential|api.?key|authorization|cookie', re.I)


class HostCommand(list):
    """An argv list retaining the location of an explicitly configured host script."""
    def __init__(self, argv, runtime_count=0):
        super().__init__(argv)
        self.runtime_count = runtime_count


class LaunchError(Exception):
    def __init__(self, category, diagnostic):
        super().__init__(diagnostic)
        self.category = category


def _argv(value, field):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v or '\0' in v for v in value):
        raise ValueError(f'{field} must be a nonempty JSON argv array of nonempty strings')
    return value


def _environment(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
                                          or not isinstance(v, str) or '\0' in v for k, v in value.items()):
        raise ValueError('env must map environment names to string values')
    return value


def configure(host, command, extra_env, config=None, inherited=None):
    """Inherited env < config env < host env < router/launcher env. Config replaces argv[0]."""
    data = {}
    if config:
        try:
            data = json.loads(Path(config).read_text(encoding='utf-8'))
            if not isinstance(data, dict) or set(data) - {'env', 'hosts'}:
                raise ValueError('launch config accepts only env and hosts')
            hosts = data.get('hosts', {})
            if not isinstance(hosts, dict) or set(hosts) - {'codex', 'claude', 'opencode'}:
                raise ValueError('hosts must map codex, claude or opencode to launch settings')
            for settings in hosts.values():
                if not isinstance(settings, dict) or set(settings) - {'command', 'runtime', 'env'}:
                    raise ValueError('host settings accept only command, runtime and env')
                _environment(settings.get('env', {}))
                for field in ('command', 'runtime'):
                    if field in settings:
                        _argv(settings[field], field)
            _environment(data.get('env', {}))
        except (OSError, ValueError, TypeError):
            # Neither malformed JSON nor OS exceptions may echo configuration values.
            raise SystemExit('invalid launch config: expected env/hosts with command/runtime argv arrays and string env values')
    settings = data.get('hosts', {}).get(host, {})
    env = {**(os.environ if inherited is None else inherited), **data.get('env', {}),
           **settings.get('env', {}), **extra_env}
    # Runtime argv is a prefix: [runtime, runtime flags, script, host fixed flags, host task flags].
    return HostCommand([*settings.get('runtime', []), *settings.get('command', command[:1]), *command[1:]],
                       len(settings.get('runtime', []))), env


def sanitize(text, env=None):
    """Bound diagnostics and redact known credential values and common credential syntax."""
    text = str(text)
    for name, value in sorted((env or {}).items(), key=lambda item: len(item[1]), reverse=True):
        if value and (_SECRET_NAME.search(name) or name.startswith('WFC_') and 'TOKEN' in name):
            variants = {value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]}
            for variant in sorted(variants, key=len, reverse=True):
                if len(variant) > DIAGNOSTIC_LIMIT:
                    prefix_at = text.find(variant[:DIAGNOSTIC_LIMIT])
                    if prefix_at >= 0:
                        text = text[:prefix_at] + '[REDACTED]' + text[prefix_at + len(variant):]
                text = text.replace(variant, '[REDACTED]')
    text = re.sub(r'(?i)((?:bearer|basic)\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r"""(?i)(?<![\w-])((?:[\w-]*(?:token|secret|password|passwd|credential|api[_-]?key|cookie)[\w-]*)[\s"']*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;]+)""",
                  r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(\b(?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', text)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
    return text[:DIAGNOSTIC_LIMIT]


def _resolve(name, env, cwd, category='executable_missing', executable=True):
    try:
        if os.path.dirname(name):
            path = Path(name)
            path = path if path.is_absolute() else Path(cwd) / path
            found = os.path.abspath(path) if path.is_file() else None
        else:
            # Resolve relative PATH entries against the worker cwd, not the caller cwd.
            paths = [str((Path(cwd) / p).resolve()) for p in env.get('PATH', os.defpath).split(os.pathsep)]
            found = (shutil.which(name, path=os.pathsep.join(paths)) if executable else
                     next((str(Path(p) / name) for p in paths if (Path(p) / name).is_file()), None))
            if found is None and os.name != 'nt':
                denied = next((str(Path(p) / name) for p in paths if (Path(p) / name).is_file()), None)
                if denied:
                    raise LaunchError('permission_denied', f'permission denied: {denied}')
        if not found:
            raise LaunchError(category, f'{category}: {name}')
        if executable and os.name != 'nt' and not os.access(found, os.X_OK):
            raise LaunchError('permission_denied', f'permission denied: {found}')
        return found
    except OSError as err:
        raise LaunchError('permission_denied' if isinstance(err, PermissionError) else 'unknown', str(err))


def platform_argv(argv, platform=None):
    """Native Windows adapters must never infer a runtime from a Unix shebang.

    Batch entrypoints require cmd.exe's shell grammar. Refuse them here rather than
    silently interpret host/task data as commands; configure a native runtime + script.
    Full Windows terminal execution is owned by the Windows backend work.
    """
    if (platform or sys.platform) == 'win32' and Path(argv[0]).suffix.lower() in ('.cmd', '.bat'):
        raise LaunchError('unknown', 'Windows cmd/bat entry requires an explicit native runtime and script argv; shell entry refused')
    return list(argv)


def prepare(command, env, cwd, platform=None):
    """Resolve executable and direct Unix shebang runtime using the exact child environment."""
    platform = platform or sys.platform
    argv = platform_argv(command, platform)
    runtime_count = getattr(command, 'runtime_count', 0)
    argv[0] = _resolve(argv[0], env, cwd, 'runtime_missing' if runtime_count else 'executable_missing')
    if runtime_count:
        argv[runtime_count] = _resolve(argv[runtime_count], env, cwd, executable=False)
    if platform == 'win32':
        return platform_argv(argv, platform)
    try:
        with open(argv[0], 'rb') as source:
            first = source.readline(4096)
    except OSError as err:
        raise LaunchError('permission_denied' if isinstance(err, PermissionError) else 'unknown', str(err))
    if first.startswith(b'#!'):
        try:
            interpreter = shlex.split(first[2:].decode('utf-8').strip())
        except (ValueError, UnicodeError):
            raise LaunchError('unknown', 'cannot parse executable shebang; configure explicit runtime argv')
        if not interpreter:
            raise LaunchError('runtime_missing', 'empty executable shebang')
        if Path(interpreter[0]).name == 'env':
            interpreter = interpreter[1:]
            if interpreter[:1] == ['-S']:
                interpreter = interpreter[1:]
            if not interpreter or interpreter[0].startswith('-') or '=' in interpreter[0]:
                raise LaunchError('unknown', 'unsupported env shebang; configure explicit runtime argv')
        interpreter[0] = _resolve(interpreter[0], env, cwd, 'runtime_missing')
        return [*interpreter, *argv]
    return argv


def write_private(path, value):
    """Atomically publish JSON without ever creating a world-readable intermediate."""
    path = Path(path)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_receipt(path):
    if not path:
        return None
    try:
        with open(path, encoding='utf-8') as source:
            return json.load(source)
    except (OSError, ValueError):
        return None


def supervise(spec_path):
    """Keep terminal stdin/stdout; drain stderr into a bounded, sanitized exit receipt."""
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    spec_path.unlink()  # The environment snapshot is transient, never a diagnostic artifact.
    receipt, env = spec['receipt'], spec['env']
    evidence = {'category': 'unknown', 'diagnostic': 'host startup not confirmed', 'at': time.time()}
    try:
        child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=env, stderr=subprocess.PIPE)
    except OSError as err:
        category = ('permission_denied' if isinstance(err, PermissionError) else
                    'executable_missing' if isinstance(err, FileNotFoundError) else 'unknown')
        write_private(receipt, {**evidence, 'category': category, 'diagnostic': sanitize(err, env)})
        return 126
    write_private(receipt, {**evidence, 'category': 'started', 'diagnostic': 'host process started', 'pid': child.pid})
    chunks = bytearray()

    def drain():
        while True:
            data = child.stderr.read1(1024)
            if not data:
                break
            # Store only a bounded prefix. Never persist the raw stream to disk.
            if len(chunks) < DIAGNOSTIC_LIMIT * 2:
                chunks.extend(data[:DIAGNOSTIC_LIMIT * 2 - len(chunks)])
            try:
                os.write(2, data)
            except OSError:
                pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    code = child.wait()
    reader.join(timeout=0.2)  # A descendant holding stderr must not keep the supervisor alive.
    write_private(receipt, {**evidence, 'category': 'process_exited', 'exitCode': code,
                            'diagnostic': sanitize(f'host process exited ({code})\n' + chunks.decode('utf-8', 'replace'), env)})
    return code if code >= 0 else 128 - code
