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
            '<button class="md-icon-button md-icon-button--compact" title="插队优先" onclick="postAction({action: \'manage_queue\', manage_action: \'early\', index: '+i+'})">' + mdIcon('local_fire', 16) + '</button>' +
            '<button class="md-icon-button md-icon-button--compact" title="上移" onclick="postAction({action: \'manage_queue\', manage_action: \'up\', index: '+i+'})">' + mdIcon('arrow_upward', 16) + '</button>' +
            '<button class="md-icon-button md-icon-button--compact" title="下移" onclick="postAction({action: \'manage_queue\', manage_action: \'down\', index: '+i+'})">' + mdIcon('arrow_downward', 16) + '</button>' +
            '<button class="md-icon-button md-icon-button--compact" title="移出队列" onclick="postAction({action: \'manage_queue\', manage_action: \'del\', index: '+i+'})">' + mdIcon('close', 16) + '</button>' +
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
    if (isHeavyPanelOpen) _refreshCtxPanel();
}

/* Name kept on purpose. frontend.html's token-est button and scrollToMsg in
   editops.js both call it; renaming buys a more accurate name at the cost of
   keeping three call sites in step. */
function toggleHeavyPanel() {
    openContextPanel();
}

/**
 * Heavy-bubble list, rendered into a container.
 *
 * The header strip and the two footer buttons this used to emit are gone: the
 * title is now the unified panel's section heading, and those buttons pointed
 * at what are now adjacent sections of the same card — a button that scrolls
 * to itself.
 *
 * @param {Element} container emptied and refilled
 */
function _renderHeavyList(container) {
    if (!container) return;
    heavyMessages.sort(function(a, b) { return b.originalK - a.originalK; });
    if (heavyMessages.length === 0) {
        container.innerHTML = '<div style="padding:var(--md-sys-spacing-3); text-align:center;'
            + 'color:var(--md-sys-color-on-surface-variant);'
            + 'font-size:var(--md-sys-typescale-body-small-size);">无 &gt;5k Token 的气泡</div>';
        return;
    }
    var html = '';
    heavyMessages.forEach(function(hm) {
        var summaryText = hm.summary || '无概括';
        html += '<div class="hp-row">' +
            '<span onclick="closeContextPanel();scrollToMsg(' + (hm.scrollId || hm.id) + ')" style="flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-right:var(--md-sys-spacing-3); cursor:pointer; color:var(--md-sys-color-primary);" title="点击跳转到该气泡">[ID: ' + hm.id + '] ' + summaryText + '</span>' +
            '<span style="flex:0 0 auto; margin-right:var(--md-sys-spacing-2); color:var(--md-sys-color-on-surface-variant); font-size:var(--md-sys-typescale-label-small-size);">' + hm.originalK.toFixed(1) + 'k</span>' +
            '<div style="display:flex; gap:var(--md-sys-spacing-1); align-items:center;">' +
            '<button class="md-icon-button md-icon-button--compact" onclick="openEditSummaryModal(' + hm.index + ')" title="编辑概括">' + mdIcon('edit', 16) + '</button>' +
            '<button class="md-button md-button--compact ' + (hm.is_omitted ? 'md-button--tonal' : 'md-button--outlined') + '" onclick="postAction({action: \'toggle_mode\', index: ' + hm.index + ', mode_type: \'omit\'})" style="min-width:56px;">' + (hm.is_omitted ? '全文' : '概括') + '</button>' +
            '</div></div>';
    });
    container.innerHTML = html;
}

/**
 * Paint the send button for the busy/idle state. Appearance only.
 *
 * Renamed from setUIEnabled, which claimed more than it did: this never touches
 * .disabled, so the button greys out while staying fully clickable. The old name
 * cost something concrete — a commit message asserted the composer was "locked"
 * on the strength of it, when the real symptom is a grey button that still works.
 *
 * Deliberately still not a real disable. is_processing has a known stuck-true
 * defect (HANDOVER section four), and a genuine disable would trap the user
 * outside the composer with no way back. That change belongs after the state
 * transitions in autopilot.py and tool_accept.py are fixed, not before.
 *
 * @param {boolean} enabled false while the session is processing
 */
function setComposerTone(enabled) {
    updateSendButtonCount();
    sendButton.style.background = enabled
        ? 'var(--md-sys-color-primary)'
        : 'var(--md-sys-color-surface-container-highest)';
    sendButton.style.color = enabled
        ? 'var(--md-sys-color-on-primary)'
        : 'var(--md-sys-color-on-surface-variant)';
}

/* Unified context panel.
 *
 * Three separate top-level surfaces used to answer one question — what is my
 * context made of, and what can I compress: a 340px popover above the composer
 * (the heavy list), a 460px dialog (batch management) and an 85%-wide
 * fullscreen overlay (the composition chart). Reaching the third took two
 * clicks through the first two, and none of them could be read next to another.
 *
 * They are now three sections of one card sharing one scroll container, which
 * is the actual gain: the composition and the controls that change it are
 * visible together.
 *
 * 640px rather than the chart's old 85%: bars laid out by share of total do not
 * need the viewport, and 640 still fits the two-column button grid below.
 *
 * Every cm- element id is unchanged, so getContextManagerFilters,
 * matchesContextFilter, updateContextManagerPreview and batchContextAction all
 * keep working untouched.
 */
function closeContextPanel() {
    var ov = document.getElementById('ctx-panel-overlay');
    if (ov) ov.remove();
    isHeavyPanelOpen = false;
}

/** Repaint the two data-driven sections. Both read the same currentHistory. */
function _refreshCtxPanel() {
    if (!document.getElementById('ctx-panel-overlay')) return;
    if (typeof renderContextComposition === 'function') {
        renderContextComposition(document.getElementById('ctx-comp-body'));
    }
    _renderHeavyList(document.getElementById('ctx-heavy-body'));
    updateContextManagerPreview();
}

function openContextPanel() {
    if (document.getElementById('ctx-panel-overlay')) { closeContextPanel(); return; }
    var ov = document.createElement('div');
    ov.id = 'ctx-panel-overlay';
    ov.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;'
        + 'background:color-mix(in srgb, var(--md-sys-color-scrim) 32%, transparent);'
        + 'display:flex;justify-content:center;align-items:center;z-index:var(--md-sys-z-overlay);';
    ov.onclick = function(e) { if (e.target === this) closeContextPanel(); };
    var box = document.createElement('div');
    box.style.cssText = 'width:640px;max-width:94vw;max-height:86vh;'
        + 'background:var(--md-sys-color-surface-container-high);'
        + 'color:var(--md-sys-color-on-surface);'
        + 'border-radius:var(--md-sys-shape-corner-extra-large);'
        + 'box-shadow:var(--md-sys-elevation-level3);'
        + 'display:flex;flex-direction:column;overflow:hidden;';
    var thChecked = (typeof thinkingVisible !== 'undefined' && thinkingVisible) ? 'checked' : '';
    var _sec = 'font-size:var(--md-sys-typescale-title-small-size);'
        + 'font-weight:var(--md-sys-typescale-title-small-weight);'
        + 'color:var(--md-sys-color-on-surface);margin:0 0 var(--md-sys-spacing-2) 0;'
        + 'display:flex;align-items:center;gap:var(--md-sys-spacing-2);';
    var _card = 'background:var(--md-sys-color-surface-container-lowest);'
        + 'border-radius:var(--md-sys-shape-corner-medium);'
        + 'padding:var(--md-sys-spacing-3);margin-bottom:var(--md-sys-spacing-3);';
    box.innerHTML = '<div class="md-modal-header"><h3>' + mdIcon('bar_chart', 18) + ' 上下文</h3>'
        + '<button class="md-modal-close" data-overlay-close onclick="closeContextPanel()" title="关闭">' + mdIcon('close', 18) + '</button></div>'
        + '<div class="md-modal-body">'
        + '<div style="' + _card + '"><h4 style="' + _sec + '">' + mdIcon('bar_chart', 16) + ' 构成</h4>'
        + '<div id="ctx-comp-body"></div></div>'
        + '<div style="' + _card + '"><h4 style="' + _sec + '">' + mdIcon('inventory', 16) + ' 高负载气泡（原尺寸 &gt;5k）</h4>'
        + '<div id="ctx-heavy-body" style="max-height:180px;overflow-y:auto;"></div></div>'
        + '<div style="' + _card + '"><h4 style="' + _sec + '">' + mdIcon('settings', 16) + ' 批量管理</h4>'
        + '<div style="margin-bottom:var(--md-sys-spacing-3);"><b>筛选类型</b>' +
        '<div style="display:flex;flex-wrap:wrap;gap:var(--md-sys-spacing-2);margin-top:var(--md-sys-spacing-2);">' +
        '<label class="md-chip"><input type="checkbox" id="cm-type-thinking">' + mdIcon('psychology', 16) + ' 思维链</label>' +
        '<label class="md-chip"><input type="checkbox" id="cm-type-assistant">' + mdIcon('smart_toy', 16) + ' 回复</label>' +
        '<label class="md-chip"><input type="checkbox" id="cm-type-user">' + mdIcon('edit', 16) + ' 用户</label>' +
        '<label class="md-chip"><input type="checkbox" id="cm-type-tool">' + mdIcon('settings', 16) + ' 工具返回</label>' +
        '<label class="md-chip"><input type="checkbox" id="cm-type-image">' + mdIcon('image', 16) + ' 含有图片</label>' +
        '</div></div>' +
        '<div class="ctl-group" style="margin-bottom:var(--md-sys-spacing-3);"><b>ID 范围</b> ' +
        '<input type="number" id="cm-id-min" class="md-num" placeholder="最小" style="width:72px;"> ~ ' +
        '<input type="number" id="cm-id-max" class="md-num" placeholder="最大" style="width:72px;"></div>' +
        '<div class="ctl-group" style="margin-bottom:var(--md-sys-spacing-3);"><b>长度范围</b> ' +
        '<input type="number" id="cm-size-min" class="md-num" value="0" step="0.1" style="width:60px;"> ~ ' +
        '<input type="number" id="cm-size-max-modal" class="md-num" value="" step="0.1" style="width:60px;" placeholder="不限"> k</div>' +
        '<div id="cm-preview" style="margin-bottom:var(--md-sys-spacing-4);padding:var(--md-sys-spacing-3);background:var(--md-sys-color-surface-container);border-radius:var(--md-sys-shape-corner-small);font-size:var(--md-sys-typescale-body-small-size);color:var(--md-sys-color-on-surface-variant);">计算中...</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--md-sys-spacing-2);">' +
        '<button id="cm-btn-omit" class="md-button md-button--tonal md-button--compact" onclick="batchContextAction(\'omit\')">' + mdIcon('inventory', 16) + ' 概括 (0)</button>' +
        '<button id="cm-btn-expand" class="md-button md-button--tonal md-button--compact" onclick="batchContextAction(\'expand\')">' + mdIcon('book', 16) + ' 展开 (0)</button>' +
        '<button id="cm-btn-hide" class="md-button md-button--tonal md-button--compact" onclick="batchContextAction(\'hide\')">' + mdIcon('visibility_off', 16) + ' 隐藏 (0)</button>' +
        '<button id="cm-btn-unhide" class="md-button md-button--outlined md-button--compact" onclick="batchContextAction(\'unhide\')">' + mdIcon('visibility', 16) + ' 取消隐藏 (0)</button>' +
        '<button id="cm-btn-purge" class="md-button md-button--danger md-button--compact" onclick="batchContextAction(\'purge\')" style="grid-column:span 2;">' + mdIcon('delete_sweep', 16) + ' 彻底删除已隐藏 (0)</button></div>' +
        '<div style="border-top:1px solid var(--md-sys-color-outline-variant);padding-top:var(--md-sys-spacing-3);margin-top:var(--md-sys-spacing-3);">' +
        '<label class="settings-label" style="cursor:pointer;"><span class="settings-title">' + mdIcon('psychology', 16) + ' 本会话新思维链可见</span><input type="checkbox" id="cm-thinking-toggle" ' + thChecked + ' onchange="postAction({action:\'toggle_thinking_visible\'});showToast(\'已切换（仅影响新产生的思维链）\',\'success\')"></label>' +
        '<div class="settings-desc">只影响本会话之后新产生的思维链，已有气泡不变。未在此切换过的会话取全局设置里「新思维链默认可见」的值。</div></div>' +
        '</div></div>';
    ov.appendChild(box);
    document.body.appendChild(ov);
    ['cm-type-thinking','cm-type-assistant','cm-type-user','cm-type-tool','cm-type-image','cm-id-min','cm-id-max','cm-size-min','cm-size-max-modal'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) { el.addEventListener('change', updateContextManagerPreview); el.addEventListener('input', updateContextManagerPreview); }
    });
    isHeavyPanelOpen = true;
    _refreshCtxPanel();
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
    // innerHTML, not textContent: these labels carry inline SVG, and a
    // textContent assignment would strip the icon on the first refresh.
    var bl = {
        'cm-btn-omit': mdIcon('inventory', 16) + ' 概括 (' + ac.omit + ')',
        'cm-btn-expand': mdIcon('book', 16) + ' 展开 (' + ac.expand + ')',
        'cm-btn-hide': mdIcon('visibility_off', 16) + ' 隐藏 (' + ac.hide + ')',
        'cm-btn-unhide': mdIcon('visibility', 16) + ' 取消隐藏 (' + ac.unhide + ')',
        'cm-btn-purge': mdIcon('delete_sweep', 16) + ' 彻底删除已隐藏 (' + ac.purge + ')'
    };
    for (var bid in bl) { var b = document.getElementById(bid); if (b) b.innerHTML = bl[bid]; }
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
