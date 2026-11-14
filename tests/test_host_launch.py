"""Launch configuration and platform adapter contract."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import host_launch


class LaunchConfiguration(unittest.TestCase):
    def test_explicit_command_runtime_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / '启动 config.json'
            config.write_text(json.dumps({'env': {'PATH': '/global', 'MODE': 'global'},
                                         'hosts': {'codex': {'command': ['/安装 路径/codex.js', '--fixed'],
                                                             'runtime': [sys.executable, '-u'],
                                                             'env': {'PATH': '/host', 'MODE': 'host'}}}}))
            command, env = host_launch.configure('codex', ['codex', 'brief'], {'MODE': 'internal'},
                                                 config, inherited={'PATH': '/inherited', 'KEEP': 'yes'})
            self.assertEqual(command, [sys.executable, '-u', '/安装 路径/codex.js', '--fixed', 'brief'])
            self.assertEqual(env, {'PATH': '/host', 'MODE': 'internal', 'KEEP': 'yes'})




class ExecutableResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {'PATH': str(self.root)}

    def script(self, name, text, mode=0o755):
        path = self.root / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_missing_executable_runtime_and_permission_are_distinct(self):
        for command, category in [(['absent'], 'executable_missing'),
                                  ([str(self.script('env host', '#!/usr/bin/env absent-runtime\n'))], 'runtime_missing'),
                                  ([str(self.script('denied', '#!/bin/sh\n', 0o644))], 'permission_denied')]:
            with self.subTest(category=category), self.assertRaises(host_launch.LaunchError) as raised:
                host_launch.prepare(command, self.env, self.root)
            self.assertEqual(raised.exception.category, category)

    def test_explicit_runtime_accepts_nonexecutable_script_with_unicode_path(self):
        script = self.script('宿主 path.py', 'print(1)\n', 0o644)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'hosts': {'codex': {'command': [str(script)], 'runtime': [sys.executable, '-u']}}}))
        command, env = host_launch.configure('codex', ['codex', 'task'], {}, config, inherited=self.env)
        self.assertEqual(host_launch.prepare(command, env, self.root), [os.path.abspath(sys.executable), '-u', str(script), 'task'])

    def test_explicit_missing_runtime_is_runtime_missing(self):
        command = host_launch.HostCommand(['missing-node', 'script.js'], runtime_count=1)
        with self.assertRaises(host_launch.LaunchError) as raised:
            host_launch.prepare(command, self.env, self.root)
        self.assertEqual(raised.exception.category, 'runtime_missing')

    def test_windows_native_exe_ignores_unix_shebang(self):
        exe = self.script('工具.exe', '#!/not/a/windows/runtime\n')
        self.assertEqual(host_launch.prepare([str(exe), 'a b', '中文'], self.env, self.root, platform='win32'),
                         [str(exe), 'a b', '中文'])

    def test_windows_cmd_is_an_explicit_refused_shell_boundary(self):
        for suffix in ('.cmd', '.CMD', '.bat'):
            with self.subTest(suffix=suffix), self.assertRaises(host_launch.LaunchError) as raised:
                host_launch.platform_argv(['host' + suffix, 'task & arbitrary command'], platform='win32')
            self.assertEqual(raised.exception.category, 'unknown')
            self.assertIn('native runtime', str(raised.exception))

    def test_diagnostics_are_bounded_and_redacted_before_truncation(self):
        raw = ('API_KEY=private-value Bearer some-auth https://name:pass@example.com\n'
               'private-value password=hidden\x1b\x00\n' + 'x' * 12000)
        diagnostic = host_launch.sanitize(raw, {'SERVICE_TOKEN': 'private-value'})
        self.assertLessEqual(len(diagnostic), 4096)
        for secret in ('private-value', 'some-auth', 'name:pass', 'hidden', '\x1b', '\x00'):
            self.assertNotIn(secret, diagnostic)

    def test_quoted_credentials_and_basic_auth_are_redacted(self):
        raw = """password="alpha beta gamma" token='two words' {"api_key": "json secret"} Authorization: Basic abcdef"""
        diagnostic = host_launch.sanitize(raw)
        for value in ('alpha', 'beta', 'gamma', 'two', 'words', 'json secret', 'abcdef'):
            self.assertNotIn(value, diagnostic)

    def test_json_escaped_credential_quotes_do_not_leave_suffixes(self):
        secret = 'alpha "tailsecret" suffix'
        raw = json.dumps({'token': secret, 'error': secret})
        diagnostic = host_launch.sanitize(raw, {'SERVICE_TOKEN': secret})
        for value in ('alpha', 'tailsecret', 'suffix'):
            self.assertNotIn(value, diagnostic)



class ControlledLaunch(unittest.TestCase):
    """Real cross-process host launch, with a controlled terminal adapter instead of a real TUI."""
    def setUp(self):
        from test_router import Env
        self.env = Env()
        self.addCleanup(self.env.close)
        self.pids = self.env.bin.parent / 'pids'
        self.addCleanup(self.stop_children)
        (self.env.bin / 'tmux').write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
sessions = Path({str(self.env.sessions)!r})
if sys.argv[1] == 'new-session':
    marker = sessions / sys.argv[sys.argv.index('-s') + 1]
    marker.write_text(json.dumps(dict(sys.argv[i + 1].split('=', 1) for i, a in enumerate(sys.argv) if a == '-e')))
    if not os.environ.get('NO_HOST_ACK'):
        i = sys.argv.index('_host-run')
        child = subprocess.Popen(sys.argv[i-2:], env={{'PATH': '/stale-terminal-path'}},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        with open({str(self.pids)!r}, 'a') as out: out.write(str(child.pid) + '\\n')
elif sys.argv[1] == 'has-session':
    sys.exit(0 if (sessions / sys.argv[sys.argv.index('-t') + 1]).exists() else 1)
elif sys.argv[1] == 'show-environment':
    marker = sessions / sys.argv[sys.argv.index('-t') + 1]
    print(sys.argv[-1] + '=' + json.loads(marker.read_text()).get(sys.argv[-1], ''))
''')
        self.output = self.env.bin.parent / 'observed.json'
        self.host = self.env.bin.parent / '安装 路径' / '宿主.py'
        self.host.parent.mkdir()
        self.host.write_text('import json, os, sys, time\n'
                             f'open({str(self.output)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "path": os.environ["PATH"], "mode": os.environ.get("MODE"), "dispatch": os.environ["WFC_DISPATCH"]}}))\n'
                             'time.sleep(1)\n')
        self.host.chmod(0o644)
        self.config = self.env.bin.parent / '启动 配置.json'

    def stop_children(self):
        import signal
        if self.pids.exists():
            for pid in self.pids.read_text().splitlines():
                try:
                    os.killpg(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def configure(self, host='codex', **settings):
        self.config.write_text(json.dumps({'hosts': {host: settings}}))

    def dispatch_result(self, host='codex', **kwargs):
        return self.env.run('dispatch', '--host', host, '--cwd', str(self.env.cwd), '--task', str(self.env.task),
                            '--report', str(self.env.report), '--launch-config', str(self.config), **kwargs)

    def status(self):
        return self.env.json('status')['dispatches'][0]

    def test_all_hosts_use_explicit_runtime_argv_and_exact_environment(self):
        for host, flag in [('codex', '-m'), ('claude', '--model'), ('opencode', '-m')]:
            with self.subTest(host=host):
                self.configure(host, command=[str(self.host), 'fixed space 中文'], runtime=[sys.executable, '-u'],
                               env={'PATH': '/explicit/node bin', 'MODE': 'override'})
                result = self.env.run('dispatch', '--host', host, '--cwd', str(self.env.cwd), '--task', str(self.env.task),
                                      '--report', str(self.env.report), '--model', 'test-model', '--launch-config', str(self.config))
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(self.output.read_text())
                self.assertEqual(observed['argv'][0], 'fixed space 中文')
                self.assertEqual(observed['argv'][observed['argv'].index(flag) + 1], 'test-model')
                self.assertEqual(observed['path'], '/explicit/node bin')
                self.assertEqual(observed['mode'], 'override')
                self.assertEqual(observed['dispatch'], json.loads(result.stdout)['dispatch'])
        self.assertFalse(list((self.env.state / 'launches').glob('*.spec')))

    def test_nvm_style_runtime_missing_then_explicit_path_recovers(self):
        script = self.host.with_name('codex')
        script.write_text('#!/usr/bin/env node\n' + self.host.read_text())
        script.chmod(0o755)
        self.configure(command=[str(script)], env={'PATH': '/no-node-here'})
        result = self.dispatch_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status()['signals']['fault']['category'], 'runtime_missing')
        runtime_dir = self.host.parent / 'nvm custom bin'
        runtime_dir.mkdir()
        (runtime_dir / 'node').symlink_to(sys.executable)
        self.configure(command=[str(script)], env={'PATH': str(runtime_dir)})
        result = self.dispatch_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())['path'], str(runtime_dir))

    def test_missing_executable_and_denied_host_are_persisted_in_status(self):
        for command, expected in [(['/not-installed/host'], 'executable_missing'), ([str(self.host)], 'permission_denied')]:
            with self.subTest(category=expected):
                self.configure(command=command)
                result = self.dispatch_result()
                self.assertNotEqual(result.returncode, 0)
                rows = self.env.json('status')['dispatches']
                self.assertEqual(rows[-1]['signals']['fault']['category'], expected)

    def test_fast_exit_retains_bounded_redacted_stderr_and_code(self):
        self.host.write_text('import os, sys\nsys.stderr.write("startup diagnostic " + os.environ["SERVICE_TOKEN"] + " password=also-secret " + "x" * 20000)\nsys.exit(23)\n')
        self.configure(command=[str(self.host)], runtime=[sys.executable], env={'SERVICE_TOKEN': 'sensitive-canary'})
        result = self.dispatch_result()
        import time
        deadline = time.monotonic() + 5
        status = self.status()
        while status['launch']['category'] == 'started' and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.status()
        self.assertEqual(status['launch']['category'], 'process_exited')
        self.assertEqual(status['launch']['exitCode'], 23)
        self.assertLessEqual(len(status['launch']['diagnostic']), 4096)
        rendered = json.dumps(status) + result.stderr
        self.assertIn('startup diagnostic', rendered)
        self.assertNotIn('sensitive-canary', rendered)
        self.assertNotIn('also-secret', rendered)
        self.assertEqual(status['status'], 'failed')
        for path in (self.env.state / 'launches').glob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_ack_is_unknown_and_resume_requires_reconcile(self):
        self.configure(command=[str(self.host)], runtime=[sys.executable])
        result = self.dispatch_result(env={'NO_HOST_ACK': '1'})
        self.assertNotEqual(result.returncode, 0)
        status = self.status()
        self.assertEqual(status['signals']['fault']['category'], 'unknown')
        self.assertEqual(status['audit'][0]['operation'], 'create')
        attempt = status['attempt']
        resumed = self.env.run('resume', status['dispatch'])
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn('lifecycle_reconcile_required', resumed.stderr)
        self.assertEqual(self.status()['attempt'], attempt)
        audit = status['audit'][0]['id']
        reconciled = self.env.run('reconcile', status['dispatch'], str(audit), '--result', 'accepted',
                                  '--actor', 'operator', '--evidence', 'confirmed-host-running')
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        status = self.status()
        self.assertEqual(status['status'], 'online')
        self.assertIsNone(status['signals']['fault'])
        self.assertEqual(status['launch']['category'], 'unknown')  # Raw evidence remains available.


    def test_resume_and_handoff_reread_config_and_keep_attempt_fencing(self):
        self.configure('claude', command=[str(self.host)], runtime=[sys.executable], env={'MODE': 'first'})
        result = self.dispatch_result('claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        dispatch = json.loads(result.stdout)['dispatch']
        old_attempt = self.status()['attempt']
        for operation, host, mode in [('resume', 'claude', 'resumed'), ('handoff', 'codex', 'handed-off')]:
            with self.subTest(operation=operation):
                self.stop_children()
                self.env.end_session(dispatch)
                self.configure(host, command=[str(self.host)], runtime=[sys.executable], env={'MODE': mode})
                args = [operation, dispatch] + (['--host', host] if operation == 'handoff' else [])
                result = self.env.run(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['result'], 'accepted')
                self.assertEqual(json.loads(self.output.read_text())['mode'], mode)
                self.assertEqual(self.status()['launch']['category'], 'started')
                self.assertNotEqual(self.status()['attempt'], old_attempt)
        self.env.report.write_text('stale result')
        stale = self.env.run('done', '--report', str(self.env.report), '--attempt', old_attempt, dispatch=dispatch)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn('no longer accepted', stale.stderr)

    def test_cli_config_overrides_environment_config_path(self):
        self.configure(command=[str(self.host)], runtime=[sys.executable], env={'MODE': 'cli-wins'})
        result = self.dispatch_result(env={'WFC_LAUNCH_CONFIG': '/missing/env-config.json'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())['mode'], 'cli-wins')

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root bypasses directory permissions')
    def test_denied_parent_path_is_a_durable_permission_diagnostic(self):
        self.configure(command=[str(self.host)])
        self.host.parent.chmod(0)
        try:
            result = self.dispatch_result()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.status()['signals']['fault']['category'], 'permission_denied')
        finally:
            self.host.parent.chmod(0o755)

    def test_unknown_preflight_is_a_retained_refusal_without_unresolvable_audit(self):
        self.host.write_text('#!/usr/bin/env -i node\n')
        self.host.chmod(0o755)
        self.configure(command=[str(self.host)])
        result = self.dispatch_result()
        self.assertNotEqual(result.returncode, 0)
        status = self.status()
        self.assertEqual(status['signals']['fault']['category'], 'unknown')
        self.assertEqual(status['audit'], [])
        self.assertIsNone(status['started'])

    def test_malformed_config_never_echoes_values(self):
        self.config.write_text('{"SERVICE_TOKEN": "do-not-echo-this"}')
        result = self.dispatch_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('do-not-echo-this', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
