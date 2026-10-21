"""End-to-end test for the real estate deal deadline tracker against MOCK APIs.

No real ClickUp workspace, Slack workspace or email account is used. The script:
  1. starts mock ClickUp (API v2), Slack (chat.postMessage), Resend (emails) and Slack webhook (error alerts) servers,
  2. imports temporary copies of the workflow and the error alert workflow into your n8n
     (API hosts pointed at the mocks, fake keys, webhook paths with an e2e prefix),
  3. plays the scenarios below: forged and invalid requests, new deals with weekend, holiday and daylight saving dates,
     duplicate deliveries, rate limits, partial failures and repair, amendments, the daily digest with paging,
     deduplication, failures and the real schedule trigger,
  4. checks every request the workflow made, the answers it gave and the node each run ended on,
     prints PASS or FAIL per check, then deletes the temporary copies.

The mock ClickUp keeps state like the real one (tasks, subtasks, custom fields, comments, filters, paging) and rejects
malformed requests the way the real API does (missing or wrong token, wrong content type, missing task name, unknown
custom field, assignee outside the workspace, bad filter JSON, unknown parent task).

Requirements: Python 3.9+ (on Windows also `pip install tzdata`), a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  E2E_SAMPLES  optional file path: saves examples of what the workflow sent (for the test report)
Usage: python tests/e2e_mock_test.py
"""
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8784
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'real-estate-deal-deadlines-clickup.json'
ERROR_WORKFLOW = ROOT / 'workflow' / 'error-alert-slack.json'
SLUG = 'real-estate-deal-deadlines-clickup'

SECRET = 'test-webhook-secret'
TOKEN = 'pk_81000000_MOCKTOKEN0000000000000000'
LIST_ID = '901100200300'
DEAL_FIELD = '5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a51'
KEY_FIELD = '5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a52'
TZ_NAME = 'America/Chicago'
TZ = ZoneInfo(TZ_NAME)
HOLIDAYS = {'2026-11-11'}
SLACK_TOKEN = 'xoxb-mock-bot-token'
EMAIL_KEY = 're_mock_key'
EMAIL_FROM = 'Deal Desk <deals@example.com>'
PAGE_SIZE = 5            # the real API pages at 100 tasks; the mock pages at 5 so paging is exercised
JORDAN, PRIYA, CASEY = 81000001, 81000002, 81000009
TEAM = [
    {'name': 'Jordan Lee', 'email': 'jordan.lee@example.com', 'clickup_id': JORDAN, 'slack_id': 'U0JORDAN01'},
    {'name': 'Priya Shah', 'email': 'priya.shah@example.com', 'clickup_id': PRIYA, 'slack_id': 'U0PRIYA002'},
    {'name': 'Casey Morgan', 'email': 'casey.morgan@example.com', 'clickup_id': CASEY, 'slack_id': 'U0CASEY009'},
]
MEMBERS = {81000000: ('Deal Bot', 'deal.bot@example.com'), JORDAN: ('Jordan Lee', 'jordan.lee@example.com'),
           PRIYA: ('Priya Shah', 'priya.shah@example.com'), CASEY: ('Casey Morgan', 'casey.morgan@example.com')}
ENV = {
    'WEBHOOK_SECRET': SECRET, 'CLICKUP_API_TOKEN': TOKEN, 'CLICKUP_LIST_ID': LIST_ID,
    'CLICKUP_DEAL_ID_FIELD_ID': DEAL_FIELD, 'CLICKUP_MILESTONE_FIELD_ID': KEY_FIELD,
    'TIMEZONE': TZ_NAME, 'DEADLINE_TIME': '17:00', 'ROLL_TO_BUSINESS_DAY': 'true', 'HOLIDAYS': ','.join(sorted(HOLIDAYS)),
    'TEAM_MEMBERS': json.dumps(TEAM), 'TC_EMAIL': 'casey.morgan@example.com',
    'DIGEST_DAYS': '7', 'DIGEST_OVERDUE_DAYS': '60', 'DIGEST_CHANNELS': 'slack,email',
    'SLACK_BOT_TOKEN': SLACK_TOKEN, 'EMAIL_API_KEY': EMAIL_KEY, 'EMAIL_FROM': EMAIL_FROM,
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
HOSTS = {'https://api.clickup.com': MOCK, 'https://slack.com': MOCK, 'https://api.resend.com': MOCK}

LOG = []          # every request: dict(svc, method, path, query, headers, body, status)
ALERTS = []       # error workflow messages
FAIL = {'find_down': False, 'digest_down': False, 'create_429_once': set(), 'create_500': set(),
        'comment_500': False, 'slack_fail': set(), 'email_429_once': set()}
LOCK = threading.Lock()


# ------------------------------------------------------------------ mock ClickUp state
class Store:
    def __init__(self):
        self.tasks, self.comments, self.n = {}, [], 0
        self.emails = {}        # Idempotency-Key -> (body hash, email id)
        self.deliveries = []    # emails actually accepted for delivery

    def new_id(self):
        self.n += 1
        return f'86b{self.n:05x}'


S = Store()
PRIORITIES = {1: ('urgent', '#f50000'), 2: ('high', '#f8ae00'), 3: ('normal', '#6fddff'), 4: ('low', '#d8d8d8')}


def member(uid):
    name, email = MEMBERS[uid]
    return {'id': uid, 'username': name, 'color': '#7b68ee', 'initials': ''.join(p[0] for p in name.split()),
            'email': email, 'profilePicture': None}


def priority_obj(p):
    if p is None:
        return None
    name, color = PRIORITIES[p]
    return {'color': color, 'id': str(p), 'orderindex': str(p), 'priority': name}


def custom_fields(values):
    out = []
    for fid, fname in ((DEAL_FIELD, 'Deal ID'), (KEY_FIELD, 'Milestone')):
        f = {'id': fid, 'name': fname, 'type': 'short_text', 'type_config': {}, 'date_created': '1760000000000',
             'hide_from_guests': False, 'required': False}
        if fid in values:
            f['value'] = values[fid]
        out.append(f)
    return out


def task_problem(body, creating):
    """The checks the real API fails a create or update on, as (status, body) or None."""
    def err(msg, code):
        return 400, {'err': msg, 'ECODE': code}
    if creating and (not isinstance(body.get('name'), str) or not body['name'].strip()):
        return err('Task name invalid', 'INPUT_005')
    for f in ('due_date', 'start_date'):
        if f in body and body[f] is not None and (not isinstance(body[f], int) or isinstance(body[f], bool)):
            return err(f'{f} invalid', 'INPUT_006')
    for f in ('due_date_time', 'start_date_time', 'notify_all'):
        if f in body and not isinstance(body[f], bool):
            return err(f'{f} must be a boolean', 'INPUT_007')
    if 'priority' in body and body['priority'] is not None and body['priority'] not in PRIORITIES:
        return err('Priority invalid', 'INPUT_008')
    if creating and 'assignees' in body:
        if not isinstance(body['assignees'], list) or any(a not in MEMBERS for a in body['assignees']):
            return err('Assignees are not members of this Workspace', 'ITEM_062')
    if 'markdown_content' in body and not isinstance(body['markdown_content'], str):
        return err('markdown_content must be a string', 'INPUT_009')
    if creating and body.get('parent') is not None and body['parent'] not in S.tasks:
        return err('Parent task not found', 'ITEM_013')
    for f in body.get('custom_fields', []) if creating else []:
        if f.get('id') not in (DEAL_FIELD, KEY_FIELD) or not isinstance(f.get('value'), str):
            return err('Custom field not found or value invalid', 'FIELD_033')
    return None


def make_task(body):
    tid = S.new_id()
    now = str(int(time.time() * 1000))
    parent = body.get('parent')
    task = {
        'id': tid, 'custom_id': None, 'custom_item_id': 0, 'name': body['name'],
        'text_content': body.get('markdown_content', ''), 'description': body.get('markdown_content', ''),
        'status': {'id': 'p90110_open', 'status': 'to do', 'color': '#87909e', 'orderindex': 0, 'type': 'open'},
        'orderindex': f'{S.n}.00000000000000000000000000000000', 'date_created': now, 'date_updated': now,
        'date_closed': None, 'date_done': None, 'archived': False, 'creator': member(81000000),
        'assignees': [member(a) for a in body.get('assignees', [])], 'group_assignees': [], 'watchers': [],
        'checklists': [], 'tags': [], 'parent': parent, 'top_level_parent': parent,
        'priority': priority_obj(body.get('priority')),
        'due_date': str(body['due_date']) if body.get('due_date') is not None else None,
        'start_date': str(body['start_date']) if body.get('start_date') is not None else None,
        'points': None, 'time_estimate': None, 'time_spent': 0,
        'custom_fields': custom_fields({f['id']: f['value'] for f in body.get('custom_fields', [])}),
        'dependencies': [], 'linked_tasks': [], 'locations': [], 'team_id': '9011000000',
        'url': f'https://app.clickup.com/t/{tid}',
        'sharing': {'public': False, 'public_share_expires_on': None, 'public_fields': ['assignees', 'priority', 'due_date'],
                    'token': None, 'seo_optimized': False},
        'permission_level': 'create', 'list': {'id': LIST_ID, 'name': 'Transactions', 'access': True},
        'project': {'id': '90115000', 'name': 'Brokerage', 'hidden': False, 'access': True},
        'folder': {'id': '90115000', 'name': 'Brokerage', 'hidden': False, 'access': True}, 'space': {'id': '90110000'},
    }
    S.tasks[tid] = task
    return task


def field(task, fid):
    return next((f.get('value') for f in task['custom_fields'] if f['id'] == fid), None)


def list_tasks(query):
    one = lambda k: (query.get(k) or [None])[0]
    for k in ('due_date_gt', 'due_date_lt', 'page'):
        if one(k) is not None and not re.fullmatch(r'\d+', one(k)):
            return 400, {'err': f'{k} invalid', 'ECODE': 'INPUT_010'}
    filters = []
    if one('custom_fields') is not None:
        try:
            filters = json.loads(one('custom_fields'))
            # The real API has more operators; this mock only needs the two for text fields.
            assert isinstance(filters, list) and all(f['field_id'] in (DEAL_FIELD, KEY_FIELD) and f['operator'] in ('=', '==')
                                                     and isinstance(f['value'], str) for f in filters)
        except Exception:
            return 400, {'err': 'Custom field filter invalid', 'ECODE': 'FIELD_120'}
    if one('order_by') not in (None, 'id', 'created', 'updated', 'due_date'):
        return 400, {'err': 'order_by invalid', 'ECODE': 'INPUT_011'}
    tasks = [t for t in S.tasks.values() if not t['archived']]
    if one('subtasks') != 'true':
        tasks = [t for t in tasks if t['parent'] is None]
    if one('include_closed') != 'true':
        tasks = [t for t in tasks if t['status']['type'] != 'closed']
    if one('due_date_gt') is not None:
        tasks = [t for t in tasks if t['due_date'] is not None and int(t['due_date']) > int(one('due_date_gt'))]
    if one('due_date_lt') is not None:
        tasks = [t for t in tasks if t['due_date'] is not None and int(t['due_date']) < int(one('due_date_lt'))]
    for f in filters:
        if f['operator'] == '==':      # exact match for text fields
            tasks = [t for t in tasks if field(t, f['field_id']) == f['value']]
        else:                          # "=" on a text field is ClickUp's fuzzy "contains" match
            tasks = [t for t in tasks if f['value'].lower() in str(field(t, f['field_id']) or '').lower()]
    if one('order_by') == 'due_date':
        tasks.sort(key=lambda t: int(t['due_date'] or 0))
    page = int(one('page') or 0)
    chunk = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    return 200, {'tasks': copy.deepcopy(chunk), 'last_page': (page + 1) * PAGE_SIZE >= len(tasks)}


# ------------------------------------------------------------------ mock HTTP server
class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PUT(self):
        self.handle_any('PUT')

    def handle_any(self, method):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = 'INVALID JSON'
        path = url.path
        svc = ('clickup' if path.startswith('/api/v2/') else 'slack' if path == '/api/chat.postMessage'
               else 'email' if path == '/emails' else 'alert' if path == '/slack/hook' else 'other')
        entry = {'svc': svc, 'method': method, 'path': path, 'query': parse_qs(url.query), 'headers': headers, 'body': body}
        with LOCK:
            LOG.append(entry)
            code, out, extra = self.route(svc, method, path, entry)
            entry['status'] = code
        self._send(code, out, extra)

    def route(self, svc, method, path, e):
        body, headers = e['body'], e['headers']
        if svc == 'alert':
            ALERTS.append(body.get('text', '') if isinstance(body, dict) else '')
            return 200, 'ok', None
        if svc == 'clickup':
            if 'authorization' not in headers:
                return 400, {'err': 'Authorization header required', 'ECODE': 'OAUTH_017'}, None
            if headers['authorization'] != TOKEN:
                return 401, {'err': 'Token invalid', 'ECODE': 'OAUTH_025'}, None
            if method in ('POST', 'PUT') and not headers.get('content-type', '').startswith('application/json'):
                return 400, {'err': 'Content-Type must be application/json', 'ECODE': 'INPUT_001'}, None
            if method in ('POST', 'PUT') and not isinstance(body, dict):
                return 400, {'err': 'Body must be a JSON object', 'ECODE': 'INPUT_002'}, None
            m = re.fullmatch(r'/api/v2/list/(\w+)/task', path)
            if m:
                if m.group(1) != LIST_ID:
                    return 401, {'err': 'Team not authorized', 'ECODE': 'OAUTH_027'}, None
                if method == 'GET':
                    if FAIL['find_down'] and 'custom_fields' in e['query']:
                        return 503, {'err': 'Service unavailable', 'ECODE': 'APP_503'}, None
                    if FAIL['digest_down'] and 'custom_fields' not in e['query']:
                        return 503, {'err': 'Service unavailable', 'ECODE': 'APP_503'}, None
                    code, out = list_tasks(e['query'])
                    return code, out, None
                if method == 'POST':
                    problem = task_problem(body, creating=True)
                    if problem:
                        return problem[0], problem[1], None
                    if body['name'] in FAIL['create_429_once']:
                        FAIL['create_429_once'].discard(body['name'])
                        reset = str(int(time.time()) + 2)
                        return 429, {'err': 'Rate limit reached', 'ECODE': 'APP_002'}, {
                            'X-RateLimit-Limit': '100', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset}
                    if body['name'] in FAIL['create_500']:
                        return 500, {'err': 'Internal server error', 'ECODE': 'APP_500'}, None
                    return 200, copy.deepcopy(make_task(body)), None
            m = re.fullmatch(r'/api/v2/task/(\w+)', path)
            if m and method == 'PUT':
                task = S.tasks.get(m.group(1))
                if not task:
                    return 404, {'err': 'Task not found', 'ECODE': 'ITEM_013'}, None
                problem = task_problem(body, creating=False)
                if problem:
                    return problem[0], problem[1], None
                if 'due_date' in body:
                    task['due_date'] = str(body['due_date'])
                if 'markdown_content' in body:
                    task['description'] = task['text_content'] = body['markdown_content']
                task['date_updated'] = str(int(time.time() * 1000))
                return 200, copy.deepcopy(task), None
            m = re.fullmatch(r'/api/v2/task/(\w+)/comment', path)
            if m and method == 'POST':
                if m.group(1) not in S.tasks:
                    return 404, {'err': 'Task not found', 'ECODE': 'ITEM_013'}, None
                if not isinstance(body.get('comment_text'), str) or not body['comment_text'].strip() or not isinstance(body.get('notify_all'), bool):
                    return 400, {'err': 'comment_text and notify_all are required', 'ECODE': 'INPUT_020'}, None
                if FAIL['comment_500']:
                    return 500, {'err': 'Internal server error', 'ECODE': 'APP_500'}, None
                S.comments.append((m.group(1), body['comment_text']))
                return 200, {'id': str(90110000 + len(S.comments)), 'hist_id': str(26500 + len(S.comments)),
                             'date': int(time.time() * 1000)}, None
            return 404, {'err': 'Route not found', 'ECODE': 'APP_404'}, None
        if svc == 'slack':
            auth = headers.get('authorization', '')
            if not auth:
                return 200, {'ok': False, 'error': 'not_authed'}, None
            if auth != 'Bearer ' + SLACK_TOKEN:
                return 200, {'ok': False, 'error': 'invalid_auth'}, None
            if not headers.get('content-type', '').startswith('application/json') or not isinstance(body, dict):
                return 200, {'ok': False, 'error': 'invalid_arguments'}, None
            if not body.get('channel'):
                return 200, {'ok': False, 'error': 'channel_not_found'}, None
            if not body.get('text') and not body.get('blocks'):
                return 200, {'ok': False, 'error': 'no_text'}, None
            if body['channel'] in FAIL['slack_fail']:
                return 200, {'ok': False, 'error': 'channel_not_found'}, None
            ts = f'{time.time():.6f}'
            return 200, {'ok': True, 'channel': 'D' + body['channel'][1:], 'ts': ts,
                         'message': {'type': 'message', 'text': body['text'], 'user': 'U0DEALBOT', 'bot_id': 'B0DEALBOT', 'ts': ts}}, None
        if svc == 'email':
            if headers.get('authorization') != 'Bearer ' + EMAIL_KEY:
                return 401, {'statusCode': 401, 'name': 'missing_api_key', 'message': 'Missing API key in the authorization header.'}, None
            if not isinstance(body, dict) or not body.get('from') or not body.get('subject') or not (body.get('html') or body.get('text')):
                return 422, {'statusCode': 422, 'name': 'validation_error', 'message': 'from, to, subject and html or text are required.'}, None
            to = body.get('to')
            to = [to] if isinstance(to, str) else to
            if not isinstance(to, list) or not to or len(to) > 50 or any(not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', str(t)) for t in to):
                return 422, {'statusCode': 422, 'name': 'validation_error', 'message': 'Invalid `to` field.'}, None
            if to[0] in FAIL['email_429_once']:
                FAIL['email_429_once'].discard(to[0])
                return 429, {'statusCode': 429, 'name': 'rate_limit_exceeded', 'message': 'Too many requests. Please limit the number of requests per second.'}, None
            ikey = headers.get('idempotency-key')
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if ikey and ikey in S.emails:
                if S.emails[ikey][0] != digest:
                    return 409, {'statusCode': 409, 'name': 'invalid_idempotent_request', 'message': 'Same idempotency key used with a different payload.'}, None
                return 200, {'id': S.emails[ikey][1]}, None
            eid = str(uuid.uuid4())
            if ikey:
                S.emails[ikey] = (digest, eid)
            S.deliveries.append({'id': eid, 'key': ikey, 'to': to, 'body': body})
            return 200, {'id': eid}, None
        return 404, {'message': 'not mocked'}, None


# ------------------------------------------------------------------ n8n helpers
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(path, env, paths, schedule_cron='0 0 1 1 *'):
    """A workflow copy with $env values replaced by test values, API hosts pointed at the mocks, e2e webhook paths."""
    wf = json.loads(path.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: "'" + env.get(m.group(1), '') + "'", value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = paths[node['name']]
            node['webhookId'] = str(uuid.uuid4())
        if node['type'] == 'n8n-nodes-base.scheduleTrigger':
            node['parameters']['rule'] = {'interval': [{'field': 'cronExpression', 'expression': schedule_cron}]}
    return wf


class Runner:
    def __init__(self, wid, paths):
        self.wid, self.paths, self.sent, self.mark = wid, paths, 0, 0

    def executions(self):
        return api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])

    def wait(self, count, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('stoppedAt')]
            if len(done) >= count:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def post(self, hook, payload, secret=SECRET, raw=None):
        self.mark = len(LOG)
        data = raw if raw is not None else json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if secret is not None:
            headers['X-Webhook-Secret'] = secret
        req = urllib.request.Request(f'{N8N}/webhook/{self.paths[hook]}', method='POST', data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        self.sent += 1
        ex = self.wait(self.sent)
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = text
        return status, body, ex

    def new(self, svc=None, method=None, path_re=None):
        with LOCK:
            items = list(LOG[self.mark:])
        return [e for e in items if (svc is None or e['svc'] == svc) and (method is None or e['method'] == method)
                and (path_re is None or re.search(path_re, e['path']))]


def details(ex):
    return api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}


def stopped_at(ex):
    result = details(ex).get('resultData') or {}
    return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')


def node_output(ex, name):
    run = ((details(ex).get('resultData') or {}).get('runData') or {}).get(name) or []
    return run[0]['data']['main'][0][0]['json'] if run else None


# ------------------------------------------------------------------ expected values (independent of the workflow code)
def deadline(base, days, roll=True):
    d = base + timedelta(days=days)
    while roll and (d.weekday() >= 5 or d.isoformat() in HOLIDAYS):
        d += timedelta(days=1)
    return d


def ms_of(dt):
    return round(dt.timestamp() * 1000)


def pages_for(n):
    return max(1, -(-n // PAGE_SIZE))


def due_ms(d):
    return ms_of(datetime.combine(d, dtime(17, 0), tzinfo=TZ))


def label(ms):
    t = datetime.fromtimestamp(ms / 1000, TZ)
    return f"{t:%a} {t:%b} {t.day}, {t.year} {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} {t.tzname()}"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat()


def deal_tasks(deal_id):
    return [t for t in S.tasks.values() if field(t, DEAL_FIELD) == deal_id]


def subtask(deal_id, key):
    found = [t for t in deal_tasks(deal_id) if t['parent'] and field(t, KEY_FIELD) == key]
    return found[0] if len(found) == 1 else None


def parent_of(deal_id):
    found = [t for t in deal_tasks(deal_id) if not t['parent']]
    return found[0] if len(found) == 1 else None


def expected_digests(window):
    """Who should get which milestones, in order, computed from the mock's data and the run's own window."""
    now, after, before = window['now'], window['dueAfter'], window['dueBefore']
    items = [t for t in S.tasks.values() if t['parent'] and field(t, KEY_FIELD) and field(t, DEAL_FIELD) and t['due_date']
             and t['status']['type'] not in ('closed', 'done') and after < int(t['due_date']) < before]
    out = {}
    for m in TEAM:
        mine = sorted([t for t in items if any(a['id'] == m['clickup_id'] for a in t['assignees'])],
                      key=lambda t: (int(t['due_date']), t['name']))
        if not mine:
            continue
        groups = []
        for t in mine:
            g = next((g for g in groups if g[0] == field(t, DEAL_FIELD)), None)
            if not g:
                g = (field(t, DEAL_FIELD), [])
                groups.append(g)
            g[1].append(t)
        out[m['name']] = {'ids': [t['id'] for g in groups for t in g[1]],
                          'overdue': sum(int(t['due_date']) < now for t in mine), 'total': len(mine)}
    return out


def lookup_pages(calls):
    """Page numbers of the deal lookups (GET with the Deal ID filter) among these calls."""
    return [c['query'].get('page', ['?'])[0] for c in calls if c['method'] == 'GET' and 'custom_fields' in c['query']]


def slack_ids(text):
    return re.findall(r'<https://app\.clickup\.com/t/(\w+)\|[^>]*>:', text)


def html_ids(html):
    return re.findall(r'<li>(?:<strong[^>]*>OVERDUE</strong> )?<a href="https://app\.clickup\.com/t/(\w+)">', html)


def by_name(email_or_slack):
    for m in TEAM:
        if email_or_slack in (m['email'], m['slack_id']):
            return m['name']
    return None


# ------------------------------------------------------------------ scenarios
RESULTS = []
SAMPLES = {}
EXPECT_FAIL = {}   # execution id -> the Stop node it should end at; each one must raise exactly one alert
UNAUTHORIZED = {'ok': False, 'error': 'unauthorized'}


def check(scenario, label_, ok, detail=''):
    RESULTS.append((scenario, label_, bool(ok), detail))


def expect_fail(ex, node):
    EXPECT_FAIL[str(ex['id'])] = node


D1 = {'deal_id': 'DEAL-1001', 'property_address': '1428 Elm Street, Springfield, IL 62704', 'side': 'buyer',
      'agent_email': 'jordan.lee@example.com', 'contract_date': '2026-10-20', 'closing_date': '2026-11-20',
      'inspection_days': 10, 'appraisal_days': 18, 'financing_days': 22,
      'custom_milestones': [{'name': 'Earnest money due', 'days': 3}, {'name': 'HOA documents review', 'days': 12, 'priority': 'high'}]}


def run_deals(r):
    # 1. Forged requests
    for scenario, secret in (('No secret header', None), ('Wrong secret', 'guess')):
        status, body, ex = r.post('Webhook: Under Contract', D1, secret=secret)
        expect_fail(ex, 'Stop: Bad Secret')
        node, _ = stopped_at(ex)
        check(f'Forged request: {scenario.lower()}', 'refused with 401, ClickUp never called',
              status == 401 and body == UNAUTHORIZED and not r.new('clickup') and node == 'Stop: Bad Secret',
              f'{status}, ended at {node}')
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1001', 'new_closing_date': '2026-12-01'}, secret='guess')
    expect_fail(ex, 'Stop: Bad Secret')
    check('Forged request: amendment', 'refused with 401, ClickUp never called',
          status == 401 and body == UNAUTHORIZED and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Bad Secret', f'{status}')

    # 2. Invalid requests
    bad = {'deal_id': 'DEAL 1001!', 'property_address': ' ', 'side': 'tenant', 'contract_date': '2026-02-30',
           'closing_date': '2026-01-15', 'inspection_days': -2, 'custom_milestones': [{'name': 'Walkthrough'}], 'agent_email': 'not-an-email'}
    status, body, ex = r.post('Webhook: Under Contract', bad)
    expect_fail(ex, 'Stop: Invalid Request')
    expected = ['deal_id is required: letters, numbers, dots, dashes or underscores, up to 64 characters',
                'property_address is required (up to 200 characters)', 'side must be buyer, seller or dual',
                'contract_date must be a real date in YYYY-MM-DD format', 'inspection_days must be a whole number from 0 to 365',
                'custom_milestones[0].days is required', 'agent_email is required']
    check('Invalid deal', 'refused with 400 and every problem listed, ClickUp never called',
          status == 400 and body == {'ok': False, 'deal_id': 'DEAL 1001!', 'errors': expected} and not r.new('clickup')
          and stopped_at(ex)[0] == 'Stop: Invalid Request', f'{status}, {len(body.get("errors", []))} errors')
    status, body, ex = r.post('Webhook: Under Contract', {**D1, 'contract_date': '2026-11-20', 'closing_date': '2026-11-01'})
    expect_fail(ex, 'Stop: Invalid Request')
    check('Invalid deal', 'closing before contract refused with 400',
          status == 400 and body == {'ok': False, 'deal_id': 'DEAL-1001', 'errors': ['closing_date must be after contract_date']}
          and not r.new('clickup'), json.dumps(body.get('errors')))
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1001', 'contract_date': '2026-10-21'})
    expect_fail(ex, 'Stop: Invalid Request')
    check('Invalid amendment', 'a new contract date is refused with 400',
          status == 400 and body.get('errors') == ['contract_date cannot be changed by an amendment. Every deadline counts from it, '
                                                   'so delete the deal task in ClickUp and send the under contract webhook again']
          and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Invalid Request', f'{status}')

    # 3. New deal with weekend, holiday and daylight saving dates
    status, body, ex = r.post('Webhook: Under Contract', D1)
    calls = r.new('clickup')
    contract = date(2026, 10, 20)
    plan = [('custom:earnest-money-due', 'Earnest money due', 3, 3), ('inspection', 'Inspection contingency ends', 10, 2),
            ('custom:hoa-documents-review', 'HOA documents review', 12, 2), ('appraisal', 'Appraisal contingency ends', 18, 2),
            ('financing', 'Financing contingency ends', 22, 2)]
    want = [(k, n, due_ms(deadline(contract, d)), p) for k, n, d, p in plan] + [('closing', 'Closing', due_ms(date(2026, 11, 20)), 1)]
    find = calls[0] if calls else {}
    q = find.get('query', {})
    check('New deal', 'looks up the deal by its Deal ID field first, with the exact match operator',
          len(calls) == 8 and find.get('method') == 'GET' and find.get('path') == f'/api/v2/list/{LIST_ID}/task'
          and json.loads(q.get('custom_fields', ['[]'])[0]) == [{'field_id': DEAL_FIELD, 'operator': '==', 'value': 'DEAL-1001'}]
          and q.get('subtasks') == ['true'] and q.get('include_closed') == ['true'] and find['headers'].get('authorization') == TOKEN,
          f'{len(calls)} ClickUp calls')
    parent = calls[1]['body'] if len(calls) > 1 else {}
    SAMPLES['parent'] = parent
    check('New deal', 'creates the deal task: assignees, contract date as start date, closing as due date',
          calls[1]['method'] == 'POST' and parent.get('name') == D1['property_address'] and parent.get('assignees') == [JORDAN, CASEY]
          and parent.get('start_date') == int(datetime(2026, 10, 20, 12, 0, tzinfo=TZ).timestamp() * 1000)
          and parent.get('start_date_time') is False and parent.get('due_date') == due_ms(date(2026, 11, 20)) and parent.get('due_date_time') is True
          and parent.get('custom_fields') == [{'id': DEAL_FIELD, 'value': 'DEAL-1001'}] and '**Deal ID:** DEAL-1001' in parent.get('markdown_content', '')
          and 'Buyer side' in parent['markdown_content'] and 'Casey Morgan' in parent['markdown_content'], parent.get('name', ''))
    subs = [c['body'] for c in calls[2:]]
    pid = parent_of('DEAL-1001')['id'] if parent_of('DEAL-1001') else None
    got = [(s.get('custom_fields', [{}, {}])[1].get('value'), s.get('name'), s.get('due_date'), s.get('priority')) for s in subs]
    check('New deal', 'creates 6 milestone subtasks in date order with exact due dates and priorities', got == want,
          ', '.join(f'{n} {label(d)}' for _, n, d, _ in got[:2]) + ', ...')
    check('New deal', 'every subtask has the parent, both assignees, a timed due date and both custom fields',
          all(s.get('parent') == pid and s.get('assignees') == [JORDAN, CASEY] and s.get('due_date_time') is True
              and s.get('custom_fields', [{}])[0] == {'id': DEAL_FIELD, 'value': 'DEAL-1001'} for s in subs) and len(subs) == 6, f'parent {pid}')
    SAMPLES['subtask'] = subs[3] if len(subs) > 3 else {}
    notes = {s.get('name'): s.get('markdown_content', '') for s in subs}
    check('New deal', 'a deadline on a Sunday or Saturday moves to Monday and says so',
          'Sun Nov 1, 2026 is a weekend day' in notes.get('HOA documents review', '') and 'Mon Nov 2, 2026 5:00 PM CST' in notes.get('HOA documents review', '')
          and 'Sat Nov 7, 2026 is a weekend day' in notes.get('Appraisal contingency ends', ''), 'Nov 1 and Nov 7 moved')
    check('New deal', 'a deadline on a holiday moves to the next business day',
          'Wed Nov 11, 2026 is a holiday' in notes.get('Financing contingency ends', '') and got[4][2] == due_ms(date(2026, 11, 12)), 'Nov 11 moved to Nov 12')
    check('New deal', '5:00 PM local on both sides of the daylight saving change',
          got[1][2] == int(datetime(2026, 10, 30, 22, 0).replace(tzinfo=ZoneInfo('UTC')).timestamp() * 1000)
          and got[2][2] == int(datetime(2026, 11, 2, 23, 0).replace(tzinfo=ZoneInfo('UTC')).timestamp() * 1000), 'Oct 30 22:00 UTC, Nov 2 23:00 UTC')
    SAMPLES['response_created'] = body
    ms = body.get('milestones', []) if isinstance(body, dict) else []
    check('New deal', 'answers 200 with the task link and every deadline',
          status == 200 and body.get('status') == 'created' and body.get('task_url') == f'https://app.clickup.com/t/{pid}'
          and [(m['key'], m['due'], m['rolled_from']) for m in ms] == [(k, iso(d), rf) for (k, _, d, _), rf in
                                                                     zip(want, [None, None, '2026-11-01', '2026-11-07', '2026-11-11', None])]
          and all(m['created'] and m['task_id'] for m in ms) and body.get('warnings') == [], f"{status} {body.get('status')}")

    # 4. The same webhook again
    status, body, ex = r.post('Webhook: Under Contract', D1)
    calls = r.new('clickup')
    check('Same deal sent twice', 'nothing created the second time (the lookup reads every page first)',
          status == 200 and body.get('status') == 'unchanged' and lookup_pages(calls) == ['0', '1'] and len(calls) == 2
          and len(deal_tasks('DEAL-1001')) == 7, f"{len(calls)} GETs (7 tasks at {PAGE_SIZE} per page), 0 writes, {len(deal_tasks('DEAL-1001'))} tasks in ClickUp")

    status, body, ex = r.post('Webhook: Under Contract', {**D1, 'closing_date': '2026-11-24'})
    calls = r.new('clickup')
    check('Same deal sent again with a different closing date', 'nothing changed; the answer says to send an amendment',
          status == 200 and body.get('status') == 'unchanged' and all(c['method'] == 'GET' for c in calls)
          and body.get('warnings') == ['This deal was already in ClickUp. These dates differ from the existing tasks and were not changed: '
                                       'Closing. Send an amendment to change them.']
          and int(subtask('DEAL-1001', 'closing')['due_date']) == due_ms(date(2026, 11, 20)), '0 writes, 1 warning')

    # The CRM resends the deal with a different contract date: every deadline would move, so nothing is written
    before = copy.deepcopy(deal_tasks('DEAL-1001'))
    status, body, ex = r.post('Webhook: Under Contract', {**D1, 'contract_date': '2026-10-19'})
    expect_fail(ex, 'Stop: Plan Rejected')
    node, error = stopped_at(ex)
    calls = r.new('clickup')
    want_error = ('Deal DEAL-1001 is already in ClickUp with contract date 2026-10-20, but this request says 2026-10-19. Nothing was changed. '
                  'If the contract date really changed, delete the deal task and its subtasks in ClickUp and send the webhook again.')
    check('Same deal sent again with a different contract date', 'refused with 409, nothing written, alert raised',
          status == 409 and body == {'ok': False, 'status': 'conflict', 'deal_id': 'DEAL-1001', 'task_id': parent_of('DEAL-1001')['id'],
                                     'task_url': parent_of('DEAL-1001')['url'], 'error': want_error}
          and all(c['method'] == 'GET' for c in calls) and lookup_pages(calls) == ['0', '1'] and deal_tasks('DEAL-1001') == before
          and node == 'Stop: Plan Rejected' and error == want_error, f'{status}, ended at {node}')

    # A deal ID that is part of another one (DEAL-100 inside DEAL-1001). ClickUp's "=" would match both; "==" does not.
    d0 = {'deal_id': 'DEAL-100', 'property_address': '4 Poplar Row, Fairview, OR 97000', 'side': 'seller',
          'agent_email': 'priya.shah@example.com', 'contract_date': '2026-12-01', 'closing_date': '2027-01-15', 'inspection_days': 10}
    status, body, ex = r.post('Webhook: Under Contract', d0)
    calls = r.new('clickup')
    check('Deal ID inside another deal ID', 'treated as a new deal; the lookup finds nothing from DEAL-1001',
          status == 200 and body.get('status') == 'created' and lookup_pages(calls) == ['0'] and len(calls) == 4
          and sorted(field(t, KEY_FIELD) or 'deal' for t in deal_tasks('DEAL-100')) == ['closing', 'deal', 'inspection']
          and len(deal_tasks('DEAL-1001')) == 7, f"{len(calls)} ClickUp calls, {len(deal_tasks('DEAL-100'))} tasks for DEAL-100")

    # The deal task itself cannot be created: nothing else is written, then a resend creates the whole deal
    d7 = {'deal_id': 'DEAL-1006', 'property_address': '702 Spruce Street, Oakdale, OH 43000', 'side': 'buyer',
          'agent_email': 'jordan.lee@example.com', 'contract_date': '2026-12-02', 'closing_date': '2027-01-08', 'inspection_days': 10}
    FAIL['create_500'].add(d7['property_address'])
    status, body, ex = r.post('Webhook: Under Contract', d7)
    FAIL['create_500'].clear()
    expect_fail(ex, 'Stop: Deal Sync Failed')
    node, error = stopped_at(ex)
    posts = r.new('clickup', 'POST')
    check('ClickUp refuses the deal task (500)', 'retried 3 times, no subtask written, sender gets 502, alert raised',
          status == 502 and body.get('status') == 'failed' and body.get('task_id') is None and body.get('ok') is False
          and [m['created'] for m in body.get('milestones', [])] == [False, False]
          and [c['body']['name'] for c in posts] == [d7['property_address']] * 3 and not deal_tasks('DEAL-1006')
          and node == 'Stop: Deal Sync Failed' and error.startswith('Could not create the deal task for DEAL-1006 in ClickUp'),
          f'{status}, {len(posts)} POSTs, ended at {node}')
    status, body, ex = r.post('Webhook: Under Contract', d7)
    check('ClickUp refuses the deal task (500)', 'resending the webhook creates the deal and its subtasks once',
          status == 200 and body.get('status') == 'created' and len(r.new('clickup', 'POST')) == 3
          and sorted(field(t, KEY_FIELD) or 'deal' for t in deal_tasks('DEAL-1006')) == ['closing', 'deal', 'inspection'],
          f"{status} {body.get('status')}")

    # 5. Cash deal while ClickUp rate limits one call
    d2 = {'deal_id': 'DEAL-1002', 'property_address': '9 Birch Court, Riverton, TX 75001', 'side': 'seller',
          'agent_email': 'priya.shah@example.com', 'contract_date': '2026-10-22', 'closing_date': '2026-11-30',
          'inspection_days': 7, 'custom_milestones': [{'name': 'Seller disclosures due', 'days': 5}]}
    FAIL['create_429_once'].add('Inspection contingency ends')
    status, body, ex = r.post('Webhook: Under Contract', d2)
    posts = [c for c in r.new('clickup', 'POST')]
    names = [c['body']['name'] for c in posts]
    keys = sorted(field(t, KEY_FIELD) or 'deal' for t in deal_tasks('DEAL-1002'))
    check('Rate limited (429) on one subtask', 'retried, and every milestone exists exactly once',
          status == 200 and names.count('Inspection contingency ends') == 2 and [c['status'] for c in posts if c['body']['name'] == 'Inspection contingency ends'] == [429, 200]
          and keys == ['closing', 'custom:seller-disclosures-due', 'deal', 'inspection'], f'{len(posts)} POSTs, tasks: {keys}')
    check('Cash deal', 'no financing or appraisal milestone, seller agent and TC assigned',
          [m['key'] for m in body.get('milestones', [])] == ['custom:seller-disclosures-due', 'inspection', 'closing']
          and all(c['body']['assignees'] == [PRIYA, CASEY] for c in posts), ', '.join(m['name'] for m in body.get('milestones', [])))

    # 6. One subtask keeps failing, then the same webhook repairs the deal
    d3 = {'deal_id': 'DEAL-1003', 'property_address': '310 Cedar Lane, Lakeside, FL 33000', 'side': 'buyer',
          'agent_email': 'jordan.lee@example.com', 'contract_date': '2026-10-26', 'closing_date': '2026-12-04',
          'inspection_days': 10, 'financing_days': 21}
    FAIL['create_500'].add('Financing contingency ends')
    status, body, ex = r.post('Webhook: Under Contract', d3)
    expect_fail(ex, 'Stop: Deal Sync Failed')
    node, error = stopped_at(ex)
    SAMPLES['response_partial'], SAMPLES['alert_partial'] = body, error
    tries = [c for c in r.new('clickup', 'POST') if c['body']['name'] == 'Financing contingency ends']
    check('ClickUp fails one subtask (500)', 'retried 3 times, the others are created, the sender gets 502, the run raises an alert',
          status == 502 and body.get('status') == 'partial' and [f['key'] for f in body.get('failed', [])] == ['financing'] and len(tries) == 3
          and sorted(field(t, KEY_FIELD) or 'deal' for t in deal_tasks('DEAL-1003')) == ['closing', 'deal', 'inspection']
          and node == 'Stop: Deal Sync Failed' and 'Financing contingency ends' in error, f'{status}, ended at {node}')
    FAIL['create_500'].clear()
    status, body, ex = r.post('Webhook: Under Contract', d3)
    posts = r.new('clickup', 'POST')
    check('ClickUp is back, same webhook resent', 'only the missing subtask is created',
          status == 200 and body.get('status') == 'repaired' and [c['body']['name'] for c in posts] == ['Financing contingency ends']
          and posts[0]['body']['parent'] == parent_of('DEAL-1003')['id'] and len(deal_tasks('DEAL-1003')) == 4, f"{len(posts)} POST, status {body.get('status')}")

    # 7. Agent not in TEAM_MEMBERS, no weekend roll, a contingency after closing
    d4 = {'deal_id': 'DEAL-1004', 'property_address': '55 Willow Way, Maple Grove, MN 55300', 'side': 'dual',
          'agent_email': 'sam.rivera@example.com', 'contract_date': '2026-10-21', 'closing_date': '2026-11-21',
          'inspection_days': 10, 'appraisal_days': 35, 'roll_to_business_day': False}
    status, body, ex = r.post('Webhook: Under Contract', d4)
    posts = r.new('clickup', 'POST')
    check('Agent not in TEAM_MEMBERS', 'assigned to the transaction coordinator only, with a warning',
          status == 200 and all(c['body']['assignees'] == [CASEY] for c in posts) and len(posts) == 4
          and body.get('warnings', [''])[0] == 'Agent sam.rivera@example.com is not in TEAM_MEMBERS, so the tasks are assigned to the transaction coordinator only',
          f'{len(posts)} POSTs')
    check('roll_to_business_day false', 'Saturday deadlines stay on Saturday',
          [c['body']['due_date'] for c in posts[1:]] == [due_ms(date(2026, 10, 31)), due_ms(date(2026, 11, 21)), due_ms(date(2026, 11, 25))],
          'Oct 31 and Nov 21 are Saturdays')
    check('Contingency after closing', 'flagged in the answer',
          'Appraisal contingency ends (Wed Nov 25, 2026 5:00 PM CST) is after closing (Sat Nov 21, 2026 5:00 PM CST). Check the contract.' in body.get('warnings', []),
          'appraisal on Nov 25, closing on Nov 21')

    # 8. Amendment: new closing date and a longer financing period
    amend = {'deal_id': 'DEAL-1001', 'amendment_id': 'AMD-1', 'new_closing_date': '2026-11-25', 'financing_days': 25,
             'note': 'Lender asked for more time'}
    old_fin, old_close = due_ms(date(2026, 11, 12)), due_ms(date(2026, 11, 20))
    new_fin, new_close = due_ms(date(2026, 11, 16)), due_ms(date(2026, 11, 25))
    status, body, ex = r.post('Webhook: Amendment', amend)
    pages = lookup_pages(r.new('clickup'))
    calls = [c for c in r.new('clickup') if c['method'] != 'GET']
    fin, close, par = subtask('DEAL-1001', 'financing'), subtask('DEAL-1001', 'closing'), parent_of('DEAL-1001')
    shape = [(c['method'], c['path']) for c in calls]
    check('Amendment', 'moves financing (25 days, Saturday to Monday), closing and the deal task',
          pages == ['0', '1'] and shape == [('PUT', f"/api/v2/task/{fin['id']}"), ('PUT', f"/api/v2/task/{close['id']}"),
                                            ('PUT', f"/api/v2/task/{par['id']}"), ('POST', f"/api/v2/task/{par['id']}/comment")]
          and calls[0]['body']['due_date'] == new_fin and calls[0]['body']['due_date_time'] is True
          and 'set by amendment AMD-1' in calls[0]['body']['markdown_content'] and calls[1]['body']['due_date'] == new_close
          and calls[2]['body'] == {'due_date': new_close, 'due_date_time': True} and int(fin['due_date']) == new_fin,
          f'{len(pages)} lookup pages, then {len(calls)} writes')
    comment = calls[3]['body'] if len(calls) > 3 else {}
    SAMPLES['comment'] = comment.get('comment_text', '')
    want_comment = ('Deadlines changed by amendment AMD-1.\nNote from the sender: Lender asked for more time\n\n'
                    f'Financing contingency ends: moved from {label(old_fin)} to {label(new_fin)}\n'
                    f'Closing: moved from {label(old_close)} to {label(new_close)}')
    check('Amendment', 'adds one comment that says exactly what moved', comment == {'comment_text': want_comment, 'notify_all': True},
          'comment on the deal task')
    SAMPLES['response_amendment'] = body
    check('Amendment', 'answers 200 with the changes',
          status == 200 and body.get('status') == 'updated' and [c['key'] for c in body.get('changes', [])] == ['financing', 'closing', 'deal']
          and body.get('comment_added') is True, f"{status} {body.get('status')}")
    status, body, ex = r.post('Webhook: Amendment', amend)
    calls = r.new('clickup')
    check('Same amendment sent twice', 'no update and no second comment',
          status == 200 and body.get('status') == 'unchanged' and lookup_pages(calls) == ['0', '1'] and len(calls) == 2
          and len([c for c in S.comments if c[0] == par['id']]) == 1, f'{len(calls)} GETs, 0 writes')

    # 9. Amendment adds a contingency the deal did not have
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1002', 'amendment_id': 'AMD-2', 'appraisal_days': 14})
    posts = r.new('clickup', 'POST')
    p2 = parent_of('DEAL-1002')
    new = posts[0]['body'] if posts else {}
    check('Amendment adds an appraisal contingency', 'creates the subtask under the existing deal and comments',
          status == 200 and len(posts) == 2 and new.get('parent') == p2['id'] and new.get('due_date') == due_ms(date(2026, 11, 5))
          and new.get('assignees') == [PRIYA, CASEY] and new.get('custom_fields', [{}, {}])[1] == {'id': KEY_FIELD, 'value': 'appraisal'}
          and posts[1]['body']['comment_text'].endswith(f'Appraisal contingency ends: added, due {label(due_ms(date(2026, 11, 5)))}'), f'{len(posts)} POSTs')

    # 10. Amendment for a deal that was never created
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-9999', 'new_closing_date': '2026-12-15'})
    expect_fail(ex, 'Stop: Plan Rejected')
    node, error = stopped_at(ex)
    want_error = ('Amendment for deal DEAL-9999 was rejected because no deal task in ClickUp has that deal ID. '
                  'Send the under contract webhook first.')
    check('Amendment for an unknown deal', 'refused with 404, nothing written, alert raised',
          status == 404 and body == {'ok': False, 'status': 'not_found', 'deal_id': 'DEAL-9999', 'error': want_error}
          and len(r.new('clickup')) == 1 and node == 'Stop: Plan Rejected' and error == want_error, f'{status}, ended at {node}')

    # 10b. Amendment that moves closing to before the contract date (the contract date is read back from ClickUp)
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1001', 'amendment_id': 'AMD-9', 'new_closing_date': '2026-10-15'})
    expect_fail(ex, 'Stop: Plan Rejected')
    node, error = stopped_at(ex)
    calls = r.new('clickup')
    check('Amendment moves closing before the contract date', 'refused with 400, nothing written, alert raised',
          status == 400 and body.get('status') == 'invalid' and body.get('errors') == ['new_closing_date must be after the contract date (2026-10-20)']
          and all(c['method'] == 'GET' for c in calls) and int(subtask('DEAL-1001', 'closing')['due_date']) == due_ms(date(2026, 11, 25))
          and node == 'Stop: Plan Rejected' and 'new_closing_date 2026-10-15 is not after the contract date 2026-10-20' in error,
          f'{status}, ended at {node}')

    # 10c. Someone cleared the deal task's start date in ClickUp, so the contract date is unknown
    p4 = parent_of('DEAL-1004')
    kept_start, p4['start_date'] = p4['start_date'], None
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1004', 'new_closing_date': '2026-11-28'})
    p4['start_date'] = kept_start
    expect_fail(ex, 'Stop: Plan Rejected')
    node, error = stopped_at(ex)
    check('Deal task lost its start date', 'amendment refused with 409, nothing written, alert raised',
          status == 409 and body.get('status') == 'no_contract_date' and all(c['method'] == 'GET' for c in r.new('clickup'))
          and int(subtask('DEAL-1004', 'closing')['due_date']) == due_ms(date(2026, 11, 21))
          and node == 'Stop: Plan Rejected' and 'the deal task has no start date' in error, f'{status}, ended at {node}')

    # 11. The comment fails after the dates moved
    FAIL['comment_500'] = True
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-1004', 'amendment_id': 'AMD-3', 'new_closing_date': '2026-11-27'})
    expect_fail(ex, 'Stop: Deal Sync Failed')
    node, error = stopped_at(ex)
    FAIL['comment_500'] = False
    check('ClickUp refuses the comment', 'dates still answered as updated, alert carries the comment to add by hand',
          status == 200 and body.get('status') == 'updated' and body.get('comment_added') is False
          and int(subtask('DEAL-1004', 'closing')['due_date']) == due_ms(date(2026, 11, 27)) and len(r.new('clickup', 'POST', r'/comment$')) == 3
          and node == 'Stop: Deal Sync Failed' and 'Please add it by hand' in error, f'ended at {node}')

    # 12. ClickUp is down
    FAIL['find_down'] = True
    status, body, ex = r.post('Webhook: Under Contract', {**D1, 'deal_id': 'DEAL-1005'})
    expect_fail(ex, 'Stop: ClickUp Failed')
    node, _ = stopped_at(ex)
    FAIL['find_down'] = False
    check('ClickUp is down (503)', 'retried 3 times, sender gets 502, nothing written, alert raised',
          status == 502 and len(r.new('clickup', 'GET')) == 3 and not r.new('clickup', 'POST') and node == 'Stop: ClickUp Failed', f'ended at {node}')


def run_digest(r, wf_copy):
    today = datetime.now(TZ).date()
    d5 = {'deal_id': 'DEAL-2001', 'property_address': 'Loft 4 <Harbor> & Pier, 77 Harbor Road, Bayview, CA 94000', 'side': 'buyer',
          'agent_email': 'jordan.lee@example.com', 'contract_date': (today - timedelta(days=15)).isoformat(),
          'closing_date': (today + timedelta(days=3)).isoformat(), 'inspection_days': 10, 'roll_to_business_day': False,
          'custom_milestones': [{'name': 'Earnest money due', 'days': 3}]}
    d6 = {'deal_id': 'DEAL-2002', 'property_address': '18 Aspen Drive, Hillcrest, CO 80000', 'side': 'seller',
          'agent_email': 'priya.shah@example.com', 'contract_date': (today - timedelta(days=2)).isoformat(),
          'closing_date': (today + timedelta(days=40)).isoformat(), 'inspection_days': 30, 'roll_to_business_day': False,
          'custom_milestones': [{'name': 'Earnest money due', 'days': 1}, {'name': 'Lender application', 'days': 3}]}
    for d in (d5, d6):
        status, body, ex = r.post('Webhook: Under Contract', d)
        assert status == 200, (status, body)
    subtask('DEAL-2002', 'custom:earnest-money-due')['status'] = {'id': 'p90110_closed', 'status': 'complete', 'color': '#008844', 'orderindex': 2, 'type': 'closed'}

    status, body, ex = r.post('Webhook: Run Now', {}, secret='guess')
    expect_fail(ex, 'Stop: Bad Run Now Secret')
    check('Run Now with a wrong secret', 'refused with 401, ClickUp never called',
          status == 401 and body == UNAUTHORIZED and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Bad Run Now Secret', f'{status}')

    # First digest: Priya's Slack DM fails, Casey's email is rate limited once
    FAIL['slack_fail'].add('U0PRIYA002')
    FAIL['email_429_once'].add('casey.morgan@example.com')
    status, body, ex = r.post('Webhook: Run Now', {})
    expect_fail(ex, 'Stop: Digest Failed')
    FAIL['slack_fail'].clear()
    node, error = stopped_at(ex)
    window = node_output(ex, 'Digest Window')
    gets = r.new('clickup', 'GET')
    due_q = [g['query'] for g in gets if g['query'].get('subtasks') == ['true']]
    expected = expected_digests(window)
    # With subtasks=true ClickUp returns deal tasks and subtasks, so both count towards the pages.
    total = sum(1 for t in S.tasks.values() if t['status']['type'] != 'closed' and window['dueAfter'] < int(t['due_date'] or 0) < window['dueBefore'])
    pages = [q.get('page', ['?'])[0] for q in due_q]
    run_day = datetime.fromtimestamp(window['now'] / 1000, TZ).date()
    check('Digest', 'asks ClickUp for open tasks and subtasks with the exact due date window, page by page',
          len(due_q) == pages_for(total) and pages == [str(i) for i in range(len(due_q))]
          and all(q.get('due_date_gt') == [str(window['dueAfter'])] and q.get('due_date_lt') == [str(window['dueBefore'])]
                  and q.get('include_closed') == ['false'] and q.get('order_by') == ['due_date'] for q in due_q)
          and window['dueBefore'] == ms_of(datetime.combine(run_day + timedelta(days=7), dtime(23, 59, 59, 999000), tzinfo=TZ)) + 1
          and window['dueAfter'] == ms_of(datetime.combine(run_day - timedelta(days=60), dtime(0, 0), tzinfo=TZ)) - 1,
          f'{total} open tasks in the window, pages {pages}')
    deal_q = [g for g in gets if g['query'].get('subtasks') == ['false']]
    open_deals = len([t for t in S.tasks.values() if not t['parent'] and t['status']['type'] != 'closed'])
    check('Digest', 'reads the deal names once (paged), not once per page of milestones',
          [g['query'].get('page', ['?'])[0] for g in deal_q] == [str(i) for i in range(pages_for(open_deals))], f'{len(deal_q)} page(s)')
    slack = [e for e in r.new('slack') if e['status'] == 200]
    emails = [e for e in r.new('email')]
    got = {}
    for e in slack:
        got.setdefault(by_name(e['body']['channel']), {})['slack'] = e['body']['text']
    for e in emails:
        if e['status'] == 200:
            got.setdefault(by_name(e['body']['to'][0]), {})['email'] = e['body']
    SAMPLES['slack'] = got.get('Jordan Lee', {}).get('slack', '')
    SAMPLES['email'] = got.get('Jordan Lee', {}).get('email', {})
    ok = set(got) == set(expected) and set(expected) == {'Jordan Lee', 'Priya Shah', 'Casey Morgan'}
    for name, exp in expected.items():
        s, em = got.get(name, {}).get('slack', ''), got.get(name, {}).get('email', {})
        ok = ok and slack_ids(s) == exp['ids'] and html_ids(em.get('html', '')) == exp['ids']
        ok = ok and f"{exp['overdue']} overdue, {exp['total'] - exp['overdue']} due in the next 7 days" in s
        ok = ok and s.count('*OVERDUE*') == exp['overdue'] and em.get('html', '').count('>OVERDUE<') == exp['overdue']
    check('Digest', 'one digest per person, grouped by deal, overdue first, exactly the open milestones in the window',
          ok, '; '.join(f"{n}: {e['total']} items, {e['overdue']} overdue" for n, e in expected.items()))
    closed = subtask('DEAL-2002', 'custom:earnest-money-due')['id']
    later = subtask('DEAL-2002', 'inspection')['id']
    check('Digest', 'leaves out completed tasks and tasks due after the window',
          all(closed not in slack_ids(v.get('slack', '')) and later not in slack_ids(v.get('slack', '')) for v in got.values()), 'checked in every digest')
    jordan = got.get('Jordan Lee', {})
    check('Digest', 'escapes deal names for Slack and for HTML email',
          'Loft 4 &lt;Harbor&gt; &amp; Pier' in jordan.get('slack', '') and '<Harbor>' not in jordan.get('slack', '')
          and 'Loft 4 &lt;Harbor&gt; &amp; Pier' in jordan.get('email', {}).get('html', '') and '<Harbor>' not in jordan.get('email', {}).get('html', ''),
          'Loft 4 <Harbor> & Pier')
    keys = [e['headers'].get('idempotency-key', '') for e in emails]
    check('Digest', 'emails go out with an Idempotency-Key, the rate limited one is retried with the same key',
          all(re.fullmatch(rf"deal-digest-{window['today']}-\d+-[0-9a-f]{{8}}", k) for k in keys) and len(emails) == 4
          and [e['status'] for e in emails if e['body']['to'] == ['casey.morgan@example.com']] == [429, 200]
          and len({k for k in keys}) == 3 and all(e['body']['from'] == EMAIL_FROM for e in emails), f'{len(emails)} email requests, 3 delivered')
    check('Digest, Priya\'s Slack DM fails (channel_not_found)', 'the others still go out, the caller gets 502, the run raises an alert',
          status == 502 and body.get('failed') == [{'member': 'Priya Shah', 'channel': 'slack', 'error': 'Slack said channel_not_found'}]
          and len(body.get('sent', [])) == 5 and node == 'Stop: Digest Failed', f'{status}, ended at {node}')

    # Second run: only the failed message is sent again
    SAMPLES['response_digest_failed'] = body
    status, body, ex = r.post('Webhook: Run Now', {})
    SAMPLES['response_digest_retry'] = body
    sent = [(by_name(e['body']['channel']), 'slack') for e in r.new('slack')] + [(by_name(e['body']['to'][0]), 'email') for e in r.new('email')]
    check('Digest run again', 'only Priya\'s failed Slack DM is sent, the rest were already delivered today',
          status == 200 and sent == [('Priya Shah', 'slack')] and len(body.get('skipped', [])) == 5 and stopped_at(ex)[0] == 'Digest Failed?',
          f'sent {sent}')
    status, body, ex = r.post('Webhook: Run Now', {})
    check('Digest run a third time', 'nothing sent at all',
          status == 200 and not r.new('slack') and not r.new('email') and body.get('sent') == [] and len(body.get('skipped', [])) == 6
          and stopped_at(ex)[0] == 'Respond: Nothing To Send', f"{len(body.get('skipped', []))} skipped")

    # A deal changes: only the people on that deal get a new digest
    status, body, ex = r.post('Webhook: Amendment', {'deal_id': 'DEAL-2001', 'new_closing_date': (today + timedelta(days=5)).isoformat()})
    assert status == 200 and body.get('status') == 'updated', body
    status, body, ex = r.post('Webhook: Run Now', {})
    sent = sorted([(by_name(e['body']['channel']), 'slack') for e in r.new('slack')] + [(by_name(e['body']['to'][0]), 'email') for e in r.new('email')])
    check('Digest after an amendment', 'only Jordan and Casey (on that deal) get an updated digest',
          status == 200 and sent == [('Casey Morgan', 'email'), ('Casey Morgan', 'slack'), ('Jordan Lee', 'email'), ('Jordan Lee', 'slack')], f'sent {sent}')
    status, body, ex = r.post('Webhook: Run Now', {'force': True})
    check('Digest with force', 'everyone gets it again, with new idempotency keys',
          status == 200 and len(r.new('slack')) == 3 and len(r.new('email')) == 3
          and all(e['headers']['idempotency-key'].endswith(f"-f{node_output(ex, 'Digest Window')['now']}") for e in r.new('email')), f"{len(body.get('sent', []))} sent")

    # The real schedule trigger: a task is completed, then the scheduled run sends the changed digests only
    subtask('DEAL-2001', 'custom:earnest-money-due')['status'] = {'id': 'p90110_closed', 'status': 'complete', 'color': '#008844', 'orderindex': 2, 'type': 'closed'}
    r.mark = len(LOG)
    wid = r.wid
    api('POST', f'/api/v1/workflows/{wid}/deactivate')
    scheduled = test_copy(WORKFLOW, ENV, r.paths, schedule_cron='* * * * *')
    for node_ in scheduled['nodes']:
        if node_['type'] == 'n8n-nodes-base.webhook':
            node_['webhookId'] = next(n['webhookId'] for n in wf_copy['nodes'] if n['name'] == node_['name'])
    api('PUT', f'/api/v1/workflows/{wid}', {'name': f'TEST {SLUG} (temporary)', 'nodes': scheduled['nodes'],
                                           'connections': scheduled['connections'], 'settings': wf_copy['settings']})
    api('POST', f'/api/v1/workflows/{wid}/activate')
    end = time.time() + 150
    ex = None
    while time.time() < end and not ex:
        time.sleep(2)
        runs = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
        ex = runs[0] if runs else None
    api('POST', f'/api/v1/workflows/{wid}/deactivate')
    r.sent = len([e for e in r.executions() if e.get('stoppedAt')])
    if ex:
        node, _ = stopped_at(ex)
        first = [e for e in LOG[r.mark:] if e['svc'] in ('slack', 'email')]
        sent = sorted({(by_name(e['body']['channel'] if e['svc'] == 'slack' else e['body']['to'][0]), e['svc']) for e in first})
        check('Scheduled run (real cron trigger)', 'runs the same digest; only the people whose list changed get a message',
              ex['status'] == 'success' and node == 'Digest Failed?' and node_output(ex, 'Digest Window')['trigger'] == 'schedule'
              and sent == [('Casey Morgan', 'email'), ('Casey Morgan', 'slack'), ('Jordan Lee', 'email'), ('Jordan Lee', 'slack')],
              f"{ex['status']}, ended at {node}, sent {len(sent)}")
    else:
        check('Scheduled run (real cron trigger)', 'runs the same digest', False, 'no scheduled run within 150 seconds')
    api('PUT', f'/api/v1/workflows/{wid}', {'name': f'TEST {SLUG} (temporary)', 'nodes': wf_copy['nodes'],
                                           'connections': wf_copy['connections'], 'settings': wf_copy['settings']})
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(1.5)

    FAIL['digest_down'] = True
    status, body, ex = r.post('Webhook: Run Now', {'force': True})
    FAIL['digest_down'] = False
    expect_fail(ex, 'Stop: Digest Query Failed')
    node, _ = stopped_at(ex)
    check('ClickUp is down during the digest', 'retried 3 times, caller gets 502, nothing sent, alert raised',
          status == 502 and body == {'ok': False, 'error': 'ClickUp did not answer, so no digest was sent. Please retry later.'}
          and len(r.new('clickup', 'GET')) == 3 and not r.new('slack') and not r.new('email') and node == 'Stop: Digest Query Failed',
          f'ended at {node}')


def run_fail_closed(r):
    for secret in (None, ''):
        status, body, ex = r.post('Webhook: Under Contract', D1, secret=secret)
        expect_fail(ex, 'Stop: Bad Secret')
        check('WEBHOOK_SECRET not set', f"deal webhook {'without a header' if secret is None else 'with an empty header'} is refused",
              status == 401 and body == UNAUTHORIZED and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Bad Secret', f'{status}')
    status, body, ex = r.post('Webhook: Run Now', {}, secret='')
    expect_fail(ex, 'Stop: Bad Run Now Secret')
    check('WEBHOOK_SECRET not set', 'Run Now with an empty header is refused',
          status == 401 and body == UNAUTHORIZED and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Bad Run Now Secret', f'{status}')


def run_misconfigured(r):
    status, body, ex = r.post('Webhook: Under Contract', D1)
    expect_fail(ex, 'Stop: Invalid Request')
    check('Settings missing or wrong', 'deal webhook answers 500 with every setting problem, ClickUp never called',
          status == 500 and body.get('errors') == ['Workflow setting: CLICKUP_LIST_ID is not set',
                                                   'Workflow setting: The transaction coordinator nobody@example.com is not in TEAM_MEMBERS']
          and not r.new('clickup') and stopped_at(ex)[0] == 'Stop: Invalid Request', f'{status}')
    status, body, ex = r.post('Webhook: Run Now', {})
    expect_fail(ex, 'Stop: Digest Not Set Up')
    node, error = stopped_at(ex)
    want = ['Workflow setting: CLICKUP_LIST_ID is not set', 'Workflow setting: DIGEST_CHANNELS can only list slack and email, not sms']
    check('Settings missing or wrong', 'Run Now answers 500 naming each digest setting problem, ClickUp never called',
          status == 500 and body == {'ok': False, 'trigger': 'run_now', 'errors': want} and node == 'Stop: Digest Not Set Up'
          and error == 'The deal digest is not set up, so nothing was sent. CLICKUP_LIST_ID is not set; '
                       'DIGEST_CHANNELS can only list slack and email, not sms.' and not r.new('clickup'), f'{status}, ended at {node}')


def import_workflow(wf, name, settings=None):
    return api('POST', '/api/v1/workflows', {'name': name, 'nodes': wf['nodes'], 'connections': wf['connections'],
                                            'settings': settings or wf.get('settings', {})})['id']


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    created = []
    failed_runs = []
    try:
        err_wf = test_copy(ERROR_WORKFLOW, ENV, {})
        err_id = import_workflow(err_wf, f'TEST {SLUG} error alerts (temporary)')
        created.append(err_id)
        api('POST', f'/api/v1/workflows/{err_id}/activate')     # n8n 2.x only runs an error workflow that is active
        paths = {'Webhook: Under Contract': f'{SLUG}-e2e-under-contract', 'Webhook: Amendment': f'{SLUG}-e2e-amendment',
                 'Webhook: Run Now': f'{SLUG}-e2e-run-now'}
        wf = test_copy(WORKFLOW, ENV, paths)
        wf['settings'] = {**wf.get('settings', {}), 'errorWorkflow': err_id}
        wid = import_workflow(wf, f'TEST {SLUG} (temporary)', wf['settings'])
        created.append(wid)
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        r = Runner(wid, paths)
        for part in (lambda: run_deals(r), lambda: run_digest(r, wf)):
            try:
                part()
            except Exception as e:      # a crash is a failed check, and the cleanup below still runs
                import traceback
                check('Test script', 'scenario crashed', False, ''.join(traceback.format_exception(e))[-1500:])
                break
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
        runners = [r]

        # Fail closed: a second copy with no WEBHOOK_SECRET at all
        paths2 = {k: v.replace('-e2e-', '-e2e-nosecret-') for k, v in paths.items()}
        wf2 = test_copy(WORKFLOW, {**ENV, 'WEBHOOK_SECRET': ''}, paths2)
        wid2 = import_workflow(wf2, f'TEST {SLUG} no secret (temporary)', {**wf2.get('settings', {}), 'errorWorkflow': err_id})
        created.append(wid2)
        api('POST', f'/api/v1/workflows/{wid2}/activate')
        time.sleep(2)
        runners.append(Runner(wid2, paths2))
        run_fail_closed(runners[-1])
        api('POST', f'/api/v1/workflows/{wid2}/deactivate')

        # Misconfigured: a third copy with a missing list ID, a transaction coordinator who is not in the team
        # and an unknown digest channel
        paths3 = {k: v.replace('-e2e-', '-e2e-badconfig-') for k, v in paths.items()}
        wf3 = test_copy(WORKFLOW, {**ENV, 'CLICKUP_LIST_ID': '', 'TC_EMAIL': 'nobody@example.com', 'DIGEST_CHANNELS': 'slack,sms'}, paths3)
        wid3 = import_workflow(wf3, f'TEST {SLUG} bad settings (temporary)', {**wf3.get('settings', {}), 'errorWorkflow': err_id})
        created.append(wid3)
        api('POST', f'/api/v1/workflows/{wid3}/activate')
        time.sleep(2)
        runners.append(Runner(wid3, paths3))
        run_misconfigured(runners[-1])
        api('POST', f'/api/v1/workflows/{wid3}/deactivate')

        # Exactly the runs that should fail did fail, and each raised exactly one alert that links to it,
        # names the node it stopped at and carries its error message
        failed_runs = [e for x in runners for e in x.executions() if e.get('status') == 'error']
        end = time.time() + 45
        while time.time() < end and len(ALERTS) < len(EXPECT_FAIL):
            time.sleep(0.5)
        time.sleep(2)   # a late extra alert would show up here
        messages = {str(e['id']): stopped_at(e)[1] for e in failed_runs}
        by_run = {}
        for a in ALERTS:
            m_id, m_node = re.search(r'/executions/(\d+)', a), re.search(r'failed at \*(.+?)\*', a)
            if m_id and m_node:
                by_run.setdefault(m_id.group(1), []).append((m_node.group(1), a))
        problems = [run for run, node in EXPECT_FAIL.items()
                    if len(by_run.get(run, [])) != 1 or by_run[run][0][0] != node or messages.get(run, '\0') not in by_run[run][0][1]]
        check('Error workflow', 'one alert per failed run, linked to that run, naming its Stop node and carrying its message',
              set(messages) == set(EXPECT_FAIL) and len(ALERTS) == len(EXPECT_FAIL) and not problems and set(by_run) == set(EXPECT_FAIL),
              f'{len(failed_runs)} failed runs, {len(EXPECT_FAIL)} expected, {len(ALERTS)} alerts'
              + (f', mismatched runs {problems}' if problems else ''))
    finally:
        for wid_ in reversed(created):
            try:
                api('POST', f'/api/v1/workflows/{wid_}/deactivate')
            except Exception:
                pass
            try:
                api('DELETE', f'/api/v1/workflows/{wid_}')
            except Exception as e:
                print('could not delete temporary workflow', wid_, e)
        server.shutdown()

    if os.environ.get('E2E_SAMPLES'):
        Path(os.environ['E2E_SAMPLES']).write_text(json.dumps(SAMPLES, indent=2), encoding='utf-8')
    passed = sum(ok for *_, ok, _ in RESULTS)
    last = None
    for scenario, label_, ok, detail in RESULTS:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label_}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == '__main__':
    main()
