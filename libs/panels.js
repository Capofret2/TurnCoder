/* ChatApp panel rendering - queue, token estimation, heavy panel, UI state */

function renderQueue(queue, isPaused) {
    queueArea.style.display = queue.length > 0 ? 'block' : 'none';
    document.getElementById('pause-btn').style.display = queue.length > 0 ? 'inline-block' : 'none';
    document.getElementById('pause-btn').innerText = isPaused ? '继续' : '暂停';
    queueArea.innerHTML = '<div style="font-size:11px; color:#666;">队列 ('+queue.length+'):</div>';
    queue.forEach(function(item, i) {
        var div = document.createElement('div');
        div.className = 'queue-item';
        div.innerHTML = '<span class="queue-content">[ID:'+item.id+'] '+item.content+'</span>' +
            '<div class="queue-ops">' +
            '<button onclick="postAction({action: \'manage_queue\', manage_action: \'early\', index: '+i+'})">🔥</button>' +
            '<button onclick="postAction({action: \'manage_queue\', manage_action: \'up\', index: '+i+'})">⬆️</button>' +
            '<button onclick="postAction({action: \'manage_queue\', manage_action: \'down\', index: '+i+'})">⬇️</button>' +
            '<button onclick="postAction({action: \'manage_queue\', manage_action: \'del\', index: '+i+'})">❌</button>' +
            '</div>';
        queueArea.appendChild(div);
    });
}

function updateTokenEst() {
    var total = 0;
    heavyMessages = [];
    currentHistory.forEach(function(m, index) {
        if (m.is_hidden || m.is_outdated_read) return;
        var originalC = m.diff_content ? m.diff_content.length : (m._content_len || (m.content || '').length);
        if (m.role === 'user' && (originalC/3) < 300) originalC *= 3;
        var currentC = m.is_omitted ? ((m.summary || '').length + 30) : originalC;
        var originalK = originalC / 3000;
        if (originalK > 5) {
            var scrollId = m.id;
            if (m.is_auto_read && m.auto_read_file) {
                for (var j = index - 1; j >= 0; j--) {
                    var em = currentHistory[j];
                    if (em.role === 'assistant' && em.content_parts) {
                        var found = false;
                        for (var pi = 0; pi < em.content_parts.length; pi++) {
                            if (em.content_parts[pi].type === 'tool_use_part') {
                                try { var td = JSON.parse(em.content_parts[pi].content); if (td.name === 'Edit' && td.input && td.input.file_path === m.auto_read_file) { scrollId = em.id; found = true; break; } } catch(e) {}
                            }
                        }
                        if (found) break;
                    }
                }
            }
            heavyMessages.push({index: index, id: m.id, role: m.role, originalK: originalK, summary: m.summary, is_omitted: m.is_omitted, scrollId: scrollId});
        }
        total += currentC;
    });
    var iLen = userInput.value.length;
    if ((iLen/3) < 300) iLen *= 3;
    var finalTokens = ((total + iLen) / 3000) + codeTokens;
    tokenEst.innerText = finalTokens.toFixed(1) + 'k ▾';
    if (isHeavyPanelOpen) renderHeavyPanel();
}

function toggleHeavyPanel() {
    isHeavyPanelOpen = !isHeavyPanelOpen;
    document.getElementById('heavy-panel').style.display = isHeavyPanelOpen ? 'block' : 'none';
    if (isHeavyPanelOpen) renderHeavyPanel();
}

function renderHeavyPanel() {
    var panel = document.getElementById('heavy-panel');
    heavyMessages.sort(function(a, b) { return b.originalK - a.originalK; });
    var html = '';
    if (heavyMessages.length === 0) {
        html += '<div style="padding:15px; text-align:center; color:#999; font-size:12px;">无 >5k Token 的气泡记录</div>';
    } else {
        html += '<div style="padding:8px 12px; font-weight:bold; font-size:13px; border-bottom:1px solid #eee; background:#f8f9fa;">高负载气泡清单 (原尺寸 >5k)</div>';
        heavyMessages.forEach(function(hm) {
            var summaryText = hm.summary || '无概括';
            html += '<div style="display:flex; justify-content:space-between; align-items:center; padding: 8px 12px; border-bottom:1px solid #f0f0f0; font-size:12px; transition: background 0.2s;" onmouseover="this.style.background=\'#f1f8ff\'" onmouseout="this.style.background=\'transparent\'">' +
                '<span onclick="scrollToMsg(' + (hm.scrollId || hm.id) + ')" style="flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-right:10px; cursor:pointer; color:#007bff; text-decoration:underline;" title="点击跳转到该气泡">[ID: ' + hm.id + '] ' + summaryText + '</span>' +
                '<div style="display:flex; gap: 4px;">' +
                '<button onclick="openEditSummaryModal(' + hm.index + ')" style="padding:2px 6px; font-size:11px; border:1px solid #bbb; border-radius:4px; background:#fff; cursor:pointer;" title="编辑概括">✏️</button>' +
                '<button onclick="postAction({action: \'toggle_mode\', index: ' + hm.index + ', mode_type: \'omit\'})" style="padding:2px 10px; font-size:11px; border:1px solid #bbb; border-radius:4px; background:' + (hm.is_omitted ? '#ffc107' : '#fff') + '; cursor:pointer; min-width: 48px;">' + (hm.is_omitted ? '全文' : '概括') + '</button>' +
                '</div></div>';
        });
    }
    html += '<div style="padding: 10px; border-top: 1px solid #eee; text-align: center; background: #f8f9fa;">' +
        '<button onclick="openContextManager()" style="padding: 6px 12px; background: #17a2b8; color: #fff; border: 1px solid #138496; border-radius: 4px; cursor: pointer; font-size: 12px; width: 100%; font-weight: bold;">🎛️ 管理上下文</button>' +
        '<button onclick="renderContextVisualization()" style="padding: 6px 12px; background: #5e35b1; color: #fff; border: 1px solid #4527a0; border-radius: 4px; cursor: pointer; font-size: 12px; width: 100%; font-weight: bold; margin-top: 6px;">📊 上下文构成可视化</button>' +
        '</div>';
    panel.innerHTML = html;
}

function setUIEnabled(enabled) {
    updateSendButtonCount();
    sendButton.style.background = enabled ? '#007bff' : '#6c757d';
    sendButton.style.color = '#fff';
}

function openContextManager() {
    var existing = document.getElementById('ctx-mgr-overlay');
    if (existing) { existing.remove(); return; }
    var ov = document.createElement('div');
    ov.id = 'ctx-mgr-overlay';
    ov.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;justify-content:center;align-items:center;z-index:2000;';
    ov.onclick = function(e) { if (e.target === this) this.remove(); };
    var box = document.createElement('div');
    box.style.cssText = 'width:460px;background:#fff;border-radius:8px;padding:20px;box-shadow:0 4px 20px rgba(0,0,0,0.3);';
    var thChecked = (typeof thinkingVisible !== 'undefined' && thinkingVisible) ? 'checked' : '';
    box.innerHTML = '<h3 style="margin:0 0 15px 0;">🎛️ 上下文批量管理</h3>' +
        '<div style="margin-bottom:12px;"><b>筛选类型</b><br>' +
        '<label style="margin-right:10px;"><input type="checkbox" id="cm-type-thinking"> 💭 思维链</label>' +
        '<label style="margin-right:10px;"><input type="checkbox" id="cm-type-assistant"> 💬 回复</label><br>' +
        '<label style="margin-right:10px;"><input type="checkbox" id="cm-type-user"> 👤 用户</label>' +
        '<label style="margin-right:10px;"><input type="checkbox" id="cm-type-tool"> 🔧 工具返回</label><br>' +
        '<label><input type="checkbox" id="cm-type-image"> 🖼️ 含有图片</label></div>' +
        '<div style="margin-bottom:12px;"><b>ID 范围</b> ' +
        '<input type="number" id="cm-id-min" placeholder="最小" style="width:80px;"> ~ ' +
        '<input type="number" id="cm-id-max" placeholder="最大" style="width:80px;"></div>' +
        '<div style="margin-bottom:12px;"><b>长度范围</b> ' +
        '<input type="number" id="cm-size-min" value="0" step="0.1" style="width:60px;"> ~ ' +
        '<input type="number" id="cm-size-max-modal" value="" step="0.1" style="width:60px;" placeholder="不限"> k</div>' +
        '<div id="cm-preview" style="margin-bottom:15px;padding:8px;background:#f8f9fa;border-radius:4px;font-size:12px;color:#666;">计算中...</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
        '<button id="cm-btn-omit" onclick="batchContextAction(\'omit\')" style="padding:8px;background:#ffc107;color:#000;border:none;border-radius:4px;cursor:pointer;font-weight:bold;">📦 概括 (0)</button>' +
        '<button id="cm-btn-expand" onclick="batchContextAction(\'expand\')" style="padding:8px;background:#28a745;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;">📖 展开 (0)</button>' +
        '<button id="cm-btn-hide" onclick="batchContextAction(\'hide\')" style="padding:8px;background:#007bff;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;">🙈 隐藏 (0)</button>' +
        '<button id="cm-btn-unhide" onclick="batchContextAction(\'unhide\')" style="padding:8px;background:#6c757d;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;">👁️ 取消隐藏 (0)</button>' +
        '<button id="cm-btn-purge" onclick="batchContextAction(\'purge\')" style="padding:8px;background:#dc3545;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;grid-column:span 2;">🗑️ 彻底删除已隐藏 (0)</button></div>' +
        '<div style="border-top:1px solid #eee;padding-top:10px;margin-top:12px;">' +
        '<label><input type="checkbox" id="cm-thinking-toggle" ' + thChecked + ' onchange="postAction({action:\'toggle_thinking_visible\'});showToast(\'已切换（仅影响新产生的思维链）\',\'success\')"> 💭 新思维链默认可见（不影响已有气泡）</label></div>' +
        '<div style="text-align:right;margin-top:12px;"><button onclick="document.getElementById(\'ctx-mgr-overlay\').remove()" style="padding:6px 15px;cursor:pointer;">关闭</button></div>';
    ov.appendChild(box);
    document.body.appendChild(ov);
    ['cm-type-thinking','cm-type-assistant','cm-type-user','cm-type-tool','cm-type-image','cm-id-min','cm-id-max','cm-size-min','cm-size-max-modal'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) { el.addEventListener('change', updateContextManagerPreview); el.addEventListener('input', updateContextManagerPreview); }
    });
    updateContextManagerPreview();
}

function getContextManagerFilters() {
    var types = [];
    if (document.getElementById('cm-type-thinking').checked) types.push('thinking');
    if (document.getElementById('cm-type-assistant').checked) types.push('assistant');
    if (document.getElementById('cm-type-user').checked) types.push('user');
    if (document.getElementById('cm-type-tool').checked) types.push('tool_result');
    if (document.getElementById('cm-type-image').checked) types.push('has_image');
    var sizeMaxEl = document.getElementById('cm-size-max-modal');
    var sizeMaxVal = sizeMaxEl ? parseFloat(sizeMaxEl.value) : NaN;
    return {
        types: types,
        id_min: parseInt(document.getElementById('cm-id-min').value) || null,
        id_max: parseInt(document.getElementById('cm-id-max').value) || null,
        size_min_k: parseFloat(document.getElementById('cm-size-min').value) || 0,
        size_max_k: isNaN(sizeMaxVal) ? null : sizeMaxVal
    };
}

function matchesContextFilter(m, filters) {
    var mtype = classifyMessageType(m);
    var hasImg = !!(m._has_image || m.image || m.multimodal_blocks);
    var wantImage = filters.types.indexOf('has_image') >= 0;
    var filterTypes = filters.types.filter(function(t) { return t !== 'has_image'; });
    if (filters.types.length > 0) {
        var typeMatch = filterTypes.length > 0 && filterTypes.indexOf(mtype) >= 0;
        var imageMatch = wantImage && hasImg;
        if (!typeMatch && !imageMatch) return false;
    }
    if (filters.id_min !== null && (m.id || 0) < filters.id_min) return false;
    if (filters.id_max !== null && (m.id || 0) > filters.id_max) return false;
    var contentLen = (typeof m._content_len === 'number') ? m._content_len : (m.content || '').length;
    if (filters.size_min_k > 0 && (contentLen / 3000) < filters.size_min_k) return false;
    if (filters.size_max_k !== null && filters.size_max_k !== undefined && (contentLen / 3000) > filters.size_max_k) return false;
    return true;
}

function updateContextManagerPreview() {
    var filters = getContextManagerFilters();
    var count = 0, totalK = 0;
    var ac = {omit: 0, expand: 0, hide: 0, unhide: 0, purge: 0};
    currentHistory.forEach(function(m) {
        if (!matchesContextFilter(m, filters)) return;
        count++;
        var cl = (typeof m._content_len === 'number') ? m._content_len : (m.content || '').length;
        totalK += cl / 3000;
        if (!m.is_omitted) ac.omit++;
        if (m.is_omitted) ac.expand++;
        if (!m.is_hidden) ac.hide++;
        if (m.is_hidden) ac.unhide++;
        if (m.is_hidden) ac.purge++;
    });
    var el = document.getElementById('cm-preview');
    if (el) el.innerHTML = '匹配: <b>' + count + '</b> 个气泡, 约 <b>' + totalK.toFixed(1) + '</b>k tokens';
    var bl = {'cm-btn-omit':'📦 概括 ('+ac.omit+')','cm-btn-expand':'📖 展开 ('+ac.expand+')','cm-btn-hide':'🙈 隐藏 ('+ac.hide+')','cm-btn-unhide':'👁️ 取消隐藏 ('+ac.unhide+')','cm-btn-purge':'🗑️ 彻底删除已隐藏 ('+ac.purge+')'};
    for (var bid in bl) { var b = document.getElementById(bid); if (b) b.textContent = bl[bid]; }
}

async function batchContextAction(action) {
    var filters = getContextManagerFilters();
    var actionableCount = 0;
    currentHistory.forEach(function(m) {
        if (!matchesContextFilter(m, filters)) return;
        if (action === 'omit' && !m.is_omitted) actionableCount++;
        else if (action === 'expand' && m.is_omitted) actionableCount++;
        else if (action === 'hide' && !m.is_hidden) actionableCount++;
        else if (action === 'unhide' && m.is_hidden) actionableCount++;
        else if (action === 'purge' && m.is_hidden) actionableCount++;
    });
    var names = {omit:'概括', expand:'展开', hide:'隐藏', unhide:'取消隐藏', purge:'彻底删除已隐藏'};
    if (actionableCount === 0) { showToast('没有可执行的气泡（均已处于目标状态）', 'error'); return; }
    try {
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'batch_context_manage', filters: filters, batch_action: action, client_sid: window.localSid})
        });
        var data = await res.json();
        showToast('已对 ' + (data.count || 0) + ' 个气泡执行「' + names[action] + '」', 'success');
        // 不关闭弹窗，允许用户连续操作
        // 乐观更新 currentHistory 中匹配的气泡状态，立即刷新预览
        if (data.state) {
            clientLastStateVersion = 0;
            handleStateUpdate(data.state);
        }
        // 等待 currentHistory 被状态更新刷新后，重新计算预览
        setTimeout(updateContextManagerPreview, 400);
    } catch(e) { showToast('操作失败: ' + e, 'error'); }
}
