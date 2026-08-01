"""The constraint that only a comment currently holds up.

libs/utils.js carries "Must stay consistent with message_toggle.py
batch_context_manage" above classifyMessageType, and the two implementations do
agree today. A comment cannot enforce that, and this repository already contains
a case of exactly that failure elsewhere: styles.css replaced a blinking
keyframe with a breathing one and said so in a comment, while a call site in
kanban.js kept passing steps(2) and undid the fix locally.

The drift this guards against is quiet. Add a tool-result prefix to the backend
and forget the frontend, and the same bubbles count as tool_result in the
context manager but as user in the visualisation — two views disagreeing, with
no error on either side.
"""
import re

import pytest

JS_FILE = 'libs/utils.js'
PY_FILE = 'api/message_toggle.py'

# Windowed rather than whole-file: an unrelated startsWith added elsewhere in
# either file would otherwise fail this test for no reason. The windows are
# generous — both functions are far shorter than this — so an edit inside them
# stays covered.
JS_ANCHOR = 'function classifyMessageType'
JS_WINDOW = 15
PY_ANCHOR = 'def batch_context_manage'
PY_WINDOW = 60

_JS_STARTS = re.compile(r"startsWith\('([^']*)'\)")
_JS_ENDS = re.compile(r"endsWith\('([^']*)'\)")
_PY_STARTS = re.compile(r"startswith\('([^']*)'\)")
_PY_ENDS = re.compile(r"endswith\('([^']*)'\)")


def _window(text, anchor, span):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if anchor in line:
            return '\n'.join(lines[i:i + span])
    pytest.fail('anchor %r not found — the function was renamed or moved'
                % anchor)


@pytest.fixture(scope='module')
def js_body(read_text):
    return _window(read_text(JS_FILE), JS_ANCHOR, JS_WINDOW)


@pytest.fixture(scope='module')
def py_body(read_text):
    return _window(read_text(PY_FILE), PY_ANCHOR, PY_WINDOW)


def test_tool_result_prefixes_match(js_body, py_body):
    js = set(_JS_STARTS.findall(js_body))
    py = set(_PY_STARTS.findall(py_body))
    assert js, 'no prefixes extracted from the JS side'
    assert js == py, (
        'tool-result prefixes have drifted.\n  only in %s: %s\n  only in %s: %s'
        % (JS_FILE, sorted(js - py), PY_FILE, sorted(py - js)))


def test_thinking_suffix_matches(js_body, py_body):
    js = set(_JS_ENDS.findall(js_body))
    py = set(_PY_ENDS.findall(py_body))
    assert js, 'no suffix extracted from the JS side'
    assert js == py, (
        'the thinking suffix has drifted.\n  %s: %s\n  %s: %s'
        % (JS_FILE, sorted(js), PY_FILE, sorted(py)))


def test_both_sides_key_thinking_off_cc_type(js_body, py_body, read_text):
    """cc_type, not tool_type.

    The rev branch unified this after five read sites tested a key the writer
    never set, so every one of those conditions was dead. tool_type must not
    come back on either side.
    """
    assert 'cc_type' in js_body
    assert 'cc_type' in py_body
    for rel in (JS_FILE, PY_FILE):
        text = read_text(rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            if 'tool_type' in line:
                stripped = line.strip()
                # A comment explaining the fix is fine; a live read is not.
                assert stripped.startswith(('//', '#')), (
                    '%s:%d reads tool_type outside a comment: %s'
                    % (rel, lineno, stripped))


def test_classification_order_is_documented_in_both(js_body, py_body):
    """assistant is decided before tool on both sides.

    An assistant bubble carrying is_tool_result files as assistant. Swapping the
    two branches on one side only would silently re-file that whole class of
    bubble in one view.
    """
    js_assistant = js_body.index("'assistant'")
    js_tool = js_body.index("'tool_result'")
    assert js_assistant < js_tool
    py_assistant = py_body.index("'assistant'")
    py_tool = py_body.index("'tool_result'")
    assert py_assistant < py_tool