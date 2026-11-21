"""Fixed Responses SSE provider. Only the identified worker brief, tool output and reply drive the fixture."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from reply_observer import EXPECTED, sha


def user(item):
    if item.get('type', 'message') == 'message' and item.get('role') == 'user':
        parts = item['content']
        if isinstance(parts, str):
            return parts
        if all(p.get('type') == 'input_text' for p in parts):
            return ''.join(p['text'] for p in parts)


def tool_names(tools, namespace=''):
    for item in tools:
        if item.get('type') == 'namespace':
            yield from tool_names(item['tools'], item['name'])
        else:
            yield (namespace, item.get('name',''), item.get('type',''))


def choose(tools, stage):
    # Some versions advertise MCP functions directly. Others put them behind code mode or
    # offer a shell. All variants run the same fixed helper with the worker identity.
    for ns, name, kind in tools:
        if ('wfcprobe' in (ns + name).lower() and name.endswith('callback') and kind == 'function'):
            return ns, name, 'function_call', {'stage':stage}
    q = lambda p: "'" + str(p).replace("'", "''") + "'"
    cmd = '& ' + q(sys.executable) + ' ' + q(Path(__file__).with_name('worker_callback.py')) + ' ' + stage
    for ns, name, kind in tools:
        if kind == 'function' and name in ('shell_command','exec_command'):
            return ns, name, 'function_call', ({'command':cmd} if name == 'shell_command' else {'cmd':cmd})
    for ns, name, kind in tools:
        if kind == 'custom' and name == 'exec':
            return ns, name, 'custom_tool_call', 'text(await tools.mcp__wfcprobe__callback('+json.dumps({'stage':stage})+'))'
    raise ValueError('No supported host callback tool advertised')


class Provider(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, Handler)
        self.records = []
        self.allow_ask = threading.Event()
        self.after_ask = threading.Event()
        self.ask_returned = threading.Event()
        self.opened = threading.Event()
        self.stages = set()
        self.lock = threading.Lock()
        self.error = None

    def item(self, body):
        items = body.get('input', [])
        keys = list(tool_names(body.get('tools', [])))
        users = [t for item in items if (t := user(item)) is not None]
        record = dict(at=time.monotonic(), end_user_hash=sha(users[-1]) if users else None,
                      exact_users=users.count(EXPECTED), assistant_request='auxiliary')
        if users and users[-1].startswith('You are a worker dispatched by the controller (dispatch '):
            record['opening_hash'] = sha(users[-1])
        action = 'auxiliary'
        for item in reversed(items):
            text = user(item)
            if text is not None:
                if text.startswith('You are a worker dispatched by the controller (dispatch '):
                    action = 'ask'
                elif text == EXPECTED:
                    action = 'finish'
                break
            if item.get('type') in ('function_call_output','custom_tool_call_output'):
                call_id = item.get('call_id')
                if call_id in ('call_wfc_ask','call_wfc_finish'):
                    output = json.dumps(item.get('output'))
                    stage = call_id.removeprefix('call_wfc_')
                    if 'WFC_LOCAL_CALLBACK_' + stage.upper() not in output:
                        self.error = 'host-issued ' + stage + ' tool returned without its worker callback receipt'
                        record['tool_error_hints'] = [hint for hint in (
                            'not found', 'Unknown tool', 'denied', 'sandbox', 'RuntimeError', 'KeyError',
                            'ValueError', 'FileNotFoundError', 'timed out') if hint.lower() in output.lower()]
                    action = stage + '_returned'
                break
        with self.lock:
            seq = len(self.records)+1
            record.update(seq=seq, assistant_request=action)
            self.records.append(record)
            if action in ('ask', 'finish'):
                if action in self.stages:
                    self.error = 'duplicate main request stage ' + action
                    raise ValueError(self.error)
                self.stages.add(action)
        if action in ('ask','finish'):
            record['tools'] = keys
            ns, name, typ, args = choose(keys, action)
            record['selected'] = [ns,name,typ]
            if action == 'ask':
                self.opened.set()
                if not self.allow_ask.wait(80):
                    raise TimeoutError('takeover gate')
            tool = dict(type=typ, name=name, call_id='call_wfc_'+action, id='fc_local_'+str(seq), status='completed')
            if ns:
                tool['namespace'] = ns
            if typ == 'function_call':
                tool['arguments'] = json.dumps(args)
            else:
                tool['input'] = args
            return seq, tool
        if action == 'ask_returned':
            self.ask_returned.set()
            if not self.after_ask.wait(65):
                raise TimeoutError('end-of-turn gate')
        message = {'ask_returned':'WFC_LOCAL_ASK_TURN_END','finish_returned':'WFC_LOCAL_REPLY_TURN_END'}.get(action,'Local metadata')
        return seq, dict(type='message', role='assistant', status='completed', id='msg_local_'+str(seq),
                         content=[dict(type='output_text', text=message, annotations=[])])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        if self.path != '/v1/responses' or not 0 < size < 4_000_000:
            return self.send_error(400)
        try:
            seq, item = self.server.item(json.loads(self.rfile.read(size)))
        except Exception as e:
            self.server.error = str(e)[:200]
            return self.send_error(400)
        resp = dict(id='resp_local_'+str(seq), status='in_progress', output=[])
        events = [dict(type='response.created', response=resp),
                  dict(type='response.output_item.done', output_index=0, item=item),
                  dict(type='response.completed', response=dict(resp, status='completed',output=[item],
                       usage=dict(input_tokens=1,output_tokens=1,total_tokens=2)))]
        body = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
