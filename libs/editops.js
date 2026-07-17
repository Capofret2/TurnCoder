/* ChatApp edit operations - copy, paste, edit modal, clipboard */
var editMsgId = null;

async function copyMsg(index) {
    var res = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({action: 'get_clipboard', index: index, client_sid: window.localSid}) });
    var data = await res.json();
    navigator.clipboard.writeText(data.text);
}

async function copyPayload(index) {
    var res = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({action: 'get_payload', index: index, client_sid: window.localSid}) });
    var data = await res.json();
    if (!data.text) { showToast('无法获取该气泡的上下文', 'error'); return; }
    // Show in edit modal for manual copy (clipboard API fails on non-HTTPS)
    document.getElementById('edit-textarea').value = data.text;
    document.getElementById('edit-modal').style.display = 'flex';
    showToast('上下文已加载到编辑框，请手动全选复制', 'success');
}

async function openEditModal(msgId, type) {
    type = type || 'content';
    // Sync with server to get accurate index — currentHistory may be stale due to WebSocket throttle
    var i = -1;
    var sid = window.localSid;
    if (sid && sid !== 'starred_session_virtual') {
        try {
            var syncRes = await fetch('/api/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'get_session_data', sid: sid})
            });
            var syncData = await syncRes.json();
            if (syncData.status === 'ok' && syncData.session_data && syncData.session_data.conversation_history) {
                var freshHistory = syncData.session_data.conversation_history;
                for (var k = 0; k < freshHistory.length; k++) {
                    if (freshHistory[k].id === msgId) { i = k; break; }
                }
            }
        } catch(e) {}
    }
    // Fallback: use currentHistory if sync failed or session is virtual
    if (i === -1) {
        for (var k = 0; k < currentHistory.length; k++) {
            if (currentHistory[k].id === msgId) { i = k; break; }
        }
    }
    if (i === -1) return; // Message no longer in history
    editIdx = i;
    editType = type;
    editMsgId = msgId;
    var actionName = type === 'annotation' ? 'get_annotation' : 'get_clipboard';
    var res = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({action: actionName, index: i, client_sid: window.localSid}) });
    var data = await res.json();
    document.getElementById('edit-textarea').value = data.text;
    document.getElementById('edit-modal').style.display = 'flex';
}

async function openEditSummaryModal(i) {
    var currentSummary = currentHistory[i].summary || '';
    var newSummary = await showPromptModal('请编辑概括内容：', currentSummary);
    if (newSummary !== null) {
        postAction({action: 'edit_summary', index: i, summary: newSummary.trim()});
    }
}

function closeEditModal() { document.getElementById('edit-modal').style.display = 'none'; }

async function saveEdit() {
    // Sync with server at save time to get accurate index (modal may have been open during mutations)
    var idx = editIdx;
    var sid = window.localSid;
    if (editMsgId !== null && sid && sid !== 'starred_session_virtual') {
        try {
            var syncRes = await fetch('/api/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'get_session_data', sid: sid})
            });
            var syncData = await syncRes.json();
            if (syncData.status === 'ok' && syncData.session_data && syncData.session_data.conversation_history) {
                var freshHistory = syncData.session_data.conversation_history;
                for (var k = 0; k < freshHistory.length; k++) {
                    if (freshHistory[k].id === editMsgId) { idx = k; break; }
                }
            }
        } catch(e) {}
    }
    if (editType === 'content') {
        postAction({action: 'edit_message', index: idx, msg_id: editMsgId, content: document.getElementById('edit-textarea').value});
    } else if (editType === 'annotation') {
        postAction({action: 'edit_annotation', index: idx, msg_id: editMsgId, content: document.getElementById('edit-textarea').value});
    } else {
        postAction({action: 'edit_summary', index: idx, msg_id: editMsgId, summary: document.getElementById('edit-textarea').value});
    }
    closeEditModal();
}

function scrollToMsg(id) {
    var el = document.getElementById('msg-bubble-' + id);
    if (el) {
        el.scrollIntoView({behavior: 'smooth', block: 'center'});
        el.style.transition = 'box-shadow 0.3s';
        el.style.boxShadow = '0 0 15px rgba(0, 123, 255, 0.6)';
        setTimeout(function() { el.style.boxShadow = '0 1px 2px rgba(0,0,0,0.1)'; }, 1500);
        if (isHeavyPanelOpen) toggleHeavyPanel();
    }
}

async function subagentSend(subagentId) {
    var pidx = parseInt(localStorage.getItem('subagent_idx') || '0');
    try {
        await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'subagent_send', subagent_id: subagentId, provider_idx: pidx})
        });
    } catch(e) { showToast('Subagent 发送失败: ' + e, 'error'); }
}

async function subagentAdopt(subagentId, responseMsgId) {
    await postAction({action: 'subagent_adopt', subagent_id: subagentId, response_msg_id: responseMsgId});
    var _saIdx = currentHistory.findIndex(function(m) { return m.id === responseMsgId; });
    if (_saIdx >= 0 && !currentHistory[_saIdx].is_collapsed) {
        postAction({action: 'toggle_mode', index: _saIdx, mode_type: 'collapse'});
    }
}

function renderContextVisualization() {
    var existing = document.getElementById('ctx-viz-overlay');
    if (existing) { existing.remove(); return; }
    var cats = [
        { name: '思维链', color: '#9c27b0', items: [] },
        { name: '回复正文', color: '#1565c0', items: [] },
        { name: '用户消息', color: '#2e7d32', items: [] },
        { name: '工具返回', color: '#ef6c00', items: [] }
    ];
    currentHistory.forEach(function(m, idx) {
        if (m.is_hidden || m.is_outdated_read) return;
        var rawLen = m._content_len || (m.content || '').length;
        var effectiveLen = m.is_omitted ? ((m.summary || '').length + 30) : rawLen;
        if (m.role === 'user' && (effectiveLen / 3) < 300) effectiveLen *= 3;
        var tk = effectiveLen / 3000;
        var _mtype = classifyMessageType(m);
        var ci = _mtype === 'thinking' ? 0 : (_mtype === 'assistant' ? 1 : (_mtype === 'tool_result' ? 3 : 2));
        var scrollTarget = m.id;
        if (_mtype === 'thinking') { for (var j = idx + 1; j < currentHistory.length; j++) { var nm = currentHistory[j]; if (nm.role === 'assistant' && !((nm.model_name && nm.model_name.endsWith('(思考过程)')) || nm.cc_type === 'thinking')) { scrollTarget = nm.id; break; } } }
        else if (m.is_tool_result && m.tool_use_id) { var _tid = m.tool_use_id.substring(0, 20); for (var j2 = 0; j2 < currentHistory.length; j2++) { var cm = currentHistory[j2]; if (cm.role === 'assistant') { var found = (cm.content && cm.content.includes(_tid)); if (!found && cm.content_parts) { for (var pi = 0; pi < cm.content_parts.length; pi++) { var p = cm.content_parts[pi]; if (p.type === 'tool_use_part' && p.content && p.content.includes(_tid)) { found = true; break; } } } if (found) { scrollTarget = cm.id; break; } } } }
        if (m.is_auto_read && m.auto_read_file) { for (var j3 = idx - 1; j3 >= 0; j3--) { var em = currentHistory[j3]; if (em.role === 'assistant' && em.content_parts) { var afound = false; for (var api = 0; api < em.content_parts.length; api++) { if (em.content_parts[api].type === 'tool_use_part') { try { var atd = JSON.parse(em.content_parts[api].content); if (atd.name === 'Edit' && atd.input && atd.input.file_path === m.auto_read_file) { scrollTarget = em.id; afound = true; break; } } catch(e) {} } } if (afound) break; } } }
        cats[ci].items.push({ id: m.id, tk: tk, sum: m.summary || '', scrollTarget: scrollTarget });
    });
    var totalK = 0;
    cats.forEach(function(c) { c.totalK = c.items.reduce(function(s, i) { return s + i.tk; }, 0); totalK += c.totalK; });
    if (totalK === 0) { showToast('无可见消息', 'error'); return; }
    var ov = document.createElement('div');
    ov.id = 'ctx-viz-overlay';
    ov.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);display:flex;justify-content:center;align-items:center;z-index:2000;';
    ov.onclick = function(e) { if (e.target === this) this.remove(); };
    var box = document.createElement('div');
    box.style.cssText = 'width:85%;height:75%;background:#fff;border-radius:8px;display:flex;overflow:hidden;position:relative;';
    var hdr = document.createElement('div');
    hdr.style.cssText = 'position:absolute;top:0;left:0;right:0;height:32px;background:rgba(255,255,255,0.95);display:flex;align-items:center;padding:0 12px;font-size:12px;border-bottom:1px solid #eee;z-index:1;gap:12px;';
    hdr.innerHTML = '<b>上下文构成</b> 总: ' + totalK.toFixed(1) + 'k | ' + cats.map(function(c) { return '<span style="color:' + c.color + '">■</span>' + c.name + ' ' + c.totalK.toFixed(1) + 'k (' + (c.totalK/totalK*100).toFixed(0) + '%)'; }).join(' | ');
    var closeBtn = document.createElement('span');
    closeBtn.style.cssText = 'position:absolute;right:10px;top:4px;cursor:pointer;font-size:20px;color:#999;';
    closeBtn.innerHTML = '&times;'; closeBtn.onclick = function() { ov.remove(); };
    hdr.appendChild(closeBtn);
    box.appendChild(hdr);
    cats.forEach(function(cat) {
        if (cat.totalK === 0) return;
        var col = document.createElement('div');
        col.style.cssText = 'width:' + (cat.totalK / totalK * 100) + '%;height:100%;padding-top:32px;display:flex;flex-direction:column;box-sizing:border-box;border-right:1px solid #eee;overflow:hidden;';
        var largeItems = cat.items.filter(function(item) { return item.tk >= 1.0; });
        var smallItems = cat.items.filter(function(item) { return item.tk < 1.0; });
        var smallTotal = smallItems.reduce(function(s, i) { return s + i.tk; }, 0);
        largeItems.forEach(function(item) {
            var blk = document.createElement('div');
            blk.style.cssText = 'flex:' + Math.max(0.001, item.tk).toFixed(4) + ';min-height:1px;background:' + cat.color + ';margin:1px;border-radius:2px;cursor:pointer;display:flex;align-items:center;justify-content:center;overflow:hidden;opacity:0.8;transition:opacity 0.15s;';
            blk.onmouseover = function() { this.style.opacity = '1'; this.style.outline = '2px solid #fff'; };
            blk.onmouseout = function() { this.style.opacity = '0.8'; this.style.outline = 'none'; };
            blk.title = '[ID:' + item.id + '] ' + item.tk.toFixed(2) + 'k | ' + item.sum;
            blk.onclick = function(e) { e.stopPropagation(); ov.remove(); scrollToMsg(item.scrollTarget || item.id); };
            var lb = document.createElement('span');
            lb.style.cssText = 'color:#fff;font-size:9px;padding:0 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;';
            lb.textContent = item.tk.toFixed(1) + 'k';
            blk.appendChild(lb); col.appendChild(blk);
        });
        if (smallItems.length > 0) {
            var smBlk = document.createElement('div');
            smBlk.style.cssText = 'flex:' + Math.max(0.001, smallTotal).toFixed(4) + ';min-height:1px;background:' + cat.color + ';margin:1px;border-radius:2px;display:flex;align-items:center;justify-content:center;overflow:hidden;opacity:0.5;';
            smBlk.title = smallItems.length + ' 个小气泡 (各<1k), 合计 ' + smallTotal.toFixed(2) + 'k';
            var smLb = document.createElement('span');
            smLb.style.cssText = 'color:#fff;font-size:8px;padding:0 2px;white-space:nowrap;';
            smLb.textContent = smallItems.length + '个<1k=' + smallTotal.toFixed(1) + 'k';
            smBlk.appendChild(smLb); col.appendChild(smBlk);
        }
        box.appendChild(col);
    });
    ov.appendChild(box); document.body.appendChild(ov);
}
