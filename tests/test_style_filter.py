"""apply_style_filter: determinism, position semantics, replayability.

The filter rewrites assistant text in place, and state.py's
update_global_settings stores the pre-filter strings in _style_filter_original so
flipping the switch back can restore them. That makes the change records part of
the contract rather than debug output: if pos stops indexing the *original*
text, the stored originals can no longer be lined up with what the user sees and
the undo path silently corrupts history.

Probabilistic rules are never asserted against a specific hash result. _prob is
md5-based and therefore stable, but pinning its output here would freeze an
implementation detail; the tests sweep msg_id instead and assert that both
outcomes are reachable.
"""
import pytest

from api.style_filter import _prob, apply_style_filter


def _replay(original, records):
    """Apply change records back onto the original, right to left.

    Asserts along the way that each pos indexes the original text — the single
    invariant the undo path in state.py depends on.
    """
    out = original
    for rec in sorted(records, key=lambda r: r['pos'], reverse=True):
        start = rec['pos']
        end = start + len(rec['old'])
        assert original[start:end] == rec['old'], (
            'pos %d does not index the original: expected %r, found %r'
            % (start, rec['old'], original[start:end]))
        out = out[:start] + rec['new'] + out[end:]
    return out


# ---------------------------------------------------------------- empty input

@pytest.mark.parametrize('value', ['', None])
def test_falsy_input_is_returned_unchanged(value):
    assert apply_style_filter(value, 1) == (value, [])


def test_text_matching_no_rule_reports_no_changes():
    text = 'plain ASCII with nothing to rewrite'
    assert apply_style_filter(text, 7) == (text, [])


# ------------------------------------------------------- deterministic rules

def test_rule_a_collapses_not_x_but_y():
    text = '\u8fd9\u4e0d\u662f\u95ee\u9898\u800c\u662f\u7279\u6027'
    out, recs = apply_style_filter(text, 1)
    assert out == '\u8fd9\u662f\u7279\u6027'
    assert len(recs) == 1
    assert recs[0]['pos'] == 1
    assert recs[0]['new'] == '\u662f'
    assert _replay(text, recs) == out


def test_rule_d_replaces_em_dash_pair():
    text = '\u524d\u2014\u2014\u540e'
    out, recs = apply_style_filter(text, 2)
    assert out == '\u524d\uff0c\u540e'
    assert [r['pos'] for r in recs] == [1]
    assert _replay(text, recs) == out


def test_rule_c_rewrites_full_width_parens():
    text = '\u7532\uff08\u4e59\uff09\u4e19'
    out, recs = apply_style_filter(text, 3)
    assert out == '\u7532\uff0c\u4e59\u4e19'
    # Ascending by position, and both positions index the original.
    assert [r['pos'] for r in recs] == [1, 3]
    assert _replay(text, recs) == out


def test_higher_priority_rule_consumes_its_span():
    """Rule A wins the whole match, so Rule C must not touch the parens inside.

    Without the occupied-set guard the two rules would both fire on overlapping
    ranges and the right-to-left application would produce garbage.
    """
    text = '\u8fd9\u4e0d\u662f\uff08\u7532\uff09\u800c\u662f\u4e59'
    out, recs = apply_style_filter(text, 4)
    assert out == '\u8fd9\u662f\u4e59'
    assert len(recs) == 1
    assert _replay(text, recs) == out


# ---------------------------------------------------------------- invariants

@pytest.mark.parametrize('text', [
    '\u8fd9\u4e0d\u662f\u95ee\u9898\u800c\u662f\u7279\u6027\uff01',
    '\u524d\u2014\u2014\u540e\uff08\u62ec\u53f7\uff09\u3002',
    '\u6781\u5176\u91cd\u8981\uff01\u300c\u5f15\u7528\u300d\u5c31\u662f\u8fd9\u6837',
    '\u65e0\u5339\u914d',
])
def test_records_replay_onto_the_original(text):
    """Whatever fires, the records must reproduce the returned text exactly."""
    for msg_id in (0, 1, 42, 9999):
        out, recs = apply_style_filter(text, msg_id)
        assert _replay(text, recs) == out


@pytest.mark.parametrize('text', [
    '\u597d\uff01\u597d\uff01\u597d\uff01',
    '\u6781\u5176\u6781\u5176',
    '\u300c\u77ed\u300d\u548c\u300c\u53e6\u4e00\u4e2a\u300d',
])
def test_same_seed_gives_the_same_result_every_call(text):
    first = apply_style_filter(text, 123)
    for _ in range(4):
        assert apply_style_filter(text, 123) == first


def test_probabilistic_rule_reaches_both_outcomes():
    """Sweeping msg_id, not pinning md5: both branches must be reachable."""
    text = '\u597d\uff01'
    seen = {apply_style_filter(text, i)[0] for i in range(50)}
    assert text in seen, 'the keep branch never fired'
    assert '\u597d\u3002' in seen, 'the rewrite branch never fired'


def test_prob_stays_in_range_and_is_stable():
    for msg_id in range(20):
        for pos in range(20):
            v = _prob(msg_id, pos)
            assert 0 <= v < 100
            assert v == _prob(msg_id, pos)