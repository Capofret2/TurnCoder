/* ChatApp code block operations - adopt/reject/undo/reverse/copy */

function toggleCodeBlock(btn) {
    var wrapper = btn.closest('.code-block-wrapper');
    var content = wrapper.querySelector('.code-block-content');
    var scrollBefore = chatContainer.scrollTop;
    content.classList.toggle('collapsed');
    chatContainer.scrollTop = scrollBefore;
    if (wrapper.classList.contains('correction-block')) {
        btn.innerText = content.classList.contains('collapsed') ? '展开 ▾' : '折叠 ▴';
        if (!content.classList.contains('collapsed')) {
            var ta = content.querySelector('textarea');
            if (ta) { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; }
        }
    } else {
        btn.innerHTML = content.classList.contains('collapsed')
            ? mdIcon('chevron_right', 14) + ' 展开'
            : mdIcon('expand_more', 14) + ' 折叠';
    }
}

function copyCodeBlock(btn) {
    var wrapper = btn.closest('.code-block-wrapper');
    var rawCode = decodeURIComponent(wrapper.dataset.raw);
    navigator.clipboard.writeText(rawCode);
    // innerHTML on both ends: the label carries an inline SVG, and reading it
    // back with innerText would drop the icon when the timer restores it.
    var oldHtml = btn.innerHTML;
    btn.innerHTML = mdIcon('check', 14) + ' 已复制';
    setTimeout(function() { btn.innerHTML = oldHtml; }, 2000);
}

async function applyCodeBlock(btn, index, partId) {
    var wrapper = btn.closest('.code-block-wrapper');
    var rawCode = decodeURIComponent(wrapper.dataset.raw);
    btn.innerHTML = mdIcon('hourglass', 14) + ' 处理中';
    btn.disabled = true;
    try {
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'apply_block', block_text: rawCode, index: index, part_id: partId})
        });
        var data = await res.json();
        if (data.success) {
            btn.innerHTML = mdIcon('check_circle', 14) + ' 已采用';
            wrapper.classList.add('accepted');
            var rejectBtn = wrapper.querySelector('.cb-reject');
            if (rejectBtn) rejectBtn.disabled = true;
            if (index !== undefined && partId) {
                await postAction({action: 'update_block_status', index: index, part_id: partId, status: 'adopted'});
            }
            if (data.warning) {
                var msgId = currentHistory[index].id;
                postAction({action: 'add_only', text: '应用代码操作后语法警告 (ID: ' + msgId + '): ' + data.warning});
            }
        } else {
            var readableError = formatBlockError(data);
            var msgId2 = currentHistory[index].id;
            btn.innerHTML = mdIcon('warning', 14) + ' 采用失败';
            btn.title = readableError;
            btn.disabled = true;
            wrapper.style.opacity = '0.7';
            var rejectBtn2 = wrapper.querySelector('.cb-reject');
            if (rejectBtn2) rejectBtn2.disabled = true;
            var bubble = wrapper.closest('.message-bubble');
            if (bubble) { var acceptAll = bubble.querySelector('.cb-accept-all'); if (acceptAll) acceptAll.style.display = 'none'; }
            if (index !== undefined && partId) postAction({action: 'update_block_status', index: index, part_id: partId, status: 'failed'});
            var bt = String.fromCharCode(96).repeat(3);
            postAction({action: 'add_only', text: '应用代码操作失败 (ID: ' + msgId2 + '):\n' + readableError + '\n\n' + bt + '\n' + rawCode + '\n' + bt});
        }
    } catch(e) {
        btn.innerHTML = mdIcon('error', 14) + ' 异常';
        btn.disabled = true;
        wrapper.style.opacity = '0.7';
    }
}

function rejectCodeBlock(btn, index, partId) {
    var wrapper = btn.closest('.code-block-wrapper');
    btn.innerHTML = mdIcon('close', 14) + ' 已不采用';
    var acceptBtn = wrapper.querySelector('.cb-accept');
    if (acceptBtn) acceptBtn.disabled = true;
    btn.disabled = true;
    wrapper.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
    var bubble = wrapper.closest('.message-bubble');
    if (bubble) { var acceptAll = bubble.querySelector('.cb-accept-all'); if (acceptAll) acceptAll.style.display = 'none'; }
    if (index !== undefined && partId) postAction({action: 'update_block_status', index: index, part_id: partId, status: 'rejected'});
}

// 前端不再管理重试逻辑——所有重要的工具执行控制由后端排序引擎负责
if (typeof window.toolWaitTimes === 'undefined') {
    window.toolWaitTimes = {};
}

async function ccToolAccept(btn, index, partId, isRetry = false) {
    var wrapper = btn.closest('.code-block-wrapper');
    var rawCode = decodeURIComponent(wrapper.dataset.raw);

    try {
        var toolData = JSON.parse(rawCode);
        postAction({action: 'cc_accept_tool', tool_json: toolData, index: index, part_id: partId, is_retry: isRetry});
    } catch(e) {
        btn.innerHTML = mdIcon('error', 14) + ' 解析失败';
        return;
    }

    btn.innerHTML = mdIcon('hourglass', 14) + ' 队列排队中';
    btn.disabled = true;
    btn.classList.add('cc-wait-btn');
    btn.dataset.partId = partId;
    btn.dataset.index = index;
    btn.dataset.raw = encodeURIComponent(rawCode);
    window.toolWaitTimes[partId] = Date.now();

    var rejectBtn = wrapper.querySelector('.cb-reject');
    if (rejectBtn) rejectBtn.disabled = true;
}

function formatBlockError(errObj) {
    if (!errObj) return '未知错误';
    var lines = [];
    if (errObj.error_code || errObj.error) lines.push((errObj.error_code ? '[' + errObj.error_code + '] ' : '') + (errObj.error || ''));
    if (errObj.target) lines.push('目标文件: ' + errObj.target);
    if (errObj.details) lines.push('详情: ' + errObj.details);
    if (errObj.context) lines.push('上下文: ' + errObj.context);
    return lines.filter(Boolean).join('\n');
}

async function acceptAllInBubble(btnElem, index) {
    // 统一由后端排序引擎处理，前端只发一次请求
    btnElem.disabled = true;
    btnElem.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
    btnElem.innerHTML = mdIcon('hourglass', 16) + ' 后端执行中';
    try {
        await postAction({action: 'cc_accept_all', index: index});
    } catch(e) {
        console.error('cc_accept_all failed:', e);
    }
    // 无需轮询DOM：后端会按序执行每个工具并推送状态更新
    setTimeout(function() {
        btnElem.disabled = false;
        btnElem.style.opacity = '1';
        btnElem.innerHTML = mdIcon('check', 16) + ' 一键采用本气泡内全部代码操作';
    }, 2000);
}

async function rejectAllInBubble(btnElem, index) {
    var msg = currentHistory[index];
    if (!msg || !msg.content_parts) return;
    btnElem.disabled = true;
    btnElem.style.opacity = 'var(--md-sys-state-disabled-content-opacity)';
    btnElem.innerHTML = mdIcon('hourglass', 16) + ' 拒绝中';
    var pendingParts = msg.content_parts.filter(function(p) {
        return (p.type === 'code' || p.type === 'terminal' || p.type === 'tool_use_part') && p.status === 'pending';
    });
    for (var i = 0; i < pendingParts.length; i++) {
        var part = pendingParts[i];
        if (part.id) {
            await postAction({action: 'update_block_status', index: index, part_id: part.id, status: 'rejected'});
        }
    }
    btnElem.innerHTML = mdIcon('close', 16) + ' 已全部拒绝';
    setTimeout(function() {
        btnElem.disabled = false;
        btnElem.style.opacity = '1';
        btnElem.innerHTML = mdIcon('close', 16) + ' 一键拒绝本气泡内全部操作';
    }, 2000);
}

async function undoCodeBlock(btn, index, partId) {
    btn.innerHTML = mdIcon('hourglass', 14) + ' 撤销中';
    btn.disabled = true;
    try {
        await postAction({action: 'undo_code_block', index: index, part_id: partId});
    } catch(e) {
        showToast('撤销异常：' + e.toString(), 'error');
        btn.innerHTML = mdIcon('undo', 14) + ' 撤销';
        btn.disabled = false;
    }
}

async function reverseCodeBlock(btn, index, partId) {
    var wrapper = btn.closest('.code-block-wrapper');
    var rawCode = decodeURIComponent(wrapper.dataset.raw);
    var lines = rawCode.split('\n');
    var strippedLines = lines.map(function(l) { return l.trim(); });
    var startIdx = strippedLines.indexOf('<'.repeat(4));
    var sepIdx = strippedLines.indexOf('='.repeat(4));
    var endIdx = strippedLines.indexOf('>'.repeat(4));
    if (startIdx === -1 || sepIdx === -1 || endIdx === -1) { showToast('非标准查找替换块，无法反向。', 'error'); return; }
    var searchLines = lines.slice(startIdx + 1, sepIdx);
    var replaceLines = lines.slice(sepIdx + 1, endIdx);
    var newLines = lines.slice(0, startIdx + 1).concat(replaceLines).concat(lines.slice(sepIdx, sepIdx + 1)).concat(searchLines).concat(lines.slice(endIdx));
    var reverseCode = newLines.join('\n');
    btn.innerHTML = mdIcon('hourglass', 14) + ' 反向中';
    btn.disabled = true;
    try {
        // 先将状态设回pending，避免幂等性保护阻止反向操作
        await postAction({action: 'update_block_status', index: index, part_id: partId, status: 'pending'});
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'apply_block', block_text: reverseCode, index: index, part_id: partId})
        });
        var data = await res.json();
        if (data.success) {
            await postAction({action: 'update_block_status', index: index, part_id: partId, status: 'pending'});
        } else {
            // 反向失败，恢复已采用状态
            await postAction({action: 'update_block_status', index: index, part_id: partId, status: 'adopted'});
            showToast('反向替换失败: ' + formatBlockError(data), 'error');
            btn.innerHTML = mdIcon('swap', 14) + ' 反向';
            btn.disabled = false;
        }
    } catch(e) {
        showToast('反向操作异常：' + e.toString(), 'error');
        btn.innerHTML = mdIcon('swap', 14) + ' 反向';
        btn.disabled = false;
    }
}

function rejectApproval(btn, index, partId) {
    var reason = prompt('拒绝原因：') || '';
    btn.disabled = true;
    postAction({action: 'cc_reject_approval', index: index, part_id: partId, reason: reason});
}

// MutationObserver: 确保申请审批工具的拒绝按钮始终存在
(function() {
    function _ensureApprovalButtons() {
        var chat = document.getElementById('chat-container');
        if (!chat) return;
        var labels = chat.querySelectorAll('.cb-label');
        for (var i = 0; i < labels.length; i++) {
            var lbl = labels[i];
            if (lbl.textContent.indexOf('申请审批') === -1) continue;
            var wrapper = lbl.closest('.code-block-wrapper');
            if (!wrapper) continue;
            var nextEl = wrapper.nextElementSibling;
            if (nextEl && nextEl.getAttribute('data-approval-reject') === '1') continue;
            var bubble = wrapper.closest('.message-bubble');
            if (!bubble) continue;
            var allBubbles = chat.querySelectorAll('.message-bubble');
            var msgIndex = -1;
            for (var j = 0; j < allBubbles.length; j++) {
                if (allBubbles[j] === bubble) { msgIndex = j; break; }
            }
            var partId = '';
            try {
                var ops = wrapper.querySelector('.cb-ops');
                if (ops) {
                    var acceptBtn = ops.querySelector('.cb-accept');
                    if (acceptBtn) {
                        var onclickStr = acceptBtn.getAttribute('onclick') || '';
                        var pidMatch = onclickStr.match(/,\s*'([^']+)'\s*\)/);
                        if (pidMatch) partId = pidMatch[1];
                    }
                }
            } catch(e) {}
            if (msgIndex < 0 || !partId) continue;
            var opsDiv = wrapper.querySelector('.cb-ops');
            if (!opsDiv) continue;
            var acceptBtnCheck = opsDiv.querySelector('.cb-accept');
            if (!acceptBtnCheck || acceptBtnCheck.disabled) continue;
            var div = document.createElement('div');
            div.setAttribute('data-approval-reject', '1');
            div.style.cssText = 'margin: 4px 0 8px 12px; text-align: left;';
            var btn = document.createElement('button');
            btn.textContent = '拒绝审批';
            btn.style.cssText = 'background:#dc3545;color:#fff;border:none;border-radius:6px;padding:6px 16px;cursor:pointer;font-size:13px;';
            btn.onclick = (function(_idx, _pid) {
                return function() { rejectApproval(btn, _idx, _pid); };
            })(msgIndex, partId);
            div.appendChild(btn);
            wrapper.parentNode.insertBefore(div, wrapper.nextSibling);
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            setInterval(_ensureApprovalButtons, 500);
        });
    } else {
        setInterval(_ensureApprovalButtons, 500);
    }
})();
