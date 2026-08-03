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

/**
 * Draw the context composition into a container as four horizontal bars.
 *
 * This was a separate 85%-wide fullscreen overlay of four vertical columns.
 * Two things were wrong with that shape: it needed the whole viewport to be
 * legible, and it was a third top-level surface for one question — what is my
 * context made of — that the heavy-bubble list beside it already half answers.
 * It now renders inline in the unified context panel.
 *
 * Colours are the semantic roles the main view already gives these four kinds
 * of bubble (UI_CONVENTIONS section two), so a block here and the bubble it
 * points at read as the same thing rather than two unrelated palettes.
 *
 * Every fill carries its on-* pair. The old code hardcoded color:#fff, which
 * under the dark scheme puts white text on warning's light yellow — the exact
 * trap section two describes, and one that survives the light theme by luck.
 *
 * @param {Element} container emptied and refilled; no-op when absent
 */
function renderContextComposition(container) {
    if (!container) return;
    var cats = [
        { name: '思维链', bg: 'var(--md-sys-color-tertiary)', fg: 'var(--md-sys-color-on-tertiary)', items: [] },
        { name: '回复正文', bg: 'var(--md-sys-color-primary)', fg: 'var(--md-sys-color-on-primary)', items: [] },
        { name: '用户消息', bg: 'var(--md-sys-color-success)', fg: 'var(--md-sys-color-on-success)', items: [] },
        { name: '工具返回', bg: 'var(--md-sys-color-warning)', fg: 'var(--md-sys-color-on-warning)', items: [] }
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
    if (totalK === 0) {
        // Written into the container, not raised as a toast. This renders inline
        // now; a toast plus an empty box reads as the panel being broken.
        container.innerHTML = '<div style="color:var(--md-sys-color-on-surface-variant);'
            + 'font-size:var(--md-sys-typescale-body-small-size);">无可见消息</div>';
        return;
    }
    container.innerHTML = '<div style="font-size:var(--md-sys-typescale-body-small-size);'
        + 'color:var(--md-sys-color-on-surface-variant);margin-bottom:var(--md-sys-spacing-2);">'
        + '总计 <b style="color:var(--md-sys-color-on-surface);">' + totalK.toFixed(1) + 'k</b>'
        + '　点击色块跳转到对应气泡</div>';
    cats.forEach(function(cat) {
        if (cat.totalK === 0) return;
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:var(--md-sys-spacing-2);'
            + 'margin-bottom:var(--md-sys-spacing-1);';
        var lb = document.createElement('span');
        lb.style.cssText = 'flex:0 0 104px;font-size:var(--md-sys-typescale-label-small-size);'
            + 'color:var(--md-sys-color-on-surface-variant);white-space:nowrap;';
        lb.textContent = cat.name + ' ' + cat.totalK.toFixed(1) + 'k ('
            + (cat.totalK / totalK * 100).toFixed(0) + '%)';
        row.appendChild(lb);
        // The bar takes the category's share of the total width, so the four rows
        // stay comparable with each other instead of each filling its own line.
        var bar = document.createElement('div');
        bar.style.cssText = 'flex:0 0 ' + (cat.totalK / totalK * 100).toFixed(2) + '%;'
            + 'display:flex;height:22px;gap:1px;min-width:3px;';
        var largeItems = cat.items.filter(function(item) { return item.tk >= 1.0; });
        var smallItems = cat.items.filter(function(item) { return item.tk < 1.0; });
        var smallTotal = smallItems.reduce(function(s, i) { return s + i.tk; }, 0);
        largeItems.forEach(function(item) {
            var blk = document.createElement('div');
            blk.style.cssText = 'flex:' + Math.max(0.001, item.tk).toFixed(4) + ';min-width:3px;'
                + 'background:' + cat.bg + ';color:' + cat.fg + ';'
                + 'border-radius:var(--md-sys-shape-corner-extra-small);cursor:pointer;'
                + 'display:flex;align-items:center;justify-content:center;overflow:hidden;'
                + 'font-size:9px;white-space:nowrap;';
            blk.title = '[ID:' + item.id + '] ' + item.tk.toFixed(2) + 'k | ' + item.sum;
            blk.textContent = item.tk.toFixed(1);
            // Close first: the panel覆盖 the chat area, so jumping without closing
            // scrolls to a bubble the user cannot see.
            blk.onclick = function(e) {
                e.stopPropagation();
                if (typeof closeContextPanel === 'function') closeContextPanel();
                scrollToMsg(item.scrollTarget || item.id);
            };
            bar.appendChild(blk);
        });
        if (smallItems.length > 0) {
            // Mixed toward transparent rather than given a different colour, so it
            // still reads as belonging to this category.
            var smBlk = document.createElement('div');
            smBlk.style.cssText = 'flex:' + Math.max(0.001, smallTotal).toFixed(4) + ';min-width:3px;'
                + 'background:color-mix(in srgb, ' + cat.bg + ' 45%, transparent);'
                + 'border-radius:var(--md-sys-shape-corner-extra-small);';
            smBlk.title = smallItems.length + ' 个小气泡（各 <1k），合计 ' + smallTotal.toFixed(2) + 'k';
            bar.appendChild(smBlk);
        }
        row.appendChild(bar);
        container.appendChild(row);
    });
}
