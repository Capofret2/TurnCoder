/* ChatApp code block operations - adopt/reject/undo/reverse/copy */

function toggleCodeBlock(btn) {
    var wrapper = btn.closest('.code-block-wrapper');
    var content = wrapper.querySelector('.code-block-content');
    var scrollBefore = chatContainer.scrollTop;
    content.classList.toggle('collapsed');
    chatContainer.scrollTop = scrollBefore;
    if (wrapper.classList.contains('correction-block')) {
        btn.innerHTML = content.classList.contains('collapsed')
            ? '展开 ' + mdIcon('expand_more', 12)
            : '折叠 ' + mdIcon('expand_less', 12);
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

/**
 * Abort an in-flight tool call. Stops autopilot and the queue behind it.
 *
 * No confirmation dialog. This is the stop-loss action: by the time a user
 * reaches for it they want it to happen now, and an extra gate works against
 * the intent. What it does do is disable the accept and retry buttons in the
 * same row immediately, so the round trip cannot be spent dispatching another
 * execution of the very being cancelled.
 *
 * The backend side is cooperative — Python cannot safely kill a thread, and
 * local executors run in bare daemon threads — so a step already inside an
 * executor may still finish. Its result is discarded on arrival. Nothing
 * further is scheduled either way, which is the part the user asked for.
 *
 * @param {HTMLElement} btn the clicked button
 * @param {number} index history index of the owning message
 * @param {string} partId content_part id of the tool call
 */
async function ccToolAbort(btn, index, partId) {
    var wrapper = btn.closest('.code-block-wrapper');
    btn.innerHTML = mdIcon('hourglass', 14) + ' 中止中';
    btn.disabled = true;
    if (wrapper) {
        var _sibs = wrapper.querySelectorAll('.cb-accept, .cb-reject:not(.cb-abort)');
        for (var i = 0; i < _sibs.length; i++) _sibs[i].disabled = true;
    }
    // scope 'one': this settles only this call. The siblings behind it are
    // released as normal and autopilot keeps running, which is what a button
    // sitting on a single row should mean.
    await postAction({action: 'cc_abort_tool', index: index, part_id: partId, scope: 'one'});
}

/**
 * Abort every unsettled tool call in one bubble, and stop autopilot with them.
 *
 * Separate from the per-row button because the effect is categorically larger:
 * it clears the approved queue and halts the autopilot loop. A per-row control
 * claiming that reach was the granularity lie this pair replaces — the button's
 * position said "this one" while the backend stopped everything.
 *
 * Confirmed for the same reason. The counts come back from the server because
 * "how many queued calls went with it" is the one part of the outcome that is
 * not visible on screen.
 *
 * @param {HTMLElement} btn the clicked button
 * @param {number} index history index of the owning message
 */
async function ccAbortAll(btn, index) {
    var ok = await showConfirmModal(
        '中止本气泡内全部未完成的工具调用？\n\n'
        + '托管会一并停止，排队中的工具调用会被取消。\n'
        + '已经进入执行器的那一步可能仍会跑完，但结果会被丢弃。',
        '中止全部');
    if (!ok) return;
    var _part = null;
    var _bubble = btn.closest('.message-bubble');
    if (_bubble) {
        var _ab = _bubble.querySelector('[data-testid="abort-tool"]');
        // The backend resolves the session from any one part in the bubble; the
        // first in-flight row is the natural choice and always exists when this
        // button is rendered.
        if (_ab) {
            var _m = /ccToolAbort\(this,\s*\d+,\s*'([^']+)'/.exec(_ab.getAttribute('onclick') || '');
            if (_m) _part = _m[1];
        }
    }
    if (!_part) { showToast('找不到可中止的工具调用', 'error'); return; }
    btn.innerHTML = mdIcon('hourglass', 16) + ' 中止中';
    btn.disabled = true;
    try {
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'cc_abort_tool', index: index, part_id: _part,
                                  scope: 'all', client_sid: window.localSid})
        });
        var d = await res.json();
        if (!res.ok) { showToast('中止失败: ' + (d.message || res.status), 'error'); return; }
        if (d.state) { clientLastStateVersion = 0; handleStateUpdate(d.state); }
        var msg = '已中止当前工具调用';
        if (d.dropped) msg += '，另取消 ' + d.dropped + ' 个排队中的调用';
        if (d.autopilot_stopped) msg += '，托管已停止';
        showToast(msg, 'success');
    } catch (e) {
        showToast('中止异常: ' + e, 'error');
    }
}

/**
 * Re-run a tool call that already returned, after an explicit confirmation.
 *
 * The dialog is load-bearing rather than polite. accept_tool refuses to queue a
 * call that already has a tool_result, so a retry has to delete that result
 * first — the previous return is gone for good — and for Edit, Write or Bash the
 * side effect runs a second time. The tool name goes into the prompt because
 * retrying Read and retrying Bash are not remotely the same risk.
 *
 * Dispatches through ccToolAccept with is_retry set, which is the parameter that
 * has been threaded from here to accept_tool all along without being read.
 *
 * @param {HTMLElement} btn the clicked button
 * @param {number} index history index of the owning message
 * @param {string} partId content_part id of the tool call
 * @param {string} toolName shown in the confirmation
 */
/* Tools whose re-execution changes nothing locally, so a retry needs no gate.
 *
 * The criterion is side effects, not speed. Read, Grep and Glob only read;
 * WebSearch and WebFetch only issue a request. The worst outcome of running any
 * of them twice is a fresher result.
 *
 * Everything absent from this set is assumed to have effects, which is the safe
 * default: Bash, Edit and Write genuinely repeat theirs, 创建子会话 starts a
 * second session, and the review tools spend another API call.
 *
 * Adding a name here is an assertion that re-running it is harmless. Get that
 * wrong and the user believes there is a confirmation where there is none. */
var CC_RETRY_NO_CONFIRM = {Read: 1, Grep: 1, Glob: 1, WebSearch: 1, WebFetch: 1};

async function ccToolRetry(btn, index, partId, toolName) {
    if (CC_RETRY_NO_CONFIRM[toolName]) {
        ccToolAccept(btn, index, partId, true);
        return;
    }
    var ok = await showConfirmModal(
        '重新执行「' + (toolName || '该工具') + '」？\n\n'
        + '本次已有的返回结果会被删除，无法恢复。\n'
        + '若该工具有副作用（Edit / Write / Bash 等），副作用会再发生一次。',
        '重新执行');
    if (!ok) return;
    ccToolAccept(btn, index, partId, true);
}

/**
 * Reject an approval request, with the reason collected in-app.
 *
 * This was a native prompt(), the last one in the project. That dialog blocks the
 * render thread, cannot follow the theme, and — the part that actually mattered —
 * can be permanently suppressed by the user in several browsers, after which it
 * returns null with no interaction at all.
 *
 * One deliberate behaviour change: cancelling now aborts the rejection. The old
 * form wrote `prompt(...) || ''`, so a cancel — or a suppressed dialog — rejected
 * the tool with an empty reason and the user never got to type anything. Do not
 * "restore" the || '': an empty reason is a valid answer, but only when the user
 * chose to give one.
 */
async function rejectApproval(btn, index, partId) {
    var reason = await showPromptModal('拒绝原因（可留空）：', '');
    if (reason === null) return;
    btn.disabled = true;
    postAction({action: 'cc_reject_approval', index: index, part_id: partId, reason: reason});
}


