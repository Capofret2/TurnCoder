/* TurnCoder — chat minimap rail.
 *
 * A vertical rail on the right edge of the chat area. Each rendered bubble gets
 * one horizontal tick: long for user turns, short for assistant turns, and
 * error-tinted for bubbles excluded from the context. Clicking anywhere on the
 * rail scrolls to a bubble.
 *
 * Ticks are evenly spaced, one pitch per bubble, so a wall of short replies is
 * as easy to hit as one long essay. Jump accuracy is unaffected: a click
 * resolves the bubble by id and defers to scrollIntoView, which never consulted
 * the tick's own position.
 *
 * Position is marked by a triangle right of the ticks, aligned with the topmost
 * bubble currently on screen. Topmost rather than the centre of the visible
 * range because a tick click scrolls its bubble to block:'start': the marker
 * then lands exactly on the tick that was clicked, which neither a midpoint nor
 * a scroll fraction would do.
 *
 * Hovering a tick opens a preview immediately. The native title attribute would
 * have been free but waits about a second, which defeats the point of skimming
 * the rail, so the tick carries aria-label instead and the panel is built here.
 *
 * The loop walks chatContainer.children, not currentHistory, because rendering
 * absorbs many messages into other bubbles (tool results under their call,
 * thinking above its reply, deduplicated autoreads). Only bubbles that actually
 * exist in the DOM have an offsetTop, and only those are scroll targets.
 */

var _mmScrollRaf = 0;

var MM_PAD = 7;       // rail inner padding, top and bottom
var MM_GAP = 8;       // preferred pitch between ticks
var MM_TICK_H = 3;    // maximum drawn tick thickness; also the CSS fallback
var MM_MIN_H = 64;    // shortest the rail is allowed to get
var MM_CURSOR_H = 10; // triangle box height; must equal .mm-cursor's two borders

// Geometry cached at render time. Scrolling does not reflow the chat, so these
// stay valid until the next renderMinimap and the scroll path costs arithmetic
// only — no getElementById per bubble per frame.
var _mmTops = [];     // bubble offsetTop, render order
var _mmBots = [];     // bubble offsetTop + offsetHeight
var _mmTickY = [];    // tick y within the rail
var _mmTickH = MM_TICK_H;  // drawn thickness this render; shrinks when dense
var _mmMsgs = [];     // message refs, not copies, so a streaming edit shows up
                      // in the hover preview with no invalidation step

function renderMinimap() {
    var rail = document.getElementById('chat-minimap');
    var chat = document.getElementById('chat-container');
    if (!rail || !chat) return;

    // Kanban replaces the chat area entirely; the rail would float over nothing.
    if (typeof _kanbanActive !== 'undefined' && _kanbanActive) {
        rail.style.display = 'none';
        // The button would otherwise float over the kanban and scroll a chat
        // area nobody can see.
        var _kb = document.getElementById('scroll-bottom-btn');
        if (_kb) _kb.classList.remove('sb-visible');
        return;
    }
    rail.style.display = '';

    var byId = {};
    if (typeof currentHistory !== 'undefined' && currentHistory) {
        for (var i = 0; i < currentHistory.length; i++) {
            byId[currentHistory[i].id] = currentHistory[i];
        }
    }

    var rows = [];
    var nodes = chat.children;
    for (var n = 0; n < nodes.length; n++) {
        var el = nodes[n];
        if (!el.id || el.id.lastIndexOf('msg-bubble-', 0) !== 0) continue;
        var mid = parseInt(el.id.substring(11), 10);
        if (isNaN(mid)) continue;
        var m = byId[mid];
        if (!m) continue;
        rows.push({
            id: mid, msg: m,
            top: el.offsetTop,
            bottom: el.offsetTop + el.offsetHeight
        });
    }

    // Fixed pitch, shrinking only once the preferred one would overflow 70% of
    // the viewport. Spacing stays exactly uniform either way.
    var count = rows.length;
    var maxH = Math.max(MM_MIN_H, Math.round((chat.clientHeight || 1) * 0.7));
    var wantH = MM_PAD * 2 + MM_TICK_H + Math.max(0, count - 1) * MM_GAP;
    var railH = Math.min(maxH, Math.max(MM_MIN_H, wantH));
    var span = Math.max(0, railH - MM_PAD * 2 - MM_TICK_H);
    var pitch = count > 1 ? span / (count - 1) : 0;

    // Two quantities have to follow the pitch rather than stay constant.
    //
    // Thickness: 200 messages in a 1000px viewport compress the pitch to
    // 3.4px, and a 3px tick would leave a 0.4px gap — the rail reads as one
    // solid bar. Shrinking the line keeps a visible gap at any density.
    //
    // Hit area: at that pitch a fixed 4px half-extent makes neighbouring
    // targets overlap by 7px, and the later sibling paints on top, so a hover
    // aimed at one tick resolves to the one below it. Half the leftover pitch
    // gives every tick exactly its own slice — no overlap, no dead zone. It is
    // set once on the rail and inherited, so the CSS needs a single calc().
    _mmTickH = count > 1
        ? Math.max(2, Math.min(MM_TICK_H, Math.floor(pitch) - 1))
        : MM_TICK_H;
    var hit = count > 1 ? Math.max(0.5, (pitch - _mmTickH) / 2) : 4;
    rail.style.height = railH + 'px';
    rail.style.setProperty('--mm-hit', hit.toFixed(2) + 'px');

    _mmTops = [];
    _mmBots = [];
    _mmTickY = [];
    _mmMsgs = [];

    var parts = [];
    for (var r = 0; r < count; r++) {
        var row = rows[r];
        var y = count > 1 ? MM_PAD + r * pitch : (railH - _mmTickH) / 2;
        _mmTops.push(row.top);
        _mmBots.push(row.bottom);
        _mmTickY.push(y);
        _mmMsgs.push(row.msg);

        var cls = 'mm-tick ';
        if (row.msg.is_hidden) cls += 'mm-hidden';
        else if (row.msg.role === 'user') cls += 'mm-user';
        else cls += 'mm-assistant';

        var label = '[ID:' + row.id + '] ' + (row.msg.is_hidden ? '已隐藏 ' : '')
            + (row.msg.role === 'user' ? '用户' : '助手');
        var summary = (row.msg.summary || '').substring(0, 40).replace(/"/g, '&quot;');
        if (summary) label += ' — ' + summary;

        // aria-label, not title: the hover panel is instant and a title would
        // stack a second, slower tooltip on top of it a second later.
        parts.push('<div class="' + cls + '" style="top:' + y.toFixed(1)
            + 'px;height:' + _mmTickH + 'px;"'
            + ' data-mid="' + row.id + '" data-idx="' + r + '"'
            + ' aria-label="' + label + '"></div>');
    }

    // Emitted empty and placed by _mmUpdateCursor, so the render path and the
    // scroll path share one implementation instead of two that can drift.
    parts.push('<div class="mm-cursor"></div>');
    rail.innerHTML = parts.join('');
    _mmUpdateCursor();
    // The tick the panel was describing no longer exists after this rebuild;
    // leaving it up would show a bubble that is no longer on the rail.
    _mmHidePreview();
    _mmPlaceScrollBtn();
    _mmUpdateScrollBtn();
}

/** Cheap path: move the triangle without rebuilding the ticks. */
function _mmUpdateCursor() {
    var rail = document.getElementById('chat-minimap');
    var chat = document.getElementById('chat-container');
    if (!rail || !chat) return;
    var cur = rail.querySelector('.mm-cursor');
    if (!cur) return;
    if (!_mmTickY.length) { cur.style.display = 'none'; return; }
    cur.style.display = '';

    // Only the first intersecting bubble matters, so this can stop early.
    var visTop = chat.scrollTop;
    var visBot = visTop + chat.clientHeight;
    var first = -1;
    for (var i = 0; i < _mmTops.length; i++) {
        if (_mmBots[i] > visTop && _mmTops[i] < visBot) { first = i; break; }
    }
    if (first < 0) {
        // Nothing intersects: the viewport is parked in trailing empty space.
        // The one case with no tick to anchor to, so fall back to a proportion.
        var railH = rail.clientHeight || 1;
        var denom = Math.max(1, chat.scrollHeight - chat.clientHeight);
        var y = MM_PAD + (visTop / denom) * Math.max(0, railH - MM_PAD * 2);
        cur.style.top = (y - MM_CURSOR_H / 2).toFixed(1) + 'px';
        return;
    }
    cur.style.top =
        (_mmTickY[first] + _mmTickH / 2 - MM_CURSOR_H / 2).toFixed(1) + 'px';
}

/* ===== Hover preview ====================================================
   Parented to #main-area rather than the rail: it must not inherit the rail's
   opacity, and a panel inside the rail would keep firing the rail's own
   mouseleave as the pointer crossed it.
   ====================================================================== */

function _mmEnsurePreview() {
    var el = document.getElementById('mm-preview');
    if (el) return el;
    var main = document.getElementById('main-area');
    if (!main) return null;
    el = document.createElement('div');
    el.id = 'mm-preview';
    el.setAttribute('aria-hidden', 'true');
    var head = document.createElement('div');
    head.id = 'mm-preview-head';
    var body = document.createElement('div');
    body.id = 'mm-preview-body';
    el.appendChild(head);
    el.appendChild(body);
    main.appendChild(el);
    return el;
}

function _mmHidePreview() {
    var el = document.getElementById('mm-preview');
    if (el) el.style.display = 'none';
}

function _mmShowPreview(tick) {
    var el = _mmEnsurePreview();
    var rail = document.getElementById('chat-minimap');
    var main = document.getElementById('main-area');
    if (!el || !rail || !main) return;
    var m = _mmMsgs[parseInt(tick.dataset.idx, 10)];
    if (!m) { _mmHidePreview(); return; }

    var text = m.content || '';
    if (typeof filterProtocolMarkers === 'function') {
        text = filterProtocolMarkers(text) || '';
    }
    text = text.trim();
    // Dehydrated bubbles arrive with the content stripped and only a summary,
    // so falling back to it is the normal path for tool results, not an edge.
    if (!text) text = (m.summary || '').trim();
    if (!text) text = '(正文按需加载，点击刻度跳转查看)';
    if (text.length > 400) text = text.substring(0, 400) + '...';

    // textContent throughout: bubble bodies routinely contain markup and this
    // panel has no business parsing it.
    document.getElementById('mm-preview-head').textContent =
        (m.role === 'user' ? '用户' : '助手') + '  [ID:' + m.id + ']'
        + (m.is_hidden ? '  已隐藏' : '');
    document.getElementById('mm-preview-body').textContent = text;

    // Shown before measuring, otherwise offsetHeight reads 0 and the panel
    // centres on the wrong point the first time each tick is hovered.
    el.style.display = 'block';
    var h = el.offsetHeight;
    var y = rail.offsetTop + tick.offsetTop + (tick.offsetHeight / 2) - (h / 2);
    y = Math.max(4, Math.min(main.clientHeight - h - 4, y));
    el.style.top = y.toFixed(1) + 'px';
    // Right of main minus the rail's left edge: the panel's right edge lands a
    // gap short of the rail, which is the only side with room for it.
    el.style.right = (main.clientWidth - rail.offsetLeft + 8) + 'px';
}

/* ===== Scroll-to-bottom button ==========================================
   Lives in #main-area, built here rather than in frontend.html. renderChat
   diffs #chat-container's children and removes the extras, so anything parked
   inside it disappears on the next render; and building it in JS avoids
   duplicating an icon path that icons.js already owns.
   ====================================================================== */

function _mmEnsureScrollBtn() {
    var btn = document.getElementById('scroll-bottom-btn');
    if (btn) return btn;
    var main = document.getElementById('main-area');
    if (!main) return null;
    btn = document.createElement('button');
    btn.id = 'scroll-bottom-btn';
    btn.type = 'button';
    btn.title = '回到底部';
    btn.setAttribute('aria-label', '回到底部');
    // innerHTML: the glyph is inline SVG and textContent would strip it.
    btn.innerHTML = (typeof mdIcon === 'function') ? mdIcon('arrow_downward', 20) : '';
    btn.onclick = function () {
        var c = document.getElementById('chat-container');
        if (!c) return;
        c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' });
    };
    main.appendChild(btn);
    return btn;
}

/** Park the button just above whatever sits below the chat area. */
function _mmPlaceScrollBtn() {
    var btn = _mmEnsureScrollBtn();
    var chat = document.getElementById('chat-container');
    var main = document.getElementById('main-area');
    if (!btn || !chat || !main) return;
    // Measured rather than hardcoded: the composer grows as the textarea wraps
    // and the tab bar may or may not be present.
    var below = main.clientHeight - (chat.offsetTop + chat.offsetHeight);
    btn.style.bottom = Math.max(8, below + 16) + 'px';
}

function _mmUpdateScrollBtn() {
    var btn = document.getElementById('scroll-bottom-btn');
    var chat = document.getElementById('chat-container');
    if (!btn || !chat) return;
    var dist = chat.scrollHeight - chat.clientHeight - chat.scrollTop;
    btn.classList.toggle('sb-visible', dist > 160);
}

(function () {
    function wire() {
        var rail = document.getElementById('chat-minimap');
        var chat = document.getElementById('chat-container');
        if (!rail || !chat) return;

        rail.addEventListener('click', function (e) {
            var tick = e.target.closest ? e.target.closest('.mm-tick') : null;
            if (tick && tick.dataset.mid) {
                var b = document.getElementById('msg-bubble-' + tick.dataset.mid);
                if (b) {
                    b.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    return;
                }
            }
            // Bare rail resolves to a message index too, so the whole control
            // speaks one language. Only the end padding is reachable now that
            // hit areas tile the rail, plus the window between a resize and the
            // next render. The old form multiplied by scrollHeight without
            // subtracting clientHeight and could never reach the true bottom;
            // clamping hid it.
            if (!_mmTickY.length) return;
            var rect = rail.getBoundingClientRect();
            var ratio = (e.clientY - rect.top) / Math.max(1, rect.height);
            var idx = Math.min(_mmTickY.length - 1,
                Math.max(0, Math.round(ratio * (_mmTickY.length - 1))));
            var tb = rail.querySelectorAll('.mm-tick')[idx];
            var target = tb && tb.dataset.mid
                ? document.getElementById('msg-bubble-' + tb.dataset.mid) : null;
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });

        // mouseover, not mouseenter: it bubbles, so one delegated listener on
        // the rail covers every tick. mouseenter would need one listener per
        // tick, which is hundreds on a long conversation.
        rail.addEventListener('mouseover', function (e) {
            var tick = e.target.closest ? e.target.closest('.mm-tick') : null;
            // The end padding is still bare even though hit areas tile the rest,
            // and without this the panel would sit there showing the last tick.
            if (tick) _mmShowPreview(tick); else _mmHidePreview();
        });
        rail.addEventListener('mouseleave', _mmHidePreview);

        // Hundreds of ticks are possible; coalesce scroll into one rAF and move
        // only the cursor, never the ticks.
        chat.addEventListener('scroll', function () {
            if (_mmScrollRaf) return;
            _mmScrollRaf = requestAnimationFrame(function () {
                _mmScrollRaf = 0;
                _mmUpdateCursor();
                _mmUpdateScrollBtn();
            });
        }, { passive: true });

        window.addEventListener('resize', renderMinimap);
        renderMinimap();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();

window.renderMinimap = renderMinimap;