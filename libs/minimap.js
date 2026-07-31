/* TurnCoder — chat minimap rail.
 *
 * A vertical rail on the right edge of the chat area. Each rendered bubble gets
 * one horizontal tick: long for user turns, short for assistant turns, and
 * error-tinted for bubbles excluded from the context. Clicking a tick scrolls to
 * that bubble; clicking bare rail scrolls proportionally.
 *
 * Ticks are positioned from bubble offsetTop rather than message index, so the
 * rail is a true minimap: a long bubble claims proportionally more rail, and a
 * click lands where the user expects.
 *
 * The loop walks chatContainer.children, not currentHistory, because rendering
 * absorbs many messages into other bubbles (tool results under their call,
 * thinking above its reply, deduplicated autoreads). Only bubbles that actually
 * exist in the DOM have an offsetTop, and only those are scroll targets.
 */

var _mmScrollRaf = 0;

function renderMinimap() {
    var rail = document.getElementById('chat-minimap');
    var chat = document.getElementById('chat-container');
    if (!rail || !chat) return;

    // Kanban replaces the chat area entirely; the rail would float over nothing.
    if (typeof _kanbanActive !== 'undefined' && _kanbanActive) {
        rail.style.display = 'none';
        return;
    }
    rail.style.display = '';

    var railH = chat.clientHeight || 1;
    var scrollH = chat.scrollHeight || 1;
    rail.style.height = railH + 'px';

    var byId = {};
    if (typeof currentHistory !== 'undefined' && currentHistory) {
        for (var i = 0; i < currentHistory.length; i++) {
            byId[currentHistory[i].id] = currentHistory[i];
        }
    }

    var parts = [];
    var nodes = chat.children;
    for (var n = 0; n < nodes.length; n++) {
        var el = nodes[n];
        if (!el.id || el.id.lastIndexOf('msg-bubble-', 0) !== 0) continue;
        var mid = parseInt(el.id.substring(11), 10);
        if (isNaN(mid)) continue;
        var m = byId[mid];
        if (!m) continue;

        var y = (el.offsetTop / scrollH) * railH;
        var cls = 'mm-tick ';
        if (m.is_hidden) cls += 'mm-hidden';
        else if (m.role === 'user') cls += 'mm-user';
        else cls += 'mm-assistant';

        var label = '[ID:' + mid + '] ' + (m.is_hidden ? '已隐藏 ' : '')
            + (m.role === 'user' ? '用户' : '助手');
        var summary = (m.summary || '').substring(0, 40).replace(/"/g, '&quot;');
        if (summary) label += ' — ' + summary;

        parts.push('<div class="' + cls + '" style="top:' + y.toFixed(1) + 'px;"'
            + ' data-mid="' + mid + '" title="' + label + '"></div>');
    }

    parts.push('<div class="mm-viewport" style="top:'
        + ((chat.scrollTop / scrollH) * railH).toFixed(1) + 'px;height:'
        + Math.max(8, (railH / scrollH) * railH).toFixed(1) + 'px;"></div>');

    rail.innerHTML = parts.join('');
}

/** Cheap path: reposition the viewport box without rebuilding the ticks. */
function _mmUpdateViewport() {
    var rail = document.getElementById('chat-minimap');
    var chat = document.getElementById('chat-container');
    if (!rail || !chat) return;
    var vp = rail.querySelector('.mm-viewport');
    if (!vp) return;
    var railH = chat.clientHeight || 1;
    var scrollH = chat.scrollHeight || 1;
    vp.style.top = ((chat.scrollTop / scrollH) * railH).toFixed(1) + 'px';
    vp.style.height = Math.max(8, (railH / scrollH) * railH).toFixed(1) + 'px';
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
            var rect = rail.getBoundingClientRect();
            var ratio = (e.clientY - rect.top) / Math.max(1, rect.height);
            chat.scrollTop = ratio * chat.scrollHeight;
        });

        // Hundreds of ticks are possible; coalesce scroll into one rAF and move
        // only the viewport box, never the ticks.
        chat.addEventListener('scroll', function () {
            if (_mmScrollRaf) return;
            _mmScrollRaf = requestAnimationFrame(function () {
                _mmScrollRaf = 0;
                _mmUpdateViewport();
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