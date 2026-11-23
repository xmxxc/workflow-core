"""Run the fixed pyz's supervisor with short local children and no account or network."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

args = argparse.ArgumentParser()
args.add_argument('--candidate', required=True, type=Path)
args.add_argument('--desktop-tools', required=True, type=Path)
opts = args.parse_args()
sys.path.insert(0, str(opts.desktop_tools.resolve()))
import startup
from launch_short import package

SECRET = 'SENSITIVE_FIXTURE_CONTENT'

class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.pyz=str(package(opts.candidate.resolve(),Path(tmp.name))/'wfc.pyz')
        sys.path.insert(0,cls.pyz)
        cls.addClassCleanup(sys.path.remove,cls.pyz)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root/'state'
        self.gen = uuid.uuid4().hex
        folder = self.state/'bundled'
        folder.mkdir(parents=True)
        self.spec = folder/(self.gen+'.host.spec')
        self.dest = startup.capture_dir(self.spec)
        self.dest.mkdir()
        self.rfile = folder/(self.gen+'.launch.json')
        self.sfile = folder/(self.gen+'.json')
        import bundled_runtime as runtime
        self.runtime = runtime
        self.job = {'dispatch': 'd-01234567', 'attempt':'a-123456789abc'}
        self.h = {'unit':self.job['dispatch'],'attempt':self.job['attempt'],'generation':self.gen,
                  'nonce':'fixture-nonce','capabilityHash':runtime.capability_digest(SECRET),
                  'snapshot':str(self.sfile),'receipt':str(self.rfile)}

    def sample(self):
        return startup.preclose(self.state,self.job,self.h,self.runtime)

    def child(self, code, observer):
        argv = [sys.executable, '-c',
                f'import sys,time; print("{SECRET} WFC controlled startup sentinel",file=sys.stderr);'
                f'time.sleep(.12); sys.exit({code})']
        env = {k:os.environ[k] for k in ('SystemRoot','WINDIR','PATH','TEMP','TMP') if k in os.environ}
        env.update(WFC_WORKER_CALLBACK=SECRET,TERM='xterm-256color')
        spec = {'argv':argv,'cwd':str(self.root),'env':env,'receipt':str(self.rfile)}
        startup.put(self.spec,spec)
        startup.put(self.dest/'prepared.json',startup.entry(argv,spec['cwd'],env))
        runner = [sys.executable]
        if observer:
            runner += [str(Path(startup.__file__)), 'supervisor']
        p = subprocess.run([*runner,self.pyz,'_host-run',str(self.spec)],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,code)
        self.assertFalse(self.spec.exists())
        return startup.read(self.rfile)

    def test_original_final_receipt_loses_creation_then_private_cleanup(self):
        final=self.child(17,False)
        self.assertEqual(final['category'],'process_exited')
        self.assertNotIn('pid',final)
        self.assertNotIn('processStart',final)
        shutil.rmtree(self.state)
        self.assertFalse(self.rfile.exists())

    def test_real_supervisor_started_and_exited_or_success(self):
        for code in (17,0):
            final=self.child(code,True)
            self.assertEqual(final['exitCode'],code)
            self.assertNotIn('pid',final)
            self.runtime.publish_cleanup({**self.h,'token':SECRET},
                                 {**self.h,'token':SECRET,'state':'exited','cleanup':'confirmed',
                                  'final_screen':SECRET+' WFC controlled startup sentinel'})
            view,roles=self.sample()
            self.assertTrue(startup.matching_launch(view))
            self.assertTrue(view['signed_end'])
            self.assertTrue(roles['worker']['pid']>0 and roles['worker']['creation'])
            self.assertIn('fixture-startup-sentinel',view['receipt']['diagnostic']['signals'])
            self.assertIn('fixture-startup-sentinel',view['natural_final_screen']['signals'])
            self.assertNotIn(SECRET,json.dumps(view))
            self.assertNotIn(str(self.root),json.dumps(view))
            self.assertNotIn('WFC_WORKER_CALLBACK',json.dumps(view))
            raw=startup.read(self.sfile)
            raw['final_screen']='WFC controlled startup sentinel altered'
            startup.put(self.sfile,raw)
            self.assertFalse(self.sample()[0]['natural_final_screen']['present'])
        (self.dest/'started.json').unlink()
        self.assertFalse(self.sample()[1]['worker'])
        different={**self.h,'attempt':'another-attempt'}
        with self.assertRaises(ValueError):
            startup.preclose(self.state,self.job,different,self.runtime)
        (self.dest/'audit.json').unlink()
        self.assertFalse(startup.matching_launch(self.sample()[0]))

    def test_trial_early_close_with_and_without_started_mirror(self):
        import desktop_probe, wfc_platform
        desktop_probe.platform.platform()
        for mirror in (True,False):
            home=self.root/('home-'+str(mirror))
            home.mkdir()
            calls=[]
            job=dict(self.job,round=1)
            handle={}
            public={}
            def cli(argv, **kw):
                work=Path(kw['cwd'])
                state=work.parent/'private-state'
                if argv[1].endswith('startup.py'):
                    self.assertEqual(argv[2:5],['dispatch',self.pyz,'dispatch'])
                    op='dispatch'
                else:
                    op=argv[2]
                calls.append(op)
                if op=='dispatch':
                    job['scope']=work.parent.name
                    folder=state/'bundled'
                    folder.mkdir()
                    sn=folder/(self.gen+'.json')
                    rc=folder/(self.gen+'.launch.json')
                    handle.update(self.h,snapshot=str(sn),receipt=str(rc))
                    ident={'pid':4711,'creation':'windows:1234'}
                    for role in ('guardian','service','kernel'):
                        handle[role]=ident
                    self.runtime.publish_cleanup({**handle,'token':SECRET},{**handle,'token':SECRET,
                        'state':'exited','cleanup':'confirmed','final_screen':SECRET+' WFC controlled startup sentinel'})
                    startup.put(rc,{'category':'process_exited','exitCode':17,'diagnostic':SECRET+' WFC controlled startup sentinel'})
                    dest=state/('observe-'+self.gen)
                    dest.mkdir()
                    if mirror:
                        startup.put(dest/'started.json',ident)
                    public.update(job,cwd=str(work),backendHandle=handle,backendAlive=False,
                                  launch=startup.read(rc))
                    value=job
                elif op=='status':
                    value={'dispatches':[public],'waiters':[]}
                elif op=='close':
                    self.assertTrue((work.parent/'evidence'/'result.json').is_file())
                    already=startup.read(work.parent/'evidence'/'result.json')
                    self.assertTrue(already['startup']['natural_final_screen']['present'])
                    value={**job,'result':'completed'}
                else:
                    self.fail('unexpected operation '+op)
                return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
            with patch.object(subprocess,'run',side_effect=cli), patch.object(wfc_platform,'process_probe',
                    return_value={'state':'exited','identity':None}):
                trial,result=desktop_probe.run_trial(Path(self.pyz).parent,['fixture'],home,self.root,
                            kind='native_collector_fixture',cli_version='fixture',cli_sha256='fixture')
            self.assertEqual(calls.count('close'),1)
            self.assertEqual(result['reply_invocations'],0)
            self.assertEqual(result['verdict'],'blocked')
            self.assertFalse((trial/'evidence'/'inputs.jsonl').exists() or (trial/'private-state').exists())
            self.assertNotIn(SECRET,json.dumps(result))
            self.assertTrue(result['cleanup']['confirmed'])
            self.assertEqual(result['cleanup']['all_four_exited'],mirror)
            self.assertEqual(result['cleanup']['identities']['worker'],'exit-verified' if mirror else 'creation-missing')

    def test_dispatch_observer_changes_only_supervisor_argv(self):
        import host_launch
        self.dest.rmdir()
        spec={'argv':['native','--no-daemon',SECRET],'env':{'CODEX_HOME':str(self.root),
            'WFC_WORKER_CALLBACK':SECRET},'cwd':str(self.root),'receipt':str(self.rfile)}
        service=self.spec.with_name(self.gen+'.spec')
        outer={'host_spec':str(self.spec),'argv':[sys.executable,self.pyz,'_host-run',str(self.spec)],
               'env':spec['env'],'cwd':spec['cwd']}
        def packaged_dispatch(*args,**kw):
            host_launch.write_private(self.spec,spec)
            host_launch.write_private(service,outer)
        with patch.object(host_launch,'write_private',host_launch.write_private), patch.object(
                sys,'argv',list(sys.argv)), patch('runpy.run_path',side_effect=packaged_dispatch):
            startup.driver(self.pyz,['dispatch'])
        self.assertEqual(startup.read(self.spec),spec)
        actual=startup.read(service)
        expected=[sys.executable,str(Path(startup.__file__).resolve()),'supervisor',*outer['argv'][1:]]
        self.assertEqual(actual,{**outer,'argv':expected})
        self.assertNotIn(SECRET,(self.dest/'prepared.json').read_text())

    def test_diff_and_unavailable(self):
        before=startup.entry(['native','--no-daemon','-m','demo',SECRET],'/cwd',{'TERM':'xterm'})
        after=startup.entry(['native','--no-daemon','-m','other',SECRET,'extra'],'/other',{'TERM':SECRET})
        diff=startup.compare(before,after)
        self.assertEqual(diff['argv_differences'],[3,5])
        self.assertEqual(diff['env_differences'],['TERM'])
        self.assertFalse(diff['cwd_matches'] or diff['cmdline_matches'])
        self.assertEqual(startup.compare({},after),{'available':False})
        self.assertNotIn(SECRET,json.dumps(after))

    def test_pyz_windows_argument_expansion(self):
        import host_launch, router, wfc_platform
        self.assertTrue(str(host_launch.__file__).startswith(self.pyz))
        cfg=self.root/'launch.json'
        native=self.root/'codex.exe'
        native.touch(mode=0o700)
        cfg.write_text(json.dumps({'hosts':{'codex':{'command':[str(native),'--no-daemon'],
                                                    'env':{'CODEX_HOME':'profile'}}}}))
        work=self.root/'work'
        with patch.object(wfc_platform,'WINDOWS',True):
            brief=router.worker_brief(self.job['dispatch'], work/'task.md', work/'report.md',attempt=self.job['attempt'])
        argv,base_env=router.codex_command(brief,[self.state,work],work,None,effort=None)
        for supplied in ({},{'TERM':'xterm-256color'}):
            cmd,env=host_launch.configure('codex',argv,base_env,str(cfg),
                                inherited={**supplied,'WFC_OWNER_TOKEN':SECRET,'CODEX_HOME':'router-home'})
            actual=host_launch.prepare(cmd,env,work,platform='win32')
            self.assertEqual(actual,[str(native),'--no-daemon','--no-alt-screen','-s','workspace-write','-a','never',
                                     '--add-dir',str(self.state),'--add-dir',str(work),brief])
            self.assertIn('Start-Process',actual[-1])
            self.assertEqual(env,{'CODEX_HOME':'profile',**supplied})
            self.assertEqual(len(actual),12)
        node=self.root/'node.exe'
        script=self.root/'codex.js'
        node.touch(mode=0o700)
        script.touch()
        cfg.write_text(json.dumps({'hosts':{'codex':{'runtime':[str(node)],'command':[str(script),'--no-daemon'],
                                           'env':{'TERM':'xterm-256color'}}}}))
        argv,base_env=router.codex_command(brief,[self.state,work],work,'gpt-6.1-sol',effort='high')
        cmd,env=host_launch.configure('codex',argv,base_env,str(cfg),inherited={'TERM':'dumb'})
        expanded=host_launch.prepare(cmd,env,work,platform='win32')
        self.assertEqual(expanded[:3],[str(node),str(script),'--no-daemon'])
        self.assertEqual(expanded[8:12],['-m','gpt-6.1-sol','-c','model_reasoning_effort=high'])
        self.assertEqual(expanded[-1],brief)
        self.assertEqual(env['TERM'],'xterm-256color')
        print('PASS fixed pyz Windows branch: 12 arguments, brief index 11, two add-dir, TERM inherited or absent')

if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]],verbosity=2)
