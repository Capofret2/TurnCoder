"""DOM-level checks driven through a real browser.

Everything here runs against tests/harness/render.html, which loads the actual
libs/ scripts over a synthetic history. No Flask process is started and nothing
reads data/sessions/: those files are the user's real transcripts and are not
test fixtures.

The suite skips itself when Playwright or its Chromium build is absent.
Playwright is a development-only dependency here, so its absence must not turn
the suite red.

The first test is the point of this file. The 500ms poller that used to sit at
the end of codeblocks.js was removed on the strength of reading it; this asserts
the outcome instead. With the poller present it fails after 500ms, because the
poller's duplicate-detection looked for data-approval-reject on the wrapper's
next sibling while chat.js only ever sets data-testid on the button, so the
guard never matched and a second button was inserted every half second.
"""
import pathlib

import pytest

pytest.importorskip('playwright', reason='playwright is not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

HARNESS = (pathlib.Path(__file__).resolve().parent / 'harness' / 'render.html')
VIEWPORT = {'width': 1280, 'height': 720}


def _rgb(value):
    """Normalise a computed colour to a 0-255 triple.

    Chromium serialises a plain hex declaration as `rgb(r, g, b)` but the result
    of color-mix() as `color(srgb 0.61 0.79 0.65)` — CSS Color 4 form, channels
    in 0..1. Both are legal and anything that reads computed styles has to accept
    either; assuming rgb() is what made the first version of the dark-accent
    assertion fail with a ValueError rather than a colour comparison.
    """
    text = value.strip()
    if text.startswith('color('):
        parts = text[text.index('(') + 1:text.rindex(')')].split()
        # parts[0] is the colour space name.
        floats = [float(p) for p in parts[1:4]]
        return tuple(round(f * 255) for f in floats)
    inner = text[text.index('(') + 1:text.rindex(')')]
    nums = [n.strip() for n in inner.replace('/', ',').split(',')]
    return tuple(int(round(float(n))) for n in nums[:3])


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip('chromium unavailable: %s' % str(exc)[:120])
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport=VIEWPORT)
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    # Explicit, though dismissing is already the default: the real
    # rejectApproval in codeblocks.js opens a prompt(), and relying on a default
    # to keep the run from hanging would surface as a timeout rather than an
    # assertion if that default ever changed.
    pg.on('dialog', lambda d: d.dismiss())
    pg.goto(HARNESS.as_uri())
    pg.wait_for_function('typeof window.__harness === "object"')
    yield pg
    # A ReferenceError in a render path leaves a half-built DOM and otherwise
    # only shows up as a puzzling assertion failure elsewhere.
    assert not errors, 'uncaught page errors: %s' % errors[:3]
    ctx.close()


def _render(page, history_js, settings=None):
    page.evaluate(
        'a => window.__harness.render(a[0], a[1])',
        [history_js, settings or {}])


# ------------------------------------------------- the removed 500ms poller

def test_approval_reject_button_is_never_duplicated(page):
    """Direct evidence for deleting the poller from codeblocks.js."""
    page.evaluate('window.__harness.render(window.__harness.approvalHistory())')

    assert page.locator('[data-testid="reject-approval"]').count() == 1
    assert page.locator('[data-approval-reject]').count() == 0

    # Well past two poll intervals.
    page.wait_for_timeout(1200)

    assert page.locator('[data-testid="reject-approval"]').count() == 1, \
        'a second reject button appeared — the poller is back'
    assert page.locator('[data-approval-reject]').count() == 0


def test_approval_reject_passes_the_history_index_not_the_dom_index(page):
    """The poller derived its index from DOM position; chat.js closes over the
    real array index. With one bubble both are 0, so the history is padded with
    an absorbed message to make them differ.

    Asserted on the postAction payload, not on a stub: codeblocks.js owns
    rejectApproval and its declaration shadows anything the harness defines
    earlier, so the real function runs. That is the stronger check anyway — it
    covers the action name and the part id reaching the request, not just the
    click landing.
    """
    page.evaluate("""
        var hist = window.__harness.approvalHistory();
        hist[0].id = 5;
        // A thinking bubble is absorbed into the reply below it, so it occupies
        // an array slot without producing a top-level .message-bubble.
        hist.unshift({id: 4, role: 'assistant', cc_type: 'thinking',
                      content: 'reasoning', created_at: 1699999999});
        hist.unshift({id: 3, role: 'user', content: 'ask', created_at: 1699999998});
        window.__harness.render(hist);
    """)
    page.locator('[data-testid="reject-approval"]').click()
    calls = page.evaluate(
        "window.__calls.postAction"
        ".filter(function (c) { return c.action === 'cc_reject_approval'; })")
    assert len(calls) == 1, 'postAction calls seen: %s' % page.evaluate(
        'window.__calls.postAction')
    assert calls[0]['index'] == 2, \
        'index %r is a DOM position, not a history index' % calls[0]['index']
    assert calls[0]['part_id'] == 'part-approval-1'


# ------------------------------------------------------ right-click collapse

def test_right_click_requests_a_collapse(page):
    page.evaluate('window.__harness.render(window.__harness.makeHistory(3))')
    prevented = page.evaluate('window.__harness.fireContextMenu(2)')
    assert prevented, 'the native menu was not suppressed'
    assert page.evaluate('window.__calls.toggleMode') == [
        {'index': 1, 'mode': 'collapse'}]


def test_right_click_inside_a_selection_yields_to_the_native_menu(page):
    """Bubble text is user-select:text on purpose and right-click is how the
    copy menu is reached. Collapsing would discard the selection along with the
    passage the user was aiming at."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(3))')
    assert page.evaluate('window.__harness.selectBubbleText(2)'), \
        'the harness failed to make a selection'
    prevented = page.evaluate('window.__harness.fireContextMenu(2)')
    assert not prevented, 'preventDefault ran despite an active selection'
    assert page.evaluate('window.__calls.toggleMode') == []


def test_a_selection_in_another_bubble_does_not_protect_this_one(page):
    page.evaluate('window.__harness.render(window.__harness.makeHistory(4))')
    assert page.evaluate('window.__harness.selectBubbleText(1)')
    page.evaluate('window.__harness.fireContextMenu(3)')
    assert page.evaluate('window.__calls.toggleMode') == [
        {'index': 2, 'mode': 'collapse'}]


# ------------------------------------------------------------- minimap rail

def test_one_tick_per_rendered_bubble_with_role_classes(page):
    page.evaluate(
        'window.__harness.render(window.__harness.makeHistory(6, {hideEvery: 3}))')
    geom = page.evaluate('window.__harness.tickGeometry()')
    assert len(geom['ticks']) == 6
    assert [t['mid'] for t in geom['ticks']] == ['1', '2', '3', '4', '5', '6']
    # hideEvery 3 marks ids 3 and 6; the hidden class outranks the role class.
    assert 'mm-hidden' in geom['ticks'][2]['cls']
    assert 'mm-hidden' in geom['ticks'][5]['cls']
    assert 'mm-user' in geom['ticks'][0]['cls']
    assert 'mm-assistant' in geom['ticks'][1]['cls']


def test_ticks_are_evenly_spaced_regardless_of_bubble_height(page):
    """The whole point of the switch away from offsetTop proportions."""
    page.evaluate("""
        var hist = window.__harness.makeHistory(8);
        // One deliberately enormous bubble: under the old proportional mapping
        // it would have claimed most of the rail.
        hist[3].content = new Array(400).join('a very long line of text\\n');
        window.__harness.render(hist);
    """)
    tops = [t['top'] for t in page.evaluate('window.__harness.tickGeometry()')['ticks']]
    gaps = [round(b - a, 2) for a, b in zip(tops, tops[1:])]
    assert len(gaps) == 7
    assert max(gaps) - min(gaps) < 0.15, 'spacing is not uniform: %s' % gaps


def test_hit_areas_tile_the_rail_without_overlap(page):
    """--mm-hit is half the pitch left over after the line, so each tick owns
    exactly its own slice. A fixed value overlaps its neighbours on a dense rail
    and, because the later sibling paints on top, a hover resolves to the tick
    below the one being pointed at."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(40))')
    geom = page.evaluate('window.__harness.tickGeometry()')
    tops = [t['top'] for t in geom['ticks']]
    pitch = (tops[-1] - tops[0]) / (len(tops) - 1)
    hit = float(geom['hit'].replace('px', ''))
    height = geom['ticks'][0]['height']
    if pitch >= 3:
        assert abs((2 * hit + height) - pitch) < 0.05, (
            'hit=%.3f height=%.1f pitch=%.3f do not tile' % (hit, height, pitch))


def test_dense_rail_thins_the_tick_so_gaps_stay_visible(page):
    """200 turns compress the pitch below the 3px line thickness; without the
    shrink the rail renders as one solid bar."""
    page.evaluate(
        'window.__harness.render(window.__harness.makeHistory(200, {lines: 2}))')
    geom = page.evaluate('window.__harness.tickGeometry()')
    assert len(geom['ticks']) == 200
    tops = [t['top'] for t in geom['ticks']]
    pitch = (tops[-1] - tops[0]) / 199
    height = geom['ticks'][0]['height']
    assert pitch < 3, 'the fixture is not dense enough to exercise this'
    assert height < 3, 'the tick did not thin: height=%s pitch=%.2f' % (height, pitch)
    assert height >= 2, 'the tick thinned past the 2px floor'


def test_cursor_sits_on_the_topmost_visible_tick(page):
    """Topmost, not the midpoint of the visible range: a jump lands its bubble
    against the top of the viewport, so the marker returns to the tick that was
    clicked."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(12))')
    page.locator('#chat-minimap .mm-tick[data-mid="7"]').click()
    # Well past the animation ceiling of 260ms.
    page.wait_for_timeout(450)
    geom = page.evaluate('window.__harness.tickGeometry()')
    cursor = page.evaluate('window.__harness.cursorTop()')
    tick = next(t for t in geom['ticks'] if t['mid'] == '7')
    expected = tick['top'] + tick['height'] / 2 - 5  # MM_CURSOR_H / 2
    assert abs(cursor - expected) < 1.5, (
        'cursor at %.1f, expected %.1f for the clicked tick' % (cursor, expected))


def test_clicking_a_tick_scrolls_that_bubble_to_the_top(page):
    page.evaluate('window.__harness.render(window.__harness.makeHistory(12))')
    page.locator('#chat-minimap .mm-tick[data-mid="9"]').click()
    page.wait_for_timeout(450)
    delta = page.evaluate("""
        (function () {
            var c = document.getElementById('chat-container');
            var b = document.getElementById('msg-bubble-9');
            return b.getBoundingClientRect().top - c.getBoundingClientRect().top;
        })()
    """)
    assert abs(delta) < 3, 'bubble top is %.1fpx from the container top' % delta


def test_custom_scroll_finishes_within_the_declared_ceiling(page):
    """MM_SCROLL_MAX_MS is 260. The animation existing at all is the point:
    scrollIntoView's own duration grows with distance and is not capped."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(60))')
    elapsed = page.evaluate("""
        (function () {
            var c = document.getElementById('chat-container');
            c.scrollTop = 0;
            var t0 = performance.now();
            var tick = document.querySelector('#chat-minimap .mm-tick[data-mid="55"]');
            tick.click();
            return new Promise(function (resolve) {
                var last = -1;
                (function poll() {
                    if (c.scrollTop === last) return resolve(performance.now() - t0);
                    last = c.scrollTop;
                    requestAnimationFrame(poll);
                })();
            });
        })()
    """)
    assert elapsed < 700, 'scroll took %.0fms' % elapsed


# ------------------------------------------------------- hover preview panel

def test_hover_opens_the_preview_with_no_delay(page):
    """The native title attribute waits about a second, which is what made it
    unusable for skimming the rail."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(6))')
    page.locator('#chat-minimap .mm-tick[data-mid="4"]').hover()
    # Read straight away: no wait_for, no polling.
    state = page.evaluate("""
        (function () {
            var el = document.getElementById('mm-preview');
            return el ? {
                display: el.style.display,
                head: document.getElementById('mm-preview-head').textContent,
                body: document.getElementById('mm-preview-body').textContent
            } : null;
        })()
    """)
    assert state is not None, 'the preview panel was never created'
    assert state['display'] == 'block'
    assert '[ID:4]' in state['head']
    assert 'bubble 4' in state['body']


def test_ticks_carry_aria_label_and_not_title(page):
    """A title would stack a second, slower tooltip over the instant panel."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(4))')
    tick = page.locator('#chat-minimap .mm-tick').first
    assert tick.get_attribute('title') is None
    assert '[ID:1]' in (tick.get_attribute('aria-label') or '')


# ------------------------------------------------- minimap state encodings

def test_pending_tool_call_gets_a_dot_on_its_tick(page):
    """The dot is a ::before rather than a child node, because minimap.js indexes
    querySelectorAll('.mm-tick') positionally for bare-rail clicks and an extra
    sibling would misalign every one of them. Asserting the rendered pseudo-element
    is therefore closer to the point than asserting the class name."""
    page.evaluate("""
        var hist = window.__harness.makeHistory(4);
        hist[1].content_parts = [{type: 'tool_use_part', id: 'p1',
                                  status: 'pending', tool_id: 't1',
                                  tool_name: 'Bash',
                                  content: JSON.stringify({type: 'tool_use',
                                      id: 't1', name: 'Bash', input: {}})}];
        window.__harness.render(hist);
    """)
    geom = page.evaluate('window.__harness.tickGeometry()')
    assert 'mm-pending' in geom['ticks'][1]['cls']
    assert all('mm-pending' not in t['cls']
               for i, t in enumerate(geom['ticks']) if i != 1)
    width = page.eval_on_selector(
        '#chat-minimap .mm-pending',
        "e => getComputedStyle(e, '::before').width")
    assert width == '5px', 'the marker did not render: %s' % width


def test_adopted_tool_call_gets_no_dot(page):
    page.evaluate("""
        var hist = window.__harness.makeHistory(4);
        hist[1].content_parts = [{type: 'tool_use_part', id: 'p1',
                                  status: 'adopted', tool_id: 't1',
                                  tool_name: 'Bash',
                                  content: JSON.stringify({type: 'tool_use',
                                      id: 't1', name: 'Bash', input: {}})}];
        window.__harness.render(hist);
    """)
    geom = page.evaluate('window.__harness.tickGeometry()')
    assert all('mm-pending' not in t['cls'] for t in geom['ticks'])


def test_summary_and_folded_ticks_are_dimmed_by_different_amounts(page):
    """Read from the computed style, not the class list: a class that is present
    while its rule is misspelled is the standard silent CSS failure, and the two
    values have to stay ordered — summary mode discards more text than folding."""
    page.evaluate("""
        var hist = window.__harness.makeHistory(6);
        hist[1].is_omitted = true;
        hist[3].is_collapsed = true;
        window.__harness.render(hist);
    """)
    geom = page.evaluate('window.__harness.tickGeometry()')
    assert 'mm-omit' in geom['ticks'][1]['cls']
    assert 'mm-collapse' in geom['ticks'][3]['cls']
    ops = page.evaluate("""
        (function () {
            var r = document.getElementById('chat-minimap');
            var g = function (s) {
                var e = r.querySelector(s);
                return e ? parseFloat(getComputedStyle(e).opacity) : null;
            };
            return {omit: g('.mm-omit'), collapse: g('.mm-collapse'),
                    plain: g('.mm-assistant:not(.mm-omit):not(.mm-collapse)')};
        })()
    """)
    assert ops['omit'] < ops['collapse'] < ops['plain'], ops


def test_a_hidden_row_emits_no_dimming_modifier(page):
    """mm-hidden already spends opacity. Emitting a second opacity class next to it
    would leave which one wins to source order, so minimap.js withholds them."""
    page.evaluate("""
        var hist = window.__harness.makeHistory(4);
        hist[1].is_hidden = true;
        hist[1].is_omitted = true;
        window.__harness.render(hist);
    """)
    cls = page.evaluate('window.__harness.tickGeometry()')['ticks'][1]['cls']
    assert 'mm-hidden' in cls
    assert 'mm-omit' not in cls and 'mm-collapse' not in cls, cls


# ------------------------------------------------------ scroll-bottom button

def test_scroll_button_lives_outside_the_diffed_container(page):
    """renderChat drops extra children of #chat-container, so a button parked
    inside it would vanish on the next render."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(30))')
    parent = page.evaluate(
        "document.getElementById('scroll-bottom-btn').parentElement.id")
    assert parent == 'main-area'
    # Still there after a second render.
    page.evaluate('window.__harness.render(window.__harness.makeHistory(30))')
    assert page.locator('#scroll-bottom-btn').count() == 1


def test_scroll_button_shows_only_when_far_from_the_bottom(page):
    page.evaluate('window.__harness.render(window.__harness.makeHistory(40))')
    page.evaluate("document.getElementById('chat-container').scrollTop = 0")
    page.wait_for_timeout(120)
    assert 'sb-visible' in (
        page.locator('#scroll-bottom-btn').get_attribute('class') or '')
    page.evaluate("""
        var c = document.getElementById('chat-container');
        c.scrollTop = c.scrollHeight;
    """)
    page.wait_for_timeout(120)
    assert 'sb-visible' not in (
        page.locator('#scroll-bottom-btn').get_attribute('class') or '')


# ------------------------------------------------------------------- theming

def test_dark_scheme_repaints_code_block_foreground(page):
    """First real verification of the a11y-dark work, which until now was only
    reasoned about."""
    page.evaluate("""
        window.__harness.render([{
            id: 1, role: 'assistant', created_at: 1700000000,
            content: 'text\\n\\n```python\\ndef f():\\n    return 1\\n```\\n'
        }]);
    """)
    sel = '#msg-bubble-1 code.hljs'
    assert page.locator(sel).count() == 1, 'no highlighted code block rendered'
    light = page.eval_on_selector(sel, 'e => getComputedStyle(e).color')
    page.evaluate("window.__harness.setTheme('dark')")
    dark = page.eval_on_selector(sel, 'e => getComputedStyle(e).color')
    assert light != dark, 'code foreground did not change: %s' % light


def test_dark_scheme_keeps_bubble_text_off_its_own_background(page):
    """The trap the conventions doc names: setting a background without a
    foreground survives light mode by accident and only fails in dark."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(4))')
    page.evaluate("window.__harness.setTheme('dark')")
    for msg_id in (1, 2):
        pair = page.eval_on_selector(
            '#msg-bubble-%d' % msg_id,
            'e => { var s = getComputedStyle(e);'
            ' return [s.backgroundColor, s.color]; }')
        assert pair[0] != pair[1], 'bubble %d renders text on its own colour' % msg_id


def test_accent_rules_resolve_and_change_the_primary_colour(page):
    """color-mix derivation has to actually resolve; an unparsed value would
    leave the property invalid and silently fall back."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(4))')
    sel = '#chat-minimap .mm-user'
    blue = page.eval_on_selector(sel, 'e => getComputedStyle(e).backgroundColor')
    assert blue == 'rgb(53, 132, 228)', 'default accent is not Adwaita blue: %s' % blue
    page.evaluate("window.__harness.setTheme('light', 'purple')")
    purple = page.eval_on_selector(sel, 'e => getComputedStyle(e).backgroundColor')
    assert purple == 'rgb(145, 65, 172)', 'purple seed did not apply: %s' % purple


def test_dark_accent_derivation_produces_a_lighter_primary(page):
    """The dark rule mixes the seed 50% with white, which is how the hand-tuned
    #99c1f1 was originally derived from #3584e4."""
    page.evaluate('window.__harness.render(window.__harness.makeHistory(4))')
    page.evaluate("window.__harness.setTheme('dark', 'green')")
    raw = page.eval_on_selector(
        '#chat-minimap .mm-user', 'e => getComputedStyle(e).backgroundColor')
    got = _rgb(raw)
    seed = (0x3a, 0x94, 0x4a)
    for channel, (g, base) in enumerate(zip(got, seed)):
        assert g > base, 'channel %d was not lightened: %s' % (channel, raw)
        # Pinning the ratio, not merely the direction: an even 50% tint is what
        # the hand-tuned #3584e4 -> #99c1f1 pair resolves to, and it is the
        # number tokens.css derives every dark accent from.
        expected = (base + 255) / 2
        assert abs(g - expected) <= 8, (
            'channel %d is %d, not the ~50%% tint %d — the mix ratio moved'
            % (channel, g, round(expected)))