// Kanban Waterfall Timeline View
var _kanbanActive = false;
var _kanbanTimer = null;
var _kanbanPixelsPerHour = 200; // default zoom: 200px/hour
var _kanbanPresenceLog = [];
var _kanbanCurrentEnter = null;
var _kanbanAutopilotCache = {}; // sid -> {history: [], started_at: null, active: false}
var _kanbanLastApState = {}; // sid -> was autopilot active last render? for transition detection

// Device identification for multi-user presence tracking
var _kanbanDeviceId = localStorage.getItem('kanban_device_id');
if (!_kanbanDeviceId) {
    _kanbanDeviceId = 'dev_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    localStorage.setItem('kanban_device_id', _kanbanDeviceId);
}

// Idle detection: 5 minutes without interaction = leave
var _kanbanIdleTimeout = 300000; // 5 minutes in ms
var _kanbanIdleTimer = null;
var _kanbanIsIdle = false;

function _kanbanResetIdleTimer() {
    if (_kanbanIsIdle && _kanbanCurrentEnter === null) {
        // Was idle, now active again: re-enter current session
        _kanbanIsIdle = false;
        var sid = window.localSid || window.currentSessionId;
        if (sid) _kanbanCurrentEnter = {sid: sid, time: Date.now()};
    }
    _kanbanIsIdle = false;
    clearTimeout(_kanbanIdleTimer);
    _kanbanIdleTimer = setTimeout(function() {
        // User went idle: record leave for current presence
        _kanbanIsIdle = true;
        if (_kanbanCurrentEnter) {
            _kanbanCurrentEnter.leave = Date.now();
            var event = {sid: _kanbanCurrentEnter.sid, enter: _kanbanCurrentEnter.time, leave: _kanbanCurrentEnter.leave, device_id: _kanbanDeviceId};
            _kanbanPresenceLog.push(event);
            try {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/action', true);
                xhr.setRequestHeader('Content-Type', 'application/json');
                xhr.send(JSON.stringify({action: 'save_kanban_presence', event: event}));
            } catch(e) {}
            _kanbanCurrentEnter = null;
        }
    }, _kanbanIdleTimeout);
}

// Attach idle detection to user interaction events
['mousemove', 'keydown', 'scroll', 'click', 'touchstart'].forEach(function(evt) {
    document.addEventListener(evt, _kanbanResetIdleTimer, {passive: true});
});
_kanbanResetIdleTimer(); // Start the timer

function _kanbanRecordEnter(sid) {
    if (_kanbanIsIdle) return; // Don't record if user is idle
    if (_kanbanCurrentEnter && _kanbanCurrentEnter.sid !== sid) {
        _kanbanCurrentEnter.leave = Date.now();
        var event = {sid: _kanbanCurrentEnter.sid, enter: _kanbanCurrentEnter.time, leave: _kanbanCurrentEnter.leave, device_id: _kanbanDeviceId};
        _kanbanPresenceLog.push(event);
        // POST to backend for persistence
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/action', true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.send(JSON.stringify({action: 'save_kanban_presence', event: event}));
        } catch(e) {}
    }
    _kanbanCurrentEnter = {sid: sid, time: Date.now()};
}

function enterKanban() {
    _kanbanActive = true;
    var kanbanView = document.getElementById('kanban-view');
    // The wrapper, not the input. Setting display on an element inside a
    // display:none ancestor reveals nothing, so pointing this at #kanban-zoom
    // leaves the control entirely absent — the quiet half-wired failure.
    // flex rather than block: the wrapper's flex-direction:column needs it.
    var kanbanZoom = document.getElementById('kanban-zoom-wrap');
    var inputArea = document.getElementById('input-area');
    if (kanbanView) kanbanView.style.display = 'block';
    if (kanbanZoom) kanbanZoom.style.display = 'flex';
    if (inputArea) inputArea.style.display = 'none';
    // Fetch presence history from backend
    try {
        var pxhr = new XMLHttpRequest();
        pxhr.open('POST', '/api/action', false);
        pxhr.setRequestHeader('Content-Type', 'application/json');
        pxhr.send(JSON.stringify({action: 'get_kanban_data'}));
        if (pxhr.status === 200) {
            var pdata = JSON.parse(pxhr.responseText);
            if (pdata.status === 'ok') {
                if (pdata.presence_log) _kanbanPresenceLog = pdata.presence_log;
            }
        }
    } catch(e) {}
    // Ensure data is available for ALL opened tabs, not just current session
    var allTabs = (typeof _openedTabs !== 'undefined' && _openedTabs) ? _openedTabs : [];
    var curSid = window.localSid || window.currentSessionId;
    var _fetchCount = 0;
    for (var _ti = 0; _ti < allTabs.length && _fetchCount < 5; _ti++) {
        var _tsid = allTabs[_ti];
        var hasData = false;
        if (_tsid === curSid && typeof currentHistory !== 'undefined' && currentHistory && currentHistory.length > 0) hasData = true;
        if (!hasData && typeof _sessionHistoryCache !== 'undefined' && _sessionHistoryCache[_tsid] && _sessionHistoryCache[_tsid].length > 0) hasData = true;
        if (!hasData) {
            _fetchCount++;
            try {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/action', false);
                xhr.setRequestHeader('Content-Type', 'application/json');
                xhr.send(JSON.stringify({action: 'get_session_data', sid: _tsid}));
                if (xhr.status === 200) {
                    var data = JSON.parse(xhr.responseText);
                    if (data.status === 'ok' && data.session_data) {
                        if (data.session_data.conversation_history) {
                            if (typeof _sessionHistoryCache !== 'undefined') {
                                _sessionHistoryCache[_tsid] = data.session_data.conversation_history;
                            }
                            if (_tsid === curSid) {
                                currentHistory = data.session_data.conversation_history;
                            }
                        }
                        // Cache autopilot history for kanban rendering
                        var _apHist = data.session_data._autopilot_history || [];
                        // Always synthesize if started_at exists - don't rely on is_processing or autopilot_active
                        // (both have known backend bugs where they stay true forever)
                        var _apStarted = data.session_data._autopilot_started_at;
                        if (_apStarted) {
                            var _lastMsgTime = _apStarted;
                            var _ch = data.session_data.conversation_history || [];
                            for (var _ci = _ch.length - 1; _ci >= 0; _ci--) {
                                if (_ch[_ci].created_at) { _lastMsgTime = _ch[_ci].created_at; break; }
                            }
                            var _alreadyRecorded = _apHist.some(function(h) { return h.started === _apStarted; });
                            if (!_alreadyRecorded) {
                                _apHist = _apHist.concat([{started: _apStarted, ended: _lastMsgTime}]);
                            }
                        }
                        _kanbanAutopilotCache[_tsid] = {
                            history: _apHist,
                            started_at: data.session_data._autopilot_started_at || null,
                            active: data.session_data.autopilot_active || false
                        };
                    }
                }
            } catch(e) {}
        }
    }
    // Deactivate all bottom tabs visually
    if (typeof renderBottomTabs === 'function') renderBottomTabs();
    // Deactivate sidebar active session
    document.querySelectorAll('.session-item.active').forEach(function(el) { el.classList.remove('active'); });
    renderKanban();
    // Both after the render: content height is written by it, so before this
    // point scrollHeight equals the viewport and there is nowhere to scroll to.
    // Bottom is where "now" is, and what just happened is why the view is open.
    if (kanbanView) {
        kanbanView.scrollTop = kanbanView.scrollHeight;
        var _tc0 = document.getElementById('bottom-tabs-container');
        if (_tc0) kanbanView.scrollLeft = _tc0.scrollLeft;
    }
    _kanbanTimer = setInterval(renderKanban, 1000);
}

function exitKanban() {
    _kanbanActive = false;
    var kanbanView = document.getElementById('kanban-view');
    // Paired with enterKanban. Fixing only the entry side leaves the control
    // unhideable: it floats over the chat and dragging it drives a render loop
    // that has already been stopped.
    var kanbanZoom = document.getElementById('kanban-zoom-wrap');
    var inputArea = document.getElementById('input-area');
    if (kanbanView) kanbanView.style.display = 'none';
    if (kanbanZoom) kanbanZoom.style.display = 'none';
    if (inputArea) inputArea.style.display = 'block';
    if (_kanbanTimer) { clearInterval(_kanbanTimer); _kanbanTimer = null; }
    // Restore bottom tab active highlight
    if (typeof renderBottomTabs === 'function') renderBottomTabs();
    // Restore sidebar active
    var activeSid = window.localSid || window.currentSessionId;
    document.querySelectorAll('.session-item').forEach(function(el) {
        if (el.dataset && el.dataset.sid === activeSid) el.classList.add('active');
    });
    // Force a state refresh to pick up any messages received during kanban
    // Don't call renderChat directly as it causes visual disruption
    if (typeof postAction === 'function') {
        postAction({action: 'ping'});
    }
}

/* ==========================================================================
   Timeline compression.
   --------------------------------------------------------------------------
   The old mapping was Y = viewHeight - age * pxPerMs: linear, and measured
   from the height of the viewport. That single expression caused all three of
   the problems reported against this view:

     - anything older than one screenful resolved to a negative Y and was
       clipped away, with no scroll to reach it;
     - an idle stretch was stretched to its real duration, so a night with no
       activity pushed the whole conversation off the top;
     - the content had no computable height, so no scrollbar could exist.

   Segments fix all three at once. Stretches shared by every session where
   nothing happened for longer than the threshold collapse to a fixed band; the
   rest expands linearly at pxPerHour. Total height then falls out of the
   segment list, which is what #kanban-content needs in order to scroll.

   `now` is pushed into the timestamp list, and that is load-bearing rather
   than incidental: without it the stretch between the last message and the
   present moment is never a candidate for collapsing, so a machine left
   running overnight opens to several thousand pixels of blank column — the one
   gap most in need of folding.

   A time inside a gap resolves to the middle of the band. There is no
   proportion left to interpolate against inside a collapsed band, and
   pretending otherwise would show two messages three hours apart as visibly
   ordered within 48px.
   ========================================================================== */
var _kanbanGapThreshold = 1800000; // 30 min
var _kanbanGapPixels = 48;
var _kanbanSegments = [];
var _kanbanContentH = 0;

/** Every timestamp any column will draw, in ms, ascending. */
function _kanbanCollectTimes(tabs, now) {
    var out = [];
    for (var i = 0; i < tabs.length; i++) {
        var sid = tabs[i];
        var hist = _kanbanHistoryFor(sid);
        for (var b = 0; b < hist.length; b++) {
            var m = hist[b];
            if (m.is_hidden) continue;
            var ts = m.started_at || m.created_at;
            if (ts) out.push(ts * 1000);
            if (m.completed_at) out.push(m.completed_at * 1000);
        }
        var ap = (_kanbanAutopilotCache[sid] || {}).history || [];
        for (var a = 0; a < ap.length; a++) {
            if (ap[a].started) out.push(ap[a].started * 1000);
            if (ap[a].ended) out.push(ap[a].ended * 1000);
        }
    }
    for (var p = 0; p < _kanbanPresenceLog.length; p++) {
        var pr = _kanbanPresenceLog[p];
        if (pr.enter) out.push(pr.enter);
        if (pr.leave) out.push(pr.leave);
    }
    out.push(now);
    out.sort(function(x, y) { return x - y; });
    return out;
}

function _kanbanBuildSegments(times, now, pxPerMs) {
    _kanbanSegments = [];
    _kanbanContentH = 0;
    if (!times.length) return;
    var y = 0;
    var segStart = times[0];
    for (var i = 1; i < times.length; i++) {
        if (times[i] - times[i - 1] <= _kanbanGapThreshold) continue;
        var h = (times[i - 1] - segStart) * pxPerMs;
        _kanbanSegments.push({t0: segStart, t1: times[i - 1], y0: y, y1: y + h, gap: false});
        y += h;
        _kanbanSegments.push({t0: times[i - 1], t1: times[i], y0: y, y1: y + _kanbanGapPixels, gap: true});
        y += _kanbanGapPixels;
        segStart = times[i];
    }
    var lastH = (now - segStart) * pxPerMs;
    _kanbanSegments.push({t0: segStart, t1: now, y0: y, y1: y + lastH, gap: false});
    _kanbanContentH = y + lastH;
}

function _kanbanTimeToY(t) {
    var segs = _kanbanSegments;
    if (!segs.length) return 0;
    if (t <= segs[0].t0) return segs[0].y0;
    for (var i = 0; i < segs.length; i++) {
        var s = segs[i];
        if (t < s.t0 || t > s.t1) continue;
        if (s.gap) return s.y0 + _kanbanGapPixels / 2;
        var span = s.t1 - s.t0;
        return s.y0 + (span > 0 ? (t - s.t0) / span : 0) * (s.y1 - s.y0);
    }
    return segs[segs.length - 1].y1;
}

/** The one place column history is resolved, so the time sweep and the draw
    pass cannot disagree about which bubbles exist. */
function _kanbanHistoryFor(sid) {
    if (sid === window.currentSessionId && typeof currentHistory !== 'undefined' && currentHistory) {
        return currentHistory;
    }
    if (typeof _sessionHistoryCache !== 'undefined' && _sessionHistoryCache[sid]) {
        return _sessionHistoryCache[sid];
    }
    if (typeof window._sessionHistoryCache !== 'undefined' && window._sessionHistoryCache[sid]) {
        return window._sessionHistoryCache[sid];
    }
    return [];
}

function renderKanban() {
    var outer = document.getElementById('kanban-view');
    var container = document.getElementById('kanban-content');
    if (!outer || !container) return;
    var now = Date.now();
    var pxPerMs = _kanbanPixelsPerHour / 3600000;
    var topPad = 20;

    // Grid and label colours, hoisted because the color-mix expressions are long
    // and appear four times below.
    //
    // These were fixed blacks. The columns take their background from the
    // computed tab colour, which is a dark surface under the dark scheme, so
    // rgba(0,0,0,...) lines drawn on them are invisible: the hour grid, the
    // ten-minute sub-grid, both label tiers and the column divider all vanished
    // at once, leaving the timeline — the entire point of this view — gone.
    //
    // on-surface inverts with the scheme, so the state-layer form works in both.
    var gridColor = 'color-mix(in srgb, var(--md-sys-color-on-surface) 12%, transparent)';
    var subGridColor = 'color-mix(in srgb, var(--md-sys-color-on-surface) 4%, transparent)';
    var labelColor = 'var(--md-sys-color-on-surface-variant)';
    // Kept fainter than labelColor, preserving the 0.4 / 0.25 hierarchy.
    var subLabelColor = 'color-mix(in srgb, var(--md-sys-color-on-surface-variant) 55%, transparent)';

    // Get tabs from _openedTabs
    var tabs = [];
    if (typeof window._openedTabs !== 'undefined' && window._openedTabs && window._openedTabs.length > 0) {
        tabs = window._openedTabs.slice();
    } else if (typeof _openedTabs !== 'undefined' && _openedTabs && _openedTabs.length > 0) {
        tabs = _openedTabs.slice();
    }
    if (tabs.length === 0) {
        // Into the content div with a temporary full height, **not** by rewriting
        // the outer box. outer.innerHTML would destroy #kanban-content and leave
        // the replacement as a sibling of this message; the populated branch only
        // rewrites the content div and never touches that sibling, so the placard
        // would stay in the corner alongside the real columns forever.
        container.style.height = '100%';
        container.style.width = '100%';
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;'
            + 'height:100%;color:var(--md-sys-color-on-surface-variant);">无已打开的标签页</div>';
        return;
    }

    /* Tab geometry, keyed by sid — **not** by array index.
     *
     * This is what made the columns sit under the wrong tabs. renderBottomTabs
     * skips any opened tab whose session is absent from _lastSessionsMap
     * (`if (!sess) return;`), so the DOM can hold fewer .bottom-tab elements
     * than _openedTabs has entries. Reading tabPositions[i] then handed column i
     * some other session's coordinates, and everything from that entry rightward
     * shifted — while clicking such a column switched to the wrong session.
     *
     * Keyed lookup makes the correspondence carried by the id, so a count
     * mismatch can no longer produce an offset. A sid with no tab element draws
     * no column, which is correct: it is not visible in the bar either.
     */
    var tabGeom = {};
    var tabEls = document.querySelectorAll('.bottom-tab');
    for (var ti = 0; ti < tabEls.length; ti++) {
        var _gsid = tabEls[ti].dataset.sid;
        if (!_gsid) continue;
        tabGeom[_gsid] = {
            left: tabEls[ti].offsetLeft,
            width: tabEls[ti].offsetWidth,
            bg: getComputedStyle(tabEls[ti]).backgroundColor
        };
    }

    _kanbanBuildSegments(_kanbanCollectTimes(tabs, now), now, pxPerMs);
    container.style.height = (_kanbanContentH + topPad + 40) + 'px';
    /* Width matched to the tab bar's own scrollWidth, not to the rightmost
     * column's right edge.
     *
     * The bar carries three more controls after the tabs (new / manager /
     * kanban), so its scrollable range is wider than the columns are. With
     * different ranges, assigning one scrollLeft to the other gets clamped at
     * the shorter end and the two drift apart — the second half of the
     * misalignment report.
     *
     * Height is written first on purpose: it decides whether a vertical
     * scrollbar appears, and that scrollbar narrows the visible width here but
     * not in the bar — so outer.clientWidth below has to be read after it.
     *
     * **The width is solved for equal maximum scrollLeft, not copied from the
     * bar's scrollWidth.** Each box's maximum is scrollWidth - clientWidth, and
     * the two clientWidths differ by the scrollbar, so matching scrollWidth does
     * not match the ranges. Copying it leaves the kanban able to scroll ~16px
     * further; at that extreme the mirror writes a value the bar clamps, and the
     * two part company again — in exactly the place a user drags to.
     *
     * Solving it instead gives a simpler expression: content width = the bar's
     * own maximum plus this box's visible width, after which
     * contentW - outer.clientWidth is identically the bar's maximum.
     */
    var _tcEl = document.getElementById('bottom-tabs-container');
    var _barMax = _tcEl ? Math.max(0, _tcEl.scrollWidth - _tcEl.clientWidth) : 0;
    container.style.width = (_barMax + outer.clientWidth) + 'px';

    var html = '';
    // Which column carries the time ruler. Was `i === 0`, which assumed the
    // first sid always draws; now that a sid without a tab is skipped, that
    // assumption would silently drop every time label — symptom is "the kanban
    // has no time markings", which reads as the ruler logic being broken.
    var _labelDrawn = false;
    for (var i = 0; i < tabs.length; i++) {
        var sid = tabs[i];
        var _g = tabGeom[sid];
        // No tab element, no column. Falling back to an even share would draw a
        // column that exists nowhere in the bar, and its position could not
        // align with any real tab — that is more misalignment, not less.
        if (!_g) continue;
        var colLeft = _g.left;
        var colW = _g.width;
        // Fallback for a tab whose computed background is transparent, matching
        // the inactive-tab formula so both paths look alike. The old 83%
        // lightness made such a column a bright slab under the dark scheme.
        var colBg = _g.bg || ('color-mix(in srgb, hsl(' + ((i * 60) % 360)
            + ' 60% 50%) 12%, var(--md-sys-color-surface-container-high))');
        var _isLabelCol = !_labelDrawn;
        _labelDrawn = true;

        html += '<div class="kanban-col" style="position:absolute;left:' + colLeft + 'px;top:0;width:' + colW + 'px;height:100%;background:' + colBg + ';border-right:1px solid var(--md-sys-color-outline-variant);overflow:hidden;">';

        // Grid, walked per segment. The old form counted hours back from now and
        // stopped at the viewport height; under a compressed axis the same pixel
        // distance means different durations in different segments, so counting
        // back from a single origin no longer resolves to the right place.
        var hourLineH = _kanbanPixelsPerHour > 500 ? 2 : 1;
        for (var sgi = 0; sgi < _kanbanSegments.length; sgi++) {
            var sg = _kanbanSegments[sgi];
            if (sg.gap) {
                // A collapsed stretch has to say so. Left blank, two messages a
                // night apart read as consecutive — a worse error than not being
                // able to reach the older one at all.
                if (_isLabelCol) {
                    var gapH = (sg.t1 - sg.t0) / 3600000;
                    html += '<div style="position:absolute;left:0;right:0;top:' + (topPad + sg.y0)
                        + 'px;height:' + _kanbanGapPixels + 'px;font-size:9px;color:' + labelColor
                        + ';line-height:' + _kanbanGapPixels + 'px;padding-left:4px;white-space:nowrap;'
                        + 'background:repeating-linear-gradient(0deg,var(--md-sys-color-surface-container-high),var(--md-sys-color-surface-container-high) 1px,transparent 1px,transparent 4px);'
                        + 'border-top:1px solid ' + gridColor + ';border-bottom:1px solid ' + gridColor + ';">'
                        + '空闲 ' + gapH.toFixed(1) + ' 小时</div>';
                } else {
                    html += '<div style="position:absolute;left:0;right:0;top:' + (topPad + sg.y0)
                        + 'px;height:' + _kanbanGapPixels + 'px;'
                        + 'background:repeating-linear-gradient(0deg,var(--md-sys-color-surface-container-high),var(--md-sys-color-surface-container-high) 1px,transparent 1px,transparent 4px);'
                        + 'border-top:1px solid ' + gridColor + ';border-bottom:1px solid ' + gridColor + ';"></div>';
                }
                continue;
            }
            var d0 = new Date(sg.t0);
            var hourT = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate(), d0.getHours(), 0, 0).getTime();
            // Density judged on the segment's own scale: pxPerHour and "is ten
            // minutes worth a line here" stopped being the same question once the
            // axis could compress.
            var subOn = (_kanbanPixelsPerHour > 120);
            while (hourT <= sg.t1) {
                if (hourT >= sg.t0) {
                    var lineY = topPad + _kanbanTimeToY(hourT);
                    html += '<div class="kanban-hour-line" style="position:absolute;left:0;right:0;top:' + lineY + 'px;height:' + hourLineH + 'px;background:' + gridColor + ';"></div>';
                    if (_isLabelCol) {
                        var ld = new Date(hourT);
                        html += '<div style="position:absolute;left:2px;top:' + (lineY - 14) + 'px;font-size:10px;color:' + labelColor + ';pointer-events:none;white-space:nowrap;">'
                            + ('0' + ld.getHours()).slice(-2) + ':' + ('0' + ld.getMinutes()).slice(-2) + '</div>';
                    }
                }
                if (subOn) {
                    for (var m = 1; m < 6; m++) {
                        var subTime = hourT + m * 600000;
                        if (subTime < sg.t0 || subTime > sg.t1) continue;
                        var subY = topPad + _kanbanTimeToY(subTime);
                        html += '<div class="kanban-hour-line" style="position:absolute;left:0;right:0;top:' + subY + 'px;height:1px;background:' + subGridColor + ';"></div>';
                        if (_isLabelCol && _kanbanPixelsPerHour > 300) {
                            var sd = new Date(subTime);
                            html += '<div style="position:absolute;left:2px;top:' + (subY - 12) + 'px;font-size:9px;color:' + subLabelColor + ';pointer-events:none;">'
                                + ('0' + sd.getHours()).slice(-2) + ':' + ('0' + sd.getMinutes()).slice(-2) + '</div>';
                        }
                    }
                }
                hourT += 3600000;
            }
        }

        // Presence bars - multi-device aware
        // Collect unique device IDs for color assignment
        var _seenDevices = {};
        for (var p = 0; p < _kanbanPresenceLog.length; p++) {
            if (_kanbanPresenceLog[p].device_id) _seenDevices[_kanbanPresenceLog[p].device_id] = true;
        }
        _seenDevices[_kanbanDeviceId] = true;
        var _deviceList = Object.keys(_seenDevices);
        // Color palette for devices: orange=self, purple=other1, teal=other2, pink=other3
        //
        // Deliberately not tokens, and the one place in this file that keeps raw
        // values. These four exist to be told apart from each other, not to match
        // the theme: mapping them onto semantic roles would collapse four devices
        // into one colour and delete the feature. All four are mid-tone and read
        // against either scheme's surfaces.
        var _deviceColors = ['rgba(255,152,0,', 'rgba(156,39,176,', 'rgba(0,150,136,', 'rgba(233,30,99,'];
        for (var p = 0; p < _kanbanPresenceLog.length; p++) {
            var pr = _kanbanPresenceLog[p];
            if (pr.sid !== sid) continue;
            // Both ends mapped, then subtracted. duration * pxPerMs is wrong the
            // moment an interval spans a collapsed band: it yields far more height
            // than the band actually occupies and the bar buries its neighbours.
            var enterY = topPad + _kanbanTimeToY(pr.enter);
            var leaveY = topPad + _kanbanTimeToY(pr.leave);
            var barTop = Math.min(enterY, leaveY);
            var barH = Math.abs(leaveY - enterY);
            if (barH < 6) barH = 6;
            var devIdx = _deviceList.indexOf(pr.device_id || _kanbanDeviceId);
            if (devIdx < 0) devIdx = 0;
            var colorBase = _deviceColors[devIdx % _deviceColors.length];
            var barLeft = 2 + devIdx * 14; // Offset each device horizontally
            html += '<div class="kanban-presence" style="position:absolute;left:' + barLeft + 'px;width:10px;top:' + barTop + 'px;height:' + barH + 'px;background:' + colorBase + '0.5);border-radius:3px;border:1px solid ' + colorBase + '0.8);"></div>';
        }
        // Current presence (this device only)
        if (_kanbanCurrentEnter && _kanbanCurrentEnter.sid === sid && !_kanbanIsIdle) {
            var curY = topPad + _kanbanTimeToY(_kanbanCurrentEnter.time);
            var curH = (topPad + _kanbanTimeToY(now)) - curY;
            if (curH < 6) curH = 6;
            var selfIdx = _deviceList.indexOf(_kanbanDeviceId);
            var selfLeft = 2 + (selfIdx >= 0 ? selfIdx : 0) * 14;
            var selfColor = _deviceColors[(selfIdx >= 0 ? selfIdx : 0) % _deviceColors.length];
            html += '<div class="kanban-presence" style="position:absolute;left:' + selfLeft + 'px;width:10px;top:' + curY + 'px;height:' + curH + 'px;background:' + selfColor + '0.7);border-radius:3px;border:1px solid ' + selfColor + '1);"></div>';
        }

        // Autopilot indicator: time-positioned segments from _autopilot_history
        var sessMap = window._lastSessionsMap || {};
        var sessData = sessMap[sid];
        var apCache = _kanbanAutopilotCache[sid] || {};
        var apHistory = apCache.history || (sessData && sessData._autopilot_history ? sessData._autopilot_history : []);
        for (var ap = 0; ap < apHistory.length; ap++) {
            var apEntry = apHistory[ap];
            var apStartY = topPad + _kanbanTimeToY(apEntry.started * 1000);
            var apEndY = topPad + _kanbanTimeToY(apEntry.ended * 1000);
            var apTop = Math.min(apStartY, apEndY);
            var apH = Math.abs(apEndY - apStartY);
            if (apH < 6) apH = 6;
            // Minutes from the timestamps, not back-computed from pixel height:
            // apH / pxPerMs was only ever valid on a uniform axis, and it now
            // under-reports any run that crosses a collapsed band.
            var apMin = Math.abs(apEntry.ended - apEntry.started) / 60;
            html += '<div style="position:absolute;right:2px;width:6px;top:' + apTop + 'px;height:' + apH + 'px;background:color-mix(in srgb, var(--md-sys-color-error) 40%, transparent);border-radius:3px;border:1px solid color-mix(in srgb, var(--md-sys-color-error) 70%, transparent);" title="托管 ' + apMin.toFixed(0) + '分钟"></div>';
        }
        // Currently active autopilot: detect transitions
        // autopilot_active flag might stay true even after all work is done (backend bug)
        // Use is_processing + active_threads as ground truth for "actually working"
        var _apFlagActive = sessData && sessData.autopilot_active === true;
        var _apActuallyWorking = _apFlagActive && sessData && (sessData.is_processing || sessData.active_threads > 0);
        var _apActive = _apActuallyWorking;
        var _apStartedAt = (sessData && sessData._autopilot_started_at) || apCache.started_at;
        // Detect transition: was active last frame, not active now → freeze into history
        var _wasActive = _kanbanLastApState[sid];
        _kanbanLastApState[sid] = _apActive;
        if (_wasActive && !_apActive && _apStartedAt) {
            // Autopilot just ended: create history entry at this moment
            var _endNow = now / 1000;
            var _alreadyFrozen = apHistory.some(function(h) { return h.started === _apStartedAt; });
            if (!_alreadyFrozen) {
                apHistory.push({started: _apStartedAt, ended: _endNow});
                if (!_kanbanAutopilotCache[sid]) _kanbanAutopilotCache[sid] = {history: [], started_at: null, active: false};
                _kanbanAutopilotCache[sid].history = apHistory;
            }
        }
        if (_apActive && _apStartedAt) {
            var apNowStartY = topPad + _kanbanTimeToY(_apStartedAt * 1000);
            var apNowH = Math.max(6, (topPad + _kanbanTimeToY(now)) - apNowStartY);
            // steps(2) is gone: styles.css already replaced the blink with a
            // breathing keyframe, and passing a step function here re-imposed the
            // hard switch locally, undoing that fix. Now identical to the tab
            // status dot's parameters.
            html += '<div style="position:absolute;right:2px;width:6px;top:' + apNowStartY + 'px;height:' + apNowH + 'px;background:color-mix(in srgb, var(--md-sys-color-error) 50%, transparent);border-radius:3px;border:1px solid color-mix(in srgb, var(--md-sys-color-error) 80%, transparent);animation:tab-pulse var(--md-sys-motion-duration-long2) infinite alternate var(--md-sys-motion-easing-emphasized);" title="托管中..."></div>';
        }

        // Bubble bars - duration-based rendering with lifecycle phases
        // One resolver, shared with the time sweep above. Two copies would drift,
        // and the drift is quiet: the sweep sees a session's history while the
        // draw pass does not, so its messages land at a time no segment covers and
        // _kanbanTimeToY's fallback piles them at the very bottom.
        var history = _kanbanHistoryFor(sid);
        for (var b = 0; b < history.length; b++) {
            var msg = history[b];
            if (msg.is_hidden) continue;
            // Use started_at (original creation time) because created_at is overwritten to completion time
            var msgCreatedAt = msg.started_at || msg.created_at;
            if (!msgCreatedAt) {
                msgCreatedAt = (now / 1000) - (history.length - b) * 120;
            }
            var msgStartMs = msgCreatedAt * 1000;
            var msgStartY = topPad + _kanbanTimeToY(msgStartMs);

            var msgDate = new Date(msgStartMs);
            var timeStr = ('0' + msgDate.getHours()).slice(-2) + ':' + ('0' + msgDate.getMinutes()).slice(-2) + ':' + ('0' + msgDate.getSeconds()).slice(-2);

            // Tool results: separate category, thin gray dot
            // Use created_at directly (not started_at) since tool_results don't go through worker_engine's timestamp overwrite
            if (msg.is_tool_result || msg.is_auto_read) {
                var toolTs = msg.created_at;
                if (toolTs) {
                    var toolY = topPad + _kanbanTimeToY(toolTs * 1000);
                    html += '<div class="kanban-bubble" title="tool ' + timeStr + '" style="position:absolute;left:50%;top:' + toolY + 'px;width:4px;height:4px;margin-left:-2px;background:var(--md-sys-color-outline);border-radius:50%;pointer-events:none;"></div>';
                }
                continue;
            }

            if (msg.role === 'user') {
                // User bubbles: thin horizontal LINE marking the exact moment
                var tooltipText = 'user ' + timeStr + (msg.summary ? ' - ' + msg.summary.substring(0, 40) : '');
                html += '<div class="kanban-bubble" title="' + tooltipText.replace(/"/g, '&quot;') + '" data-sid="' + sid + '" onclick="if(typeof switchToTab===\'function\'){exitKanban();switchToTab(\'' + sid + '\');}" style="position:absolute;left:12px;right:4px;top:' + msgStartY + 'px;height:2px;background:var(--md-sys-color-primary);cursor:pointer;"></div>';
            } else {
                // Assistant bubbles: duration bar extending DOWNWARD from creation point
                // Try multiple timing field names for phase data
                var ttfbMs = 0, downloadMs = 0;
                var _t = msg.timing || msg;
                if (_t.ttfb) ttfbMs = _t.ttfb * 1000;
                else if (_t.first_token) ttfbMs = _t.first_token * 1000;
                else if (_t.first_token_time) ttfbMs = _t.first_token_time * 1000;
                if (_t.download_t) downloadMs = _t.download_t * 1000;
                else if (_t.recv_time) downloadMs = _t.recv_time * 1000;
                else if (_t.download_time) downloadMs = _t.download_time * 1000;
                else if (_t.stream_time) downloadMs = _t.stream_time * 1000;
                // Total duration: prefer completed_at for exact value
                var durationMs = 0;
                var isLastAndProcessing = false;
                if (msg.completed_at && msgCreatedAt) {
                    durationMs = (msg.completed_at - msgCreatedAt) * 1000;
                } else if (ttfbMs > 0 || downloadMs > 0) {
                    durationMs = ttfbMs + downloadMs;
                } else if (b === history.length - 1 && sessData && (sessData.is_processing || sessData.active_threads > 0)) {
                    durationMs = now - msgStartMs;
                    isLastAndProcessing = true;
                }
                // If no data at all: durationMs stays 0 = unknown = minimal marker
                // If durationMs is still 0, we truly don't know the duration
                var durationStr = (durationMs / 1000).toFixed(1) + 's';
                var tooltipText = 'assistant ' + timeStr + ' [' + durationStr + ']' + (msg.summary ? ' - ' + msg.summary.substring(0, 40) : '');

                if (ttfbMs > 0 || downloadMs > 0) {
                    // 3-phase extending DOWNWARD: waiting (red-orange) then streaming (green)
                    var ttfbH = Math.max(3, ttfbMs * pxPerMs);
                    var dlH = Math.max(3, downloadMs * pxPerMs);
                    html += '<div class="kanban-bubble" title="' + tooltipText.replace(/"/g, '&quot;') + '" data-sid="' + sid + '" onclick="if(typeof switchToTab===\'function\'){exitKanban();switchToTab(\'' + sid + '\');}" style="position:absolute;left:14px;right:6px;top:' + msgStartY + 'px;height:' + ttfbH + 'px;background:color-mix(in srgb, var(--md-sys-color-warning) 75%, transparent);border-radius:2px 2px 0 0;cursor:pointer;"></div>';
                    html += '<div class="kanban-bubble" style="position:absolute;left:14px;right:6px;top:' + (msgStartY + ttfbH) + 'px;height:' + dlH + 'px;background:color-mix(in srgb, var(--md-sys-color-success) 75%, transparent);border-radius:0 0 2px 2px;cursor:pointer;" onclick="if(typeof switchToTab===\'function\'){exitKanban();switchToTab(\'' + sid + '\');}"></div>';
                } else if (durationMs > 0) {
                    // Estimated or real-time growing duration
                    var bubbleH = Math.max(4, durationMs * pxPerMs);
                    // _barBg, not bgColor: var is function-scoped and that name is
                    // already the column background above. The two overwrite each
                    // other and only work today because the column's html is built
                    // before this loop runs.
                    var _barBg = isLastAndProcessing
                        ? 'color-mix(in srgb, var(--md-sys-color-warning) 60%, transparent)'
                        : 'color-mix(in srgb, var(--md-sys-color-success) 55%, transparent)';
                    html += '<div class="kanban-bubble" title="' + tooltipText.replace(/"/g, '&quot;') + '" data-sid="' + sid + '" onclick="if(typeof switchToTab===\'function\'){exitKanban();switchToTab(\'' + sid + '\');}" style="position:absolute;left:14px;right:6px;top:' + msgStartY + 'px;height:' + bubbleH + 'px;background:' + _barBg + ';border-radius:2px;cursor:pointer;"></div>';
                } else {
                    // Unknown duration: minimal 4px marker, no fake time bar
                    html += '<div class="kanban-bubble" title="' + tooltipText.replace(/"/g, '&quot;') + '" data-sid="' + sid + '" onclick="if(typeof switchToTab===\'function\'){exitKanban();switchToTab(\'' + sid + '\');}" style="position:absolute;left:14px;right:6px;top:' + msgStartY + 'px;height:4px;background:color-mix(in srgb, var(--md-sys-color-success) 40%, transparent);border-radius:2px;cursor:pointer;"></div>';
                }
            }
        }

        html += '</div>';
    }
    container.innerHTML = html;
}

/* Zoom, on a log scale.
 *
 * The slider carries a dimensionless 0–1000 position, not px/h. 720 is the full
 * range's ratio (3600 / 5), so v=0 gives 5 px/h and v=1000 gives 3600 — the same
 * endpoints as before — while the feel in between changes from a constant
 * 24 px/h per pixel of travel to a constant ~2.5% per pixel.
 *
 * The inverse is provided rather than open-coded at the two call sites that need
 * it (the readout and the initial slider position). Two copies of a conversion
 * drift, and the symptom here is a readout that disagrees with the slider — the
 * user believes the number and concludes the slider is broken.
 */
var _KZ_MIN = 5, _KZ_RATIO = 720;

function _kanbanZoomToPph(v) {
    return _KZ_MIN * Math.pow(_KZ_RATIO, v / 1000);
}

function _kanbanPphToZoom(pph) {
    return Math.round(1000 * Math.log(pph / _KZ_MIN) / Math.log(_KZ_RATIO));
}

/** Format for the readout. Sub-hour scales need a finer unit than "N/h". */
function _kanbanZoomLabel(pph) {
    if (pph >= 60) return Math.round(pph) + '/h';
    return (pph * 60).toFixed(0) + '/d';
}

// Initialize presence tracking on page load
document.addEventListener('DOMContentLoaded', function() {
    var zoomEl = document.getElementById('kanban-zoom');
    var readoutEl = document.getElementById('kanban-zoom-readout');
    if (zoomEl) {
        // Position the slider from the current scale rather than trusting the
        // markup's default, so the two cannot start out disagreeing.
        zoomEl.value = _kanbanPphToZoom(_kanbanPixelsPerHour);
        zoomEl.addEventListener('input', function() {
            _kanbanPixelsPerHour = _kanbanZoomToPph(parseFloat(this.value) || 0);
            if (readoutEl) readoutEl.textContent = _kanbanZoomLabel(_kanbanPixelsPerHour);
            if (_kanbanActive) renderKanban();
        });
    }
    /* Horizontal scroll, shared with the tab bar.
     *
     * Not an independent axis. Columns are positioned from .bottom-tab's
     * offsetLeft, which is a coordinate inside #bottom-tabs-container's own
     * scrollable content, so the two boxes have to hold the same horizontal
     * offset or a tab ends up above someone else's column — and clicking that
     * column switches to the wrong session.
     *
     * The guard flag is required, not defensive: without it A's scroll event
     * writes B's scrollLeft, B's scroll event writes A's, and sub-pixel rounding
     * keeps them nudging each other forever. The visible result is a kanban that
     * drifts sideways on its own.
     *
     * Vertical is deliberately not synced — the tab bar has no vertical scroll,
     * and the kanban's vertical offset is the user's reading position.
     */
    var kv = document.getElementById('kanban-view');
    var tc = document.getElementById('bottom-tabs-container');
    if (kv && tc) {
        var _syncing = false;
        var _mirror = function(from, to) {
            if (_syncing) return;
            _syncing = true;
            to.scrollLeft = from.scrollLeft;
            // Released on the next task, after the assignment's own scroll event
            // has been dispatched and swallowed.
            setTimeout(function() { _syncing = false; }, 0);
        };
        kv.addEventListener('scroll', function() { if (_kanbanActive) _mirror(kv, tc); }, {passive: true});
        tc.addEventListener('scroll', function() { if (_kanbanActive) _mirror(tc, kv); }, {passive: true});
    }

    // Record initial presence after a short delay
    setTimeout(function() {
        var sid = window.localSid || window.currentSessionId;
        if (sid) _kanbanRecordEnter(sid);
    }, 500);
});