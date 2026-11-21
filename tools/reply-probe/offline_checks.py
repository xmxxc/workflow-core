"""Offline oracles using the real Linux evidence shape and synthetic Windows run data."""
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from local_provider import Provider, choose
from reply_observer import EXPECTED, sha
from windows_probe import rollout


class Fixture:
    item = Provider.item
    def __init__(self):
        self.records = []
        self.stages = set()
        self.lock = threading.Lock()
        self.allow_ask = threading.Event()
        self.after_ask = threading.Event()
        self.opened = threading.Event()
        self.ask_returned = threading.Event()
        self.allow_ask.set()
        self.after_ask.set()
        self.error = None

def u(t):
    return dict(type='message',role='user',content=[dict(type='input_text',text=t)])
f=Fixture()
tools=[dict(type='namespace',name='mcp__wfcprobe',tools=[dict(type='function',name='callback')])]
brief='You are a worker dispatched by the controller (dispatch d-test). --attempt a-test'
def request(item):
    return f.item(dict(tools=tools,input=[item]))[1]
request(u('auxiliary'))
assert request(u(brief))['arguments']=='{"stage": "ask"}'
request(dict(type='function_call_output',call_id='call_wfc_ask',output='WFC_LOCAL_CALLBACK_ASK'))
assert f.ask_returned.is_set()
request(u('auxiliary two'))
assert request(u(EXPECTED))['arguments']=='{"stage": "finish"}'
request(dict(type='function_call_output',call_id='call_wfc_finish',output='WFC_LOCAL_CALLBACK_FINISH'))
assert [r['assistant_request'] for r in f.records]==['auxiliary','ask','ask_returned','auxiliary','finish','finish_returned']
assert [r['end_user_hash'] for r in f.records if r['exact_users']]==[sha(EXPECTED)]
with TemporaryDirectory() as d:
    cwd=Path(d)
    day=cwd/'sessions'
    day.mkdir()
    log=day/'rollout-local-s1.jsonl'
    def row(p):return dict(type='response_item',payload=p)
    rows=[dict(type='session_meta',payload=dict(id='s1',source='cli',cwd=str(cwd),cli_version='0.164.0')),
          row(u(brief)),row(u('D_GO')),row(u(EXPECTED)),dict(type='event_msg',payload=dict(type='user_message',message=EXPECTED))]
    def read():
        log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return rollout(cwd,dict(dispatch='d-test',attempt='a-test'),cwd)[0]
    found=read()
    assert found['exact']==[4] and found['projected']==[5]
    rows.append(row(u(EXPECTED)))
    assert len(read()['exact'])==2
    rows[0]['payload']['source']={'subagent':{}}
    try:read()
    except IndexError:pass
    else:raise AssertionError('subagent accepted')
print('PASS fixed worker callback sequence, ancillary requests, exact wrapped user text, projection, duplicate, subagent')

from windows_probe import tracked_identities
sample = {'guardian':{'pid':5,'creation':'w:5'},'kernel':{'pid':6,'creation':'w:6'},
          'service':{'pid':7,'creation':'w:7'}}
exit_only = {'category':'process_exited','exitCode':1}
assert [p['role'] for p in tracked_identities(sample,exit_only,{})]==['guardian','kernel','service']
recorded = {'pid':8,'processStart':'w:8'}
assert tracked_identities(sample,exit_only,recorded)[-1]=={'role':'host','pid':8,'creation':'w:8'}
assert tracked_identities(sample,recorded,{}) == tracked_identities(sample,exit_only,recorded)
assert tracked_identities({},exit_only,{}) == []
print('PASS public early exit receipt with missing pid, captured creation identity, missing roles')
