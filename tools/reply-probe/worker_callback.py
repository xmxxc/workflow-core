"""Private stdio MCP test tool. Runs ask/done under the launched worker's callback grant."""
import json
import os
from pathlib import Path
import subprocess
import sys


def invoke(stage):
    work = Path(os.environ['PROBE_WORK'])
    job = json.loads((work / 'assignment.json').read_text(encoding='utf-8'))
    if job['dispatch'] != os.environ['WFC_DISPATCH'] or not os.environ.get('WFC_WORKER_CALLBACK'):
        raise ValueError('worker assignment mismatch')
    if stage not in ('ask', 'finish'):
        raise ValueError('unknown fixture action')
    with (work / (stage + '.claimed')).open('x'):
        pass
    args = ['ask', 'D_GO?']
    if stage == 'finish':
        report = work / 'report.md'
        report.write_text('controlled reply consumed ' + job['attempt'], encoding='utf-8')
        args = ['done', '--report', str(report), '--message-id', 'done:' + job['dispatch'] + ':' + job['attempt']]
    p = subprocess.run([sys.executable, os.environ['PROBE_PYZ'], *args, '--attempt', job['attempt']],
                       capture_output=True, text=True, encoding='utf-8', timeout=15)
    if p.returncode:
        raise RuntimeError('worker callback exit ' + str(p.returncode))
    r = json.loads(p.stdout)
    (work / (stage + '.json')).write_text(json.dumps({'messageId': r['messageId'], 'stage': stage}), encoding='utf-8')
    return 'WFC_LOCAL_CALLBACK_' + stage.upper()


def main():
    schema = {'type':'object','properties':{'stage':{'type':'string','enum':['ask','finish']}},
              'required':['stage'],'additionalProperties':False}
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        reply = {'jsonrpc':'2.0','id':request['id']}
        if method == 'initialize':
            reply['result'] = {'protocolVersion':request['params']['protocolVersion'], 'capabilities':{'tools':{}},
                               'serverInfo':{'name':'wfcprobe','version':'1'}}
        elif method == 'tools/list':
            reply['result'] = {'tools':[{'name':'callback','description':'Run the controlled worker callback.',
                                         'inputSchema':schema}]}
        elif method == 'tools/call' and request['params'].get('name') == 'callback':
            try:
                answer = invoke(request['params']['arguments']['stage'])
                reply['result'] = {'content':[{'type':'text','text':answer}]}
            except Exception as e:
                reply['result'] = {'isError':True,'content':[{'type':'text','text':type(e).__name__}]}
        elif method == 'ping':
            reply['result'] = {}
        else:
            reply['error'] = {'code':-32601, 'message':'Method not found'}
        print(json.dumps(reply), flush=True)

if __name__ == '__main__':
    if len(sys.argv) == 2:
        print(invoke(sys.argv[1]), flush=True)
    else:
        main()
