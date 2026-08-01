"""Sidebar behaviour, driven through a real browser against main.js.

main.js is the largest file in the project — session rendering, drag reordering,
the tab bar, the whole state pipeline — and until now none of it was covered,
because two things stopped it being loaded outside frontend.html:

- three top-level addEventListener calls threw on a missing element;
- it declares chatContainer, currentHistory, bubbleCache, globalSettings and
  socket with const/let, which is a SyntaxError against the `var` declarations
  render.html needs for chat.js.

The first is fixed by initApp. The second is why this runs against its own
harness page rather than joining the existing one.

The comparison test is the load-bearing one here. Right-click and the overflow
button currently share openSessionMenu, and nothing except this assertion would
notice if someone split them into two copies — the entries would drift apart
silently, surfacing only when a user reports that one entry point is missing an
option.
"""
import pathlib

import pytest

pytest.importorskip('playwright', reason='playwright is not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

HARNESS = (pathlib.Path(__file__).resolve().parent / 'harness' / 'sidebar.html')
VIEWPORT = {'width': 1280, 'height': 720}


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
    pg.goto(HARNESS.as_uri())
    pg.wait_for_function('typeof window.__sb === "object"')
    yield pg
    # main.js loading at all is half of what this file proves, so an uncaught
    # error here matters even when the assertions below pass.
    assert not errors, 'uncaught page errors: %s' % errors[:3]
    ctx.close()


def _render(page, n=3):
    page.evaluate(
        'n => window.__sb.render(window.__sb.makeSessions(n), "sid1")', n)


# --------------------------------------------------- main.js loads at all

def test_main_js_evaluates_without_the_composer_markup(page):
    """initApp's sentinel. Before the extraction, three top-level
    addEventListener calls threw and nothing after them ran."""
    assert page.evaluate('typeof renderSessions') == 'function'
    assert page.evaluate('typeof openSessionMenu') == 'function'
    assert page.evaluate('typeof initApp') == 'function'
    # The composer is absent, so initApp must not have run.
    assert page.evaluate('typeof _wireComposer') == 'function'


def test_rendering_produces_one_row_per_session_in_order(page):
    _render(page, 4)
    sids = page.evaluate("""
        Array.prototype.map.call(
            document.querySelectorAll('#session-list .session-item'),
            function (el) { return el.dataset.sid; })
    """)
    assert sids == ['sid1', 'sid2', 'sid3', 'sid4']


# ------------------------------------------------ session context menu

def test_right_click_opens_the_session_menu_and_suppresses_the_native_one(page):
    _render(page)
    prevented = page.evaluate('window.__sb.fireContextMenu("sid2")')
    assert prevented, 'the native menu was not suppressed'
    assert page.locator('#session-item-menu').count() == 1


def test_right_click_and_the_overflow_button_offer_identical_entries(page):
    """The reason the feature reuses openSessionMenu instead of declaring a second
    copy of the entries. Nothing else would catch the two drifting apart: the
    entries would simply differ, with no error anywhere."""
    _render(page)
    page.evaluate('window.__sb.fireContextMenu("sid2")')
    from_right_click = page.evaluate('window.__sb.menuEntries()')
    page.evaluate('window.__sb.closeMenu()')
    assert page.evaluate('window.__sb.clickOverflow("sid2")')
    from_button = page.evaluate('window.__sb.menuEntries()')

    assert from_right_click, 'right-click produced no entries'
    assert from_right_click == from_button, (
        'the two entry points have drifted: right-click %s, button %s'
        % (from_right_click, from_button))
    # Pinned so that dropping one silently is a failure rather than a surprise.
    assert from_right_click == ['rename', 'clone', 'archive', 'delete']


@pytest.mark.parametrize('act,action', [
    ('clone', 'duplicate_session'),
    ('archive', 'archive_session'),
    ('delete', 'delete_session'),
])
def test_each_menu_entry_dispatches_its_own_action(page, act, action):
    _render(page)
    page.evaluate('window.__sb.fireContextMenu("sid3")')
    assert page.evaluate('window.__sb.clickMenuEntry(%r)' % act)
    calls = page.evaluate('window.__calls.postAction')
    assert len(calls) == 1, 'postAction saw: %s' % calls
    assert calls[0]['action'] == action
    assert calls[0]['sid'] == 'sid3'


def test_rename_goes_through_the_modal_rather_than_posting_directly(page):
    """renameSession opens a prompt and only posts once a name comes back, so a
    payload appearing here would mean a session was renamed without being asked."""
    _render(page)
    page.evaluate('window.__sb.fireContextMenu("sid2")')
    assert page.evaluate('window.__sb.clickMenuEntry("rename")')
    assert page.evaluate('window.__calls.postAction') == [], \
        'rename dispatched before the user supplied a name'
    assert page.evaluate('window.__calls.rename') == [
        {'sid': 'sid2', 'name': '会话 2'}]


def test_opening_the_menu_twice_leaves_only_one(page):
    """openSessionMenu removes any existing instance first. Without that, every
    right-click would stack another copy at a new position."""
    _render(page)
    page.evaluate('window.__sb.fireContextMenu("sid1")')
    page.evaluate('window.__sb.fireContextMenu("sid3")')
    assert page.locator('#session-item-menu').count() == 1


def test_the_menu_is_clamped_inside_the_viewport(page):
    """It is positioned from clientX/clientY, so a right-click near the edge would
    otherwise open a menu partly off screen with no way to reach the lower items."""
    _render(page)
    page.evaluate("""
        var el = document.querySelector('#session-list .session-item[data-sid="sid2"]');
        el.dispatchEvent(new MouseEvent('contextmenu', {
            bubbles: true, cancelable: true,
            clientX: window.innerWidth - 2, clientY: window.innerHeight - 2
        }));
    """)
    box = page.evaluate("""
        (function () {
            var r = document.getElementById('session-item-menu').getBoundingClientRect();
            return {right: r.right, bottom: r.bottom, left: r.left, top: r.top};
        })()
    """)
    assert box['right'] <= VIEWPORT['width'], box
    assert box['bottom'] <= VIEWPORT['height'], box
    assert box['left'] >= 0 and box['top'] >= 0, box