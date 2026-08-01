"""Backend action-layer coverage, driven through Flask's test client.

/api/action is a ~650-line if/elif chain and had no tests at all. The cost of
that was concrete: app.py imported ToolResult from api.cc_executors, a module
that does not exist, so every 拒绝审批 press returned 500 and the tool stayed
pending forever. A single request asserting a 200 would have caught it on the
first run. test_reject_approval_reaches_the_backend below is that request.

Everything this branch added on the backend — both abort scopes, the is_retry
purge, the 400-on-failure contract — was likewise unverified: the DOM cases only
prove a payload left the browser.

The data directory is redirected before `import app`, and that ordering is not
negotiable. app.py builds its Api at module scope, and Api.__init__ reads
CHATAPP_DATA_DIR, creates sessions/ under it and loads every transcript it
finds. Importing without this reads the user's real conversations and writes
them back on the first save.
"""
import json
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix='turncoder_actions_')
os.environ['CHATAPP_DATA_DIR'] = _TMP

import app as app_module  # noqa: E402
from api import _tls  # noqa: E402

TOOL_ID = 'toolu_test_1'


@pytest.fixture
def backend():
    return app_module.backend


@pytest.fixture
def client():
    return app_module.app.test_client()


@pytest.fixture
def sid(client):
    """A fresh session per test, so state never leaks between them."""
    res = client.post('/api/action', json={'action': 'create_session'})
    assert res.status_code == 200
    new_sid = res.get_json().get('new_sid')
    assert new_sid, 'create_session returned no sid'
    # get_full_state and several mixins read the thread-local rather than an
    # argument, and the request handler only sets it from client_sid.
    _tls.active_sid = new_sid
    return new_sid


def _seed_tool_call(backend, sid, status='executing', tool_name='Bash'):
    """One assistant turn holding a single tool call. Returns its msg_index.

    Both the flat tool_id/tool_name/tool_input and the JSON in content are
    written, because every persisted part carries both and _resolve_part_tool_id
    is built to accept either.
    """
    payload = {'type': 'tool_use', 'id': TOOL_ID, 'name': tool_name,
               'input': {'command': 'ls'}}
    backend.sessions[sid]['conversation_history'] = [
        {'id': 1, 'role': 'user', 'content': 'go', 'summary': '', 'created_at': 0},
        {'id': 2, 'role': 'assistant', 'content': 'running', 'summary': '',
         'created_at': 0,
         'content_parts': [{
             'type': 'tool_use_part', 'id': 'p1', 'status': status,
             'tool_id': TOOL_ID, 'tool_name': tool_name,
             'tool_input': payload['input'],
             'content': json.dumps(payload),
         }]},
    ]
    return 1


def _results_for(backend, sid, tool_use_id=TOOL_ID):
    return [m for m in backend.sessions[sid]['conversation_history']
            if m.get('is_tool_result') and m.get('tool_use_id') == tool_use_id]


# ------------------------------------------------------- the import defect

def test_reject_approval_reaches_the_backend(client, backend, sid):
    """Direct evidence for the api.cc_executors -> api.tool_executors fix.

    Before it, this returned 500: ModuleNotFoundError was raised inside the
    branch and swallowed by the handler's outer except. The button had therefore
    never once worked, and no test noticed because none of them went past the
    browser.
    """
    idx = _seed_tool_call(backend, sid, status='pending', tool_name='申请审批')
    res = client.post('/api/action', json={
        'action': 'cc_reject_approval', 'index': idx, 'part_id': 'p1',
        'reason': '不需要', 'client_sid': sid})
    assert res.status_code == 200, res.get_data(as_text=True)[:300]
    settled = _results_for(backend, sid)
    assert len(settled) == 1, 'no result bubble was written, so the tool is still pending'
    assert '不需要' in settled[0]['content']
    part = backend.sessions[sid]['conversation_history'][idx]['content_parts'][0]
    assert part['status'] == 'rejected'


# ------------------------------------------------------------------ abort

def test_abort_scope_one_settles_only_that_call(client, backend, sid):
    """scope='one' must leave autopilot and the queue alone. Folding those in
    would mean aborting a single row silently halted the whole batch."""
    idx = _seed_tool_call(backend, sid)
    sess = backend.sessions[sid]
    sess['autopilot_active'] = True
    sess['autopilot_turns_left'] = 5
    res = client.post('/api/action', json={
        'action': 'cc_abort_tool', 'index': idx, 'part_id': 'p1',
        'scope': 'one', 'client_sid': sid})
    assert res.status_code == 200, res.get_data(as_text=True)[:300]
    body = res.get_json()
    assert body['scope'] == 'one'
    assert body['autopilot_stopped'] is False
    assert body['dropped'] == 0
    assert sess['autopilot_active'] is True, 'a single abort stopped autopilot'
    # Settled, or every sibling behind it blocks forever.
    assert len(_results_for(backend, sid)) == 1
    assert TOOL_ID in sess.get('_aborted_tool_ids', [])


def test_abort_scope_all_stops_autopilot_and_clears_the_queue(client, backend, sid):
    """The four fields stop_autopilot does not touch are the ones that matter
    here: a surviving _approved_tool_queue is released by the next
    _advance_approved_queue, and a surviving _ap_tool_sid lets
    _continue_autopilot_tool_queue claim the right to start another turn."""
    idx = _seed_tool_call(backend, sid)
    sess = backend.sessions[sid]
    sess['autopilot_active'] = True
    sess['autopilot_turns_left'] = 5
    sess['_ap_tool_sid'] = sid
    sess['_approved_tool_queue'] = [
        {'tool_use_id': 'toolu_queued_1', 'tool_data': {}, 'target_sid': sid,
         'msg_index': idx, 'part_id': 'p_absent'}]
    res = client.post('/api/action', json={
        'action': 'cc_abort_tool', 'index': idx, 'part_id': 'p1',
        'scope': 'all', 'client_sid': sid})
    assert res.status_code == 200, res.get_data(as_text=True)[:300]
    body = res.get_json()
    assert body['scope'] == 'all'
    assert body['autopilot_stopped'] is True
    assert body['dropped'] == 1
    assert sess['autopilot_active'] is False
    assert sess['autopilot_turns_left'] == 0
    assert sess['_approved_tool_queue'] == []
    assert '_ap_tool_sid' not in sess
    assert sess['is_processing'] is False
    assert 'toolu_queued_1' in sess['_aborted_tool_ids']


def test_abort_defaults_to_the_batch_scope(client, backend, sid):
    """A caller omitting the field must keep the endpoint's original meaning.
    Silently downgrading to 'one' would read as "I pressed abort and autopilot
    kept running", which is far harder to diagnose than an error."""
    idx = _seed_tool_call(backend, sid)
    backend.sessions[sid]['autopilot_active'] = True
    res = client.post('/api/action', json={
        'action': 'cc_abort_tool', 'index': idx, 'part_id': 'p1',
        'client_sid': sid})
    assert res.get_json()['scope'] == 'all'
    assert backend.sessions[sid]['autopilot_active'] is False


def test_abort_with_an_unknown_part_returns_400(client, backend, sid):
    """400 rather than a 200 carrying an error: postAction only raises a toast on
    a non-OK response, so a 200 would swallow the message and leave the user with
    a greyed-out button and no explanation."""
    idx = _seed_tool_call(backend, sid)
    res = client.post('/api/action', json={
        'action': 'cc_abort_tool', 'index': idx, 'part_id': 'no-such-part',
        'client_sid': sid})
    assert res.status_code == 400
    assert res.get_json()['status'] == 'error'


# ------------------------------------------------------------------ retry

def _seed_adopted_with_result(backend, sid):
    idx = _seed_tool_call(backend, sid, status='adopted', tool_name='NoSuchToolHere')
    backend.sessions[sid]['conversation_history'].append({
        'id': 3, 'role': 'user', 'is_tool_result': True, 'tool_use_id': TOOL_ID,
        'content': 'OLD_RESULT_MARKER', 'summary': 'old', 'created_at': 0,
    })
    return idx


def _tool_json(backend, sid, idx):
    return backend.sessions[sid]['conversation_history'][idx]['content_parts'][0]['content']


def test_a_plain_re_accept_is_blocked_by_the_dedup_guard(client, backend, sid):
    """The reason the retry button needs is_retry at all. accept_tool returns
    immediately when a result already exists, so re-dispatching without the flag
    is a silent no-op that also advances the queue a second time."""
    idx = _seed_adopted_with_result(backend, sid)
    res = client.post('/api/action', json={
        'action': 'cc_accept_tool', 'tool_json': _tool_json(backend, sid, idx),
        'index': idx, 'part_id': 'p1', 'client_sid': sid})
    assert res.status_code == 200
    bodies = [m.get('content', '') for m in backend.sessions[sid]['conversation_history']]
    assert any('OLD_RESULT_MARKER' in b for b in bodies), \
        'the guard let the call through, so is_retry would be redundant'


def test_retry_purges_the_previous_result(client, backend, sid):
    """is_retry was on accept_tool's signature and forwarded from app.py all
    along while the body never read it. The tool name is deliberately one with no
    executor, so the re-run produces an unsupported-tool error rather than
    actually running anything."""
    idx = _seed_adopted_with_result(backend, sid)
    res = client.post('/api/action', json={
        'action': 'cc_accept_tool', 'tool_json': _tool_json(backend, sid, idx),
        'index': idx, 'part_id': 'p1', 'is_retry': True, 'client_sid': sid})
    assert res.status_code == 200
    hist = backend.sessions[sid]['conversation_history']
    bodies = [m.get('content', '') for m in hist]
    assert not any('OLD_RESULT_MARKER' in b for b in bodies), \
        'the previous result survived, so the dedup guard will block the re-run'
    # A fresh result took its place, which is what proves the call got through.
    assert len(_results_for(backend, sid)) == 1


def test_retry_clears_the_abort_flag_so_abort_then_retry_works(client, backend, sid):
    idx = _seed_tool_call(backend, sid, tool_name='NoSuchToolHere')
    client.post('/api/action', json={
        'action': 'cc_abort_tool', 'index': idx, 'part_id': 'p1',
        'scope': 'one', 'client_sid': sid})
    assert TOOL_ID in backend.sessions[sid]['_aborted_tool_ids']
    client.post('/api/action', json={
        'action': 'cc_accept_tool', 'tool_json': _tool_json(backend, sid, idx),
        'index': idx, 'part_id': 'p1', 'is_retry': True, 'client_sid': sid})
    assert TOOL_ID not in backend.sessions[sid]['_aborted_tool_ids'], \
        'the id stayed on the abort list, so the retry result would be discarded'


# ------------------------------------------------- recycle bin, no hard delete

def test_delete_is_a_soft_delete_and_restore_undoes_it(client, backend, sid):
    other = client.post('/api/action', json={'action': 'create_session'}).get_json()['new_sid']
    client.post('/api/action', json={'action': 'delete_session', 'sid': other})
    assert other in backend.sessions, 'the session was removed rather than flagged'
    assert backend.sessions[other]['soft_deleted'] is True
    assert backend.sessions[other].get('deleted_at'), 'no timestamp for the bin to sort by'

    client.post('/api/action', json={'action': 'restore_session', 'sid': other})
    assert not backend.sessions[other].get('soft_deleted')
    assert not backend.sessions[other].get('deleted_at')


def test_there_is_no_purge_endpoint(client, backend):
    """Asserted deliberately. Dropping a sid from self.sessions is what makes
    save_sessions' orphan sweep unlink the file, so such an endpoint would be the
    only path in the application that destroys a transcript."""
    assert not hasattr(backend, 'purge_deleted_sessions')


# --------------------------------------------------------- pushed state shape

def test_aborted_ids_are_not_pushed_to_the_frontend(backend, sid):
    """No consumer reads this on the client, and unlike _approved_tool_queue it
    grows monotonically to a 200-entry ceiling while state pushes are frequent
    during autopilot."""
    backend.sessions[sid]['_aborted_tool_ids'] = ['toolu_x', 'toolu_y']
    state = backend.get_full_state()
    pushed = state['sessions'][sid]
    assert 'conversation_history' in pushed, 'the active session was not sent in full'
    assert '_aborted_tool_ids' not in pushed


def test_deleted_at_is_pushed_because_the_bin_displays_it(backend, client, sid):
    other = client.post('/api/action', json={'action': 'create_session'}).get_json()['new_sid']
    client.post('/api/action', json={'action': 'delete_session', 'sid': other})
    _tls.active_sid = sid
    state = backend.get_full_state()
    assert 'deleted_at' in state['sessions'][other], \
        'renderTrashList cannot order or label entries without it'