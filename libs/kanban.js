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
    var kanbanZoom = document.getElementById('kanban-zoom');
    var inputArea = document.getElementById('input-area');
    if (kanbanView) kanbanView.style.display = 'block';
    if (kanbanZoom) kanbanZoom.style.display = 'block';
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
    _kanbanTimer = setInterval(renderKanban, 1000);
}

function exitKanban() {
    _kanbanActive = false;
    var kanbanView = document.getElementById('kanban-view');
    var kanbanZoom = document.getElementById('kanban-zoom');
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

function renderKanban() {
    var container = document.getElementById('kanban-view');
    if (!container) return;
    var rawHeight = container.clientHeight || window.innerHeight - 80;
    var bottomPadding = 30; // Reserve space at bottom so "now" isn't at the very edge
    var viewHeight = rawHeight - bottomPadding;
    var now = Date.now();
    var pxPerMs = _kanbanPixelsPerHour / 3600000;

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
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--md-sys-color-on-surface-variant);">无已打开的标签页</div>';
        return;
    }

    // Read actual tab positions and colors from DOM
    var tabEls = document.querySelectorAll('.bottom-tab');
    var tabPositions = [];
    var tabColors = [];
    for (var ti = 0; ti < tabEls.length; ti++) {
        tabPositions.push({left: tabEls[ti].offsetLeft, width: tabEls[ti].offsetWidth});
        tabColors.push(getComputedStyle(tabEls[ti]).backgroundColor);
    }

    var html = '';
    for (var i = 0; i < tabs.length; i++) {
        var sid = tabs[i];
        // Column positioning from actual tab DOM
        var colLeft = tabPositions[i] ? tabPositions[i].left : (i * (container.clientWidth / tabs.length));
        var colW = tabPositions[i] ? tabPositions[i].width : (container.clientWidth / tabs.length);
        // Fallback uses the same formula as an inactive tab, so the two paths —
        // computed tab colour available or not — look alike. The old 83%
        // lightness made a fallback column a bright slab under the dark scheme.
        var colBg = tabColors[i] || ('color-mix(in srgb, hsl(' + ((i * 60) % 360)
            + ' 60% 50%) 12%, var(--md-sys-color-surface-container-high))');

        html += '<div class="kanban-col" style="position:absolute;left:' + colLeft + 'px;top:0;width:' + colW + 'px;height:100%;background:' + colBg + ';border-right:1px solid var(--md-sys-color-outline-variant);overflow:hidden;">';

        // Hour lines + sub-hour lines when zoomed in
        var hoursToShow = Math.ceil(viewHeight / _kanbanPixelsPerHour) + 2;
        var nowDate = new Date(now);
        var currentHourStart = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate(), nowDate.getHours(), 0, 0).getTime();
        for (var h = 0; h <= hoursToShow; h++) {
            var lineTime = currentHourStart - h * 3600000;
            var lineAge = now - lineTime;
            var lineY = viewHeight - (lineAge * pxPerMs);
            var hourLineH = _kanbanPixelsPerHour > 500 ? 2 : 1;
            html += '<div class="kanban-hour-line" style="position:absolute;left:0;right:0;top:' + lineY + 'px;height:' + hourLineH + 'px;background:' + gridColor + ';"></div>';
            // Time label on first column
            if (i === 0) {
                var lineDate = new Date(lineTime);
                var timeLabel = ('0' + lineDate.getHours()).slice(-2) + ':' + ('0' + lineDate.getMinutes()).slice(-2);
                html += '<div style="position:absolute;left:2px;top:' + (lineY - 14) + 'px;font-size:10px;color:' + labelColor + ';pointer-events:none;white-space:nowrap;">' + timeLabel + '</div>';
            }
            // 10-minute sub-lines when zoom is high enough (>120 px/h = each 10min > 20px)
            if (_kanbanPixelsPerHour > 120) {
                for (var m = 1; m < 6; m++) {
                    var subTime = lineTime + m * 600000;
                    var subAge = now - subTime;
                    var subY = viewHeight - (subAge * pxPerMs);
                    var subLineH = _kanbanPixelsPerHour > 1000 ? 2 : 1;
                    html += '<div class="kanban-hour-line" style="position:absolute;left:0;right:0;top:' + subY + 'px;height:' + subLineH + 'px;background:' + subGridColor + ';"></div>';
                    if (i === 0 && _kanbanPixelsPerHour > 300) {
                        var subDate = new Date(subTime);
                        var subLabel = ('0' + subDate.getHours()).slice(-2) + ':' + ('0' + subDate.getMinutes()).slice(-2);
                        html += '<div style="position:absolute;left:2px;top:' + (subY - 12) + 'px;font-size:9px;color:' + subLabelColor + ';pointer-events:none;">' + subLabel + '</div>';
                    }
                }
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
            var enterAge = now - pr.enter;
            var leaveAge = now - pr.leave;
            var enterY = viewHeight - (enterAge * pxPerMs);
            var leaveY = viewHeight - (leaveAge * pxPerMs);
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
            var curAge = now - _kanbanCurrentEnter.time;
            var curY = viewHeight - (curAge * pxPerMs);
            var curH = curAge * pxPerMs;
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
            var apStartAge = now - apEntry.started * 1000;
            var apEndAge = now - apEntry.ended * 1000;
            var apStartY = viewHeight - (apStartAge * pxPerMs);
            var apEndY = viewHeight - (apEndAge * pxPerMs);
            var apTop = Math.min(apStartY, apEndY);
            var apH = Math.abs(apEndY - apStartY);
            if (apH < 6) apH = 6;
            html += '<div style="position:absolute;right:2px;width:6px;top:' + apTop + 'px;height:' + apH + 'px;background:color-mix(in srgb, var(--md-sys-color-error) 40%, transparent);border-radius:3px;border:1px solid color-mix(in srgb, var(--md-sys-color-error) 70%, transparent);" title="托管 ' + (apH / pxPerMs / 60000).toFixed(0) + '分钟"></div>';
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
            var apNowStartAge = now - _apStartedAt * 1000;
            var apNowStartY = viewHeight - (apNowStartAge * pxPerMs);
            var apNowH = Math.max(6, viewHeight - apNowStartY);
            // steps(2) is gone: styles.css already replaced the blink with a
            // breathing keyframe, and passing a step function here re-imposed the
            // hard switch locally, undoing that fix. Now identical to the tab
            // status dot's parameters.
            html += '<div style="position:absolute;right:2px;width:6px;top:' + apNowStartY + 'px;height:' + apNowH + 'px;background:color-mix(in srgb, var(--md-sys-color-error) 50%, transparent);border-radius:3px;border:1px solid color-mix(in srgb, var(--md-sys-color-error) 80%, transparent);animation:tab-pulse var(--md-sys-motion-duration-long2) infinite alternate var(--md-sys-motion-easing-emphasized);" title="托管中..."></div>';
        }

        // Bubble bars - duration-based rendering with lifecycle phases
        var history = [];
        if (sid === window.currentSessionId && typeof currentHistory !== 'undefined') {
            history = currentHistory;
        } else if (typeof _sessionHistoryCache !== 'undefined' && _sessionHistoryCache[sid]) {
            history = _sessionHistoryCache[sid];
        } else if (typeof window._sessionHistoryCache !== 'undefined' && window._sessionHistoryCache[sid]) {
            history = window._sessionHistoryCache[sid];
        }
        for (var b = 0; b < history.length; b++) {
            var msg = history[b];
            if (msg.is_hidden) continue;
            // Use started_at (original creation time) because created_at is overwritten to completion time
            var msgCreatedAt = msg.started_at || msg.created_at;
            if (!msgCreatedAt) {
                msgCreatedAt = (now / 1000) - (history.length - b) * 120;
            }
            var msgStartMs = msgCreatedAt * 1000;
            var msgStartAge = now - msgStartMs;
            var msgStartY = viewHeight - (msgStartAge * pxPerMs);

            var msgDate = new Date(msgStartMs);
            var timeStr = ('0' + msgDate.getHours()).slice(-2) + ':' + ('0' + msgDate.getMinutes()).slice(-2) + ':' + ('0' + msgDate.getSeconds()).slice(-2);

            // Tool results: separate category, thin gray dot
            // Use created_at directly (not started_at) since tool_results don't go through worker_engine's timestamp overwrite
            if (msg.is_tool_result || msg.is_auto_read) {
                var toolTs = msg.created_at;
                if (toolTs) {
                    var toolAge = now - toolTs * 1000;
                    var toolY = viewHeight - (toolAge * pxPerMs);
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

// Initialize presence tracking on page load
document.addEventListener('DOMContentLoaded', function() {
    var zoomEl = document.getElementById('kanban-zoom');
    if (zoomEl) {
        zoomEl.addEventListener('input', function() {
            _kanbanPixelsPerHour = parseInt(this.value) || 100;
            if (_kanbanActive) renderKanban();
        });
    }
    // Record initial presence after a short delay
    setTimeout(function() {
        var sid = window.localSid || window.currentSessionId;
        if (sid) _kanbanRecordEnter(sid);
    }, 500);
});