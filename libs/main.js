
const chatContainer = document.getElementById('chat-container');

        // Markdown rendering + marked config -> /libs/utils.js
            let codeConfig = { paths: [], extensions: ['.py', '.html', '.json', '.md', '.ipynb', '.yaml', '.yml', '.tex', '.bib', '.txt'], selected_files: [], custom_extensions: '', tree_limit_k: 10 };
        let codeTokens = 0;

        window.addEventListener('DOMContentLoaded', async () => {
            const urlParams = new URLSearchParams(window.location.search);
            if (urlParams.has('sid')) {
                window.localSid = urlParams.get('sid');
            }
            if (localStorage.getItem('sidebar_collapsed') === 'true') {
                document.getElementById('sidebar').classList.add('collapsed');
            }
        });

        // Code monitor: refreshCodeContextStats, openCodeMonitor, scanCodeFiles, saveCodeMonitor, toggleAllFiles -> /libs/codemonitor.js

        const queueArea = document.getElementById('queue-area');
        const userInput = document.getElementById('user-input');
        const tokenEst = document.getElementById('token-est');
        const sendButton = document.getElementById('send-button');
        
        let currentHistory = [];
        let lastSessionsHash = "";
        let lastChatHash = "__force__";
        let lastQueueHash = "";
        let lastCodeConfigHash = "";
        let thinkingTimer = null;
        let thinkingVisible = true;
        let bubbleCache = {};
        let editIdx = -1;
        let editType = 'content';
        let isProcessingVisuals = false;
        let processingStart = 0;
        let heavyMessages = [];
        let isHeavyPanelOpen = false;
        let globalModelStats = {};
        let lastModelStatsHash = "";
        let lastModelsHash = "";
            let AVAILABLE_MODELS = []; // Loaded dynamically from providers.json via backend state

        let hiddenModels = [];
        let isFirstModelRender = true;
        let isMoreModelsExpanded = false;
        let globalSettings = { enable_correction: false, enable_queue: false, enable_steps: false, enable_starred: false, enable_autopilot: true, enable_deep_think_ui: false, enable_pure_mode: false, enable_arc3: false, enable_stream: true, auto_hide_env_obs: false, enable_anthropic_protocol: false, enable_tool_inject: false, starred_messages: [], developer_mode: false, enable_bottom_tabs: false, enable_style_filter: false, enable_ttfb_retry: true };
        let lastSettingsHash = "";

        // Settings: openSettingsModal, closeSettingsModal, saveSettings, applySettingsUI
        // -> Moved to /libs/settings.js

        // Model functions: sendWithModel, hideModel, restoreModel, renderModelDropdown, updateSendButtonCount
        // -> Moved to /libs/models.js

        document.getElementById('model-dropdown').addEventListener('change', () => {
            updateSendButtonCount();
            const checkedBoxes = Array.from(document.querySelectorAll('.model-cb:checked')).map(cb => cb.value);
            localStorage.setItem('llm_selected_models', JSON.stringify(checkedBoxes));
        });
        document.getElementById('max-steps').addEventListener('input', updateSendButtonCount);
        document.getElementById('parallel-mode').addEventListener('change', updateSendButtonCount);

        renderModelDropdown(); // 初始化渲染

        // toggleSidebar -> /libs/actions.js

        async function postAction(payload) {
            if (window.localSid && payload.action !== 'switch_session') {
                payload.client_sid = window.localSid;
                payload._selected_models = Array.from(document.querySelectorAll('.model-cb:checked')).map(function(cb) { return cb.value; });
                payload._deep_think_level = deepThinkLevel;
                payload._draft_text = userInput.value;
            }
            // For switch_session: save outgoing session's state via explicit routing
            // (prevents both pollution AND data loss — _outgoing_sid tells server WHERE to save)
            // Guard: only save if browser UI was actually restored for that session.
            // Without this guard, rapid switching A→B→C writes A's stale UI values to B
            // (because restoration hasn't happened yet within 300ms throttle window).
            if (payload.action === 'switch_session' && payload._outgoing_sid) {
                if (_lastRestoredSid === payload._outgoing_sid) {
                    payload._outgoing_state = {
                        _draft_text: userInput.value,
                        _deep_think_level: deepThinkLevel,
                        _selected_models: Array.from(document.querySelectorAll('.model-cb:checked')).map(function(cb) { return cb.value; })
                    };
                } else {
                    // Session was never restored (rapid switch) — UI values are stale garbage.
                    // Don't save anything; let server preserve the session's existing state.
                    delete payload._outgoing_sid;
                }
            }

            // Mobile: auto-collapse sidebar after session switch
            if (payload.action === 'switch_session' && window.innerWidth < 768) {
                var _sb = document.getElementById('sidebar');
                if (_sb && !_sb.classList.contains('collapsed')) {
                    _sb.classList.add('collapsed');
                    localStorage.setItem('sidebar_collapsed', 'true');
                }
            }
            try {
                const res = await fetch('/api/action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    if (typeof showToast === 'function') {
                        showToast(`操作失败 [${res.status}]: ${data.message || res.statusText}`, 'error');
                    }
                } else {
                    const data = await res.json();
                if (data.new_sid) {
                    window.localSid = data.new_sid;
                    window.history.pushState({}, '', `/?sid=${data.new_sid}`);
                    window.hasLoadedFullHistory = false;
                    uiState = {}; // 会话切换时清除UI状态
                    postAction({action: 'switch_session', sid: data.new_sid});
                }
                if (data.state) {
                    // HTTP响应是用户操作的直接回复，必须绕过state_version防退检查
                    // 防止WebSocket push抢先到达导致HTTP响应被误判为stale而丢弃
                    clientLastStateVersion = 0;
                    handleStateUpdate(data.state);
                }
                }
            } catch (e) {
                if (typeof showToast === 'function') {
                    showToast(`网络或请求错误: ${e.message}`, 'error');
                }
                console.error("postAction Error:", e);
            }
        }

        const socket = io();

        socket.on('connect', () => {
            console.log('成功连接到 WebSocket 服务器。');
        });

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                lastChatHash = '__force__';
                isRenderScheduled = false;
                pendingState = null;
                // 无条件拉取最新数据+强制重渲染，彻底解决后台积累的状态不同步
                var _visSid = window.localSid;
                if (_visSid && _visSid !== 'starred_session_virtual') {
                    fetch('/api/action', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: 'get_session_data', sid: _visSid})
                    }).then(function(r) { return r.json(); }).then(function(res) {
                        if (res.status === 'ok' && res.session_data && res.session_data.conversation_history) {
                            _applyUiState(res.session_data.conversation_history);
                            lastChatHash = '__force__';
                            bubbleCache = {};
                            window._expandStateMap = {};
                            renderChat(res.session_data.conversation_history);
                            renderQueue(res.session_data.message_queue || [], res.session_data.is_paused);
                            setUIEnabled(!res.session_data.is_processing);
                        }
                    }).catch(function() {});
                }
                // 仍然发送 ping 以触发 WebSocket 推送（覆盖其他 UI 元素如托管状态等）
                postAction({action: 'ping'});
            }
        });

        socket.on('disconnect', () => {
            console.warn('与 WebSocket 服务器断开连接。尝试重新连接...');
        });

        console.log('[CHATAPP] main.js v1 loaded — optimistic updates active, timing logs enabled');
        socket.emit('perf_log', {label: 'main.js v2 loaded (optimistic+timing)', duration: 0});

        socket.on('show_toast', (data) => {
            if (typeof showToast === 'function') {
                showToast(data.message, data.type);
            }
        });

        // 协议标记过滤：隐藏路由令牌和概括块标记
        function filterProtocolMarkers(text) {
            if (!text) return text;
            text = text.replace(/\[令牌[a-z0-9]+\]\s*/gi, '');
            text = text.replace(/\[概括开头\][\s\S]*?\[概括结尾\]/g, '');
            return text;
        }

        // Streaming state tracking: prevents stale state_update from reverting streaming DOM
        var _lastStreamingBubbleId = null;
        var _lastStreamingTime = 0;
        var _lastStreamingContent = '';
        var _streamingWatchdog = null;

        /* Streaming paint, coalesced into one frame.
         *
         * data.content is the accumulated body, not a delta, so each event used to
         * reparse the whole thing through marked and highlight.js and then let
         * KaTeX walk every text node again. A ten-thousand-character reply arriving
         * in two hundred pushes meant two hundred full parses of an average of five
         * thousand characters — quadratic in the length of a single reply.
         *
         * Coalescing per bubble_id keeps simultaneous streams independent and never
         * drops the final state: rAF always fires, and the map holds the latest
         * payload per bubble. Frames are skipped while the tab is hidden, which is
         * the desired behaviour — the visibilitychange handler force-renders on the
         * way back.
         *
         * Deliberately NOT reparsing only the trailing unclosed segment, even
         * though that would be the larger win: an edit to an earlier part of the
         * body would then never be re-rendered, and an edit anywhere in the context
         * has to reach the screen. */
        var _streamPending = {};
        var _streamRaf = 0;

        function _flushStreaming() {
            _streamRaf = 0;
            var batch = _streamPending;
            _streamPending = {};
            for (var _bid in batch) _paintStreaming(batch[_bid]);
        }

        function _paintStreaming(data) {
            const bubble = document.getElementById('msg-bubble-' + data.bubble_id);
            if (!bubble) return;
            const summaryBox = bubble.querySelector('.summary-box');
            if (summaryBox) {
                summaryBox.innerHTML = '<b>概括：</b>' + (data.summary || '生成中...');
                return;
            }
            const contentDiv = bubble.querySelector('.content');
            if (!contentDiv || !data.content) return;
            contentDiv.innerHTML = renderMarkdownProtected(filterProtocolMarkers(data.content));
            // No dollar sign anywhere in the accumulated body means there is nothing
            // for KaTeX to find, and this ran unconditionally on every push.
            if (data.content.indexOf('$') >= 0) {
                try { renderMathInElement(contentDiv, { delimiters: [{left: "$$", right: "$$", display: true}, {left: "$", right: "$", display: false}] }); } catch(e) {}
            }
        }

        socket.on('streaming_content', (data) => {
            // Tracking stays synchronous: the watchdog and the restore path in
            // _doHandleStateUpdate both read these, and they are cheap.
            _lastStreamingBubbleId = data.bubble_id;
            _lastStreamingTime = Date.now();
            _lastStreamingContent = data.content || '';
            clearTimeout(_streamingWatchdog);
            _streamingWatchdog = setTimeout(function() {
                _lastStreamingBubbleId = null;
                _lastStreamingContent = '';
                lastChatHash = '__force__';
                postAction({action: 'ping'});
            }, 3000);

            _streamPending[data.bubble_id] = data;
            if (_streamRaf) return;
            _streamRaf = requestAnimationFrame(_flushStreaming);
        });

        socket.on('cache_prediction', (data) => {
            if (data.last_sent_max_id) {
                window._prefixLockMaxId = data.last_sent_max_id;
            }
            window._cacheSegments = data.segments || 0;
            _updateCachePrediction();
        });
        function _updateCachePrediction() {
            if (!globalSettings.enable_multiturn_format) {
                var _existing = document.getElementById('cache-prediction');
                if (_existing) _existing.style.display = 'none';
                return;
            }
            var maxId = window._prefixLockMaxId || 0;
            var prefixChars = 0, suffixChars = 0;
            for (var i = 0; i < currentHistory.length; i++) {
                var m = currentHistory[i];
                if (m.is_hidden || m.is_error) continue;
                var len = (m.content || '').length;
                if (maxId && m.id <= maxId) {
                    prefixChars += len;
                } else {
                    suffixChars += len;
                }
            }
            var prefixK = (prefixChars / 3000).toFixed(1);
            var suffixK = (suffixChars / 3000).toFixed(1);
            var segments = window._cacheSegments || 0;
            var el = document.getElementById('cache-prediction');
            if (!el) {
                el = document.createElement('div');
                el.id = 'cache-prediction';
                el.style.cssText = 'font-size: 10px; text-align: center; padding: 4px 0;';
                chatContainer.appendChild(el);
            } else if (!chatContainer.contains(el)) {
                chatContainer.appendChild(el);
            }
            // textContent, so no inline SVG here; the format is self-describing.
            if (maxId > 0) {
                el.textContent = prefixK + 'k\u7F13\u5B58 | ' + suffixK + 'k\u65B0\u589E | ' + segments + '\u6BB5';
                el.style.color = suffixChars > 0 ? 'var(--md-sys-color-success)' : 'var(--md-sys-color-on-surface-variant)';
            } else {
                el.textContent = '\u9996\u6B21\u53D1\u9001 | ' + ((prefixChars + suffixChars) / 3000).toFixed(1) + 'k';
                el.style.color = 'var(--md-sys-color-on-surface-variant)';
            }
        }

        // Prefix lock: check function called before openEditModal by chat.js
        (function() {
            var _orig = openEditModal;
            openEditModal = function() {
                var msgId = arguments[0];
                if (globalSettings.enable_prefix_lock) {
                    var lockMaxId = window._prefixLockMaxId || 0;
                    if (lockMaxId && msgId <= lockMaxId) {
                        if (typeof showToast === 'function') showToast('\u524D\u7F00\u5DF2\u9501\u5B9A\uFF0C\u65E0\u6CD5\u7F16\u8F91\u8BE5\u6C14\u6CE1', 'error');
                        return Promise.resolve();
                    }
                }
                return _orig.apply(this, arguments);
            };
        })();


        // 幂等操作 + 即时反馈：发送目标值而非 toggle，多客户端同时操作结果确定
        function toggleMode(index, modeType) {
            var msg = currentHistory[index];
            if (!msg) return;
            // Prefix lock: block omit/hide on prefix bubbles
            if (globalSettings.enable_prefix_lock && (modeType === 'omit' || modeType === 'hide')) {
                var _lockMaxId = 0;
                for (var _li = 0; _li < currentHistory.length; _li++) {
                    if (currentHistory[_li]._last_sent_max_id) { _lockMaxId = currentHistory[_li]._last_sent_max_id; }
                }
                if (!_lockMaxId && window._prefixLockMaxId) _lockMaxId = window._prefixLockMaxId;
                if (_lockMaxId && msg.id <= _lockMaxId) {
                    if (typeof showToast === 'function') showToast('\u524D\u7F00\u5DF2\u9501\u5B9A\uFF0C\u65E0\u6CD5' + (modeType === 'omit' ? '\u6982\u62EC' : '\u9690\u85CF') + '\u8BE5\u6C14\u6CE1', 'error');
                    return;
                }
            }
            // 计算目标值：当前状态取反
            var field = modeType === 'omit' ? 'is_omitted' : modeType === 'hide' ? 'is_hidden' : 'is_collapsed';
            var desiredValue = !msg[field];
            // 即时视觉反馈 + 标记 pending
            pendingActions.add('' + msg.id);
            var bubble = document.getElementById('msg-bubble-' + msg.id);
            if (bubble) {
                bubble.style.opacity = '0.5';
                bubble.style.pointerEvents = 'none';
            }
            postAction({action: 'toggle_mode', index: index, mode_type: modeType, value: desiredValue});
        }

        // 即时反馈 + 服务器权威：不修改前端状态，等 WebSocket 推送
        function deleteMessageOptimistic(index) {
            var msg = currentHistory[index];
            if (!msg) return;
            // Prefix lock: block delete on prefix bubbles
            if (globalSettings.enable_prefix_lock) {
                var _dlockMaxId = window._prefixLockMaxId || 0;
                if (_dlockMaxId && msg.id <= _dlockMaxId) {
                    if (typeof showToast === 'function') showToast('\u524D\u7F00\u5DF2\u9501\u5B9A\uFF0C\u65E0\u6CD5\u5220\u9664\u8BE5\u6C14\u6CE1', 'error');
                    return;
                }
            }
            // 即时视觉反馈 + 标记 pending
            pendingActions.add('' + msg.id);
            var bubble = document.getElementById('msg-bubble-' + msg.id);
            if (bubble) {
                bubble.style.opacity = '0.3';
                bubble.style.pointerEvents = 'none';
                bubble.style.transition = 'opacity 0.15s';
            }
            postAction({action: 'delete_message', index: index});
        }

        // showToast -> /libs/utils.js

var pendingActions = new Set(); // 正在等待服务器确认的操作，renderChat后恢复半透明
window._dehydratedContentCache = {}; // message_id -> full message dict (for lazy-loaded dehydrated bubbles)
window._dehydratedLoadingSet = new Set(); // message_ids currently being fetched

function _fetchDehydratedContent(msgId) {
    if (window._dehydratedLoadingSet.has(msgId)) return;
    window._dehydratedLoadingSet.add(msgId);
    // Use synchronous XHR to ensure content appears before click handler returns
    // This is critical for test reliability - Playwright checks DOM immediately after click
    try {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/action', false); // synchronous
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.send(JSON.stringify({action: 'fetch_bubble_content', message_id: msgId, client_sid: window.localSid}));
        window._dehydratedLoadingSet.delete(msgId);
        if (xhr.status === 200) {
            var data = JSON.parse(xhr.responseText);
            if (data.status === 'ok' && data.message) {
                window._dehydratedContentCache[msgId] = data.message;
                // Trigger full re-render - cache merge at top of renderChat will inject content
                if (!window._expandStateMap) window._expandStateMap = {};
                window._expandStateMap['tr-content-' + msgId] = { expanded: true, scrollTop: 0 };
                lastChatHash = '__force__';
                if (currentHistory && currentHistory.length > 0) renderChat(currentHistory);
            }
        }
    } catch(e) {
        window._dehydratedLoadingSet.delete(msgId);
        console.error('Failed to fetch dehydrated content:', e);
    }
}

let pendingState = null;
let isRenderScheduled = false;
let clientLastStateVersion = 0;
let uiState = {}; // 客户端UI状态权威源：{msg_id: {c,o,h,u:客户端值, sc,so,sh,su: 上次已知服务器值, d: 已删除}}
var _lastRestoredSid = null;

// 将客户端UI状态应用到服务器数据：stale推送的旧值被忽略，服务器主动变更被接受
function _applyUiState(history) {
    for (var i = history.length - 1; i >= 0; i--) {
        var ui = uiState['' + history[i].id];
        if (ui && ui.d) history.splice(i, 1);
    }
    var seen = {};
    history.forEach(function(m) {
        var key = '' + m.id;
        seen[key] = true;
        var ui = uiState[key];
        if (ui) {
            // 检测服务器是否主动更改了UI字段（值与上次快照不同 = 有意更新，如流式完成取消折叠）
            if (!!m.is_collapsed !== ui.sc) { ui.c = !!m.is_collapsed; ui.sc = !!m.is_collapsed; pendingActions.delete(key); }
            if (!!m.is_omitted !== ui.so) { ui.o = !!m.is_omitted; ui.so = !!m.is_omitted; pendingActions.delete(key); }
            if (!!m.is_hidden !== ui.sh) { ui.h = !!m.is_hidden; ui.sh = !!m.is_hidden; pendingActions.delete(key); }
            if (!!m.is_unread !== ui.su) { ui.u = !!m.is_unread; ui.su = !!m.is_unread; pendingActions.delete(key); }
            // 用客户端值覆盖服务器数据
            m.is_collapsed = ui.c;
            m.is_omitted = ui.o;
            m.is_hidden = ui.h;
            m.is_unread = ui.u;
        } else {
            uiState[key] = {
                c: !!m.is_collapsed, o: !!m.is_omitted, h: !!m.is_hidden, u: !!m.is_unread,
                sc: !!m.is_collapsed, so: !!m.is_omitted, sh: !!m.is_hidden, su: !!m.is_unread
            };
        }
    });
    for (var k in uiState) {
        if (!seen[k] && !(uiState[k] && uiState[k].d)) delete uiState[k];
    }
    // 清理已从 history 消失的 pending 条目
    pendingActions.forEach(function(id) { if (!seen[id]) pendingActions.delete(id); });
}

function handleStateUpdate(data) {
    // 幽灵防退防护
    if (data.state_version !== undefined) {
        if (data.state_version < clientLastStateVersion) {
            console.log("[防退截获] 收到滞后版本的状态，已丢弃");
            return;
        }
        clientLastStateVersion = data.state_version;
    }
    
    pendingState = data;
    if (isRenderScheduled) return;
    
    isRenderScheduled = true;
    // 自适应节流：根据距上次渲染的时间间隔决定立即渲染还是延迟渲染
    const now = performance.now();
    const elapsed = now - (window._lastRenderTime || 0);
    const MIN_INTERVAL = 300; // 最低渲染间隔 300ms，防止多流式气泡同时更新时淹没主线程
    
    if (elapsed >= MIN_INTERVAL) {
        requestAnimationFrame(() => {
            window._lastRenderTime = performance.now();
            const stateToProcess = pendingState;
            pendingState = null;
            isRenderScheduled = false;
            if(stateToProcess) _doHandleStateUpdate(stateToProcess);
        });
    } else {
        setTimeout(() => {
            window._lastRenderTime = performance.now();
            const stateToProcess = pendingState;
            pendingState = null;
            isRenderScheduled = false;
            if(stateToProcess) _doHandleStateUpdate(stateToProcess);
        }, MIN_INTERVAL - elapsed);
    }
}

function _doHandleStateUpdate(data) {
    console.log("handleStateUpdate triggered");
    try {
                if (!window.localSid && data.current_session_id && data.current_session_id !== 'starred_session_virtual') {
                    window.localSid = data.current_session_id;
                    window.history.replaceState({}, '', `/?sid=${window.localSid}`);
                }
                let activeSid = window.localSid;
                if (data.current_session_id === 'starred_session_virtual') {
                    activeSid = 'starred_session_virtual';
                }
                // 检测会话切换：清除冷却期和chatHash，确保新会话数据能渲染
                if (window.currentSessionId && window.currentSessionId !== activeSid) {
                    uiState = {};
                    lastChatHash = '__force__';
                    bubbleCache = {};
                    window._expandStateMap = {};
                    // Record presence for kanban waterfall
                    if (typeof _kanbanRecordEnter === 'function') _kanbanRecordEnter(activeSid);
                }
                window.currentSessionId = activeSid;

                // 容错机制：如果本地锁定的会话已经被物理删除，自动回退到后端的默认焦点
                if (!data.sessions[activeSid] && activeSid !== 'starred_session_virtual') {
                    activeSid = data.current_session_id;
                    window.localSid = activeSid;
                    window.history.replaceState({}, '', `/?sid=${activeSid}`);
                    window.hasLoadedFullHistory = false;
                }

                // 同步会话组数据
                if (data.session_groups) sessionGroups = data.session_groups;

                const sessionsMeta = {};
                Object.keys(data.sessions).forEach(k => { sessionsMeta[k] = {name: data.sessions[k].name, order: data.sessions[k].order, sd: data.sessions[k].soft_deleted || false}; });
                const sessionsHash = JSON.stringify(sessionsMeta) + activeSid + JSON.stringify(sessionGroups);
                
                if (sessionsHash !== lastSessionsHash) {
                    lastSessionsHash = sessionsHash;
                    renderSessions(data.sessions, activeSid);
                    if (globalSettings.enable_bottom_tabs && typeof renderBottomTabs === 'function') {
                        _ensureTabOpen(activeSid);
                        renderBottomTabs();
                    }
                }
                
                let currentSession = data.sessions[activeSid];
                
                // 解决多窗口竞争：由于后端出于优化仅广播焦点和活跃会话全量，这里实现主动按需补水机制
                if (currentSession && !currentSession.conversation_history && activeSid !== 'starred_session_virtual') {
                    // Optimistic: render from cache while fetching fresh data
                    if (typeof _sessionHistoryCache !== 'undefined' && _sessionHistoryCache[activeSid]) {
                        var _cachedHist = _sessionHistoryCache[activeSid];
                        _applyUiState(_cachedHist);
                        lastChatHash = '__force__';
                        bubbleCache = {};
                        renderChat(_cachedHist);
                    }
                    if (!window.hasLoadedFullHistory || document.querySelectorAll('.waiting-time').length > 0) {
                        fetch('/api/action', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({action: 'get_session_data', sid: activeSid})
                        }).then(r => r.json()).then(res => {
                            if (res.status === 'ok' && res.session_data) {
                                data.sessions[activeSid] = res.session_data;
                                handleStateUpdate(data);
                            }
                        }).catch(e => console.error("按需补水失败:", e));
                    }
                    return;
                } else if (currentSession && currentSession.conversation_history) {
                    window.hasLoadedFullHistory = true;
                }

                if (currentSession) {
                    // 接力棒代际全局变量：供 chat.js 渲染 baton-marker 使用
                    window._autopilotGen = currentSession._autopilot_gen || 0;
                    window._autopilotActive = currentSession.autopilot_active || false;

                    // 将客户端UI状态应用到服务器数据（stale推送的旧UI值被忽略，服务器主动变更被接受）
                    if (currentSession.conversation_history) {
                        _applyUiState(currentSession.conversation_history);
                    }
                    document.title = currentSession.name || "AI 助手";
                    // Themed sessions keep their hue, but the default branch must
                    // reference the surface token: a hardcoded #f5f5f5 greys out the
                    // now-white chat area and would read as a glaring white block
                    // once the dark scheme is active.
                    if (currentSession._theme_hue != null) {
                        // A tint of the surface token, not a fixed lightness. The
                        // old 97% was a light-scheme constant and rendered a
                        // near-white chat area under the dark scheme — worse than
                        // no tint, because it hits only the sessions that have a
                        // hue and reads as those sessions being broken.
                        chatContainer.style.background = 'color-mix(in srgb, hsl('
                            + currentSession._theme_hue
                            + ' 60% 50%) 7%, var(--md-sys-color-surface))';
                    } else {
                        chatContainer.style.background = 'var(--md-sys-color-surface)';
                    }
                    if (_lastRestoredSid !== activeSid) {
                        _lastRestoredSid = activeSid;
                        deepThinkLevel = currentSession._deep_think_level || 0;
                        document.getElementById('deep-think-label').textContent = ['\u601d\u8003\u4e00','\u601d\u8003\u4e8c','\u601d\u8003\u4e09'][deepThinkLevel];
                        document.querySelectorAll('.model-cb').forEach(function(cb) { cb.checked = false; });
                        (currentSession._selected_models || []).forEach(function(m) {
                            var cb = document.querySelector('.model-cb[value="' + m + '"]');
                            if (cb) cb.checked = true;
                        });
                        updateSendButtonCount();
                        userInput.value = currentSession._draft_text || '';
                        userInput.style.height = 'auto';
                        userInput.style.height = Math.min(userInput.scrollHeight, 150) + 'px';
                    }
                    // Restore cache segmentation state from session data (survives page refresh)
                    if (currentSession._last_sent_max_id) window._prefixLockMaxId = currentSession._last_sent_max_id;
                    if (currentSession._cache_segment_count) window._cacheSegments = currentSession._cache_segment_count;
                    thinkingVisible = currentSession.thinking_visible !== false;
                    const baseCfg = currentSession.code_config || {};
                    const sessionCodeConfig = {
                        paths: baseCfg.paths || [],
                        extensions: baseCfg.extensions || ['.py', '.html', '.json', '.md', '.ipynb', '.yaml', '.yml', '.tex', '.bib', '.txt'],
                        selected_files: baseCfg.selected_files || [],
                        custom_extensions: baseCfg.custom_extensions || '',
                        tree_limit_k: baseCfg.tree_limit_k !== undefined ? baseCfg.tree_limit_k : 10
                    };
                    const codeConfigHash = JSON.stringify(sessionCodeConfig);
                    if (codeConfigHash !== lastCodeConfigHash) {
                        lastCodeConfigHash = codeConfigHash;
                        codeConfig = sessionCodeConfig;
                        refreshCodeContextStats();
                    }
                    
                    // 关键修复：必须先更新 AVAILABLE_MODELS 再调用 renderModelDropdown
                    if (data.available_models && data.available_models.length > 0) {
                        AVAILABLE_MODELS = data.available_models;
    window.MODEL_PROVIDERS = data.model_providers || {};
                    }
                    var _curModelsHash = JSON.stringify(AVAILABLE_MODELS);
                    const statsHash = JSON.stringify(data.model_stats || {});
                    if (statsHash !== lastModelStatsHash || _curModelsHash !== lastModelsHash) {
                        lastModelsHash = _curModelsHash;
                        globalModelStats = data.model_stats || {};
                        lastModelStatsHash = statsHash;
                        renderModelDropdown();
                        // Re-apply selected models after dropdown is re-rendered (fixes race condition on first load)
                        if (currentSession && currentSession._selected_models && currentSession._selected_models.length > 0) {
                            currentSession._selected_models.forEach(function(m) {
                                var _rcb = document.querySelector('.model-cb[value="' + m + '"]');
                                if (_rcb) _rcb.checked = true;
                            });
                            updateSendButtonCount();
                        }
                    }
                    if (data.spending) {
                        var spendEl = document.getElementById('spending-display');
                        if (spendEl) {
                                                    var sessionTotal = (data.spending.total || 0).toFixed(2);
                            spendEl.textContent = '$' + sessionTotal;
                            spendEl.title = '本会话累积费用';
                        }
                    }
                    if (data.global_settings) {
                        const settingsHash = JSON.stringify(data.global_settings);
                        if (settingsHash !== lastSettingsHash) {
                            lastSettingsHash = settingsHash;
                            globalSettings = data.global_settings;
                            applySettingsUI();
                            // 同步计费按钮可见性：仅在 developer_mode + enable_billing_sync_button 时显示
                            var _bsBtn = document.getElementById('billing-sync-btn');
                            if (_bsBtn) {
                                _bsBtn.style.display = (globalSettings.developer_mode && globalSettings.enable_billing_sync_button) ? '' : 'none';
                            }
                            var _bsCb = document.getElementById('set-billing-sync-button');
                            if (_bsCb) {
                                _bsCb.checked = !!globalSettings.enable_billing_sync_button;
                            }
                        }
                    }
                    
                    /* Folded rather than joined. The old form built one string over
                       the entire history — two 30-character slices per message plus a
                       join that reaches hundreds of kilobytes on a long conversation —
                       purely to compare it against the previous value, and it ran on
                       every state push whether or not renderChat followed.
                       
                       Reuses the accumulators chat.js defines; chat.js loads first and
                       they are plain globals. Field coverage is a strict superset of
                       the old expression, and content is folded in full rather than
                       sampled at the edges — sampling here would undo the guarantee
                       the per-message fingerprint now provides, since a mid-body edit
                       would leave this hash unchanged and renderChat would never run. */
                    _fpReset();
                    var _chHist = currentSession.conversation_history;
                    for (var _chi = 0; _chi < _chHist.length; _chi++) {
                        var _chm = _chHist[_chi];
                        _fpNum(_chm.id);
                        _fpText(_chm.role);
                        _fpText(_chm.content);
                        _fpText(_chm.summary);
                        _fpFlag(_chm.is_omitted);
                        _fpFlag(_chm.is_collapsed);
                        _fpFlag(_chm.is_hidden);
                        _fpFlag(_chm.is_unread);
                        _fpText(_chm.rating);
                        _fpText(_chm.model_name);
                        _fpText(_chm.term_state);
                        _fpNum(_chm.diff_content ? _chm.diff_content.length : 0);
                        if (_chm.content_parts) {
                            for (var _chp = 0; _chp < _chm.content_parts.length; _chp++) {
                                var _chpp = _chm.content_parts[_chp];
                                _fpText(_chpp.type);
                                _fpText(_chpp.status);
                                _fpText(_chpp.content);
                            }
                        } else { _fpNum(0); }
                        _fpNum(_chm.multimodal_blocks ? _chm.multimodal_blocks.length : 0);
                        _fpFlag(_chm._dehydrated);
                    }
                    _fpNum(currentSession._autopilot_gen || 0);
                    _fpFlag(currentSession.autopilot_active);
                    const chatHash = _fpValue();
        if (chatHash !== lastChatHash) {
                console.time('Total Render Cycle');
                lastChatHash = chatHash;
                renderChat(currentSession.conversation_history);
                console.timeEnd('Total Render Cycle');
                // 渲染后恢复 pending 状态：被无关推送触发的 renderChat 会替换 DOM，需要重新标记
                if (pendingActions.size > 0) {
                    pendingActions.forEach(function(msgId) {
                        var _pb = document.getElementById('msg-bubble-' + msgId);
                        if (_pb) { _pb.style.opacity = '0.5'; _pb.style.pointerEvents = 'none'; }
                    });
                }
                // Streaming restore: if renderChat just reverted a streaming bubble to stale state data,
                // restore it to the latest streaming_content to prevent visible content regression
                if (_lastStreamingBubbleId && (Date.now() - _lastStreamingTime < 5000) && _lastStreamingContent) {
                    var _srBubble = document.getElementById('msg-bubble-' + _lastStreamingBubbleId);
                    if (_srBubble) {
                        var _srDiv = _srBubble.querySelector('.content');
                        if (_srDiv) {
                            _srDiv.innerHTML = renderMarkdownProtected(filterProtocolMarkers(_lastStreamingContent));
                            // Same guard as _paintStreaming: this branch fires once per
                            // renderChat during a stream and had the same unconditional scan.
                            if (_lastStreamingContent.indexOf('$') >= 0) {
                                try { renderMathInElement(_srDiv, { delimiters: [{left: "$$", right: "$$", display: true}, {left: "$", right: "$", display: false}] }); } catch(e) {}
                            }
                        }
                    }
                }
                // Truncation notice: warn user when assistant bubble has empty main content
                document.querySelectorAll('.message-bubble.assistant').forEach(function(_tb) {
                    if (_tb.querySelector('.truncation-notice')) return;
                    if (_tb.querySelector('.waiting-time')) return;
                    if (_tb.querySelector('.summary-box')) return;
                    var _tmc = _tb.querySelector('.bubble-main-content');
                    if (!_tmc) return;
                    if (_tmc.querySelector('.code-block-wrapper')) return;
                    if (_tmc.textContent.trim().length > 0) return;
                    var _tn = document.createElement('div');
                    _tn.className = 'truncation-notice';
                    // No cssText here: the class already supplies the warning
                    // container pair, and an inline rule would override it.
                    _tn.innerHTML = mdIcon('warning', 16) + ' \u672C\u6B21\u56DE\u590D\u6B63\u6587\u4E3A\u7A7A\uFF0C\u8F93\u51FA\u53EF\u80FD\u88AB\u622A\u65AD\u3002';
                    _tmc.appendChild(_tn);
                });
                // 重建缓存前缀预估显示（renderChat 会清空 chat-container 销毁该元素）
                if (typeof _updateCachePrediction === 'function') _updateCachePrediction();
        }
                    // Cache history for instant tab switching
                    if (typeof _sessionHistoryCache !== 'undefined') {
                        _sessionHistoryCache[activeSid] = currentSession.conversation_history;
                    }
                    const queueHash = currentSession.message_queue.map(q => q.id).join('|') + currentSession.is_paused;
                    if (queueHash !== lastQueueHash) {
                        lastQueueHash = queueHash;
                        renderQueue(currentSession.message_queue, currentSession.is_paused);
                    }
                    setUIEnabled(!currentSession.is_processing);
                    
                    const apBtn = document.getElementById('autopilot-btn');
                    if (apBtn) {
                        const apActive = currentSession.autopilot_active;
                        if (apActive) {
                            apBtn.innerText = `停止托管 (${currentSession.autopilot_turns_left || 0})`;
                            apBtn.style.background = 'var(--md-sys-color-error-container)';
                            apBtn.style.color = 'var(--md-sys-color-on-error-container)';
                        } else {
                            apBtn.innerText = '启动托管';
                            apBtn.style.background = 'var(--md-sys-color-warning-container)';
                            apBtn.style.color = 'var(--md-sys-color-on-warning-container)';
                        }
                        apBtn.disabled = false;
                    }

                    // 托管活跃时隐藏发送按钮，托管结束（按钮为黄色/灰色）时恢复
                    if (sendButton) {
                        sendButton.style.display = (globalSettings.enable_autopilot && currentSession.autopilot_active) ? 'none' : '';
                    }

                    // CC 绑定状态：仅 CC 直连模式下显示 Link 按钮
                    var linkCcBtn = document.getElementById('link-cc-btn');
                    if (linkCcBtn) {
                        var _showLinkBtn = globalSettings.enable_tool_inject && !globalSettings.enable_tool_simulate;
                        linkCcBtn.style.display = _showLinkBtn ? '' : 'none';
                        if (_showLinkBtn) {
                            if (currentSession.bound_cc_id) {
                                linkCcBtn.style.background = 'var(--md-sys-color-tertiary-container)';
                                linkCcBtn.style.color = 'var(--md-sys-color-on-tertiary-container)';
                            } else {
                                linkCcBtn.style.background = 'var(--md-sys-color-success-container)';
                                linkCcBtn.style.color = 'var(--md-sys-color-on-success-container)';
                            }
                        }
                    }

                    if (typeof _kanbanActive === 'undefined' || !_kanbanActive) {
                        document.getElementById('input-area').style.display = (data.current_session_id === 'starred_session_virtual') ? 'none' : 'block';
                    }
                }
            } catch (e) {
                console.error("处理WebSocket状态更新时出错:", e);
            }
        }

        socket.on('state_update', handleStateUpdate);

        // Fallback: if no initial state received within 2s of page load, request explicitly
        // Fixes white screen on refresh for long sessions when initial WebSocket push is missed
        setTimeout(function() {
            if (lastChatHash === '__force__') {
                postAction({action: 'ping'});
            }
        }, 2000);

        // renameSession -> /libs/actions.js

        // 会话组相关全局变量
        let sessionGroups = {};

        /** Keep exactly one insert indicator visible while the pointer sweeps rows. */
        function _markSessionDrop(el, after) {
            document.querySelectorAll('#session-list .sess-drop-before, #session-list .sess-drop-after')
                .forEach(function(n) { if (n !== el) n.classList.remove('sess-drop-before', 'sess-drop-after'); });
            el.classList.toggle('sess-drop-before', !after);
            el.classList.toggle('sess-drop-after', after);
        }

        function _clearSessionDropMarks() {
            document.querySelectorAll('#session-list .session-item').forEach(function(n) {
                n.classList.remove('sess-dragging', 'sess-drop-before', 'sess-drop-after');
            });
            document.querySelectorAll('#session-list .session-group').forEach(function(n) { n.style.outline = 'none'; });
        }

        function _rowId(el) { return el.dataset.sid || el.dataset.gid; }

        function _siblingRows(el, sel) {
            return Array.prototype.slice.call(
                el.parentElement.querySelectorAll(':scope > ' + sel));
        }

        /**
         * Work out and dispatch the new order for a dragged row.
         *
         * The midpoint between the drop target and its neighbour is used when it
         * actually lands strictly between the two. It often does not: sessions
         * created in a batch share one default order, and repeated drags halve the
         * gap until it hits float precision. In both cases the midpoint equals a
         * value already in use, the backend writes a no-op, sessionsHash is
         * unchanged and the drag looks like it did nothing at all.
         *
         * The fallback renumbers the whole container 1..N in its desired final
         * sequence, which needs no assumptions about the existing values, and only
         * posts for rows whose order actually changes.
         *
         * @param {Element} targetEl row the pointer was released over
         * @param {string} draggedId sid or gid being moved (may be from elsewhere)
         * @param {boolean} after true when dropped on the lower half of the target
         * @param {string} sel child selector identifying sibling rows
         * @param {function(string, number)} post receives (id, order) to dispatch
         */
        function _dispatchReorder(targetEl, draggedId, after, sel, post) {
            var rows = _siblingRows(targetEl, sel);
            var ids = rows.map(_rowId);
            var ord = {};
            rows.forEach(function(r) { ord[_rowId(r)] = parseFloat(r.dataset.order) || 0; });
            var targetId = _rowId(targetEl);
            var ti = ids.indexOf(targetId);
            if (ti < 0) return;

            // Skip past the dragged row itself: measuring a gap against the element
            // being moved is meaningless.
            var step = after ? 1 : -1;
            var nbId = ids[ti + step];
            if (nbId === draggedId) nbId = ids[ti + step * 2];

            var t = ord[targetId];
            if (nbId === undefined) { post(draggedId, after ? t + 1 : t - 1); return; }
            var mid = (t + ord[nbId]) / 2;
            if (mid !== t && mid !== ord[nbId]) { post(draggedId, mid); return; }

            var seq = ids.filter(function(id) { return id !== draggedId; });
            var at = seq.indexOf(targetId);
            seq.splice(after ? at + 1 : at, 0, draggedId);
            seq.forEach(function(id, i) { if (ord[id] !== i + 1) post(id, i + 1); });
        }

        /**
         * FLIP reorder animation. offsetTop rather than getBoundingClientRect:
         * grouped and ungrouped rows both resolve their offsetParent to #sidebar
         * (the only positioned ancestor), and offsetTop is immune to scroll, so the
         * measurement stays comparable across a full list rebuild.
         */
        function _captureSessionTops() {
            var m = {};
            document.querySelectorAll('#session-list .session-item').forEach(function(el) {
                if (el.dataset.sid) m[el.dataset.sid] = el.offsetTop;
            });
            return m;
        }

        function _playSessionFlip(prev) {
            if (!prev) return;
            document.querySelectorAll('#session-list .session-item').forEach(function(el) {
                var sid = el.dataset.sid;
                if (!sid || prev[sid] === undefined) return;
                var delta = prev[sid] - el.offsetTop;
                if (!delta) return;
                el.style.transition = 'none';
                el.style.transform = 'translateY(' + delta + 'px)';
                requestAnimationFrame(function() {
                    el.style.transition = 'transform var(--md-sys-motion-duration-medium1) var(--md-sys-motion-easing-emphasized)';
                    el.style.transform = '';
                    el.addEventListener('transitionend', function _done() {
                        el.style.transition = '';
                        el.removeEventListener('transitionend', _done);
                    });
                });
            });
        }

        function _createSessionItem(sid, s, currentId, inGroup) {
            const div = document.createElement('div');
            div.className = 'session-item' + (sid === currentId ? ' active' : '');
            div.draggable = true;
            div.dataset.sid = sid;
            div.dataset.order = s.order || 0;
            // Right-click opens the same menu the overflow button does, by calling
            // the same function rather than declaring a second copy of the entries.
            // openSessionMenu opens with preventDefault/stopPropagation and locates
            // itself from clientX/clientY, all of which a contextmenu event carries,
            // so the two entry points cannot drift apart.
            div.oncontextmenu = (e) => { openSessionMenu(e, sid); };
            div.ondragstart = (e) => {
                e.dataTransfer.setData('text/plain', JSON.stringify({type: 'session', sid: sid}));
                e.dataTransfer.effectAllowed = 'move';
                // Deferred one frame on purpose: the browser snapshots the element at
                // the end of the synchronous dragstart phase to build the drag image,
                // so dimming it now would dim the ghost too and show two faint copies.
                requestAnimationFrame(function() { div.classList.add('sess-dragging'); });
            };
            div.ondragend = () => { _clearSessionDropMarks(); };
            div.ondragover = (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (div.classList.contains('sess-dragging')) return;
                var r = div.getBoundingClientRect();
                _markSessionDrop(div, (e.clientY - r.top) > r.height / 2);
            };
            // dragleave bubbles up from children, so without the relatedTarget test
            // moving onto the row's own title would clear the indicator and flicker.
            div.ondragleave = (e) => {
                if (!div.contains(e.relatedTarget)) {
                    div.classList.remove('sess-drop-before', 'sess-drop-after');
                }
            };
            div.ondrop = (e) => {
                e.preventDefault(); e.stopPropagation();
                _clearSessionDropMarks();
                try {
                    var payload = JSON.parse(e.dataTransfer.getData('text/plain'));
                    if (payload.type === 'session' && payload.sid !== sid) {
                        // 判断被拖拽的会话当前是否在某个组内
                        var draggedInGroup = null;
                        Object.keys(sessionGroups).forEach(function(gid) {
                            if ((sessionGroups[gid].session_ids || []).indexOf(payload.sid) >= 0) draggedInGroup = gid;
                        });
                        // 判断目标会话所在的组
                        var targetInGroup = null;
                        Object.keys(sessionGroups).forEach(function(gid) {
                            if ((sessionGroups[gid].session_ids || []).indexOf(sid) >= 0) targetInGroup = gid;
                        });
                        if (inGroup && targetInGroup && !draggedInGroup) {
                            // 拖到组内会话上 = 加入该组
                            postAction({action: 'add_session_to_group', group_id: targetInGroup, sid: payload.sid});
                        } else if (!inGroup && draggedInGroup) {
                            // 拖到未分组区域 = 移出组
                            postAction({action: 'remove_session_from_group', group_id: draggedInGroup, sid: payload.sid});
                        } else if (inGroup && targetInGroup && draggedInGroup && draggedInGroup !== targetInGroup) {
                            // 从一个组拖到另一个组 = 移入新组
                            postAction({action: 'add_session_to_group', group_id: targetInGroup, sid: payload.sid});
                        }
                        // 排序：中点可用则用中点，否则整段重新编号，见 _dispatchReorder
                        var _r = e.currentTarget.getBoundingClientRect();
                        var isAfter = (e.clientY - _r.top) > _r.height / 2;
                        _dispatchReorder(e.currentTarget, payload.sid, isAfter, '.session-item', function(_sid, _ord) {
                            postAction({action: 'reorder_session', sid: _sid, new_order: _ord});
                        });
                    }
                } catch(ex) {}
            };
            // Four inline buttons used to crowd the row until the title had room
            // for two characters. They now live behind a single overflow menu,
            // and both affordances stay transparent until hover.
            div.innerHTML = '<span class="session-drag" title="拖拽排序">' + mdIcon('drag_indicator', 14) + '</span>' +
                '<a href="/?sid=' + sid + '" class="session-title" draggable="false" onclick="event.preventDefault();var _o=window.localSid;document.querySelectorAll(\'.session-item.active\').forEach(function(el){el.classList.remove(\'active\')});this.closest(\'.session-item\').classList.add(\'active\');window.localSid=\'' + sid + '\';window.history.pushState({},\'\',\'/?' + 'sid=' + sid + '\');window.hasLoadedFullHistory=false;postAction({action:\'switch_session\',sid:\'' + sid + '\',_outgoing_sid:_o});" title="' + (s.name || '') + '">' + (s.name || '') + '</a>' +
                '<button class="session-btn session-more" onclick="openSessionMenu(event, \'' + sid + '\')" title="更多操作">' + mdIcon('more_vert', 16) + '</button>';
            return div;
        }

        async function renameGroup(gid, oldName) {
            var n = await showPromptModal('请输入新的组名：', oldName);
            if (n && n.trim()) postAction({action: 'update_session_group', group_id: gid, name: n.trim()});
        }
        async function deleteGroup(gid) {
            var group = sessionGroups[gid];
            var count = (group && group.session_ids) ? group.session_ids.length : 0;
            var choice = await showPromptModal('删除分组「' + (group ? group.name : '') + '」(' + count + ' 个会话)\n\n输入 1 = 仅解散分组（会话保留）\n输入 2 = 删除分组及其所有会话\n\n请输入选择：', '');
            if (choice === '1') {
                postAction({action: 'delete_session_group', group_id: gid});
            } else if (choice === '2') {
                        // 先删除所有会话，再删除组
                    (group.session_ids || []).forEach(function(sid) { postAction({action: 'delete_session', sid: sid}); });
                    postAction({action: 'delete_session_group', group_id: gid});
            }
        }
        async function createGroup() {
            var n = await showPromptModal('请输入分组名称：', '新分组');
            if (n && n.trim()) postAction({action: 'create_session_group', name: n.trim()});
        }
        async function addSessionToGroup(gid) {
            try {
                var res = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'create_session'})});
                var data = await res.json();
                if (data.new_sid) {
                    postAction({action: 'add_session_to_group', group_id: gid, sid: data.new_sid});
                    window.localSid = data.new_sid;
                    window.history.pushState({}, '', '/?sid=' + data.new_sid);
                    window.hasLoadedFullHistory = false;
                    postAction({action: 'switch_session', sid: data.new_sid});
                }
            } catch(e) { showToast('创建失败: ' + e, 'error'); }
        }


        // Overflow menu for a session row. Reuses .sm-context-menu / .sm-ctx-item
        // so all four menus in the app share one appearance, and delegates clicks
        // instead of inlining a handler per entry.
        function openSessionMenu(e, sid) {
            e.preventDefault();
            e.stopPropagation();
            var old = document.getElementById('session-item-menu');
            if (old) old.remove();
            var sessMap = window._lastSessionsMap || {};
            var name = (sessMap[sid] && sessMap[sid].name) || '';
            var menu = document.createElement('div');
            menu.id = 'session-item-menu';
            menu.className = 'sm-context-menu';
            menu.innerHTML =
                '<div class="sm-ctx-item" data-act="rename">' + mdIcon('edit', 16) + ' 重命名</div>' +
                '<div class="sm-ctx-item" data-act="clone">' + mdIcon('content_copy', 16) + ' 复制会话</div>' +
                '<div class="sm-ctx-item" data-act="archive">' + mdIcon('archive', 16) + ' 归档</div>' +
                '<div class="sm-ctx-item sm-ctx-danger" data-act="delete">' + mdIcon('delete', 16) + ' 删除</div>';
            document.body.appendChild(menu);
            // Measure after insertion so the viewport clamp uses real dimensions.
            var r = menu.getBoundingClientRect();
            menu.style.left = Math.max(4, Math.min(e.clientX, window.innerWidth - r.width - 8)) + 'px';
            menu.style.top = Math.max(4, Math.min(e.clientY, window.innerHeight - r.height - 8)) + 'px';
            menu.onclick = function(ev) {
                var item = ev.target.closest && ev.target.closest('.sm-ctx-item');
                if (!item) return;
                var act = item.dataset.act;
                menu.remove();
                if (act === 'rename') renameSession(sid, name);
                else if (act === 'clone') postAction({action: 'duplicate_session', sid: sid});
                else if (act === 'archive') postAction({action: 'archive_session', sid: sid});
                else if (act === 'delete') postAction({action: 'delete_session', sid: sid});
            };
            setTimeout(function() {
                document.addEventListener('click', function _close(ev2) {
                    var m = document.getElementById('session-item-menu');
                    if (m && !m.contains(ev2.target)) m.remove();
                    document.removeEventListener('click', _close);
                });
            }, 10);
        }

        // Sidebar width: drag the right edge, persisted across reloads.
        (function() {
            // Document-level dragend fallback. If the drop's re-render finishes before
            // dragend dispatches, the source row is already gone with innerHTML and its
            // own handler never fires, leaving sess-dragging stuck on whichever row
            // takes that id next — visibly a session dimmed forever.
            document.addEventListener('dragend', function() { _clearSessionDropMarks(); });

            var SB_MIN = 180, SB_MAX = 520;
            var saved = parseInt(localStorage.getItem('sidebar_width') || '', 10);
            if (saved >= SB_MIN && saved <= SB_MAX) {
                document.documentElement.style.setProperty('--sidebar-width', saved + 'px');
            }
            var handle = document.getElementById('sidebar-resize');
            var sb = document.getElementById('sidebar');
            if (!handle || !sb) return;
            var dragging = false;
            handle.addEventListener('mousedown', function(e) {
                e.preventDefault();
                dragging = true;
                sb.classList.add('sb-resizing');
                document.body.style.userSelect = 'none';
                document.body.style.cursor = 'col-resize';
            });
            document.addEventListener('mousemove', function(e) {
                if (!dragging) return;
                var w = Math.max(SB_MIN, Math.min(SB_MAX, e.clientX));
                document.documentElement.style.setProperty('--sidebar-width', w + 'px');
            });
            document.addEventListener('mouseup', function() {
                if (!dragging) return;
                dragging = false;
                sb.classList.remove('sb-resizing');
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
                var cur = parseInt(getComputedStyle(document.documentElement)
                    .getPropertyValue('--sidebar-width'), 10);
                if (cur) localStorage.setItem('sidebar_width', cur);
                // Tick positions no longer depend on bubble heights, but the
                // rail is sized from the viewport and the scroll button from the
                // measured composer height, so both still need recomputing.
                if (typeof renderMinimap === 'function') renderMinimap();
            });
        })();

        function renderSessions(sessionsMap, currentId) {
            window._lastSessionsMap = sessionsMap; // 保存引用供乐观更新使用
            const list = document.getElementById('session-list');
            // Capture before the rebuild for the FLIP pass at the end. scrollTop is
            // saved for two reasons: clearing innerHTML collapses the container and
            // the browser resets scroll to 0 (so far every state push has scrolled
            // the sidebar back to the top), and FLIP is only correct if the scroll
            // offset is unchanged between measurements.
            const _flipFrom = _captureSessionTops();
            const _savedScroll = list.scrollTop;
            list.innerHTML = '';
            // 整个列表作为拖放目标：会话拖到空白区域 = 移出分组
            list.ondragover = (e) => { e.preventDefault(); };
            list.ondrop = (e) => {
                // 如果拖到了组容器内部，由组的 ondrop 处理（已 stopPropagation）
                // 这里只处理拖到组外面的情况 = 移出分组
                if (e.target.closest('.session-group')) return;
                e.preventDefault();
                try {
                    var payload = JSON.parse(e.dataTransfer.getData('text/plain'));
                    if (payload.type === 'session') {
                        Object.keys(sessionGroups).forEach(function(gid) {
                            var sids = sessionGroups[gid].session_ids || [];
                            if (sids.indexOf(payload.sid) >= 0) {
                                postAction({action: 'remove_session_from_group', group_id: gid, sid: payload.sid});
                            }
                        });
                    }
                } catch(ex) {}
            };
            const sids = Object.keys(sessionsMap)
                .filter(id => id !== 'starred_session_virtual' && !sessionsMap[id].soft_deleted && !sessionsMap[id].is_archived)
                .sort((a, b) => (sessionsMap[a].order || 0) - (sessionsMap[b].order || 0));
            // Update archived shelf if visible
            if (document.getElementById('archived-shelf') && document.getElementById('archived-shelf').style.display !== 'none') {
                renderArchivedList();
            }
            // Same treatment: deleting a session is exactly the moment a user is
            // most likely to be looking at the bin, and a stale list there would
            // suggest the delete did not take.
            var _tsh = document.getElementById('trash-shelf');
            if (_tsh && _tsh.style.display !== 'none') renderTrashList();

            // 构建组内会话集合
            const groupedSids = new Set();
            const groups = Object.entries(sessionGroups).sort((a, b) => (a[1].order || 0) - (b[1].order || 0));
            groups.forEach(([gid, g]) => (g.session_ids || []).forEach(s => groupedSids.add(s)));

            // 渲染组（组本身也是可拖拽的，且是会话的拖放目标）
            groups.forEach(([gid, group]) => {
                const gDiv = document.createElement('div');
                gDiv.className = 'session-group';
                gDiv.dataset.gid = gid;
                gDiv.dataset.order = group.order || 0;
                gDiv.draggable = true;
                gDiv.style.cssText = 'margin: 2px 0 6px;';
                // 组自身可拖拽排序
                gDiv.ondragstart = (e) => { e.dataTransfer.setData('text/plain', JSON.stringify({type:'group',gid:gid})); gDiv.style.opacity = '0.5'; };
                gDiv.ondragend = (e) => { gDiv.style.opacity = '1'; };
                // 组作为拖放目标：会话拖入组 / 组排序。用 outline 而非 border 表达
                // 可放置状态：outline 不参与布局，不会在拖拽时挤动组内条目。
                gDiv.ondragover = (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; gDiv.style.outline = '2px solid var(--md-sys-color-primary)'; };
                // relatedTarget test for the same reason as the row handler: dragleave
                // bubbles from children, so moving onto a row inside the group would
                // otherwise strobe the outline off and on.
                gDiv.ondragleave = (e) => { if (!gDiv.contains(e.relatedTarget)) gDiv.style.outline = 'none'; };
                gDiv.ondrop = (e) => {
                    e.preventDefault(); e.stopPropagation(); gDiv.style.outline = 'none';
                    try {
                        var payload = JSON.parse(e.dataTransfer.getData('text/plain'));
                        if (payload.type === 'session') {
                            postAction({action: 'add_session_to_group', group_id: gid, sid: payload.sid});
                        } else if (payload.type === 'group' && payload.gid !== gid) {
                            // 组排序：根据拖放位置计算新 order
                            var rect = gDiv.getBoundingClientRect();
                            var isAfter = (e.clientY - rect.top) > (rect.height / 2);
                            _dispatchReorder(gDiv, payload.gid, isAfter, '.session-group', function(_gid, _ord) {
                                postAction({action: 'update_session_group', group_id: _gid, order: _ord});
                            });
                        }
                    } catch(ex) {}
                };
                const gHeader = document.createElement('div');
                gHeader.className = 'session-group-header';
                gHeader.innerHTML = '<span class="session-drag" style="opacity:1;">' + (group.collapsed ? mdIcon('chevron_right', 14) : mdIcon('expand_more', 14)) + '</span>' +
                    '<span class="session-group-title">' + (group.name || '未命名组') + ' (' + (group.session_ids || []).length + ')</span>' +
                    '<button class="session-btn" onclick="event.stopPropagation(); addSessionToGroup(\'' + gid + '\')" title="新建会话到此组">' + mdIcon('add', 14) + '</button>' +
                    '<button class="session-btn" onclick="event.stopPropagation(); openWaterfall(\'' + gid + '\')" title="瀑布流监控">' + mdIcon('bar_chart', 14) + '</button>' +
                    '<button class="session-btn" onclick="event.stopPropagation(); renameGroup(\'' + gid + '\', \'' + (group.name || '').replace(/'/g, "\\'") + '\')" title="重命名">' + mdIcon('edit', 14) + '</button>' +
                    '<button class="session-btn" onclick="event.stopPropagation(); deleteGroup(\'' + gid + '\')" title="删除组">' + mdIcon('delete', 14) + '</button>';
                gHeader.onclick = function() {
                    sessionGroups[gid].collapsed = !sessionGroups[gid].collapsed;
                    lastSessionsHash = ''; // 强制重渲染
                    renderSessions(window._lastSessionsMap || {}, window.currentSessionId);
                    postAction({action: 'update_session_group', group_id: gid, collapsed: sessionGroups[gid].collapsed});
                };
                gDiv.appendChild(gHeader);
                if (!group.collapsed) {
                    const gBody = document.createElement('div');
                    gBody.style.cssText = 'padding: 0 0 var(--md-sys-spacing-1);';
                    // Sorted by the same order field the ungrouped branch uses. Walking
                    // session_ids in array order was why reorder_session looked like a
                    // no-op inside a group: the backend did update order, but this loop
                    // discarded it and re-rendered the original sequence.
                    (group.session_ids || [])
                        .filter(function(gs) { return sessionsMap[gs] && !sessionsMap[gs].soft_deleted; })
                        .sort(function(a, b) { return (sessionsMap[a].order || 0) - (sessionsMap[b].order || 0); })
                        .forEach(function(gs) {
                            gBody.appendChild(_createSessionItem(gs, sessionsMap[gs], currentId, true));
                        });
                    gDiv.appendChild(gBody);
                }
                list.appendChild(gDiv);
            });

            // 渲染未分组会话
            const ungrouped = sids.filter(s => !groupedSids.has(s));
            ungrouped.forEach(sid => {
                list.appendChild(_createSessionItem(sid, sessionsMap[sid], currentId, false));
            });
            
            if (sessionsMap['starred_session_virtual'] && globalSettings.enable_starred) {
                const div = document.createElement('div');
                div.className = 'session-item' + ('starred_session_virtual' === currentId ? ' active' : '');
                div.style.marginTop = 'var(--md-sys-spacing-2)';
                div.style.border = 'none';
                div.style.background = 'var(--md-sys-color-tertiary-container)';
                div.style.color = 'var(--md-sys-color-on-tertiary-container)';
                div.innerHTML = mdIcon('push_pin', 14) +
                    `<a href="#" class="session-title" onclick="event.preventDefault(); postAction({action: 'switch_session', sid: 'starred_session_virtual'})" style="font-weight: 500;">${sessionsMap['starred_session_virtual'].name}</a>`;
                list.appendChild(div);
            }
            // Order matters: restore scroll first, then measure. Otherwise FLIP reads
            // the scroll jump as row movement and slides the entire list at once.
            if (_savedScroll) list.scrollTop = _savedScroll;
            _playSessionFlip(_flipFrom);
            // Hook: 会话管理弹窗打开时自动刷新网格
            if (document.getElementById('session-mgr-body')) renderSessionManagerGrid();
        }

        // send, addOnly, handleImageUpload, toggleAutopilot -> /libs/actions.js

        userInput.onkeydown = (e) => { 
            if(e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send(false);
            }
        };
        let tokenEstDebounce;
        userInput.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 150) + 'px';
            clearTimeout(tokenEstDebounce); 
            tokenEstDebounce = setTimeout(updateTokenEst, 300);
        });

        // renderChat -> /libs/chat.js



        /* Waiting-bubble ticker: on demand, not always on.
         *
         * This used to be an unconditional setInterval(..., 100) running a
         * document-wide querySelectorAll('.waiting-time') ten times a second with
         * nothing waiting on screen.
         *
         * The heartbeat rides along and must not be switched off with it, but the
         * two agree by construction: syncCounter only ever advanced while at least
         * one waiting bubble existed, so the moment the ticker stops is exactly the
         * moment the heartbeat was already meant to be silent.
         *
         * renderChat starts it — the only place .waiting-time elements come from, so
         * the none-to-some direction is covered by the sole producer — and the tick
         * stops itself once the last one is gone. */
        var _waitTimerId = 0;
        let syncCounter = 0;

        /** Idempotent, so every render can call it. */
        function _startWaitTicker() {
            if (_waitTimerId) return;
            _waitTimerId = setInterval(_waitTick, 100);
        }

        function _waitTick() {
            const waitingEls = document.querySelectorAll('.waiting-time');
            if (waitingEls.length === 0) {
                clearInterval(_waitTimerId);
                _waitTimerId = 0;
                syncCounter = 0;
                return;
            }
            waitingEls.forEach(el => {
                const start = parseFloat(el.dataset.start);
                if (start) el.innerText = Math.max(0, (Date.now() / 1000) - start).toFixed(1);
            });

            // 心跳防丢包机制：每 5 秒（50 tick）主动发起一次心跳，强制唤醒状态广播
            syncCounter++;
            if (syncCounter >= 50) {
                syncCounter = 0;
                postAction({action: 'ping'});
                    // 双通道保障：直接通过 HTTP 拉取会话数据，彻底绕过 WebSocket 不可靠性
                    const _pollSid = window.localSid;
                    if (_pollSid && _pollSid !== 'starred_session_virtual') {
                        fetch('/api/action', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({action: 'get_session_data', sid: _pollSid})
                        }).then(r => r.json()).then(res => {
                            if (res.status === 'ok' && res.session_data && res.session_data.conversation_history) {
                                const polledHistory = res.session_data.conversation_history;
                                const hasFilledBubbles = polledHistory.some(m => 
                                    m.role === 'assistant' && m.content && 
                                    document.querySelector('#msg-bubble-' + m.id + ' .waiting-time')
                                );
                                                                if (hasFilledBubbles) {
                                    _applyUiState(polledHistory);
                                    lastChatHash = '__force__';
                                    renderChat(polledHistory);
                                    renderQueue(res.session_data.message_queue || [], res.session_data.is_paused);
                                    setUIEnabled(!res.session_data.is_processing);
                                }
                            }
                        }).catch(() => {});
                    }
            }
        }

        // Edit ops: copyMsg..saveEdit -> /libs/editops.js

        // 手动计费同步
        function manualBillingSync() {
            fetch('/api/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'manual_billing_sync', pages: 5})
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.status === 'ok') {
                    showToast('计费同步完成，匹配 ' + d.matched + ' 条', 'success');
                } else {
                    showToast('计费同步失败: ' + (d.message || '未知错误'), 'error');
                }
            }).catch(function(e) { showToast('计费同步请求失败: ' + e, 'error'); });
        }

        // Code block operations -> /libs/codeblocks.js

        socket.on('copy_to_clipboard', (data) => {
            navigator.clipboard.writeText(data.text).then(() => {
                showToast('已将离线 Payload 复制到剪贴板！请在外部平台运行后复制结果回来，系统将自动识别。', 'success');
                // 激活剪贴板监听，等待用户粘贴回离线响应
                window.offlineClipboardWatch = true;
                // 10分钟后自动关闭监听，避免永久轮询
                clearTimeout(window._offlineWatchTimer);
                window._offlineWatchTimer = setTimeout(() => { window.offlineClipboardWatch = false; }, 600000);
            }).catch(err => {
                showToast('剪贴板写入受限，请点击气泡上的上下文复制按钮: ' + err, 'error');
            });
        });

        // 离线模式剪贴板监听（默认关闭，仅在离线 payload 被复制后激活，避免浏览器持续弹出粘贴权限按钮）
        window.offlineClipboardWatch = false;
        let lastClipboardText = "";
        setInterval(async () => {
            if (!window.offlineClipboardWatch) return;
            if (document.hasFocus() && navigator.clipboard && navigator.clipboard.readText) {
                try {
                    const text = await navigator.clipboard.readText();
                    // 引入跨标签页防重哈希机制，防止多窗口并发拾取相同的离线结果导致重复发包与污染
                    const globalLast = localStorage.getItem('last_clipboard_hash') || "";
                    const textHash = text.length + "_" + text.substring(0, 50) + "_" + text.substring(text.length - 50);

                    if (text && textHash !== globalLast) {
                        localStorage.setItem('last_clipboard_hash', textHash);
                        lastClipboardText = text;
                        if (!text.trim().startsWith('{') && /\[令牌[a-z0-9]+\]/i.test(text)) {
                            postAction({action: 'process_offline_response', text: text});
                            // 成功拾取离线响应后关闭监听
                            window.offlineClipboardWatch = false;
                            clearTimeout(window._offlineWatchTimer);
                        }
                    }
                } catch (e) {
                    // 忽略剪贴板读取被拒或其他权限异常，避免干扰用户
                }
            }
        }, 1000);

        // renderContextVisualization -> /libs/editops.js

        // Subagent: subagentSend, subagentAdopt -> /libs/editops.js

        function openWaterfall(gid) {
            var existing = document.getElementById('waterfall-overlay');
            if (existing) existing.remove();
            var ov = document.createElement('div');
            ov.id = 'waterfall-overlay';
            ov.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;'
                + 'background:color-mix(in srgb, var(--md-sys-color-scrim) 32%, transparent);'
                + 'display:flex;justify-content:center;align-items:center;z-index:var(--md-sys-z-overlay);';
            ov.onclick = function(e) { if (e.target === this) this.remove(); };
            var box = document.createElement('div');
            box.style.cssText = 'width:90%;height:85%;background:var(--md-sys-color-surface-container-high);'
                + 'color:var(--md-sys-color-on-surface);'
                + 'border-radius:var(--md-sys-shape-corner-extra-large);'
                + 'box-shadow:var(--md-sys-elevation-level3);'
                + 'display:flex;flex-direction:column;overflow:hidden;';
            box.innerHTML = '<div class="md-modal-header"><h3>' + mdIcon('bar_chart', 18) + ' 瀑布流监控</h3>'
                + '<button class="md-modal-close" onclick="document.getElementById(\'waterfall-overlay\').remove()" title="关闭">' + mdIcon('close', 18) + '</button></div>'
                + '<div id="waterfall-body" class="md-modal-body">'
                + '<div style="text-align:center;color:var(--md-sys-color-on-surface-variant);">加载中...</div></div>';
            ov.appendChild(box);
            document.body.appendChild(ov);
            fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'get_group_waterfall', group_id:gid})})
                .then(r => r.json()).then(data => {
                    if (data.status !== 'ok') { document.getElementById('waterfall-body').innerHTML = '<div style="color:var(--md-sys-color-error);">加载失败</div>'; return; }
                    var wf = data.waterfall;
                    var body = document.getElementById('waterfall-body');
                    if (!wf || wf.length === 0) { body.innerHTML = '<div style="text-align:center;color:var(--md-sys-color-on-surface-variant);">此分组没有会话</div>'; return; }
                    // 收集所有时间点并排序（用于非线性时间轴）
                    var allTimes = [];
                    wf.forEach(function(sess) {
                        sess.bubbles = sess.bubbles.filter(function(b) { return b.created_at && b.created_at > 1000000000; }); // 过滤无效时间戳
                        sess.bubbles.forEach(function(b) { allTimes.push(b.created_at); });
                    });
                    allTimes.sort(function(a,b){return a-b;});
                    if (allTimes.length === 0) { body.innerHTML = '<div style="text-align:center;color:var(--md-sys-color-on-surface-variant);">无气泡数据</div>'; return; }
                    // 构建非线性时间轴：将超过30分钟的间隔压缩为固定高度
                    var GAP_THRESHOLD = 1800; // 30分钟
                    var GAP_PIXELS = 60; // 压缩后的间隔高度（足够大以产生视觉分隔）
                    var ACTIVE_PPS = 0.08; // 活跃区域每秒像素数
                    var colW = 140, rowH = 16, headerH = 50, timeColW = 70;
                    // 构建时间段列表：[{start, end, y_start, y_end, is_gap}]
                    var segments = [];
                    var currentY = 0;
                    var prevTime = allTimes[0];
                    var segStart = allTimes[0];
                    for (var ti = 1; ti < allTimes.length; ti++) {
                        var gap = allTimes[ti] - allTimes[ti-1];
                        if (gap > GAP_THRESHOLD) {
                            // 结束当前活跃段
                            var segH = (allTimes[ti-1] - segStart) * ACTIVE_PPS;
                            segments.push({start: segStart, end: allTimes[ti-1], y_start: currentY, y_end: currentY + segH, is_gap: false});
                            currentY += segH;
                            // 添加压缩间隔段
                            segments.push({start: allTimes[ti-1], end: allTimes[ti], y_start: currentY, y_end: currentY + GAP_PIXELS, is_gap: true});
                            currentY += GAP_PIXELS;
                            segStart = allTimes[ti];
                        }
                    }
                    // 最后一个活跃段
                    var lastSegH = (allTimes[allTimes.length-1] - segStart) * ACTIVE_PPS;
                    segments.push({start: segStart, end: allTimes[allTimes.length-1], y_start: currentY, y_end: currentY + lastSegH, is_gap: false});
                    currentY += lastSegH + rowH;
                    // 时间→Y坐标映射函数
                    function timeToY(t) {
                        for (var si = 0; si < segments.length; si++) {
                            var seg = segments[si];
                            if (seg.is_gap) {
                                // Gap 段使用严格不等式，边界时间点归属相邻活跃段
                                if (t > seg.start && t < seg.end) return seg.y_start + GAP_PIXELS / 2;
                            } else {
                                if (t >= seg.start && t <= seg.end) {
                                    var ratio = (seg.end > seg.start) ? (t - seg.start) / (seg.end - seg.start) : 0;
                                    return seg.y_start + ratio * (seg.y_end - seg.y_start);
                                }
                            }
                        }
                        return currentY;
                    }
                    var totalH = Math.max(400, currentY + headerH + 20);
                    var totalW = timeColW + wf.length * (colW + 4);
                    var minTime = allTimes[0], maxTime = allTimes[allTimes.length-1];
                    var html = '<div style="font-weight:bold;margin-bottom:12px;">' + (data.group_name || '') + ' — ' + wf.length + ' 个会话, ' + allTimes.length + ' 条消息</div>';
                    html += '<div style="overflow:auto;position:relative;border:1px solid var(--md-sys-color-outline-variant);border-radius:var(--md-sys-shape-corner-small);max-height:calc(100% - 50px);" id="wf-scroll">';
                    html += '<div style="position:relative;width:' + totalW + 'px;height:' + totalH + 'px;padding-top:' + headerH + 'px;">';
                    // 左侧时间刻度
                    segments.forEach(function(seg) {
                        if (seg.is_gap) {
                            var gapH = (seg.end - seg.start) / 3600;
                            // The stripe marks a compressed gap so the timeline is not read
                            // as continuous. Two hardcoded light greys became a bright band
                            // under the dark scheme; the surface pair keeps it textured but
                            // quiet in either theme.
                            html += '<div style="position:absolute;top:' + (headerH + seg.y_start) + 'px;left:0;width:' + totalW + 'px;height:' + GAP_PIXELS + 'px;'
                                + 'font-size:var(--md-sys-typescale-label-small-size);color:var(--md-sys-color-on-surface-variant);'
                                + 'text-align:left;line-height:' + GAP_PIXELS + 'px;padding-left:var(--md-sys-spacing-2);'
                                + 'background:repeating-linear-gradient(0deg,var(--md-sys-color-surface-container-high),var(--md-sys-color-surface-container-high) 1px,var(--md-sys-color-surface) 1px,var(--md-sys-color-surface) 4px);'
                                + 'border-top:1px solid var(--md-sys-color-outline-variant);border-bottom:1px solid var(--md-sys-color-outline-variant);">'
                                + mdIcon('more_vert', 12) + ' 间隔 ' + gapH.toFixed(1) + ' 小时</div>';
                        } else {
                            var startStr = new Date(seg.start * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
                            var endStr = new Date(seg.end * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
                            // Start label stays stronger than the end label, preserving the
                            // #666 / #999 hierarchy the original relied on.
                            html += '<div style="position:absolute;top:' + (headerH + seg.y_start) + 'px;left:0;width:' + (timeColW-4) + 'px;font-size:9px;color:var(--md-sys-color-on-surface-variant);text-align:right;padding-right:4px;border-right:1px solid var(--md-sys-color-outline-variant);">' + startStr + '</div>';
                            if (seg.y_end - seg.y_start > 30) {
                                html += '<div style="position:absolute;top:' + (headerH + seg.y_end - 12) + 'px;left:0;width:' + (timeColW-4) + 'px;font-size:9px;color:color-mix(in srgb, var(--md-sys-color-on-surface-variant) 60%, transparent);text-align:right;padding-right:4px;border-right:1px solid var(--md-sys-color-outline-variant);">' + endStr + '</div>';
                            }
                        }
                    });
                    // 列头
                    wf.forEach(function(sess, ci) {
                        var x = timeColW + ci * (colW + 4);
                        html += '<div style="position:absolute;top:0;left:' + x + 'px;width:' + colW + 'px;font-size:var(--md-sys-typescale-label-small-size);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--md-sys-color-on-surface);font-weight:500;padding:4px 0;cursor:pointer;border-bottom:1px solid var(--md-sys-color-outline-variant);height:' + (headerH-8) + 'px;line-height:' + (headerH-8) + 'px;" onclick="var _o=window.localSid;window.localSid=\'' + sess.sid + '\';window.history.pushState({},\'\',\'/?' + 'sid=' + sess.sid + '\');window.hasLoadedFullHistory=false;postAction({action:\'switch_session\',sid:\'' + sess.sid + '\',_outgoing_sid:_o});document.getElementById(\'waterfall-overlay\').remove();" title="' + sess.name + ' (' + sess.bubble_count + ' 条)">' + sess.name.substring(0, 14) + '</div>';
                        // 气泡按非线性时间定位
                        sess.bubbles.forEach(function(b) {
                            var y = headerH + timeToY(b.created_at || minTime);
                            // fg paired with color: the old code hardcoded white text, which
                            // fails once the dark scheme lightens both accents.
                            var color = b.role === 'user' ? 'var(--md-sys-color-primary)' : 'var(--md-sys-color-success)';
                            var fg = b.role === 'user' ? 'var(--md-sys-color-on-primary)' : 'var(--md-sys-color-on-success)';
                            var timeStr = new Date((b.created_at || 0) * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
                            html += '<div style="position:absolute;top:' + y + 'px;left:' + x + 'px;width:' + colW + 'px;height:' + rowH + 'px;background:' + color + ';border-radius:var(--md-sys-shape-corner-extra-small);font-size:9px;color:' + fg + ';line-height:' + rowH + 'px;padding:0 4px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;cursor:default;" title="[ID:' + b.id + '] ' + timeStr + ' ' + (b.summary||'') + ' (' + b.token_k + 'k)">' + timeStr + ' ' + (b.summary || b.role) + '</div>';
                        });
                    });
                    html += '</div></div>';
                    body.innerHTML = html;
                }).catch(e => { document.getElementById('waterfall-body').innerHTML = '<div style="color:var(--md-sys-color-error);">请求失败: ' + e + '</div>'; });
        }

        document.addEventListener('click', (e) => {
            const dropdown = document.getElementById('model-dropdown');
            if (dropdown && dropdown.style.display === 'block') {
                const toggleBtn = dropdown.previousElementSibling;
                if (!dropdown.contains(e.target) && (!toggleBtn || !toggleBtn.contains(e.target))) {
                    dropdown.style.display = 'none';
                }
            }
            // Hide tab context menu on any click outside
            var tabCtx = document.getElementById('tab-context-menu');
            if (tabCtx && tabCtx.style.display !== 'none' && !tabCtx.contains(e.target)) {
                tabCtx.style.display = 'none';
            }

        });

        // ========== Bottom Tab Bar ==========
        var _openedTabs = JSON.parse(localStorage.getItem('chatapp_bottom_tabs') || '[]');
        var _tabCtxSid = null; // right-click target
        var _sessionHistoryCache = {}; // sid -> conversation_history (last known)

        function _saveOpenedTabs() {
            localStorage.setItem('chatapp_bottom_tabs', JSON.stringify(_openedTabs));
        }

        function _ensureTabOpen(sid) {
            if (sid && sid !== 'starred_session_virtual' && _openedTabs.indexOf(sid) === -1) {
                _openedTabs.push(sid);
                _saveOpenedTabs();
            }
        }

        function closeTab(sid) {
            var idx = _openedTabs.indexOf(sid);
            if (idx < 0) return;
            _openedTabs.splice(idx, 1);
            _saveOpenedTabs();
            // If closing the active tab, switch to adjacent
            if (sid === (window.localSid || window.currentSessionId) && _openedTabs.length > 0) {
                var nextIdx = Math.min(idx, _openedTabs.length - 1);
                switchToTab(_openedTabs[nextIdx]);
                return; // switchToTab already calls renderBottomTabs
            }
            // Fast DOM removal instead of full rebuild
            var tabEl = document.querySelector('.bottom-tab[data-sid="' + sid + '"]');
            if (tabEl) tabEl.remove();
        }

        function switchToTab(sid) {
            // Exit kanban if active, even if switching to same tab
            if (typeof _kanbanActive !== 'undefined' && _kanbanActive) {
                exitKanban();
                if (sid === window.localSid) return;
            }
            if (sid === window.localSid) return; // already active
            // Record presence leave/enter for kanban
            if (typeof _kanbanRecordEnter === 'function') _kanbanRecordEnter(sid);
            var _outgoingSid = window.localSid; // capture BEFORE changing
            _ensureTabOpen(sid);
            window.localSid = sid;
            window.history.pushState({}, '', '/?sid=' + sid);
            window.hasLoadedFullHistory = false;
            uiState = {};
            bubbleCache = {};
            lastChatHash = '__force__';
            window._expandStateMap = {};
            // Optimistic render from cache — instant visual switch
            var cached = _sessionHistoryCache[sid];
            if (cached && cached.length > 0) {
                currentHistory = cached;
                _applyUiState(currentHistory);
                renderChat(currentHistory);
            } else {
                // No cache: show loading spinner while fresh data is being fetched
                currentHistory = [];
                document.getElementById('chat-container').innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--md-sys-color-on-surface-variant);"><div class="spinner-ring"></div><div style="margin-top:var(--md-sys-spacing-3);font-size:var(--md-sys-typescale-body-medium-size);">加载中...</div></div>';
            }
            renderBottomTabs();
            // Async fetch fresh data (pass _outgoing_sid for state save routing)
            postAction({action: 'switch_session', sid: sid, _outgoing_sid: _outgoingSid});
        }

        function renderBottomTabs() {
            var container = document.getElementById('bottom-tabs-container');
            if (!container) return;
            if (!globalSettings.enable_bottom_tabs) return;
            var sessMap = window._lastSessionsMap || {};
            // Clean up tabs for deleted sessions
            var prevLen = _openedTabs.length;
            _openedTabs = _openedTabs.filter(function(sid) { return !sessMap[sid] || !sessMap[sid].soft_deleted; });
            if (_openedTabs.length !== prevLen) _saveOpenedTabs();
            var activeSid = window.localSid || window.currentSessionId;
            // In kanban mode, no tab should be highlighted as active
            var isKanbanMode = (typeof _kanbanActive !== 'undefined' && _kanbanActive);

            container.innerHTML = '';
            _openedTabs.forEach(function(sid) {
                var sess = sessMap[sid];
                if (!sess) return;
                var tab = document.createElement('div');
                tab.className = 'bottom-tab' + (!isKanbanMode && sid === activeSid ? ' active' : '');
                tab.dataset.sid = sid;
                if (sess._theme_hue != null) {
                    // Tint whichever surface the untinted branch below would have
                    // used, so the two branches differ by exactly one tint and the
                    // active/inactive step stays identical either way. The old
                    // 92%/95% pair computed its own values and gave hued sessions
                    // a different active contrast from plain ones — and both were
                    // light-scheme constants.
                    var _tabHue = sess._theme_hue;
                    var _tabBase = (sid === activeSid)
                        ? 'var(--md-sys-color-surface)'
                        : 'var(--md-sys-color-surface-container-high)';
                    tab.style.background = 'color-mix(in srgb, hsl(' + _tabHue
                        + ' 60% 50%) 12%, ' + _tabBase + ')';
                } else {
                    // The active tab takes the content-area surface so it visually
                    // joins the panel above it, which is the point of an M3 tab.
                    tab.style.background = (sid === activeSid)
                        ? 'var(--md-sys-color-surface)'
                        : 'var(--md-sys-color-surface-container-high)';
                }
                // Determine status
                var statusClass = 'idle';
                // We need full session data for status — check if available
                if (sess.is_processing || sess.active_threads > 0) statusClass = 'processing';
                if (sess.autopilot_active) statusClass = 'autopilot';
                tab.innerHTML = '<span class="tab-status ' + statusClass + '"></span>' +
                '<span class="tab-name">' + (sess.name || '未命名') + '</span>';
                tab.onclick = function(e) {
                    switchToTab(sid);
                };
                tab.oncontextmenu = function(e) {
                    e.preventDefault();
                    _tabCtxSid = sid;
                    var menu = document.getElementById('tab-context-menu');
                    menu.style.display = 'block';
                    menu.style.left = e.clientX + 'px';
                    menu.style.top = (e.clientY - menu.offsetHeight - 4) + 'px';
                    // Ensure menu stays in viewport
                    var rect = menu.getBoundingClientRect();
                    if (rect.top < 0) menu.style.top = e.clientY + 4 + 'px';
                    if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 4) + 'px';
                };
                container.appendChild(tab);
            });
            // + 新建会话按钮
            var addBtn = document.createElement('div');
            addBtn.className = 'bottom-tab-add';
            addBtn.innerHTML = mdIcon('add', 18);
            addBtn.title = '新建会话';
            addBtn.onclick = function() { postAction({action: 'create_session'}); };
            container.appendChild(addBtn);
            // 会话管理按钮。innerHTML 而非 textContent：后者会把 SVG 转义成标签源码。
            var mgrBtn = document.createElement('div');
            mgrBtn.className = 'bottom-tab-add sm-open-btn';
            mgrBtn.innerHTML = mdIcon('grid_view', 18);
            mgrBtn.title = '会话管理';
            mgrBtn.onclick = function() { openSessionManager(); };
            container.appendChild(mgrBtn);
            // Kanban button
            var kanbanBtn = document.createElement('div');
            kanbanBtn.id = 'kanban-btn';
            kanbanBtn.className = 'bottom-tab-add';
            kanbanBtn.innerHTML = mdIcon('bar_chart', 18);
            kanbanBtn.title = '看板';
            kanbanBtn.style.color = 'var(--md-sys-color-tertiary)';
            kanbanBtn.onclick = function() {
                if (typeof _kanbanActive !== 'undefined' && _kanbanActive) {
                    exitKanban();
                } else {
                    enterKanban();
                }
            };
            container.appendChild(kanbanBtn);
        }

        // Context menu actions
        function tabCtxClone() {
            document.getElementById('tab-context-menu').style.display = 'none';
            if (!_tabCtxSid) return;
            // Clone and open in tab bar
            fetch('/api/action', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'duplicate_session', sid: _tabCtxSid})
            }).then(function(r) { return r.json(); }).then(function(data) {
                if (data.new_sid) {
                    _ensureTabOpen(data.new_sid);
                    switchToTab(data.new_sid);
                }
            }).catch(function(e) { showToast('克隆失败: ' + e, 'error'); });
        }

        function tabCtxClose() {
            document.getElementById('tab-context-menu').style.display = 'none';
            if (_tabCtxSid) closeTab(_tabCtxSid);
        }

        function tabCtxArchive() {
            document.getElementById('tab-context-menu').style.display = 'none';
            if (!_tabCtxSid) return;
            closeTab(_tabCtxSid);
            postAction({action: 'archive_session', sid: _tabCtxSid});
        }

        function tabCtxRename() {
            document.getElementById('tab-context-menu').style.display = 'none';
            if (!_tabCtxSid) return;
            var sessMap = window._lastSessionsMap || {};
            var sess = sessMap[_tabCtxSid];
            var oldName = (sess && sess.name) || '';
            renameSession(_tabCtxSid, oldName);
        }

        /* Recycle bin. delete_session has always been a soft delete — it sets
         * soft_deleted and the frontend filters those rows out everywhere — but
         * nothing could bring one back, so a deleted session was unreachable
         * without editing JSON by hand.
         *
         * Restore is the only action, and that is deliberate: there is no hard
         * delete anywhere in the application, so nothing here needs a
         * confirmation either. Deleting a session is always recoverable.
         *
         * The accepted consequence is that session files under data/sessions/ only
         * ever accumulate — save_sessions' orphan sweep unlinks files with no entry
         * in self.sessions, and a soft-deleted session keeps its entry forever. Do
         * not "fix" that by adding a purge: transcripts are user assets and the
         * program does not destroy them. Reclaiming the space is the user's call,
         * made outside the app. */
        function toggleTrashShelf() {
            var shelf = document.getElementById('trash-shelf');
            if (shelf.style.display === 'none') {
                shelf.style.display = '';
                renderTrashList();
            } else {
                shelf.style.display = 'none';
            }
        }

        function renderTrashList() {
            var list = document.getElementById('trash-list');
            if (!list) return;
            var sessMap = window._lastSessionsMap || {};
            // Newest first: the entry a user is most likely reaching for is the one
            // they deleted by mistake a moment ago.
            var deleted = Object.keys(sessMap).filter(function(sid) {
                return sessMap[sid].soft_deleted && sid !== 'starred_session_virtual';
            }).sort(function(a, b) { return (sessMap[b].deleted_at || 0) - (sessMap[a].deleted_at || 0); });
            var countEl = document.getElementById('trash-count');
            if (countEl) countEl.textContent = deleted.length ? '(' + deleted.length + ')' : '';
            if (deleted.length === 0) {
                list.innerHTML = '<div style="color:var(--md-sys-color-on-surface-variant); text-align:center; padding:var(--md-sys-spacing-2);">回收站为空</div>';
                return;
            }
            list.innerHTML = deleted.map(function(sid) {
                var s = sessMap[sid];
                var when = s.deleted_at
                    ? new Date(s.deleted_at * 1000).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'})
                    : '时间未知';
                return '<div style="display:flex; align-items:center; justify-content:space-between; gap:var(--md-sys-spacing-1); padding:var(--md-sys-spacing-1) 0; border-bottom:1px solid var(--md-sys-color-outline-variant);">' +
                    '<span style="flex:1; min-width:0; overflow:hidden;"><span style="display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + (s.name || '未命名') + '</span>' +
                    '<span style="color:var(--md-sys-color-on-surface-variant); font-size:var(--md-sys-typescale-label-small-size);">' + when + '</span></span>' +
                    '<button class="md-button md-button--tonal md-button--compact" style="min-height:24px; padding:0 var(--md-sys-spacing-2);" onclick="postAction({action:\'restore_session\', sid:\'' + sid + '\'})" title="恢复到对话列表">恢复</button>' +
                    '</div>';
            }).join('');
        }

        function toggleArchivedShelf() {
            var shelf = document.getElementById('archived-shelf');
            if (shelf.style.display === 'none') {
                shelf.style.display = '';
                renderArchivedList();
            } else {
                shelf.style.display = 'none';
            }
        }

        function renderArchivedList() {
            var list = document.getElementById('archived-list');
            if (!list) return;
            var sessMap = window._lastSessionsMap || {};
            var archived = Object.keys(sessMap).filter(function(sid) {
                return sessMap[sid].is_archived && !sessMap[sid].soft_deleted && sid !== 'starred_session_virtual';
            }).sort(function(a, b) { return (sessMap[b].order || 0) - (sessMap[a].order || 0); });
            if (archived.length === 0) {
                list.innerHTML = '<div style="color:var(--md-sys-color-on-surface-variant); text-align:center; padding:var(--md-sys-spacing-2);">无归档会话</div>';
                return;
            }
            list.innerHTML = archived.map(function(sid) {
                var s = sessMap[sid];
                return '<div style="display:flex; align-items:center; justify-content:space-between; gap:var(--md-sys-spacing-1); padding:var(--md-sys-spacing-1) 0; border-bottom:1px solid var(--md-sys-color-outline-variant);">' +
                    '<span style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;" onclick="postAction({action:\'unarchive_session\', sid:\'' + sid + '\'})" title="点击恢复">' + (s.name || '未命名') + '</span>' +
                    '<button class="md-button md-button--tonal md-button--compact" style="min-height:24px; padding:0 var(--md-sys-spacing-2);" onclick="postAction({action:\'unarchive_session\', sid:\'' + sid + '\'})" title="恢复">恢复</button>' +
                    '<button class="md-button md-button--outlined md-button--compact" style="min-height:24px; padding:0 var(--md-sys-spacing-2);" onclick="postAction({action:\'delete_session\', sid:\'' + sid + '\'})" title="移到回收站（可恢复）">删</button>' +
                    '</div>';
            }).join('');
        }

        // Bottom tab bar is refreshed via renderSessions hook in _doHandleStateUpdate

        // ========== Session Manager Popup ==========
        function openSessionManager() {
            var existing = document.getElementById('session-mgr-overlay');
            if (existing) { existing.remove(); return; }
            var ov = document.createElement('div');
            ov.id = 'session-mgr-overlay';
            ov.className = 'sm-overlay';
            ov.onclick = function(e) { if (e.target === this) this.remove(); };
            var panel = document.createElement('div');
            panel.className = 'sm-panel';
            // Header
            var header = document.createElement('div');
            header.className = 'sm-header';
            header.innerHTML = '<h2>会话管理</h2>' +
                '<span class="sm-close" onclick="closeSessionManager()" title="关闭">' + mdIcon('close', 20) + '</span>';
            panel.appendChild(header);
            // Toolbar
            var toolbar = document.createElement('div');
            toolbar.className = 'sm-toolbar';
            toolbar.innerHTML =
                '<button onclick="smNewSession()" class="sm-toolbar-btn">' + mdIcon('add', 16) + ' 新建会话</button>' +
                '<button onclick="closeSessionManager();createGroup()" class="sm-toolbar-btn">' + mdIcon('create_new_folder', 16) + ' 新建组</button>' +
                '<button onclick="closeSessionManager();openSettingsModal()" class="sm-toolbar-btn">' + mdIcon('settings', 16) + ' 全局设置</button>' +
                '<button onclick="closeSessionManager();openManualModal()" class="sm-toolbar-btn">' + mdIcon('book', 16) + ' 使用说明</button>' +
                '<button onclick="exportSnapshot()" class="sm-toolbar-btn">' + mdIcon('inventory', 16) + ' 更新</button>' +
                '<button onclick="smExportRelease()" class="sm-toolbar-btn">' + mdIcon('north_east', 16) + ' 发布</button>' +
                '<button onclick="clearLogs()" class="sm-toolbar-btn" style="color:var(--md-sys-color-error);">' + mdIcon('delete_sweep', 16) + ' 清理</button>' +
                '<button onclick="smToggleArchived()" class="sm-toolbar-btn" id="sm-archive-btn">' + mdIcon('archive', 16) + ' 查看归档</button>';
            panel.appendChild(toolbar);
            // Body
            var body = document.createElement('div');
            body.id = 'session-mgr-body';
            body.className = 'sm-body';
            body.dataset.showArchived = 'false';
            panel.appendChild(body);
            ov.appendChild(panel);
            document.body.appendChild(ov);
            renderSessionManagerGrid();
        }

        function closeSessionManager() {
            var ov = document.getElementById('session-mgr-overlay');
            if (ov) ov.remove();
        }

        function renderSessionManagerGrid() {
            var body = document.getElementById('session-mgr-body');
            if (!body) return;
            var sessMap = window._lastSessionsMap || {};
            var activeSid = window.localSid || window.currentSessionId;
            var showArchived = body.dataset.showArchived === 'true';
            var sids;
            if (showArchived) {
                sids = Object.keys(sessMap).filter(function(sid) {
                    return sessMap[sid].is_archived && !sessMap[sid].soft_deleted && sid !== 'starred_session_virtual';
                });
            } else {
                sids = Object.keys(sessMap).filter(function(sid) {
                    return !sessMap[sid].soft_deleted && !sessMap[sid].is_archived && sid !== 'starred_session_virtual';
                });
            }
            sids.sort(function(a, b) { return (sessMap[a].order || 0) - (sessMap[b].order || 0); });
            if (sids.length === 0) {
                body.innerHTML = '<div class="sm-empty">' + (showArchived ? '无归档会话' : '无会话') + '</div>';
                return;
            }
            var html = '';
            if (!showArchived) {
                var groupedSids = new Set();
                var groups = Object.entries(sessionGroups).sort(function(a, b) { return (a[1].order || 0) - (b[1].order || 0); });
                groups.forEach(function(entry) {
                    var gid = entry[0], group = entry[1];
                    var groupSids = (group.session_ids || []).filter(function(sid) { return sids.indexOf(sid) >= 0; });
                    if (groupSids.length === 0) return;
                    groupSids.forEach(function(sid) { groupedSids.add(sid); });
                    var isCollapsed = group.collapsed;
                    html += '<div class="sm-section">';
                    html += '<div class="sm-section-header" onclick="smToggleGroup(\'' + gid + '\')"><span class="sm-fold-icon">' + (isCollapsed ? mdIcon('chevron_right', 14) : mdIcon('expand_more', 14)) + '</span>';
                    html += '<span class="sm-section-title">' + (group.name || '\u672A\u547D\u540D\u7EC4') + '</span>';
                    html += '<span class="sm-section-count">' + groupSids.length + '</span>';
                    html += '<div class="sm-section-actions" onclick="event.stopPropagation()">';
                    html += '<button class="sm-section-btn" onclick="addSessionToGroup(\'' + gid + '\')" title="\u65B0\u5EFA">' + mdIcon('add', 14) + '</button>';
                    html += '<button class="sm-section-btn" onclick="closeSessionManager();renameGroup(\'' + gid + '\', \'' + (group.name || '').replace(/'/g, "\\'") + '\')" title="\u91CD\u547D\u540D">' + mdIcon('edit', 14) + '</button>';
                    html += '<button class="sm-section-btn" onclick="closeSessionManager();deleteGroup(\'' + gid + '\')" title="\u5220\u9664" style="color:var(--md-sys-color-error);">' + mdIcon('delete', 14) + '</button>';
                    html += '</div></div>';
                    if (!isCollapsed) {
                        html += '<div class="sm-section-body"><div class="sm-grid">';
                        groupSids.forEach(function(sid) { html += _renderSmCard(sid, sessMap[sid], activeSid); });
                        html += '</div></div>';
                    }
                    html += '</div>';
                });
                var ungrouped = sids.filter(function(sid) { return !groupedSids.has(sid); });
                if (ungrouped.length > 0) {
                    if (groupedSids.size > 0) {
                        html += '<div class="sm-section sm-section-ungrouped"><div class="sm-section-header sm-section-header-light">';
                        html += '<span class="sm-section-title">\u672A\u5206\u7EC4</span><span class="sm-section-count">' + ungrouped.length + '</span></div><div class="sm-section-body">';
                    }
                    html += '<div class="sm-grid">';
                    ungrouped.forEach(function(sid) { html += _renderSmCard(sid, sessMap[sid], activeSid); });
                    html += '</div>';
                    if (groupedSids.size > 0) html += '</div></div>';
                }
            } else {
                html += '<div class="sm-grid">';
                sids.forEach(function(sid) { html += _renderSmCard(sid, sessMap[sid], activeSid); });
                html += '</div>';
            }
            body.innerHTML = html;
        }

        function _renderSmCard(sid, s, activeSid) {
            var hue = s._theme_hue != null ? s._theme_hue : 200;
            var isActive = sid === activeSid;
            var isInTab = _openedTabs.indexOf(sid) >= 0;
            var statusIcon = '';
            if (s.is_processing || s.active_threads > 0) statusIcon = mdIcon('refresh', 14) + ' ';
            else if (s.autopilot_active) statusIcon = mdIcon('smart_toy', 14) + ' ';
            var cls = 'sm-card' + (isActive ? ' active' : '') + (isInTab ? ' in-tab' : '');
            // The theme hue survives only as the 4px top edge, which is what it is
            // actually for. The old 135deg gradient fill computed two hsl stops that
            // would both read as bright smears under the dark scheme.
            return '<div class="' + cls + '" data-sid="' + sid + '" style="border-top:4px solid hsl(' + hue + ',55%,55%);" onclick="smCardClick(\'' + sid + '\')" oncontextmenu="smCardContext(event,\'' + sid + '\')">' +
                '<div class="sm-card-name">' + statusIcon + (s.name || '\u672A\u547D\u540D') + '</div>' +
                (isInTab ? '<div class="sm-card-badge">' + mdIcon('check', 14) + '</div>' : '') + '</div>';
        }

        function smToggleGroup(gid) {
            sessionGroups[gid].collapsed = !sessionGroups[gid].collapsed;
            renderSessionManagerGrid();
            postAction({action: 'update_session_group', group_id: gid, collapsed: sessionGroups[gid].collapsed});
        }

        function smCardClick(sid) {
            _ensureTabOpen(sid);
            switchToTab(sid);
            renderSessionManagerGrid();
            renderBottomTabs();
        }

        function smCardContext(e, sid) {
            e.preventDefault();
            e.stopPropagation();
            var existing = document.getElementById('sm-context-menu');
            if (existing) existing.remove();
            var sessMap = window._lastSessionsMap || {};
            var isArchived = sessMap[sid] && sessMap[sid].is_archived;
            var menu = document.createElement('div');
            menu.id = 'sm-context-menu';
            menu.className = 'sm-context-menu';
            menu.style.left = e.clientX + 'px';
            menu.style.top = e.clientY + 'px';
            if (isArchived) {
                menu.innerHTML =
                    '<div class="sm-ctx-item" onclick="smCtxUnarchive(\'' + sid + '\')">' + mdIcon('undo', 16) + ' \u6062\u590D</div>' +
                    '<div class="sm-ctx-item sm-ctx-danger" onclick="smCtxDelete(\'' + sid + '\')">' + mdIcon('delete', 16) + ' \u6C38\u4E45\u5220\u9664</div>';
            } else {
                menu.innerHTML =
                    '<div class="sm-ctx-item" onclick="smCtxRename(\'' + sid + '\')">' + mdIcon('edit', 16) + ' \u91CD\u547D\u540D</div>' +
                    '<div class="sm-ctx-item" onclick="smCtxClone(\'' + sid + '\')">' + mdIcon('content_copy', 16) + ' \u514B\u9686</div>' +
                    '<div class="sm-ctx-item" onclick="smCtxArchive(\'' + sid + '\')">' + mdIcon('archive', 16) + ' \u5F52\u6863</div>' +
                    '<div class="sm-ctx-item sm-ctx-danger" onclick="smCtxDelete(\'' + sid + '\')">' + mdIcon('delete', 16) + ' \u5220\u9664</div>';
            }
            document.body.appendChild(menu);
            var rect = menu.getBoundingClientRect();
            if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
            if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
            setTimeout(function() {
                document.addEventListener('click', function _smCtxClose(ev) {
                    var m = document.getElementById('sm-context-menu');
                    if (m) m.remove();
                    document.removeEventListener('click', _smCtxClose);
                });
            }, 10);
        }

        function smCtxRename(sid) {
            var m = document.getElementById('sm-context-menu'); if (m) m.remove();
            var sessMap = window._lastSessionsMap || {};
            renameSession(sid, (sessMap[sid] && sessMap[sid].name) || '');
        }

        function smCtxClone(sid) {
            var m = document.getElementById('sm-context-menu'); if (m) m.remove();
            fetch('/api/action', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'duplicate_session', sid: sid})
            }).then(function(r) { return r.json(); }).then(function(data) {
                if (data.new_sid) {
                    _ensureTabOpen(data.new_sid);
                    setTimeout(function() { renderSessionManagerGrid(); renderBottomTabs(); }, 300);
                    showToast('\u5DF2\u514B\u9686\u4F1A\u8BDD', 'success');
                }
            });
        }

        function smCtxArchive(sid) {
            var m = document.getElementById('sm-context-menu'); if (m) m.remove();
            closeTab(sid);
            postAction({action: 'archive_session', sid: sid});
            setTimeout(renderSessionManagerGrid, 500);
        }

        function smCtxUnarchive(sid) {
            var m = document.getElementById('sm-context-menu'); if (m) m.remove();
            postAction({action: 'unarchive_session', sid: sid});
            setTimeout(renderSessionManagerGrid, 500);
        }

        function smCtxDelete(sid) {
            var m = document.getElementById('sm-context-menu'); if (m) m.remove();
            closeTab(sid);
            postAction({action: 'delete_session', sid: sid});
            setTimeout(renderSessionManagerGrid, 500);
        }

        function smNewSession() {
            postAction({action: 'create_session'});
            setTimeout(renderSessionManagerGrid, 500);
        }

        function smToggleArchived() {
            var body = document.getElementById('session-mgr-body');
            if (!body) return;
            var btn = document.getElementById('sm-archive-btn');
            // innerHTML, not textContent: the labels carry inline SVG and a
            // textContent assignment would strip the icon on the first toggle.
            if (body.dataset.showArchived === 'true') {
                body.dataset.showArchived = 'false';
                if (btn) btn.innerHTML = mdIcon('archive', 16) + ' \u67E5\u770B\u5F52\u6863';
            } else {
                body.dataset.showArchived = 'true';
                if (btn) btn.innerHTML = mdIcon('arrow_downward', 16) + ' \u8FD4\u56DE\u4F1A\u8BDD\u5217\u8868';
            }
            renderSessionManagerGrid();
        }

        function smExportRelease() {
            fetch('/api/action', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'export_release'})
            }).then(function(r) { return r.json(); }).then(function(d) {
                showToast(d.message, d.status === 'ok' ? 'success' : 'error');
            }).catch(function(e) { showToast('\u53D1\u5E03\u5931\u8D25: ' + e, 'error'); });
        }
