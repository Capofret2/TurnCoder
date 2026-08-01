"""batch_context_manage: classification order, filters, and reversibility.

The classification is exercised through the real method rather than by
re-deriving it in the test, because a re-derivation only ever proves the test
agrees with itself. Each case is probed by asking which single type filter
matches it.

The ordering case is the one worth having: assistant is decided before tool, so
an assistant bubble carrying is_tool_result files as assistant. That is invisible
when reading either branch alone and is exactly what a refactor reorders.
"""
import pytest

from api.message_toggle import MessageToggleMixin

TYPES = ('thinking', 'assistant', 'tool_result', 'user')
THINK_SUFFIX = ' (\u601d\u8003\u8fc7\u7a0b)'


class _Api(MessageToggleMixin):
    """Minimal host for the mixin. Only these five members are touched."""

    def __init__(self, history, sid='s1'):
        self.sessions = {sid: {'conversation_history': history}}
        self._active_sid = sid
        self.global_settings = {}
        # Deliberately absent from disk: the purge branch guards every unlink
        # with os.path.exists, so a missing directory must be harmless.
        self.data_dir = '/tmp/turncoder-tests-no-such-dir'
        self.save_calls = 0

    @property
    def _session(self):
        return self.sessions[self._active_sid]

    def save_sessions(self, push_update=False):
        self.save_calls += 1


def _run(history, types, action, **filters):
    filters['types'] = list(types)
    api = _Api([dict(m) for m in history])
    res = api.batch_context_manage(filters, action)
    return res['count'], api._session['conversation_history']


def _classify(msg):
    """Which of the four types does batch_context_manage assign to msg?"""
    hits = [t for t in TYPES if _run([msg], [t], 'hide')[0] == 1]
    assert len(hits) <= 1, 'a message matched more than one type: %r' % (hits,)
    return hits[0] if hits else None


# ------------------------------------------------------------ classification

def test_thinking_detected_by_model_name_suffix():
    assert _classify({'id': 1, 'role': 'assistant',
                      'model_name': 'gpt' + THINK_SUFFIX}) == 'thinking'


def test_thinking_detected_by_cc_type():
    """cc_type, not tool_type: worker_engine writes the former."""
    assert _classify({'id': 2, 'role': 'assistant',
                      'cc_type': 'thinking'}) == 'thinking'


def test_thinking_outranks_role():
    assert _classify({'id': 3, 'role': 'user',
                      'model_name': 'x' + THINK_SUFFIX}) == 'thinking'


def test_plain_assistant_is_assistant():
    assert _classify({'id': 4, 'role': 'assistant', 'content': 'hi'}) == 'assistant'


def test_assistant_wins_over_tool_result():
    """The ordering case. assistant is checked before is_tool_result."""
    assert _classify({'id': 5, 'role': 'assistant',
                      'is_tool_result': True}) == 'assistant'


@pytest.mark.parametrize('extra', [
    {'is_tool_result': True},
    {'tool_use_id': 'toolu_abc'},
    {'content': '**Tool Result** (tool: toolu_x)\n\nbody'},
    {'content': '**Tool Error** (tool: toolu_x)\n\nboom'},
    {'content': '**\u5ba1\u7a3f [1/45] paper.txt (model) \u6211\u65b9\u80dc**'},
    {'content': '**\u89c4\u5212 [2/45] paper.txt (model)**'},
    {'content': '**\u805a\u5408\u89c4\u5212 [1/3] (model)**'},
])
def test_user_role_tool_markers_are_tool_results(extra):
    msg = {'id': 6, 'role': 'user'}
    msg.update(extra)
    assert _classify(msg) == 'tool_result'


def test_plain_user_is_user():
    assert _classify({'id': 7, 'role': 'user', 'content': 'hello'}) == 'user'


def test_tool_marker_must_be_a_prefix_and_exact():
    """Prefix match, so the same words mid-string stay a user turn."""
    assert _classify({'id': 8, 'role': 'user',
                      'content': 'see **Tool Result** below'}) == 'user'
    assert _classify({'id': 9, 'role': 'user',
                      'content': 'Tool Result (tool: x)'}) == 'user'


# ------------------------------------------------------------------- filters

def _mixed():
    return [
        {'id': 10, 'role': 'user', 'content': 'a'},
        {'id': 20, 'role': 'assistant', 'content': 'b'},
        {'id': 30, 'role': 'user', 'content': 'c', 'image': {'path': 'p.png'}},
    ]


def test_empty_type_list_matches_everything():
    count, _ = _run(_mixed(), [], 'hide')
    assert count == 3


def test_has_image_alone_matches_only_images():
    count, hist = _run(_mixed(), ['has_image'], 'hide')
    assert count == 1
    assert hist[2]['is_hidden'] is True
    assert 'is_hidden' not in hist[0]


def test_has_image_unions_with_a_type():
    count, _ = _run(_mixed(), ['assistant', 'has_image'], 'hide')
    assert count == 2


def test_id_bounds_are_inclusive():
    assert _run(_mixed(), [], 'hide', id_min=20)[0] == 2
    assert _run(_mixed(), [], 'hide', id_max=20)[0] == 2
    assert _run(_mixed(), [], 'hide', id_min=20, id_max=20)[0] == 1


def test_size_bounds_use_content_length_over_3000():
    hist = [
        {'id': 1, 'role': 'user', 'content': 'x' * 300},
        {'id': 2, 'role': 'user', 'content': 'x' * 6000},
    ]
    assert _run(hist, [], 'hide', size_min_k=1)[0] == 1
    assert _run(hist, [], 'hide', size_max_k=1)[0] == 1


# ------------------------------------------------------------------- actions

def test_actions_are_idempotent():
    hist = [{'id': 1, 'role': 'user', 'content': 'a'}]
    api = _Api(hist)
    assert api.batch_context_manage({'types': []}, 'hide')['count'] == 1
    assert api.batch_context_manage({'types': []}, 'hide')['count'] == 0


def test_omit_and_expand_are_a_pair():
    hist = [{'id': 1, 'role': 'user', 'content': 'a'}]
    api = _Api(hist)
    assert api.batch_context_manage({'types': []}, 'omit')['count'] == 1
    assert api._session['conversation_history'][0]['is_omitted'] is True
    assert api.batch_context_manage({'types': []}, 'expand')['count'] == 1
    assert api._session['conversation_history'][0]['is_omitted'] is False


def test_purge_only_removes_hidden_messages():
    hist = [
        {'id': 1, 'role': 'user', 'content': 'keep'},
        {'id': 2, 'role': 'user', 'content': 'drop', 'is_hidden': True},
    ]
    count, remaining = _run(hist, [], 'purge')
    assert count == 1
    assert [m['id'] for m in remaining] == [1]


def test_virtual_starred_session_is_refused():
    api = _Api([{'id': 1, 'role': 'user', 'content': 'a'}],
               sid='starred_session_virtual')
    assert api.batch_context_manage({'types': []}, 'hide') == {'count': 0}
    assert api.save_calls == 0


def test_save_is_skipped_when_nothing_matched():
    api = _Api([{'id': 1, 'role': 'user', 'content': 'a'}])
    api.batch_context_manage({'types': ['assistant']}, 'hide')
    assert api.save_calls == 0


# ------------------------------------------------- inline thinking round trip

def test_inline_thinking_hide_then_unhide_restores_the_original():
    """This branch rewrites the user's stored content, so it must be reversible.

    The original text moves into _inline_thinking_originals and the body is
    replaced by a placeholder; unhide has to put it back byte for byte and drop
    the bookkeeping key.
    """
    body = 'secret reasoning'
    original = '[\u601d\u8003\u5f00\u59cb]\n%s\n[\u601d\u8003\u7ed3\u675f]' % body
    api = _Api([{'id': 1, 'role': 'assistant', 'content': original}])

    assert api.batch_context_manage({'types': ['thinking']}, 'hide')['count'] == 1
    msg = api._session['conversation_history'][0]
    assert body not in msg['content']
    assert '\u5df2\u9690\u85cf' in msg['content']
    assert msg['_inline_thinking_originals'] == {'1': body}

    assert api.batch_context_manage({'types': ['thinking']}, 'unhide')['count'] == 1
    msg = api._session['conversation_history'][0]
    assert msg['content'] == original
    assert '_inline_thinking_originals' not in msg