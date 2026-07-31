        function renderChat(history) {
            if (!document.getElementById('baton-keyframes')) {
                var _bkf = document.createElement('style');
                _bkf.id = 'baton-keyframes';
                _bkf.textContent = '@keyframes baton-pulse{0%,100%{opacity:1}50%{opacity:0.15}}';
                document.head.appendChild(_bkf);
            }
            const isSessionSwitched = window.lastRenderedSessionId !== window.currentSessionId;
            window.lastRenderedSessionId = window.currentSessionId;
            const isScrolledToBottom = isSessionSwitched || (chatContainer.scrollHeight - chatContainer.clientHeight <= chatContainer.scrollTop + 1);
            const savedScrollTop = chatContainer.scrollTop;
            currentHistory = history;

            // Dehydrated content cache merge + client-side dehydration enforcement
            // Messages from get_session_data arrive with full content but should still show triggers
            if (!window._dehydratedContentCache) window._dehydratedContentCache = {};
            history.forEach(function(m) {
                if (m._dehydrated && window._dehydratedContentCache[m.id]) {
                    // Merge cached content into dehydrated message
                    var cached = window._dehydratedContentCache[m.id];
                    if (cached.content !== undefined) m.content = cached.content;
                    if (cached.content_parts !== undefined) m.content_parts = cached.content_parts;
                    if (cached.multimodal_blocks !== undefined) m.multimodal_blocks = cached.multimodal_blocks;
                    if (cached.thinking !== undefined) m.thinking = cached.thinking;
                    if (cached.diff_content !== undefined) m.diff_content = cached.diff_content;
                    delete m._dehydrated;
                } else if (!m._dehydrated && !m.is_omitted && (m.is_tool_result || m.is_collapsed || m.is_hidden)) {
                    // Client-side dehydration: messages from non-dehydrated sources should still show triggers
                    if (!window._dehydratedContentCache[m.id]) {
                        m._dehydrated = true;
                    } else {
                        // Cache exists - merge it
                        var cached2 = window._dehydratedContentCache[m.id];
                        if (cached2.content !== undefined) m.content = cached2.content;
                        if (cached2.content_parts !== undefined) m.content_parts = cached2.content_parts;
                        if (cached2.multimodal_blocks !== undefined) m.multimodal_blocks = cached2.multimodal_blocks;
                        if (cached2.thinking !== undefined) m.thinking = cached2.thinking;
                        if (cached2.diff_content !== undefined) m.diff_content = cached2.diff_content;
                    }
                }
            });

            // === Expand state preservation: save current states before rebuild ===
            if (!window._expandStateMap) window._expandStateMap = {};
            ['tr-content-', 'th-content-', 'sa-req-', 'sa-resp-'].forEach(function(_sp) {
                chatContainer.querySelectorAll('[id^="' + _sp + '"]').forEach(function(el) {
                    if (el.style.display === 'block') {
                        window._expandStateMap[el.id] = { expanded: true, scrollTop: el.scrollTop };
                    } else if (window._expandStateMap[el.id]) {
                        delete window._expandStateMap[el.id];
                    }
                });
            });
            chatContainer.querySelectorAll('[data-expand-id]').forEach(function(el) {
                var _eid = el.getAttribute('data-expand-id');
                if (el.style.display === 'block') {
                    window._expandStateMap[_eid] = { expanded: true, scrollTop: el.scrollTop };
                } else if (window._expandStateMap[_eid]) {
                    delete window._expandStateMap[_eid];
                }
            });

            const fragment = document.createDocumentFragment();

        // 预处理：tool_result映射 + thinking→response映射 + 活跃tool_use_id追踪
        const toolResultMap = {};
        const toolResultSkipSet = new Set();
        const activeToolUseIds = new Set();
        const _inlineRenderedTRs = new Set();
        const autoReadByTrigger = {};  // trigger_id -> {msg, index}
        const autoReadSkipSet = new Set();
        const lastEditForFile = {};
        const subagentMap = {};  // parent_tool_use_id -> {request: {msg, index}, response: {msg, index}}
        const subagentSkipSet = new Set();
        const thinkingMap = {};
        const thinkingSkipSet = new Set();
        // 第一遍预扫描：收集所有可能触发 subagent 的 tool_use ID（Agent, WebSearch 等，按出现顺序）
        const _agentToolIds = [];
        const _agentToolClaimed = new Set();
        history.forEach((m, i) => {
            if (m.role === 'assistant' && m.content_parts) {
                m.content_parts.forEach(p => {
                    if (p.type === 'tool_use_part') {
                        try { let td = JSON.parse(p.content); if (td.id) _agentToolIds.push({id: td.id, name: td.name, msgIdx: i, prompt: (td.input || {}).prompt || (td.input || {}).query || (td.input || {}).description || ''}); } catch(e) {}
                    }
                });
            }
        });
        history.forEach((m, i) => {
            if (m.is_subagent_request || m.is_subagent_response) {
                let _saKey = m.subagent_parent_tool_id;
                // 回退匹配：没有 parent_tool_id 时
                if (!_saKey) {
                    // 优先查找同 subagent_id 的请求已匹配的 Agent tool_use（确保请求和响应吸附到同一个块）
                    let _sameSubId = m.subagent_id;
                    if (_sameSubId) {
                        for (let _sk in subagentMap) {
                            let _entry = subagentMap[_sk];
                            if ((_entry.request && _entry.request.msg.subagent_id === _sameSubId) || (_entry.response && _entry.response.msg.subagent_id === _sameSubId)) {
                                _saKey = _sk;
                                break;
                            }
                        }
                    }
                    // 基于内容的匹配：比较 subagent 内容与工具的 prompt/query，只有内容一致才吸附
                    if (!_saKey) {
                        let _saContent = m.content || '';
                        let _subagentToolNames = new Set(['Agent', 'WebSearch', 'WebFetch']);
                        for (let _ai = 0; _ai < _agentToolIds.length; _ai++) {
                            let _at = _agentToolIds[_ai];
                            if (_at.msgIdx >= i || _agentToolClaimed.has(_at.id)) continue;
                            if (!_subagentToolNames.has(_at.name)) continue;
                            if (_at.prompt && _saContent.length > 0) {
                                let _promptSnippet = _at.prompt.substring(0, Math.min(80, _at.prompt.length));
                                if (_promptSnippet.length > 5 && _saContent.includes(_promptSnippet)) {
                                    _saKey = _at.id;
                                    _agentToolClaimed.add(_saKey);
                                    break;
                                }
                            }
                        }
                        // 找不到内容匹配时不吸附，让 subagent 气泡作为独立气泡正常显示
                    }
                } else {
                    _agentToolClaimed.add(_saKey);
                }
                if (_saKey) {
                    if (!subagentMap[_saKey]) subagentMap[_saKey] = {};
                    if (m.is_subagent_request) subagentMap[_saKey].request = {msg: m, index: i};
                    if (m.is_subagent_response) subagentMap[_saKey].response = {msg: m, index: i};
                    subagentSkipSet.add(m.id);
                }
            }
            if (m.is_auto_read && m.auto_read_file && !m.is_outdated_read) {
                if (m.auto_read_trigger_id) {
                    autoReadByTrigger[m.auto_read_trigger_id] = {msg: m, index: i};
                    autoReadSkipSet.add(m.id);
                } else {
                    // 无 trigger_id（跨会话广播等）：吸附到同文件的最近 Edit/Read
                    let _lastToolId = lastEditForFile[m.auto_read_file];
                    if (_lastToolId) {
                        autoReadByTrigger[_lastToolId] = {msg: m, index: i};
                        autoReadSkipSet.add(m.id);
                    }
                }
            }
            if (!m.is_hidden && m.role === 'assistant' && m.content_parts) {
                m.content_parts.forEach(p => { if (p.type === 'tool_use_part') { try { let td = JSON.parse(p.content); if ((td.name === 'Edit' || td.name === 'Read') && td.input && td.input.file_path) lastEditForFile[td.input.file_path] = td.id; } catch(e) {} } });
            }
            if (m.is_tool_result && m.tool_use_id) {
                if (!toolResultMap[m.tool_use_id]) toolResultMap[m.tool_use_id] = [];
                toolResultMap[m.tool_use_id].push({msg: m, index: i});
            }
                        if (!m.is_hidden && !m._edited && m.role === 'assistant' && m.content) {
                if (m.content_parts) { m.content_parts.forEach(p => { if (p.type === 'tool_use_part') { try { let td = JSON.parse(p.content); if (td.id) activeToolUseIds.add(td.id); } catch(e) {} } }); }
                let _tuM; let _tuR = /"id"\s*:\s*"(toolu_[^"]+)"/g; while ((_tuM = _tuR.exec(m.content)) !== null) activeToolUseIds.add(_tuM[1]);
            }

        });
        Object.keys(toolResultMap).forEach(tuId => { toolResultMap[tuId].forEach(tr => toolResultSkipSet.add(tr.msg.id)); });
        for (let i = 0; i < history.length; i++) {
            let m = history[i];
            let isThinking = (m.model_name && m.model_name.endsWith('(思考过程)')) || m.tool_type === 'thinking';
            if (isThinking) {
                for (let j = i + 1; j < history.length; j++) {
                    let next = history[j];
                    if (next.role === 'assistant' && !((next.model_name && next.model_name.endsWith('(思考过程)')) || next.tool_type === 'thinking')) {
                        if (!next.is_hidden) {
                            if (!thinkingMap[next.id]) thinkingMap[next.id] = [];
                            thinkingMap[next.id].push({msg: m, index: i});
                            thinkingSkipSet.add(m.id);
                        }
                        break;
                    }
                }
            }
        }

        console.time('📦 Message Loop & Hashing');
        history.forEach((msg, index) => {
            const isStarred = globalSettings.enable_starred && globalSettings.starred_messages && globalSettings.starred_messages.some(m => m.content === msg.content && m.role === msg.role);
            if (toolResultSkipSet.has(msg.id) || thinkingSkipSet.has(msg.id) || _inlineRenderedTRs.has(msg.id) || autoReadSkipSet.has(msg.id) || subagentSkipSet.has(msg.id)) return;
            if (msg.is_enforcement) return;
            if (msg.is_auto_read && !globalSettings.enable_show_all_autoread) return;
            if (msg.is_outdated_read) {
                msg = Object.assign({}, msg, {is_collapsed: true, summary: '📁 ' + (msg.auto_read_file || msg.summary || '') + ' (已有更新的自动读取结果)'});
            }
            let msgHash = msg.id + '_' + msg.role + '_' + (msg.content||'').length + '_' + (msg.content||'').slice(0,30) + '_' + (msg.content||'').slice(-30) + '_' + (msg.summary||'') + '_' + msg.is_collapsed + '_' + msg.is_omitted + '_' + msg.is_hidden + '_' + msg.is_unread + '_' + (msg.rating||'') + '_' + (msg.model_name||'') + '_' + (msg.timing ? msg.timing.ttfb + '_' + (msg.timing.download||0) : '') + '_' + (msg.content_parts ? msg.content_parts.map(p => p.type + '_' + p.status + '_' + (p.content||'').length + '_' + (p.content||'').slice(0,10) + '_' + (p.content||'').slice(-10)).join(',') : 'none') + '_' + (msg.diff_content ? msg.diff_content.length : 0) + '_' + (msg.term_state||'') + '_mm' + (msg.multimodal_blocks ? msg.multimodal_blocks.length : 0) + '_' + index + '_' + isStarred + '_bt' + (window._autopilotActive && msg._autopilot_gen !== undefined && msg._autopilot_gen === window._autopilotGen ? 1 : 0);
            if (msg.content_parts) {
                msg.content_parts.forEach(p => {
                    if (p.type === 'tool_use_part') {
                        try { let td = JSON.parse(p.content); let trs = toolResultMap[td.id]; if (trs) trs.forEach(tr => { msgHash += '_tr_' + tr.msg.id + '_' + (tr.msg.content||'').length + '_h' + (tr.msg.is_hidden?1:0) + '_d' + (tr.msg._dehydrated?1:0); }); if (td.id && subagentMap[td.id]) { let _sa = subagentMap[td.id]; if (_sa.request) msgHash += '_sareq_' + _sa.request.msg.id; if (_sa.response) msgHash += '_saresp_' + _sa.response.msg.id + '_' + (_sa.response.msg.content||'').length + '_sse' + (_sa.response.msg.has_subagent_sse?1:0); } } catch(e) {}
                    }
                });
            }
            if (thinkingMap[msg.id]) { thinkingMap[msg.id].forEach(th => { msgHash += '_th_' + th.msg.id + '_' + (th.msg.content||'').length + '_' + th.msg.is_hidden; }); }
            // 将 auto-read 结果纳入 hash，否则 auto-read 返回后 chatHash 不变导致不重渲染
            if (msg.content_parts) { msg.content_parts.forEach(p => { if (p.type === 'tool_use_part') { try { let td = JSON.parse(p.content); let ar = autoReadByTrigger[td.id]; if (ar) msgHash += '_ar_' + ar.msg.id + '_' + (ar.msg.content||'').length; } catch(e) {} } }); }
            
            // 🚀 核心优化：缓存命中或 forceReuse 时直接复用 DOM 节点，避免 replaceChild 闪烁
            if (bubbleCache[msg.id]) {
                const cached = bubbleCache[msg.id];
                const canReuse = cached.hash === msgHash || (cached.forceReuse && performance.now() < cached.forceReuse);
                if (canReuse) {
                    if (cached.forceReuse) { cached.hash = msgHash; cached.forceReuse = 0; }
                    if (thinkingMap[msg.id] && window._thinkingCache) {
                        thinkingMap[msg.id].forEach(({msg: thMsg}) => {
                            if (window._thinkingCache[thMsg.id]) {
                                fragment.appendChild(window._thinkingCache[thMsg.id].el);
                            }
                        });
                    }
                    fragment.appendChild(cached.el);
                    return;
                }
            }

                const bubble = document.createElement('div');
                bubble.id = 'msg-bubble-' + msg.id;
                bubble.classList.add('message-bubble', msg.role);
                if (msg.is_omitted) bubble.classList.add('omit-mode');
                if (msg.is_terminal) {
                    if (msg.term_state === 'running') {
                        bubble.style.backgroundColor = '#fff9c4';
                        bubble.style.border = '1px solid #ffe082';
                    } else {
                        bubble.style.backgroundColor = '#f7f7f9';
                        bubble.style.border = '1px solid #e0e0e0';
                    }
                }

                const isHidden = msg.is_omitted || msg.is_collapsed;
                const isWaiting = (msg.role === 'assistant' && !msg.content && !msg.content_parts && !msg.timing && !msg._dehydrated);
                const tokenK = ((typeof msg._content_len === 'number' ? msg._content_len : (msg.content || "").length) / 3000).toFixed(2);
                
                let tags = '';
                if(msg.is_hidden) tags += `<span class="status-tag tag-hide" style="background:#007bff; color:#fff;">已隐藏</span>`;
                if(msg.is_omitted) tags += `<span class="status-tag tag-omit">概括模式</span>`;
                if(msg.is_collapsed) tags += `<span class="status-tag tag-collapse">UI折叠</span>`;
                if(msg.diff_content) tags += `<span class="status-tag tag-annotated">已批注</span>`;
                
                            let mainContentContainer = document.createElement('div');
            mainContentContainer.className = 'bubble-main-content';
            let hasPendingActions = false;
            let hasFailedOrRejected = false;

            // 预处理 Pass 0：描述符格式工具调用解析
            if (!isHidden && !isWaiting && msg.role === 'assistant' && !msg.content_parts && msg.content && globalSettings.enable_tool_inject && !msg.is_terminal) {
                let _enableDesc = globalSettings.enable_descriptor_tool_calls !== false;
                let _hasDescMarkers = _enableDesc && msg.content.includes('开始]');
                if (_hasDescMarkers) {
                    let _tcContent = msg.content;
                    let _tcMap = {};
                    let _tcCounter = 0;
                    let _hasTools = false;

                    // Sub-pass B: 描述符格式 [ToolName开始]...[ToolName结束]
                    if (_enableDesc) {
                        let _knownTools = new Set(['Read', 'Write', 'Edit', 'Bash', 'WebSearch', 'WebFetch', '自动审稿', '压缩', '触发器', '单关卡审稿', '展开气泡', '命名会话', '创建子会话', '结束子会话', '申请审批']);
                        let _numParams = new Set(['offset', 'limit', 'timeout', 'count', 'interval_minutes', 'checkpoint_index', 'max_steps']);
                        let _boolParams = new Set(['replace_all', 'run_in_background', 'force_full', 'enable_baseline', 'cancel']);
                        let _arrParams = new Set(['allowed_domains', 'blocked_domains', 'message_ids', 'checkpoint_indices']);
                        let _enablePlanned = !!globalSettings.enable_planned_tools;
                        let _dLines = _tcContent.split('\n');
                        let _dLineStarts = [];
                        let _dPos = 0;
                        for (let _li = 0; _li < _dLines.length; _li++) { _dLineStarts.push(_dPos); _dPos += _dLines[_li].length + 1; }
                        let _dHits = [];
                        let _dI = 0;
                        let _dSeqCounter = 0;
                        while (_dI < _dLines.length) {
                            let _dS = _dLines[_dI].trim();
                            let _dTool = null;
                            let _dIsPlanned = false;
                            let _dModeSuffix = '';
                            if (_dS.startsWith('[') && _dS.length > 4) {
                                if (_enablePlanned && _dS.endsWith('立即开始]')) {
                                    let _cand = _dS.substring(1, _dS.length - 5);
                                    if (_knownTools.has(_cand)) { _dTool = _cand; _dModeSuffix = '立即'; }
                                } else if (_enablePlanned && _dS.endsWith('计划开始]')) {
                                    let _cand = _dS.substring(1, _dS.length - 5);
                                    if (_knownTools.has(_cand)) { _dTool = _cand; _dModeSuffix = '计划'; _dIsPlanned = true; }
                                } else if (_dS.endsWith('开始]')) {
                                    let _cand = _dS.substring(1, _dS.length - 3);
                                    if (_knownTools.has(_cand)) _dTool = _cand;
                                }
                            }
                            if (!_dTool) { _dI++; continue; }
                            let _dEndMk = _dModeSuffix ? '[' + _dTool + _dModeSuffix + '结束]' : '[' + _dTool + '结束]';
                            let _dDescL = [], _dParams = {}, _dCurP = null, _dCurL = [];
                            let _dFound = false, _dJ = _dI + 1;
                            while (_dJ < _dLines.length) {
                                let _dJS = _dLines[_dJ].trim();
                                if (_dJS === _dEndMk) {
                                    if (_dCurP !== null) { let _v = _dCurL.join('\n'); if (_v.startsWith('\n')) _v = _v.substring(1); if (_v.endsWith('\n')) _v = _v.substring(0, _v.length - 1); _dParams[_dCurP] = _v; }
                                    _dFound = true; break;
                                }
                                let _dPM = _dJS.match(/^\[([a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*)参数\]$/);
                                if (_dPM) {
                                    if (_dCurP !== null) { let _v = _dCurL.join('\n'); if (_v.startsWith('\n')) _v = _v.substring(1); if (_v.endsWith('\n')) _v = _v.substring(0, _v.length - 1); _dParams[_dCurP] = _v; }
                                    _dCurP = _dPM[1]; _dCurL = []; _dJ++; continue;
                                }
                                if (_dCurP === null) _dDescL.push(_dLines[_dJ]); else _dCurL.push(_dLines[_dJ]);
                                _dJ++;
                            }
                            if (!_dFound) { _dI++; continue; }
                            let _dInput = {};
                            for (let [_k, _val] of Object.entries(_dParams)) {
                                if (_numParams.has(_k)) { let _n = parseInt(_val.trim(), 10); _dInput[_k] = isNaN(_n) ? _val : _n; }
                                else if (_boolParams.has(_k)) { let _vl = _val.trim().toLowerCase(); _dInput[_k] = _vl === 'true' ? true : (_vl === 'false' ? false : _val); }
                                else if (_arrParams.has(_k)) { let _sv = _val.trim(); if (_sv.startsWith('[') && _sv.endsWith(']')) { try { _dInput[_k] = JSON.parse(_sv); } catch(e) { _dInput[_k] = _val; } } else _dInput[_k] = _val; }
                                else _dInput[_k] = _val;
                            }
                            let _dSP = _dLineStarts[_dI];
                            let _dEP = _dLineStarts[_dJ] + _dLines[_dJ].length;
                            let _dDesc = _dDescL.join('\n').trim();
                            _dSeqCounter++;
                            let _dObj = {name: _dTool, input: _dInput, _descriptor: true};
                            if (_enablePlanned) {
                                _dObj._is_planned = _dIsPlanned;
                                _dObj._tool_seq = _dSeqCounter;
                                // Extract 等待 param as metadata
                                if (_dIsPlanned && _dInput['等待'] !== undefined) {
                                    let _wRaw = String(_dInput['等待']).trim();
                                    if (_wRaw) {
                                        _dObj._wait_list = _wRaw.split(/\s+/).map(Number);
                                    }
                                    delete _dInput['等待'];
                                } else if (_dIsPlanned) {
                                    _dObj._wait_list = [];
                                }
                            }
                            _dHits.push({start: _dSP, end: _dEP, obj: _dObj, desc: _dDesc});
                            _dI = _dJ + 1;
                        }
                        for (let _hi = _dHits.length - 1; _hi >= 0; _hi--) {
                            let _h = _dHits[_hi];
                            let _ph = '\x02TC' + _tcCounter + '\x03';
                            _tcCounter++;
                            _tcMap[_ph] = _h.obj;
                            let _repl = _h.desc ? (_h.desc + '\n' + _ph) : _ph;
                            _tcContent = _tcContent.substring(0, _h.start) + _repl + _tcContent.substring(_h.end);
                            _hasTools = true;
                        }
                    }

                    // Expansion: split by placeholders, create parts
                    if (_hasTools) {
                        let parts = [];
                        let tcSeq = 0;
                        let _phKeys = Object.keys(_tcMap);
                        let _phRe = new RegExp('(' + _phKeys.map(function(k) { return k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }).join('|') + ')');
                        let _segments = _tcContent.split(_phRe);
                        for (let _seg of _segments) {
                            if (_tcMap[_seg]) {
                                tcSeq++;
                                let _tid = 'toolu_' + msg.id + '_' + tcSeq;
                                let _obj = _tcMap[_seg];
                                let _wObj = {type: 'tool_use', id: _tid, name: _obj.name, input: _obj.input};
                                if (_obj._descriptor) _wObj._descriptor = true;
                                let _w = JSON.stringify(_wObj, null, 2);
                                let _st = (window.currentSessionId === 'starred_session_virtual') ? 'adopted' : 'pending';
                                let _partEntry = {type: 'tool_use_part', content: _w, status: _st, id: 'tc-' + Math.random().toString(36).substr(2, 9)};
                                if (_obj._is_planned) { _partEntry._is_planned = true; _partEntry._wait_list = _obj._wait_list || []; }
                                if (_obj._tool_seq) _partEntry._tool_seq = _obj._tool_seq;
                                parts.push(_partEntry);
                            } else if (_seg.trim()) {
                                parts.push({type: 'text', content: _seg.trim(), id: 'tc-' + Math.random().toString(36).substr(2, 9)});
                            }
                        }
                        if (parts.length > 0 && parts.some(function(p) { return p.type === 'tool_use_part'; })) msg.content_parts = parts;
                    }
                }
            }
            if (!isHidden && !isWaiting && msg.role === 'assistant' && !msg.content_parts && msg.content && msg.content.includes('`' + '`' + '`') && !msg.is_terminal) {
                    let parts = [];
                    let regex = /`{3}([^\n]*)\n([\s\S]*?)`{3}/g;
                    let match;
                    let lastIdx = 0;
                    while ((match = regex.exec(msg.content)) !== null) {
                        if (match.index > lastIdx) parts.push({type: 'text', content: msg.content.substring(lastIdx, match.index)});
                        let lang = match[1].trim();
                        let codeStr = match[2].trim();
                        if (!globalSettings.enable_tool_inject && lang.toLowerCase() === 'correction') {
                            parts.push({type: 'correction', content: codeStr, id: 'poly-' + Math.random().toString(36).substr(2, 9)});
                        } else if (!globalSettings.enable_tool_inject && codeStr.startsWith('File:')) {
                            let blockStatus = (window.currentSessionId === 'starred_session_virtual') ? 'adopted' : 'pending';
                            parts.push({type: 'code', content: codeStr, status: blockStatus, id: 'poly-' + Math.random().toString(36).substr(2, 9)});
                   } else if (!globalSettings.enable_tool_inject && lang.toLowerCase() === 'terminal') {
                            let blockStatus = (window.currentSessionId === 'starred_session_virtual') ? 'adopted' : 'pending';
                            parts.push({type: 'terminal', content: codeStr, status: blockStatus, id:'poly-' + Math.random().toString(36).substr(2, 9)});
                        } else {
                            parts.push({type: 'text', content: match[0]});
                        }
                        lastIdx = regex.lastIndex;
                    }
                    if (lastIdx < msg.content.length) parts.push({type: 'text', content: msg.content.substring(lastIdx)});
                    if (parts.some(p => p.type === 'code' || p.type === 'correction' || p.type === 'terminal' || p.type === 'tool_use_part')) msg.content_parts = parts;
                }

                                if (msg.is_hidden && (msg.is_subagent_request || msg.is_subagent_response)) {
                    bubble.style.backgroundColor = '#f3e5f5';
                    bubble.style.border = '1px solid #ce93d8';
                    if (msg.is_collapsed) {
                        let _saLabel = msg.is_subagent_request ? 'Subagent 请求' : 'Subagent 响应';
                        let _saStatus = (msg.is_subagent_response && msg.has_subagent_sse) ? ' ✅' : '';
                        mainContentContainer.innerHTML = `<div class="summary-box" style="cursor:pointer; border-left-color:#9c27b0; background:#f3e5f5; color:#6a1b9a;" onclick="toggleMode(${index},'collapse')" title="点击展开"><b>[${_saLabel}]${_saStatus}</b> ${msg.summary || '暂无'}</div>`;
                    } else {
                    mainContentContainer.innerHTML = `<div class="content">${renderMarkdownProtected(msg.content || msg.summary || '')}</div>`;
                    if (msg.is_subagent_request) {
                        mainContentContainer.insertAdjacentHTML('beforeend', `<div style="margin:8px 0; text-align:center;"><button onclick="subagentSend('${msg.subagent_id}')" style="padding:8px 20px; background:#6a1b9a; color:#fff; border:none; border-radius:20px; cursor:pointer; font-size:13px;">🔍 发送 Subagent</button></div>`);
                    }
                    if (msg.is_subagent_response && msg.has_subagent_sse) {
                        mainContentContainer.insertAdjacentHTML('beforeend', `<div style="margin:8px 0; text-align:center;"><button onclick="subagentAdopt('${msg.subagent_id}', ${msg.id})" style="padding:8px 20px; background:#28a745; color:#fff; border:none; border-radius:20px; cursor:pointer; font-size:13px;">✅ 采纳此结果返回给CC</button></div>`);
                    }
                    }
                } else if (msg.is_hidden) {
                    mainContentContainer.innerHTML = `<div class="summary-box" style="cursor: pointer; border-left-color: #007bff; background: #e3f2fd; color: #004085;" onclick="toggleMode(${index}, 'hide')" title="点击取消隐藏"><b>[完全隐藏] 概括：</b>${msg.summary || '暂无'} <span style="font-size: 11px; opacity: 0.8;">(该气泡已从上下文中剔除)</span></div>`;
                } else if (isHidden) {
                    let unreadStyle = msg.is_unread ? 'border-left-color: #28a745; background: #e6ffed; color: #155724;' : '';
                    let unreadBadge = msg.is_unread ? '<span style="font-size:10px; background:#28a745; color:#fff; padding:1px 4px; border-radius:3px; margin-right:5px; vertical-align:middle;">未读</span>' : '';
                    mainContentContainer.innerHTML = `<div class="summary-box" style="cursor: pointer; ${unreadStyle}" onclick="toggleMode(${index}, '${msg.is_omitted ? 'omit' : 'collapse'}')" title="点击展开">${unreadBadge}<b>概括：</b>${msg.summary || '生成中...'}</div>`;
                } else if (isWaiting) {
                    mainContentContainer.innerHTML = `<div class="content"><i>（<span style="color:#007bff;">${msg.model_name || '默认模型'}</span> 等待中... 已用时 <span class="waiting-time" data-start="${msg.start_time || Date.now()/1000}">0.0</span>s）</i></div>`;
                } else if (msg.is_terminal) {
                    let safeContent = msg.content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    mainContentContainer.innerHTML = `<div class="content"><pre style="background:transparent; border:none; padding:0; margin:0; color:#333; font-family:Consolas, monospace; font-size:12px; white-space:pre-wrap; word-wrap:break-word;">${safeContent}</pre></div>`;
                } else if (msg.diff_content) {
                    mainContentContainer.innerHTML = `<div class="content">${renderMarkdownProtected(msg.diff_content)}</div>`;
                } else if (msg.content_parts) {
                    let renderParts = JSON.parse(JSON.stringify(msg.content_parts));
                    renderParts.forEach((part, pIdx) => {
                        if (part.type === 'correction') {
                            let codeStr = part.content;
                            let searchRegex = /<{4}\n([\s\S]*?)\n={4}\n([\s\S]*?)\n>{4}/g;
                            let match;
                            while ((match = searchRegex.exec(codeStr)) !== null) {
                                let searchStr = match[1];
                                let replaceStr = match[2];
                                if (searchStr.trim()) {
                                    for (let i = pIdx - 1; i >= 0; i--) {
                                        if (renderParts[i].type === 'text') {
                                            if (renderParts[i].content.includes(searchStr)) {
                                                let diffHtml = `<del style="color:#999;">${searchStr}</del><ins style="color:#28a745; text-decoration:none; font-weight:bold;">${replaceStr}</ins>`;
                                                renderParts[i].content = renderParts[i].content.replace(searchStr, diffHtml);
                                                break;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    });

                    renderParts.forEach((part, indexInParts) => {
                        if (part.type === 'text') {
                            // 内联思维链解析：检测 [思考开始]...[思考结束] 标记
                            let _itContent = part.content;
                            let _itRegex = /(?:^|\n)\[思考开始\]\n([\s\S]*?)\n\[思考结束\](?:\n|$)/g;
                            if (_itRegex.test(_itContent)) {
                                _itRegex.lastIndex = 0;
                                let _itLastIdx = 0;
                                let _itMatch;
                                let _itExpandIdx = 0;
                                while ((_itMatch = _itRegex.exec(_itContent)) !== null) {
                                    if (_itMatch.index > _itLastIdx) {
                                        let _itBefore = _itContent.substring(_itLastIdx, _itMatch.index).trim();
                                        if (_itBefore) {
                                            let _itTextDiv = document.createElement('div');
                                            _itTextDiv.className = 'content';
                                            _itTextDiv.innerHTML = renderMarkdownProtected(typeof filterProtocolMarkers === 'function' ? filterProtocolMarkers(_itBefore) : _itBefore);
                                            mainContentContainer.appendChild(_itTextDiv);
                                        }
                                    }
                                    let _itThinkContent = _itMatch[1];
                                    let _itBlock = document.createElement('div');
                                    _itBlock.className = 'inline-thinking-block';
                                    _itBlock.setAttribute('data-type', 'inline_thinking');
                                    _itBlock.style.cssText = 'margin: 4px 0; border: 1px solid #ce93d8; border-radius: 6px; background: #f3e5f5; font-size: 12px; overflow: hidden;';
                                    let _itHeader = document.createElement('div');
                                    _itHeader.style.cssText = 'padding: 4px 10px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; background: #ede7f6;';
                                    _itHeader.innerHTML = '<span style="color:#6a1b9a; font-weight:bold;"><span class="it-arrow" style="font-size:10px;">▶</span> 💭 内联思维链</span><span style="color:#999; font-size:11px;">' + (_itThinkContent.length > 50 ? '~' + (_itThinkContent.length / 3000).toFixed(2) + 'k' : '') + '</span>';
                                    let _itBody = document.createElement('div');
                                    _itBody.className = 'inline-thinking-body';
                                    _itBody.style.cssText = 'display: none; padding: 6px 10px; border-top: 1px solid #ce93d8; white-space: pre-wrap; line-height: 1.5; color: #4a148c; max-height: 300px; overflow-y: auto;';
                                    _itBody.textContent = _itThinkContent;
                                    _itExpandIdx++;
                                    _itBody.setAttribute('data-expand-id', 'it-' + msg.id + '-' + _itExpandIdx);
                                    _itHeader.onclick = function() {
                                        let isShown = _itBody.style.display !== 'none';
                                        _itBody.style.display = isShown ? 'none' : 'block';
                                        _itHeader.querySelector('.it-arrow').textContent = isShown ? '▶' : '▼';
                                    };
                                    _itBlock.appendChild(_itHeader);
                                    _itBlock.appendChild(_itBody);
                                    mainContentContainer.appendChild(_itBlock);
                                    _itLastIdx = _itMatch.index + _itMatch[0].length;
                                }
                                if (_itLastIdx < _itContent.length) {
                                    let _itAfter = _itContent.substring(_itLastIdx).trim();
                                    if (_itAfter) {
                                        let _itTextDiv = document.createElement('div');
                                        _itTextDiv.className = 'content';
                                        _itTextDiv.innerHTML = renderMarkdownProtected(typeof filterProtocolMarkers === 'function' ? filterProtocolMarkers(_itAfter) : _itAfter);
                                        mainContentContainer.appendChild(_itTextDiv);
                                    }
                                }
                            } else {
                            const textDiv = document.createElement('div');
                            textDiv.className = 'content';
                            if (part.id) textDiv.dataset.partId = part.id;
                            textDiv.innerHTML = renderMarkdownProtected(typeof filterProtocolMarkers === 'function' ? filterProtocolMarkers(part.content) : part.content);
                            mainContentContainer.appendChild(textDiv);
                            }
                        } else if (part.type === 'correction') {
                            const wrapper = document.createElement('div');
                            wrapper.className = 'code-block-wrapper correction-block';
                            wrapper.style.borderColor = 'transparent';
                            wrapper.style.backgroundColor = 'transparent';
                            wrapper.style.marginBottom = '2px';
                            
                            const header = document.createElement('div');
                            header.className = 'code-block-header';
                            header.style.backgroundColor = 'transparent';
                            header.style.borderBottom = 'none';
                            header.style.padding = '2px 5px';
                            let isExpanded = window._lastEditedCorrectionId === part.id;
                            let btnText = isExpanded ? '折叠 ▴' : '展开 ▾';
                            header.innerHTML = `<div style="display:flex; align-items:center;"><span class="cb-label" style="color:#999; font-weight:normal; font-size:11px;">💭 内部反思与修正...</span></div>
                                                <div class="cb-ops"><button class="cb-btn cb-toggle" style="border:none; background:transparent; color:#999; font-size:11px; padding:2px 5px; box-shadow:none;" onclick="toggleCodeBlock(this)">${btnText}</button></div>`;
                            
                            const contentDiv = document.createElement('div');
                            contentDiv.className = 'code-block-content' + (isExpanded ? '' : ' collapsed');
                            
                            const textarea = document.createElement('textarea');
                            textarea.style.width = '100%';
                            textarea.style.boxSizing = 'border-box';
                            textarea.style.padding = '8px';
                            textarea.style.border = '1px dashed #ccc';
                            textarea.style.borderRadius = '4px 4px 0 0';
                            textarea.style.outline = 'none';
                            textarea.style.resize = 'none';
                            textarea.style.overflow = 'hidden';
                            textarea.style.minHeight = '30px';
                            textarea.style.fontFamily = 'inherit';
                            textarea.style.fontSize = '12px';
                            textarea.style.backgroundColor = '#f8f9fa';
                            textarea.style.color = '#666';
                            textarea.value = part.content;
                            
                            textarea.addEventListener('input', function() {
                                this.style.height = 'auto';
                                this.style.height = (this.scrollHeight) + 'px';
                            });
                            // 延时触发一次以适应初始内容
                            setTimeout(() => {
                                textarea.style.height = 'auto';
                                textarea.style.height = (textarea.scrollHeight) + 'px';
                            }, 0);
                            
                            const saveBtn = document.createElement('button');
                            saveBtn.innerText = '保存批注';
                            saveBtn.style.margin = '0';
                            saveBtn.style.padding = '4px 10px';
                            saveBtn.style.fontSize = '11px';
                            saveBtn.style.cursor = 'pointer';
                            saveBtn.style.border = '1px solid #ccc';
                            saveBtn.style.borderTop = 'none';
                            saveBtn.style.backgroundColor = '#fff';
                            saveBtn.style.color = '#666';
                            saveBtn.style.borderRadius = '0 0 4px 4px';
                            saveBtn.style.width = '100%';
                            saveBtn.onclick = () => {
                                window._lastEditedCorrectionId = part.id;
                                postAction({action: 'edit_correction', msg_index: index, part_id: part.id, content: textarea.value});
                                saveBtn.innerText = '✅ 已保存';
                                setTimeout(() => saveBtn.innerText = '保存批注', 2000);
                            };
                            
                            const btnContainer = document.createElement('div');
                            btnContainer.style.textAlign = 'center';
                            btnContainer.appendChild(saveBtn);

                            contentDiv.appendChild(textarea);
                            contentDiv.appendChild(btnContainer);

                            wrapper.appendChild(header);
                            wrapper.appendChild(contentDiv);
                            mainContentContainer.appendChild(wrapper);
                        } else if (part.type === 'code') {
                            if (part.status === 'pending') hasPendingActions = true;
                            if (part.status === 'rejected' || part.status === 'failed') hasFailedOrRejected = true;
                            
                            const wrapper = document.createElement('div');
                            wrapper.className = 'code-block-wrapper';
                            wrapper.dataset.raw = encodeURIComponent(part.content);

                            let blockTypeLabel = part.content.includes('@code_config') ? '更新监听配置' : (part.content.includes('<'.repeat(4)) ? '查找替换' : '创建文件');
                            let opsHtml = '';
                            let statusLabel = '';

                            switch(part.status) {
                                case 'adopted':
                                    statusLabel = `<span style="color: var(--md-sys-color-success); font-weight: 500;">已采用</span>`;
                                    let reverseBtnHtml = part.content.includes('<'.repeat(4)) ? `<button class="cb-btn cb-reverse" onclick="reverseCodeBlock(this, ${index}, '${part.id}')">${mdIcon('swap', 14)} 反向</button>` : '';
                                    if (window.currentSessionId === 'starred_session_virtual') {
                                        opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    } else {
                                        opsHtml = `<button class="cb-btn cb-undo" onclick="undoCodeBlock(this, ${index}, '${part.id}')">${mdIcon('undo', 14)} 撤销</button>
                                                   ${reverseBtnHtml}
                                                   <button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    }
                                    break;
                                case 'rejected':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">未采用</span>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                case 'failed':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">${mdIcon('warning', 14)} 失败</span>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                default: // pending
                                    statusLabel = blockTypeLabel;
                                    opsHtml = `
                                        <button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>
                                        <button class="cb-btn cb-accept" onclick="applyCodeBlock(this, ${index}, '${part.id}')">${mdIcon('check', 14)} 采用</button>
                                        <button class="cb-btn cb-reject" onclick="rejectCodeBlock(this, ${index}, '${part.id}')">${mdIcon('close', 14)} 不采用</button>
                                    `;
                            }
                            
                            const header = document.createElement('div');
                            header.className = 'code-block-header';
                            const toggleBtn = `<button class="cb-btn cb-toggle" onclick="toggleCodeBlock(this)">${mdIcon('expand_more', 14)} 折叠</button>`;
                            let warningHtml = '';
                            if (part.warning && part.status === 'pending') {
                                warningHtml = `<span style="color: var(--md-sys-color-on-warning-container); background: var(--md-sys-color-warning-container); border: none; border-radius: var(--md-sys-shape-corner-extra-small); padding: 1px var(--md-sys-spacing-2); font-size: var(--md-sys-typescale-label-small-size); margin-left: var(--md-sys-spacing-3); display: inline-flex; align-items: center; gap: 2px;" title="生成时预检查发现问题，但您仍可尝试采用">${mdIcon('warning', 12)} 预检警告: ${part.warning}</span>`;
                            }
                            header.innerHTML = `<div style="display:flex; align-items:center;"><span class="cb-label">${statusLabel}</span>${warningHtml}</div><div class="cb-ops">${toggleBtn}${opsHtml}</div>`;
                            
                            const contentDiv = document.createElement('div');
                            contentDiv.className = 'code-block-content';
                            
                            let lang = 'diff'; // Default
                            const firstLine = part.content.split('\n')[0];
                            if (firstLine.includes('File:')) {
                                const filename = firstLine.substring(firstLine.indexOf('File:') + 5).trim().replace(/`/g, '');
                                const ext = filename.includes('.') ? filename.split('.').pop() : filename;
                                const langMap = { 'py': 'python', 'js': 'javascript', 'html': 'html', 'css': 'css', 'ts': 'typescript', 'vue': 'vue', 'json': 'json', 'md': 'markdown', 'sh': 'bash', 'yaml': 'yaml', 'yml': 'yaml', 'tex': 'latex', 'bib': 'plaintext', 'txt': 'plaintext', '@code_config': 'json' };
                                if (langMap[ext]) {
                                    lang = langMap[ext];
                                }
                            }
                            
                            contentDiv.innerHTML = renderMarkdownProtected('`'.repeat(3) + `${lang}\n${part.content}\n` + '`'.repeat(3));

                            wrapper.appendChild(header);
                            wrapper.appendChild(contentDiv);
                            mainContentContainer.appendChild(wrapper);
                        } else if (part.type === 'terminal') {
                            if (part.status === 'pending') hasPendingActions = true;
                            if (part.status === 'rejected' || part.status === 'failed') hasFailedOrRejected = true;
                            
                            const wrapper = document.createElement('div');
                            wrapper.className = 'code-block-wrapper';
                            wrapper.style.borderColor = '#ffe082';
                            wrapper.dataset.raw = encodeURIComponent(part.content);

                            let opsHtml = '';
                            let statusLabel = '';
                            switch(part.status) {
                                case 'adopted':
                                    statusLabel = `<span style="color: var(--md-sys-color-success); font-weight: 500;">已执行</span>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    break;
                                case 'rejected':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">已跳过</span>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                case 'failed':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">${mdIcon('warning', 14)} 执行失败</span>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                default:
                                    statusLabel = `${mdIcon('terminal', 14)} 终端操作`;
                                    opsHtml = `
                                        <button class="cb-btn cb-copy" onclick="copyCodeBlock(this)">${mdIcon('content_copy', 14)} 复制</button>
                                        <button class="cb-btn cb-accept" onclick="applyCodeBlock(this, ${index}, '${part.id}')">${mdIcon('play_arrow', 14)} 执行</button>
                                        <button class="cb-btn cb-reject" onclick="rejectCodeBlock(this, ${index}, '${part.id}')">${mdIcon('skip_next', 14)} 跳过</button>
                                    `;
                            }
                            
                            const header = document.createElement('div');
                            header.className = 'code-block-header';
                            header.style.backgroundColor = '#fff8e1';
                            const toggleBtn = `<button class="cb-btn cb-toggle" onclick="toggleCodeBlock(this)">${mdIcon('expand_more', 14)} 折叠</button>`;
                            let warningHtml = '';
                            if (part.warning && part.status === 'pending') {
                                warningHtml = `<span style="color: #856404; background: #fff3cd; border: 1px solid #ffeeba; border-radius: 3px; padding: 1px 5px; font-size: 11px; margin-left: 10px;">⚠️ ${part.warning}</span>`;
                            }
                            header.innerHTML = `<div style="display:flex; align-items:center;"><span class="cb-label">${statusLabel}</span>${warningHtml}</div><div class="cb-ops">${toggleBtn}${opsHtml}</div>`;
                            
                            const contentDiv = document.createElement('div');
                            contentDiv.className = 'code-block-content';
                            let safeTermContent = part.content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                            contentDiv.innerHTML = `<pre style="background-color: #fff8e1 !important; border-color: #ffe082; padding: 12px; font-family: Consolas, monospace; font-size: 13px; white-space: pre-wrap; word-wrap: break-word;">${safeTermContent}</pre>`;

                            wrapper.appendChild(header);
                            wrapper.appendChild(contentDiv);
                            mainContentContainer.appendChild(wrapper);
                        } else if (part.type === 'tool_use_part') {
                            if (part.status === 'pending') hasPendingActions = true;
                            if (part.status === 'rejected') hasFailedOrRejected = true;
                            let toolData = {};
                            try { toolData = JSON.parse(part.content); } catch(e) {}
                            let toolName = toolData.name || '?';
                            let toolId = (toolData.id || '').substring(0, 25);
                            const wrapper = document.createElement('div');
                            wrapper.className = 'code-block-wrapper';
                            if (part._is_planned) {
                                wrapper.classList.add('cb-planned');
                                wrapper.style.borderLeft = '3px solid #e91e63';
                            }
                            wrapper.style.borderColor = '#ccc';
                            wrapper.dataset.raw = encodeURIComponent(part.content);
                            let opsHtml = '', statusLabel = '';
                            switch(part.status) {
                                case 'executing':
                                    statusLabel = `<span style="color: var(--md-sys-color-primary); font-weight: 500;">${mdIcon('hourglass', 14)} 执行中</span> ${mdIcon('build', 14)}<b>${toolName}</b>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button><button class="cb-btn cb-accept" onclick="ccToolAccept(this, ${index}, '${part.id}')">${mdIcon('refresh', 14)} 重试</button>`;
                                    hasPendingActions = true;
                                    break;
                                case 'adopted':
                                    let _hasToolResult = toolData.id && toolResultMap[toolData.id] && toolResultMap[toolData.id].length > 0;
                                    if (_hasToolResult) {
                                        statusLabel = `<span style="color: var(--md-sys-color-success); font-weight: 500;">已采纳</span>${mdIcon('build', 14)}<b>${toolName}</b>`;
                                        opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button>`;
                                    } else {
                                        statusLabel = `<span style="color: var(--md-sys-color-warning); font-weight: 500;">已发送，等待返回</span>${mdIcon('build', 14)}<b>${toolName}</b>`;
                                        hasPendingActions = true;
                                        opsHtml = `<button class="cb-btn cb-accept" onclick="ccToolAccept(this, ${index}, '${part.id}')">${mdIcon('refresh', 14)} 重试</button><button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button>`;
                                    }
                                    break;
                                case 'rejected':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">已拒绝</span> ${mdIcon('build', 14)}<b>${toolName}</b>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                case 'failed':
                                    statusLabel = `<span style="color: var(--md-sys-color-error); font-weight: 500;">${mdIcon('block', 14)} 被拦截</span> ${mdIcon('build', 14)}<b>${toolName}</b>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button>`;
                                    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
                                    break;
                                default:
                                    statusLabel = `${mdIcon('build', 14)}<b>${toolName}</b>`;
                                    opsHtml = `<button class="cb-btn cb-copy" onclick="copyCodeBlock(this)" title="复制">${mdIcon('content_copy', 14)}</button><button class="cb-btn cb-accept" onclick="ccToolAccept(this, ${index}, '${part.id}')">${mdIcon('check', 14)} 采纳</button>
                                        <button class="cb-btn cb-reject" onclick="rejectCodeBlock(this, ${index}, '${part.id}')">${mdIcon('close', 14)} 拒绝</button>`;
                                    if (toolName === '申请审批') {
                                        opsHtml += `<button class="cb-btn cb-reject" onclick="rejectApproval(this, ${index}, '${part.id}')">${mdIcon('block', 14)} 拒绝审批</button>`;
                                    }
                            }
                            const header = document.createElement('div');
                            header.className = 'code-block-header';
                            header.style.backgroundColor = part._is_planned ? '#fce4ec' : '#ede7f6';
                            let ccToggleText = (part.status === 'adopted') ? (mdIcon('chevron_right', 14) + ' 展开') : (mdIcon('expand_more', 14) + ' 折叠');
                            let _seqBadge = part._tool_seq ? `<span style="background:#7e57c2;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;margin-left:6px;">#${part._tool_seq}</span>` : '';
                            let _waitBadge = '';
                            if (part._is_planned && part._wait_list && part._wait_list.length > 0) {
                                _waitBadge = `<span style="background:#e91e63;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;margin-left:4px;">等待 ${part._wait_list.map(n => '#' + n).join(' ')}</span>`;
                            }
                                                        header.innerHTML = `<div style="display:flex;align-items:center;"><span class="cb-label">${statusLabel}</span>${_seqBadge}${_waitBadge}<span style="color:#7e57c2;font-size:11px;margin-left:10px;">(${toolId})</span></div><div class="cb-ops"><button class="cb-btn cb-toggle" onclick="toggleCodeBlock(this)">${ccToggleText}</button>${opsHtml}</div>`;
                            header.style.cursor = 'pointer';
                            header.onclick = function(e) { if (!e.target.closest('button')) toggleCodeBlock(this.querySelector('.cb-toggle')); };
                            const contentDiv = document.createElement('div');
                            contentDiv.className = 'code-block-content' + (part.status === 'adopted' ? ' collapsed' : '');
                            {
                                let _e = function(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); };
                                let _metaParams = new Set(['file_path', 'offset', 'limit', 'timeout', 'replace_all', 'run_in_background', 'dangerouslyDisableSandbox', 'pages', 'force_full', 'enable_baseline', 'cancel', 'mode', 'interval_minutes', 'count', 'alarm_time', 'max_steps']);
                                let _detectedLang = null;
                                let _filePath = (toolData.input || {}).file_path || '';
                                if (_filePath) {
                                    let _ext = _filePath.includes('.') ? _filePath.split('.').pop().toLowerCase() : '';
                                    let _lm = {'py':'python','js':'javascript','html':'html','css':'css','ts':'typescript','vue':'vue','json':'json','md':'markdown','sh':'bash','yaml':'yaml','yml':'yaml','tex':'latex','rb':'ruby','go':'go','rs':'rust','java':'java','c':'c','cpp':'cpp','h':'c','hpp':'cpp','jsx':'javascript','tsx':'typescript','sql':'sql','xml':'xml','toml':'ini','cfg':'ini','ini':'ini'};
                                    if (_lm[_ext]) _detectedLang = _lm[_ext];
                                }
                                let _hp = [];
                                for (let [_pk, _pv] of Object.entries(toolData.input || {})) {
                                    let _nameHtml = '<span style="color:#7c3aed; font-size:var(--font-main); font-weight:500;">' + _e(_pk) + ':</span>';
                                    let _pvStr = String(_pv);
                                    let _highlighted = '';
                                    let _paramLang = null;
                                    if (_pk === 'command') {
                                        _paramLang = 'bash';
                                    } else if (_metaParams.has(_pk)) {
                                        _paramLang = '_meta';
                                    } else if (_detectedLang && _pvStr.includes('\n')) {
                                        _paramLang = _detectedLang;
                                    } else if (_pvStr.includes('\n') || _pvStr.length > 80) {
                                        _paramLang = 'auto';
                                    }
                                    try {
                                        if (_paramLang === '_meta') {
                                            let _trimV = _pvStr.trim();
                                            if (/^\d+(\.\d+)?$/.test(_trimV)) {
                                                _highlighted = '<span class="hljs-number">' + _e(_pvStr) + '</span>';
                                            } else if (_trimV === 'true' || _trimV === 'false') {
                                                _highlighted = '<span class="hljs-literal">' + _e(_pvStr) + '</span>';
                                            } else {
                                                _highlighted = _e(_pvStr);
                                            }
                                        } else if (_paramLang === 'auto') {
                                            _highlighted = hljs.highlightAuto(_pvStr).value;
                                        } else if (_paramLang) {
                                            _highlighted = hljs.highlight(_pvStr, {language: _paramLang, ignoreIllegals: true}).value;
                                        } else {
                                            _highlighted = _e(_pvStr);
                                        }
                                    } catch(ex) {
                                        _highlighted = _e(_pvStr);
                                    }
                                    let _valueHtml = '<code class="hljs" style="background:transparent; padding:0; font-size:var(--font-main);">' + _highlighted + '</code>';
                                    _hp.push(_nameHtml + '\n' + _valueHtml);
                                }
                                contentDiv.innerHTML = '<pre class="tool-params-hljs" style="background:#f6f8fa; padding:12px; border-radius:6px; font-size:var(--font-main); font-family:Consolas, Monaco, monospace; white-space:pre-wrap; word-wrap:break-word; margin:0; border:1px solid #ccc; line-height:1.5;">' + _hp.join('\n\n') + '</pre>';
                            }
                            wrapper.appendChild(header);
                            wrapper.appendChild(contentDiv);
                            mainContentContainer.appendChild(wrapper);
                            // 申请审批工具：在 wrapper 下方追加独立可见的拒绝按钮
                            if (toolName === '申请审批' && part.status === 'pending') {
                                let _approvalRejectDiv = document.createElement('div');
                                _approvalRejectDiv.style.cssText = 'margin: 4px 0 8px 12px; text-align: left;';
                                let _rejBtn = document.createElement('button');
                                _rejBtn.setAttribute('data-testid', 'reject-approval');
                                _rejBtn.textContent = '拒绝审批';
                                _rejBtn.style.cssText = 'background:#dc3545;color:#fff;border:none;border-radius:6px;padding:6px 16px;cursor:pointer;font-size:13px;';
                                _rejBtn.onclick = function() { rejectApproval(_rejBtn, index, part.id); };
                                _approvalRejectDiv.appendChild(_rejBtn);
                                mainContentContainer.appendChild(_approvalRejectDiv);
                            }
                                        // 内联渲染关联的 tool_result（编辑过的气泡跳过内联，让结果作为独立气泡显示）
                            let _tuId = toolData.id || '';
                            let _trResults = msg._edited ? [] : (toolResultMap[_tuId] || []);
                            if ((toolName === 'Edit' || toolName === 'Read') && toolData.id) {
                                let _arEntry = autoReadByTrigger[toolData.id];
                                if (_arEntry && _arEntry.index > index) {
                                    _trResults = _trResults.concat([_arEntry]);
                                    _inlineRenderedTRs.add(_arEntry.msg.id);
                                }
                            }
                            _trResults.forEach(({msg: trMsg, index: trIndex}) => {
                                _inlineRenderedTRs.add(trMsg.id);
                                if (trMsg._dehydrated) {
                                    var _dhTokenK = ((trMsg._content_len || 0) / 3000).toFixed(2);
                                    var _dhIsErr = (trMsg.summary || '').includes('Error');
                                    var _dhColor = _dhIsErr ? '#dc3545' : '#28a745';
                                    var _dhTimeStr = (trMsg.execution_time_s != null) ? ' ' + trMsg.execution_time_s.toFixed(1) + 's' : '';
                                    var _dhDiv = document.createElement('div');
                                    _dhDiv.style.cssText = 'margin: 2px 0 4px 12px; border-left: 3px solid ' + _dhColor + '; padding: 3px 8px; background: #f8f9fa; border-radius: 4px; font-size: 12px;';
                                    var _dhContentId = 'tr-content-' + trMsg.id;
                                    var _dhMsgId = trMsg.id;
                                    _dhDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;"><span><span class="tr-arrow" style="font-size:10px;">\u25b6</span> <span style="color:' + _dhColor + '; font-weight:bold;">' + (_dhIsErr ? '\u274c Error' : '\u2705 Result') + _dhTimeStr + '</span> <span style="color:#999;">[ID:' + trMsg.id + '] ~' + _dhTokenK + 'k</span></span><span style="display:flex; gap:2px;"><button onclick="event.stopPropagation(); copyMsg(' + trIndex + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="\u590d\u5236">\ud83d\udccb</button><button onclick="event.stopPropagation(); openEditModal(' + trMsg.id + ', \'content\')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="\u7f16\u8f91">\u270f\ufe0f</button><button onclick="event.stopPropagation(); postAction({action:\'toggle_mode\',index:' + trIndex + ',mode_type:\'hide\'})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="\u9690\u85cf">\ud83d\ude48</button><button onclick="event.stopPropagation(); postAction({action:\'delete_message\',index:' + trIndex + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="\u5220\u9664">\ud83d\uddd1\ufe0f</button></span></div>';
                                    _dhDiv.querySelector('div').onclick = function(e) { if (e.target.tagName === 'BUTTON') return; _fetchDehydratedContent(_dhMsgId); };
                                    mainContentContainer.appendChild(_dhDiv);
                                    return;
                                }
                                                if (trMsg.is_hidden) {
                                    let trHiddenDiv = document.createElement('div');
                                    trHiddenDiv.style.cssText = 'margin: 2px 0 4px 12px; border-left: 3px solid #007bff; padding: 3px 8px; background: #e3f2fd; border-radius: 4px; font-size: 11px; display:flex; justify-content:space-between; align-items:center;';
                                    trHiddenDiv.innerHTML = '<span style="color:#004085;">🙈 已隐藏 [ID:' + trMsg.id + ']</span><button onclick="postAction({action:\'toggle_mode\',index:' + trIndex + ',mode_type:\'hide\'})" style="border:none;background:#007bff;color:#fff;cursor:pointer;font-size:10px;padding:2px 6px;border-radius:3px;">取消隐藏</button>';
                                    mainContentContainer.appendChild(trHiddenDiv);
                                    return;
                                }
                                let trTokenK = ((trMsg.content || '').length / 3000).toFixed(2);
                                let trIsErr = (trMsg.summary || '').includes('Error');
                                let trColor = trIsErr ? '#dc3545' : '#28a745';
                                let trDiv = document.createElement('div');
                                trDiv.style.cssText = 'margin: 2px 0 4px 12px; border-left: 3px solid ' + trColor + '; padding: 3px 8px; background: #f8f9fa; border-radius: 04px 4px 0; font-size: 12px;';
                                let trContentId = 'tr-content-' + trMsg.id;
                                let _trTimeStr = (trMsg.execution_time_s != null) ? ' ' + trMsg.execution_time_s.toFixed(1) + 's' : '';
                                // Autoread annotation: replace generic backend text with contextual message.
                                // "by your Edit" is replaced with "by another session" when rendered inline
                                // below a non-originating tool (cross-session broadcast absorbed by Read tool).
                                let _trDisplayContent = trMsg.content || '';
                                if (trMsg.is_auto_read) {
                                    _trDisplayContent = _trDisplayContent.replace('This file was modified by your Edit.', 'This file was modified by another session.');
                                    _trDisplayContent = _trDisplayContent.replace('This file was modified.', 'This file was modified by another session.');
                                }
                                trDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" onclick="var el=document.getElementById(\'' + trContentId + '\'); if(el){el.style.display=el.style.display===\'none\'?\'block\':\'none\'; this.querySelector(\'.tr-arrow\').textContent=el.style.display===\'none\'?\'▶\':\'▼\';}">' +
                                    '<span><span class="tr-arrow" style="font-size:10px;">▶</span> <span style="color:' + trColor + '; font-weight:bold;">' + (trIsErr ? '❌ Error' : '✅ Result') + _trTimeStr + '</span> <span style="color:#999;">[ID:' + trMsg.id + '] ~' + trTokenK + 'k</span></span>' +
                                    '<span style="display:flex; gap:2px;">' +
                                        '<button onclick="event.stopPropagation(); copyMsg(' + trIndex + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="复制">📋</button>' +
                                        '<button onclick="event.stopPropagation(); openEditModal(' + trMsg.id + ', \'content\')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="编辑">✏️</button>' +
                                        '<button onclick="event.stopPropagation(); postAction({action:\'toggle_mode\',index:' + trIndex + ',mode_type:\'hide\'})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="隐藏">🙈</button>' +
                                        '<button onclick="event.stopPropagation(); postAction({action:\'delete_message\',index:' + trIndex + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="删除">🗑️</button>' +
                                    '</span>' +
                                '</div>' +
                                '<div id="' + trContentId + '" style="display:none; margin-top:4px; padding:4px; background:#fff; border-radius:4px; border:1px solid #eee; max-height:300px; overflow-y:auto;"><div class="content">' + renderMarkdownProtected(_trDisplayContent) + '</div></div>';
                                mainContentContainer.appendChild(trDiv);
                                if (trMsg.multimodal_blocks) {
                                    trMsg.multimodal_blocks.forEach(function(mb) {
                                        if (mb.type === 'image' || mb.type === 'document') {
                                            let mmDiv = document.createElement('div');
                                            mmDiv.style.cssText = 'margin: 4px 0 4px 0; padding: 4px; border-left: 3px solid #17a2b8; background: #f0f8ff; border-radius: 4px;';
                                            let src = mb.source || {};
                                            let mediaType = src.media_type || 'image/jpeg';
                                            let srcUrl = '';
                                            if (src.type === 'file' && src.path) {
                                                srcUrl = '/data/images/' + src.path;
                                            } else if (src.data) {
                                                srcUrl = 'data:' + mediaType + ';base64,' + src.data;
                                            }
                                            if (mediaType === 'application/pdf') {
                                                let embed = document.createElement('embed');
                                                embed.src = srcUrl;
                                                embed.type = 'application/pdf';
                                                embed.style.cssText = 'width: 100%; height: 600px; border-radius: 4px;';
                                                mmDiv.appendChild(embed);
                                            } else {
                                                let img = document.createElement('img');
                                                img.src = srcUrl;
                                                img.style.cssText = 'max-width: 100%; max-height: 400px; border-radius: 4px; cursor: pointer;';
                                                img.alt = 'Tool result image';
                                                img.onclick = function() { window.open(img.src, '_blank'); };
                                                mmDiv.appendChild(img);
                                            }
                                            let _trContentEl = trDiv.querySelector('#' + trContentId);
                                            if (_trContentEl) _trContentEl.appendChild(mmDiv);
                                            else trDiv.appendChild(mmDiv);
                                        }
                                    });
                                }
                            });
                            // 内联渲染关联的 subagent 请求和响应（吸附在 Agent tool_use 块下方）
                            try { if (toolData.id && subagentMap[toolData.id]) {
                                let _saEntry = subagentMap[toolData.id];
                                if (_saEntry.request) {
                                    let saReqMsg = _saEntry.request.msg;
                                    let saReqIdx = _saEntry.request.index;
                                    let saReqDiv = document.createElement('div');
                                    saReqDiv.style.cssText = 'margin: 2px 0 4px 12px; border-left: 3px solid #9c27b0; padding: 3px 8px; background: #f3e5f5; border-radius: 4px; font-size: 12px;';
                                    let saReqContentId = 'sa-req-' + saReqMsg.id;
                                    let saReqTokenK = ((saReqMsg.content || '').length / 3000).toFixed(2);
                                    let sendBtnHtml = '<button onclick="event.stopPropagation(); subagentSend(\'' + saReqMsg.subagent_id + '\')" style="border:none;background:#6a1b9a;color:#fff;cursor:pointer;font-size:10px;padding:2px 8px;border-radius:3px;margin-left:4px;">🔍 发送</button>';
                                    saReqDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" onclick="var el=document.getElementById(\'' + saReqContentId + '\'); if(el){el.style.display=el.style.display===\'none\'?\'block\':\'none\'; this.querySelector(\'.sa-arrow\').textContent=el.style.display===\'none\'?\'▶\':\'▼\';}">' +
                                        '<span><span class="sa-arrow" style="font-size:10px;">▶</span> <span style="color:#6a1b9a; font-weight:bold;">🤖 Subagent 请求</span> <span style="color:#999;">[ID:' + saReqMsg.id + '] ~' + saReqTokenK + 'k</span></span>' +
                                        '<span style="display:flex; gap:2px; align-items:center;">' + sendBtnHtml +
                                            '<button onclick="event.stopPropagation(); copyMsg(' + saReqIdx + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="复制">📋</button>' +
                                            '<button onclick="event.stopPropagation(); openEditModal(' + saReqMsg.id + ', \'content\')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="编辑">✏️</button>' +
                                            '<button onclick="event.stopPropagation(); postAction({action:\'toggle_mode\',index:' + saReqIdx + ',mode_type:\'hide\'})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="隐藏">🙈</button>' +
                                            '<button onclick="event.stopPropagation(); postAction({action:\'delete_message\',index:' + saReqIdx + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="删除">🗑️</button>' +
                                        '</span></div>' +
                                        '<div id="' + saReqContentId + '" style="display:none; margin-top:4px; padding:4px; background:#fff; border-radius:4px; border:1px solid #ce93d8; max-height:300px; overflow-y:auto;"><div class="content">' + renderMarkdownProtected(saReqMsg.content || '') + '</div></div>';
                                    mainContentContainer.appendChild(saReqDiv);
                                }
                                if (_saEntry.response) {
                                    let saRespMsg = _saEntry.response.msg;
                                    let saRespIdx = _saEntry.response.index;
                                    let saRespDiv = document.createElement('div');
                                    let saRespColor = saRespMsg.has_subagent_sse ? '#28a745' : '#fd7e14';
                                    saRespDiv.style.cssText = 'margin: 2px 0 4px 12px; border-left: 3px solid ' + saRespColor + '; padding: 3px 8px; background: #f8f9fa; border-radius: 4px; font-size: 12px;';
                                    let saRespContentId = 'sa-resp-' + saRespMsg.id;
                                    let saRespTokenK = ((saRespMsg.content || '').length / 3000).toFixed(2);
                                    let adoptBtnHtml = saRespMsg.has_subagent_sse ? '<button onclick="event.stopPropagation(); subagentAdopt(\'' + saRespMsg.subagent_id + '\', ' + saRespMsg.id + ')" style="border:none;background:#28a745;color:#fff;cursor:pointer;font-size:10px;padding:2px 8px;border-radius:3px;margin-left:4px;">✅ 返回</button>' : '<span style="color:#fd7e14;font-size:10px;">⏳ 响应中...</span>';
                                    saRespDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" onclick="var el=document.getElementById(\'' + saRespContentId + '\'); if(el){el.style.display=el.style.display===\'none\'?\'block\':\'none\'; this.querySelector(\'.sa-arrow\').textContent=el.style.display===\'none\'?\'▶\':\'▼\';}">' +
                                        '<span><span class="sa-arrow" style="font-size:10px;">▶</span> <span style="color:' + saRespColor + '; font-weight:bold;">' + (saRespMsg.has_subagent_sse ? '✅ Subagent 结果' : '⏳ Subagent 响应中') + '</span> <span style="color:#999;">[ID:' + saRespMsg.id + '] ~' + saRespTokenK + 'k</span></span>' +
                                        '<span style="display:flex; gap:2px; align-items:center;">' + adoptBtnHtml +
                                            '<button onclick="event.stopPropagation(); copyMsg(' + saRespIdx + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="复制">📋</button>' +
                                            '<button onclick="event.stopPropagation(); openEditModal(' + saRespMsg.id + ', \'content\')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="编辑">✏️</button>' +
                                            '<button onclick="event.stopPropagation(); postAction({action:\'toggle_mode\',index:' + saRespIdx + ',mode_type:\'hide\'})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="隐藏">🙈</button>' +
                                            '<button onclick="event.stopPropagation(); postAction({action:\'delete_message\',index:' + saRespIdx + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="删除">🗑️</button>' +
                                        '</span></div>' +
                                        '<div id="' + saRespContentId + '" style="display:none; margin-top:4px; padding:4px; background:#fff; border-radius:4px; border:1px solid #eee; max-height:300px; overflow-y:auto;"><div class="content">' + renderMarkdownProtected(saRespMsg.content || '') + '</div></div>';
                                    mainContentContainer.appendChild(saRespDiv);
                                }
                            }
                            } catch(_saErr) { console.error('Subagent inline render error:', _saErr); }
                        }
                    });
                } else if (msg._dehydrated && msg.is_tool_result) {
                    // Dehydrated standalone tool_result: render as normal collapsed result
                    var _dsTkK = ((msg._content_len || 0) / 3000).toFixed(2);
                    var _dsIsErr = (msg.summary || '').includes('Error');
                    var _dsColor = _dsIsErr ? '#dc3545' : '#28a745';
                    var _dsMsgId = msg.id;
                    mainContentContainer.innerHTML = '<div style="border-left: 3px solid ' + _dsColor + '; padding: 3px 8px; background: #f8f9fa; border-radius: 4px; font-size: 12px;"><div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;"><span><span class="tr-arrow" style="font-size:10px;">\u25b6</span> <span style="color:' + _dsColor + '; font-weight:bold;">' + (_dsIsErr ? '\u274c Error' : '\u2705 Result') + '</span> <span style="color:#999;">[ID:' + msg.id + '] ~' + _dsTkK + 'k</span></span></div></div>';
                    mainContentContainer.querySelector('div > div').onclick = function() { _fetchDehydratedContent(_dsMsgId); };
                } else { // Fallback for old messages or pure text
                    let _fbContent = typeof filterProtocolMarkers === 'function' ? filterProtocolMarkers(msg.content) : msg.content;
                    let _fbItRegex = /(?:^|\n)\[思考开始\]\n([\s\S]*?)\n\[思考结束\](?:\n|$)/g;
                    if (msg.role === 'assistant' && _fbItRegex.test(_fbContent)) {
                        _fbItRegex.lastIndex = 0;
                        let _fbLastIdx = 0;
                        let _fbMatch;
                        let _fbExpandIdx = 0;
                        while ((_fbMatch = _fbItRegex.exec(_fbContent)) !== null) {
                            if (_fbMatch.index > _fbLastIdx) {
                                let _fbBefore = _fbContent.substring(_fbLastIdx, _fbMatch.index).trim();
                                if (_fbBefore) {
                                    let _fbTextDiv = document.createElement('div');
                                    _fbTextDiv.className = 'content';
                                    _fbTextDiv.innerHTML = renderMarkdownProtected(_fbBefore);
                                    mainContentContainer.appendChild(_fbTextDiv);
                                }
                            }
                            let _fbThinkContent = _fbMatch[1];
                            let _fbBlock = document.createElement('div');
                            _fbBlock.className = 'inline-thinking-block';
                            _fbBlock.setAttribute('data-type', 'inline_thinking');
                            _fbBlock.style.cssText = 'margin: 4px 0; border: 1px solid #ce93d8; border-radius: 6px; background: #f3e5f5; font-size: 12px; overflow: hidden;';
                            let _fbHeader = document.createElement('div');
                            _fbHeader.style.cssText = 'padding: 4px 10px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; background: #ede7f6;';
                            _fbHeader.innerHTML = '<span style="color:#6a1b9a; font-weight:bold;"><span class="it-arrow" style="font-size:10px;">▶</span> 💭 内联思维链</span>';
                            let _fbBody = document.createElement('div');
                            _fbBody.className = 'inline-thinking-body';
                            _fbBody.style.cssText = 'display: none; padding: 6px 10px; border-top: 1px solid #ce93d8; white-space: pre-wrap; line-height: 1.5; color: #4a148c; max-height: 300px; overflow-y: auto;';
                            _fbBody.textContent = _fbThinkContent;
                            _fbExpandIdx++;
                            _fbBody.setAttribute('data-expand-id', 'itfb-' + msg.id + '-' + _fbExpandIdx);
                            _fbHeader.onclick = function() {
                                let isShown = _fbBody.style.display !== 'none';
                                _fbBody.style.display = isShown ? 'none' : 'block';
                                _fbHeader.querySelector('.it-arrow').textContent = isShown ? '▶' : '▼';
                            };
                            _fbBlock.appendChild(_fbHeader);
                            _fbBlock.appendChild(_fbBody);
                            mainContentContainer.appendChild(_fbBlock);
                            _fbLastIdx = _fbMatch.index + _fbMatch[0].length;
                        }
                        if (_fbLastIdx < _fbContent.length) {
                            let _fbAfter = _fbContent.substring(_fbLastIdx).trim();
                            if (_fbAfter) {
                                let _fbTextDiv = document.createElement('div');
                                _fbTextDiv.className = 'content';
                                _fbTextDiv.innerHTML = renderMarkdownProtected(_fbAfter);
                                mainContentContainer.appendChild(_fbTextDiv);
                            }
                        }
                    } else {
                    mainContentContainer.innerHTML = `<div class="content">${renderMarkdownProtected(_fbContent)}</div>`;
                    }
                }

                if (msg.image && !msg.is_hidden && !isHidden && !isWaiting) {
                    const imgDiv = document.createElement('div');
                    imgDiv.style.margin = '8px 0';
                    let _uImgSrc = msg.image.path ? '/data/images/' + msg.image.path : 'data:' + msg.image.mime_type + ';base64,' + msg.image.base64;
                    imgDiv.innerHTML = `<img src="${_uImgSrc}" style="max-width:100%; max-height:500px; border-radius:8px; cursor:pointer; display:block;" onclick="window.open(this.src)" title="点击查看原图">`;
                    mainContentContainer.appendChild(imgDiv);
                }

                // 独立气泡的 multimodal_blocks 图片渲染（补充内联渲染覆盖不到的场景）
                if (msg.multimodal_blocks && !msg.is_hidden && !isHidden && !isWaiting) {
                    msg.multimodal_blocks.forEach(function(mb) {
                        if (mb.type === 'image' || mb.type === 'document') {
                            let mmDiv2 = document.createElement('div');
                            mmDiv2.style.cssText = 'margin: 4px 0; padding: 4px; border-left: 3px solid #17a2b8; background: #f0f8ff; border-radius: 4px;';
                            let mmSrc = mb.source || {};
                            let mmMediaType = mmSrc.media_type || 'image/jpeg';
                            let mmSrcUrl = '';
                            if (mmSrc.type === 'file' && mmSrc.path) {
                                mmSrcUrl = '/data/images/' + mmSrc.path;
                            } else if (mmSrc.data) {
                                mmSrcUrl = 'data:' + mmMediaType + ';base64,' + mmSrc.data;
                            }
                            if (mmMediaType === 'application/pdf') {
                                let mmEmbed = document.createElement('embed');
                                mmEmbed.src = mmSrcUrl;
                                mmEmbed.type = 'application/pdf';
                                mmEmbed.style.cssText = 'width: 100%; height: 600px; border-radius: 4px;';
                                mmDiv2.appendChild(mmEmbed);
                            } else {
                                let mmImg = document.createElement('img');
                                mmImg.src = mmSrcUrl;
                                mmImg.style.cssText = 'max-width: 100%; max-height: 400px; border-radius: 4px; cursor: pointer;';
                                mmImg.alt = 'Tool result image';
                                mmImg.onclick = function() { window.open(mmImg.src, '_blank'); };
                                mmDiv2.appendChild(mmImg);
                            }
                            mainContentContainer.appendChild(mmDiv2);
                        }
                    });
                }

                // Footer logic
                let timeStr = msg.created_at ? new Date(msg.created_at * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '';
                let footer = `<div class="bubble-footer">${tags} ${timeStr} ~ ${tokenK}k Tokens</div>`;
                if (msg.role === 'assistant') {
                    const mName = msg.model_name ? formatModelDisplay(msg.model_name) : '';
                    let timingStr = '';
                    if (msg.timing) {
                        let _ts = '';
                        if (typeof msg.timing.context_t === 'number') _ts += `组装: ${msg.timing.context_t.toFixed(1)}s `;
                        if (msg.timing.payload_bytes) _ts += `↑${(msg.timing.payload_bytes/1024).toFixed(0)}kB `;
                        if (msg.timing.ttfb_retries && msg.timing.ttfb_retries.length > 0) {
                            let _retries = msg.timing.ttfb_retries;
                            let _totalRetryTime = _retries.reduce((sum, r) => sum + (r.elapsed || 0), 0);
                            _ts += `🔄×${_retries.length}(+${_totalRetryTime.toFixed(0)}s) `;
                        }
                        if (typeof msg.timing.upload_t === 'number') {
                            _ts += `上传: ${msg.timing.upload_t.toFixed(1)}s `;
                            _ts += `首字: ${msg.timing.ttfb.toFixed(1)}s `;
                            _ts += `接收: ${(msg.timing.download_t || msg.timing.download || 0).toFixed(1)}s`;
                        } else if (msg.timing.stream === false) {
                            _ts += `总用时: ${(msg.timing.ttfb + (msg.timing.download_t || msg.timing.download || 0)).toFixed(1)}s`;
                        } else {
                            _ts += `首字: ${msg.timing.ttfb.toFixed(1)}s 接收: ${(msg.timing.download_t || msg.timing.download || 0).toFixed(1)}s`;
                        }
                        timingStr = _ts;
                    }
                    let billingStr = '';
                    if (msg.billing) {
                        let b = msg.billing;
                        let parts = [];
                        if (b.input_tokens) parts.push(`入:${(b.input_tokens/1000).toFixed(2)}k`);
                        if (b.cache_read) parts.push(`缓存:${(b.cache_read/1000).toFixed(2)}k`);
                        if (b.cache_write) parts.push(`写:${(b.cache_write/1000).toFixed(2)}k`);
                        if (b.output_tokens) parts.push(`出:${(b.output_tokens/1000).toFixed(2)}k`);
                        if (b.cost_usd) parts.push(`$${b.cost_usd.toFixed(3)}`);
                        billingStr = parts.join(' ');
                        // Show prediction comparison if available
                        if (msg._billing_prediction) {
                            let p = msg._billing_prediction;
                            billingStr += ` (预:入${(p.pred_non_cached/1000).toFixed(1)}k 缓${(p.pred_cache_read/1000).toFixed(1)}k 写${(p.pred_cache_write/1000).toFixed(1)}k)`;
                        }
                    }
                    let batonStr = '';
                    if (window._autopilotActive && msg._autopilot_gen !== undefined && msg._autopilot_gen === window._autopilotGen) {
                        batonStr = '<span class="baton-marker" style="color:#dc3545;font-size:11px;font-weight:bold;"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#dc3545;margin-right:3px;animation:baton-pulse 1s infinite;vertical-align:middle;box-shadow:0 0 4px rgba(220,53,69,0.8);"></span>\u6258\u7BA1\u4E2D</span>';
                    }
                    let footerParts = [tags, batonStr, timeStr, mName, timingStr, billingStr, `~${tokenK}k`].filter(s => s);
                    footer = `<div class="bubble-footer">${footerParts.join(' | ')}</div>`;
                }

                if (hasPendingActions && !hasFailedOrRejected) {
                    footer = `<div style="text-align: right; margin-top: var(--md-sys-spacing-3); display: flex; justify-content: flex-end; gap: var(--md-sys-spacing-2);"><button class="cb-reject-all" onclick="rejectAllInBubble(this, ${index})">${mdIcon('close', 16)} 一键拒绝</button><button class="cb-accept-all" onclick="acceptAllInBubble(this, ${index})">${mdIcon('check', 16)} 一键采用</button></div>` + footer;
                }
                
                // Selection is a tonal fill rather than a swapped glyph: abstract
                // icons cannot carry the 🌟/⭐ distinction the emoji pair relied on.
                let upBtnStyle = msg.rating === 'up'
                    ? 'background: var(--md-sys-color-success-container); color: var(--md-sys-color-on-success-container);'
                    : 'opacity: var(--md-sys-state-disabled-content-opacity);';
                let downBtnStyle = msg.rating === 'down'
                    ? 'background: var(--md-sys-color-error-container); color: var(--md-sys-color-on-error-container);'
                    : 'opacity: var(--md-sys-state-disabled-content-opacity);';
                let starBtnStyle = isStarred ? 'color: var(--md-sys-color-tertiary);' : 'opacity: var(--md-sys-state-disabled-content-opacity);';

                bubble.innerHTML = `
                    <div class="ops-buttons">
                        ${globalSettings.enable_starred ? `<button style="${starBtnStyle}" onclick="postAction({action: 'toggle_star', index: ${index}})" title="${isStarred ? '取消收藏' : '收藏该上下文'}">${mdIcon('star', 14)}</button>` : ''}
                        ${msg.role === 'assistant' ? `<button onclick="postAction({action: 'retry_message', index: ${index}})" title="以当时上下文重新生成">${mdIcon('refresh', 14)}</button><button style="${upBtnStyle}" onclick="postAction({action: 'rate_message', index: ${index}, rating: 'up'})" title="好评">${mdIcon('thumb_up', 14)}</button><button style="${downBtnStyle}" onclick="postAction({action: 'rate_message', index: ${index}, rating: 'down'})" title="差评">${mdIcon('thumb_down', 14)}</button>` : ''}
                        <button onclick="toggleMode(${index}, 'hide')">${mdIcon('visibility_off', 14)} ${msg.is_hidden ? '取消隐藏' : '隐藏'}</button>
                        <button onclick="toggleMode(${index}, 'omit')">${mdIcon('inventory', 14)} ${msg.is_omitted ? '全文' : '概括'}</button>
                        <button onclick="toggleMode(${index}, 'collapse')">${mdIcon('folder', 14)} ${msg.is_collapsed ? '展开' : '折叠'}</button>
                        <button onclick="copyMsg(${index})" title="复制正文">${mdIcon('content_copy', 14)}</button>${msg.role === 'assistant' ? `<button onclick="copyPayload(${index})" title="复制发送时的上下文">${mdIcon('inventory', 14)}</button>` : ''}${msg._style_filter_original ? `<button onclick="toggleStyleDiff(${msg.id})" title="显示风格过滤差异">${mdIcon('search', 14)}</button>` : ''}
                        <button onclick="openEditModal(${msg.id}, 'content')" title="编辑气泡原文">${mdIcon('edit', 14)}</button>${globalSettings.developer_mode ? `<button onclick="openEditModal(${msg.id}, 'annotation')" title="批注 (生成Diff对比差异)">${mdIcon('brush', 14)}</button>` : ''}<button onclick="deleteMessageOptimistic(${index})" title="${msg.is_hidden ? '彻底删除' : '隐藏 (再次点击彻底删除)'}">${mdIcon('delete', 14)}</button>
                    </div>`;
                
                const idBadge = document.createElement('div');
                idBadge.className = 'msg-id-inline';
                idBadge.textContent = `[ID: ${msg.id}]`;
                bubble.appendChild(idBadge);
                bubble.appendChild(mainContentContainer);
                bubble.insertAdjacentHTML('beforeend', footer);


                
                if (!isHidden && !isWaiting) renderMathInElement(bubble, { delimiters: [{left: "$$", right: "$$", display: true}, {left: "$", right: "$", display: false}] });

                // 气泡右键菜单
                bubble.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    var menu = document.getElementById('bubble-context-menu');
                    if (!menu) return;
                    var items = '';
                    if (globalSettings.enable_starred) items += '<div class="bubble-ctx-item" onclick="postAction({action:\'toggle_star\',index:' + index + '})">' + mdIcon('star', 16) + (isStarred ? ' 取消收藏' : ' 收藏') + '</div>';
                    if (msg.role === 'assistant') items += '<div class="bubble-ctx-item" onclick="postAction({action:\'retry_message\',index:' + index + '})">' + mdIcon('refresh', 16) + ' 重试</div><div class="bubble-ctx-item" onclick="postAction({action:\'rate_message\',index:' + index + ',rating:\'up\'})">' + mdIcon('thumb_up', 16) + ' 好评</div><div class="bubble-ctx-item" onclick="postAction({action:\'rate_message\',index:' + index + ',rating:\'down\'})">' + mdIcon('thumb_down', 16) + ' 差评</div>';
                    items += '<div class="bubble-ctx-item" onclick="toggleMode(' + index + ',\'hide\')">' + mdIcon('visibility_off', 16) + (msg.is_hidden ? ' 取消隐藏' : ' 隐藏') + '</div>';
                    items += '<div class="bubble-ctx-item" onclick="toggleMode(' + index + ',\'omit\')">' + (msg.is_omitted ? mdIcon('book', 16) + ' 全文' : mdIcon('inventory', 16) + ' 概括') + '</div>';
                    items += '<div class="bubble-ctx-item" onclick="toggleMode(' + index + ',\'collapse\')">' + (msg.is_collapsed ? mdIcon('folder_open', 16) + ' 展开' : mdIcon('folder', 16) + ' 折叠') + '</div>';
                    items += '<div class="bubble-ctx-item" onclick="copyMsg(' + index + ')">' + mdIcon('content_copy', 16) + ' 复制</div>';
                    if (msg.role === 'assistant') items += '<div class="bubble-ctx-item" onclick="copyPayload(' + index + ')">' + mdIcon('inventory', 16) + ' 复制上下文</div>';
                    items += '<div class="bubble-ctx-item" onclick="openEditModal(' + msg.id + ',\'content\')">' + mdIcon('edit', 16) + ' 编辑</div>';
                    items += '<div class="bubble-ctx-item" onclick="deleteMessageOptimistic(' + index + ')">' + mdIcon('delete', 16) + ' 删除</div>';
                    // NOTE: plain assignment, not +=. Everything accumulated above is
                    // discarded and the menu only ever shows the collapse entry. Behaviour
                    // left as-is pending a decision on whether that was intentional.
                    items = '<div class="bubble-ctx-item" onclick="toggleMode(' + index + ',\'collapse\')">' + (msg.is_collapsed ? mdIcon('folder_open', 16) + ' 展开' : mdIcon('folder', 16) + ' 折叠') + '</div>';
                    menu.innerHTML = items;
                    menu.style.display = 'block';
                    menu.style.left = e.clientX + 'px';
                    menu.style.top = e.clientY + 'px';
                    var rect = menu.getBoundingClientRect();
                    if (rect.bottom > window.innerHeight) menu.style.top = (e.clientY - rect.height) + 'px';
                    if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 4) + 'px';
                });

                if (thinkingMap[msg.id]){
                    if (!window._thinkingCache) window._thinkingCache = {};
                    thinkingMap[msg.id].forEach(({msg: thMsg, index: thIndex}) => {
                        let thElHash = thMsg.id + '_' + (thMsg.content||'').length + '_' + thMsg.is_hidden + '_' + thIndex;
                        if (window._thinkingCache[thMsg.id] && window._thinkingCache[thMsg.id].hash === thElHash) {
                            fragment.appendChild(window._thinkingCache[thMsg.id].el);
                return;
                        }
                        let thTokenK = ((typeof thMsg._content_len === 'number' ? thMsg._content_len : (thMsg.content || '').length) / 3000).toFixed(2);
                        let thDiv = document.createElement('div');
                        thDiv.style.cssText = 'max-width: 85%; margin-right: auto; margin-bottom: 2px; padding: 4px 10px; border-radius: 8px 8px 2px 2px; background: #f3e5f5; border: 1px solid #ce93d8; font-size: 12px;';
                        if (thMsg.is_hidden) {
                            thDiv.style.background = '#e1bee7';
                            thDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center;"><span style="color:#6a1b9a;">🙈 思维链已隐藏 [ID:' + thMsg.id + '] ~' + thTokenK + 'k</span><span style="display:flex; gap:2px;"><button onclick="event.stopPropagation(); copyMsg(' + thIndex + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="复制">📋</button><button onclick="event.stopPropagation(); openEditModal(' + thMsg.id + ', ' + "'content'" + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="编辑">✏️</button><button onclick="event.stopPropagation(); postAction({action:' + "'toggle_mode'" + ',index:' + thIndex + ',mode_type:' + "'hide'" + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="取消隐藏">🙈</button><button onclick="event.stopPropagation(); postAction({action:' + "'delete_message'" + ',index:' + thIndex + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="删除">🗑️</button></span></div>';
                        } else {
                            let thContentId = 'th-content-' + thMsg.id;
                            thDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" onclick="var el=document.getElementById(' + "'" + thContentId + "'" + '); if(el){el.style.display=el.style.display===' + "'none'" + '?' + "'block'" + ':' + "'none'" + '; this.querySelector(' + "'.th-arrow'" + ').textContent=el.style.display===' + "'none'" + '?' + "'▶'" + ':' + "'▼'" + ';}">' +
                                '<span><span class="th-arrow" style="font-size:10px;">▶</span><span style="color:#6a1b9a; font-weight:bold;">💭 思维链</span> <span style="color:#999;">[ID:' + thMsg.id + '] ~' + thTokenK + 'k</span></span>' +
                                '<span style="display:flex; gap:2px;">' +
                                    '<button onclick="event.stopPropagation(); copyMsg(' + thIndex + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="复制">📋</button>' +
                                    '<button onclick="event.stopPropagation(); openEditModal(' + thMsg.id + ', ' + "'content'" + ')" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="编辑">✏️</button>' +
                                    '<button onclick="event.stopPropagation(); postAction({action:' + "'toggle_mode'" + ',index:' + thIndex + ',mode_type:' + "'hide'" + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="隐藏">🙈</button>' +
                                    '<button onclick="event.stopPropagation(); postAction({action:' + "'delete_message'" + ',index:' + thIndex + '})" style="border:none;background:transparent;cursor:pointer;font-size:11px;padding:1px 3px;" title="删除">🗑️</button>' +
                                '</span>' +
                            '</div>' +
                            '<div id="' + thContentId + '" style="display:none; margin-top:4px; padding:4px; background:#fce4ec; border-radius:4px; border:1px solid #f8bbd0; max-height:400px; overflow-y:auto; white-space:pre-wrap; font-size:12px; line-height:1.5;">' + (thMsg.content || '').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</div>';
                }
                        window._thinkingCache[thMsg.id] = { hash: thElHash, el: thDiv };
                        fragment.appendChild(thDiv);
                    });
                }
                bubbleCache[msg.id] = { hash: msgHash, el: bubble, msg: msg };
                fragment.appendChild(bubble);
            });
            console.timeEnd('📦 Message Loop & Hashing');

            const dom0 = performance.now();

            let targetNodes = Array.from(fragment.children);

            while (chatContainer.children.length > targetNodes.length) {
                chatContainer.removeChild(chatContainer.lastChild);
            }

            targetNodes.forEach((node, idx) => {
                if (idx < chatContainer.children.length) {
                    if (chatContainer.children[idx] !== node) {
                        chatContainer.replaceChild(node, chatContainer.children[idx]);
                    }
                } else {
                    chatContainer.appendChild(node);
                }
            });

            const dom1 = performance.now();
            socket.emit('perf_log', {label: 'DOM Injection', duration: dom1 - dom0});

            // 滚动策略：同步执行，不能用requestAnimationFrame（RAF优先级低于socket macrotask，
            // 流式更新时下一次renderChat会在RAF执行前被调用，导致isScrolledToBottom误判产生跳动）
            if (isSessionSwitched || isScrolledToBottom) {
                chatContainer.scrollTop = chatContainer.scrollHeight;
            } else {
                chatContainer.scrollTop = savedScrollTop;
            }
            updateTokenEst();

            // === Expand state preservation: restore states after rebuild ===
            if (window._expandStateMap && Object.keys(window._expandStateMap).length > 0) {
                ['tr-content-', 'th-content-', 'sa-req-', 'sa-resp-'].forEach(function(_rp) {
                    chatContainer.querySelectorAll('[id^="' + _rp + '"]').forEach(function(el) {
                        var _st = window._expandStateMap[el.id];
                        if (_st && _st.expanded) {
                            el.style.display = 'block';
                            var _arrow = el.parentElement && el.parentElement.querySelector('.tr-arrow, .th-arrow, .sa-arrow');
                            if (!_arrow && el.previousElementSibling) {
                                _arrow = el.previousElementSibling.querySelector('.tr-arrow, .th-arrow, .sa-arrow');
                            }
                            if (_arrow) _arrow.textContent = '\u25bc';
                            if (_st.scrollTop > 0) {
                                var _elRef = el;
                                setTimeout(function() { _elRef.scrollTop = _st.scrollTop; }, 0);
                            }
                        }
                    });
                });
                chatContainer.querySelectorAll('[data-expand-id]').forEach(function(el) {
                    var _eid = el.getAttribute('data-expand-id');
                    var _st = window._expandStateMap[_eid];
                    if (_st && _st.expanded) {
                        el.style.display = 'block';
                        var _arrow = el.previousElementSibling && el.previousElementSibling.querySelector('.it-arrow');
                        if (_arrow) _arrow.textContent = '\u25bc';
                        if (_st.scrollTop > 0) {
                            var _elRef = el;
                            setTimeout(function() { _elRef.scrollTop = _st.scrollTop; }, 0);
                        }
                    }
                });
            }
        }

        // === Style Filter Diff Display ===
        function _sfEscapeHtml(text) {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function computeInlineDiff(oldText, newText) {
            let result = '';
            let i = 0, j = 0;
            while (i < oldText.length || j < newText.length) {
                if (i < oldText.length && j < newText.length && oldText[i] === newText[j]) {
                    result += _sfEscapeHtml(oldText[i]);
                    i++; j++;
                } else {
                    let bestI = -1, bestJ = -1, bestCost = 999;
                    for (let di = 0; di <= 10 && i + di <= oldText.length; di++) {
                        for (let dj = 0; dj <= 10 && j + dj <= newText.length; dj++) {
                            if (di === 0 && dj === 0) continue;
                            if (i + di < oldText.length && j + dj < newText.length && oldText[i + di] === newText[j + dj]) {
                                let cost = di + dj;
                                if (cost < bestCost) { bestCost = cost; bestI = i + di; bestJ = j + dj; }
                                break;
                            }
                        }
                        if (bestCost <= di + 1) break;
                    }
                    if (bestI >= 0) {
                        if (bestI > i) result += '<del style="color:#dc3545;text-decoration:line-through;background:#fff0f0;">' + _sfEscapeHtml(oldText.substring(i, bestI)) + '</del>';
                        if (bestJ > j) result += '<ins style="color:#28a745;text-decoration:underline;background:#f0fff0;">' + _sfEscapeHtml(newText.substring(j, bestJ)) + '</ins>';
                        i = bestI; j = bestJ;
                    } else {
                        if (i < oldText.length) result += '<del style="color:#dc3545;text-decoration:line-through;background:#fff0f0;">' + _sfEscapeHtml(oldText.substring(i)) + '</del>';
                        if (j < newText.length) result += '<ins style="color:#28a745;text-decoration:underline;background:#f0fff0;">' + _sfEscapeHtml(newText.substring(j)) + '</ins>';
                        break;
                    }
                }
            }
            return result;
        }

        function toggleStyleDiff(msgId) {
            var cached = bubbleCache[msgId];
            if (!cached || !cached.msg || !cached.msg._style_filter_original) return;
            var bubble = cached.el;
            var originals = cached.msg._style_filter_original;
            var isShowing = bubble.classList.toggle('sf-showing-diff');
            var mainContent = bubble.querySelector('.bubble-main-content');
            if (!mainContent) return;
            var textDivs = mainContent.querySelectorAll('.content[data-part-id]');
            if (isShowing) {
                textDivs.forEach(function(div) {
                    var partId = div.dataset.partId;
                    if (originals[partId]) {
                        div.dataset.sfNormalHtml = div.innerHTML;
                        var currentPart = (cached.msg.content_parts || []).find(function(p) { return p.id === partId; });
                        var currentText = currentPart ? currentPart.content : '';
                        div.innerHTML = computeInlineDiff(originals[partId], currentText);
                    }
                });
            } else {
                textDivs.forEach(function(div) {
                    if (div.dataset.sfNormalHtml) {
                        div.innerHTML = div.dataset.sfNormalHtml;
                        delete div.dataset.sfNormalHtml;
                    }
                });
            }
        }
