/* ChatApp user actions - send, add, image upload, autopilot, sidebar, rename */

var deepThinkLevel = 0; // 0=思考一(无), 1=思考二(中), 2=思考三(极限)
function cycleDeepThink() {
    deepThinkLevel = (deepThinkLevel + 1) % 3;
    var labels = ['思考一', '思考二', '思考三'];
    document.getElementById('deep-think-label').textContent = labels[deepThinkLevel];
}

async function linkCC() {
    try {
        showToast('正在寻找存活的 CC 实例...', 'success');
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'link_cc', client_sid: window.localSid})
        });
        var data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
        } else {
            showToast(data.message, 'error');
        }
        // 第二次调用：等效于连续按两次
        var res2 = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'link_cc', client_sid: window.localSid})
        });
        var data2 = await res2.json();
        if (data2.status === 'ok') {
            showToast(data2.message, 'success');
        } else {
            showToast(data2.message, 'error');
        }
    } catch(e) {
        showToast('请求异常: ' + e, 'error');
    }
}

function send(isOffline) {
    isOffline = isOffline || false;
    var val = userInput.value.trim();
    var steps = parseInt(document.getElementById('max-steps').value) || 1;
    var parallel = document.getElementById('parallel-mode').checked;
    var modelCheckboxes = document.querySelectorAll('.model-cb:checked');
    var isDeepThink = (typeof deepThinkLevel !== 'undefined') ? deepThinkLevel : 0;
    var models = Array.from(modelCheckboxes).map(function(cb) { return cb.value; });
    if (models.length === 0) {
        showToast('请先在模型列表中选择至少一个模型', 'error');
        return;
    }
    userInput.value = '';
    userInput.style.height = 'auto';
    document.getElementById('model-dropdown').style.display = 'none';
    postAction({action: 'send_message', text: val, models: models, steps: steps, parallel: parallel, is_offline: isOffline, is_deep_think: isDeepThink});
}

function addOnly() {
    var val = userInput.value.trim();
    if (val) { userInput.value = ''; userInput.style.height = 'auto'; postAction({action: 'add_only', text: val}); }
}

function addOnlyAssistant() {
    var val = userInput.value.trim();
    if (val) { userInput.value = ''; userInput.style.height = 'auto'; postAction({action: 'add_only', text: val, role: 'assistant'}); }
}

function handleImageUpload(input) {
    var file = input.files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) {
        showToast('图片大小不能超过 10MB', 'error');
        input.value = '';
        return;
    }
    var reader = new FileReader();
    reader.onload = function(e) {
        var dataUrl = e.target.result;
        var parts = dataUrl.split(',');
        var mimeMatch = parts[0].match(/:(.*?);/);
        var mimeType = mimeMatch ? mimeMatch[1] : 'image/png';
        var base64 = parts[1];
        postAction({action: 'add_image', image_data: base64, mime_type: mimeType});
    };
    reader.readAsDataURL(file);
    input.value = '';
}

function toggleAutopilot() {
    if (document.getElementById('autopilot-btn').innerText.indexOf('停止托管') >= 0) {
        postAction({action: 'stop_autopilot'});
    } else {
        var val = userInput.value.trim();
        var turns = parseInt(document.getElementById('autopilot-turns').value) || 3;
        var maxK = parseFloat(document.getElementById('autopilot-max-k').value) || 8.0;
        var modelCheckboxes = document.querySelectorAll('.model-cb:checked');
        var models = Array.from(modelCheckboxes).map(function(cb) { return cb.value; });
        var arcModel = models.find(function(m) { return stripComposite(m).startsWith('[ARC3]'); });
        var llmModel = models.find(function(m) { return !stripComposite(m).startsWith('[ARC3]'); });
        if (models.length === 0) { showToast('请至少选择一个模型', 'error'); return; }
        if (models.length > 2 || (models.length === 2 && (!arcModel || !llmModel))) {
            showToast('托管模式仅支持单模型，或【1个ARC3环境 + 1个大模型】组合', 'error'); return;
        }
        var isDeepThink = (typeof deepThinkLevel !== 'undefined') ? deepThinkLevel : 0;
        userInput.value = '';
        userInput.style.height = 'auto';
        document.getElementById('model-dropdown').style.display = 'none';
        postAction({action: 'start_autopilot', text: val, models: models, turns: turns, max_k: maxK, is_deep_think: isDeepThink});
    }
}

// 托管运行中修改步数立即生效：监听 autopilot-turns 输入框变化
(function() {
    var turnsInput = document.getElementById('autopilot-turns');
    if (turnsInput) {
        turnsInput.addEventListener('change', function(e) {
            if (!e.isTrusted) return;
            var btn = document.getElementById('autopilot-btn');
            if (btn && btn.innerText.indexOf('停止托管') >= 0) {
                var newTurns = parseInt(turnsInput.value);
                if (!isNaN(newTurns) && newTurns >= 0) {
                    postAction({action: 'update_autopilot_turns', turns: newTurns});
                }
            }
        });
    }
})();

function toggleSidebar() {
    var sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('collapsed');
    localStorage.setItem('sidebar_collapsed', sidebar.classList.contains('collapsed'));
}

async function renameSession(sid, oldName) {
    var newName = await showPromptModal('请输入新的对话名称：', oldName);
    if (newName !== null && newName.trim() !== '') {
        postAction({action: 'rename_session', sid: sid, name: newName.trim()});
    }
}

async function restartServer() {
    try {
        showToast('正在进行语法检查，请稍候...', 'success');
        const res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'restart_server'})
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
            setTimeout(() => window.location.reload(), 2000);
        } else {
            showToast('重启被拦截: ' + data.message, 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e, 'error');
    }
}

async function exportSnapshot() {
    try {
        showToast('正在打包仓库快照...', 'success');
        const res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'export_snapshot'})
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
        } else {
            showToast('导出失败: ' + data.message, 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e, 'error');
    }
}

async function clearLogs() {
    try {
        showToast('正在清理日志...', 'success');
        const res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'clear_logs'})
        });
        const data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
        } else {
            showToast(data.message, 'error');
        }
    } catch(e) {
        showToast('请求异常: ' + e, 'error');
    }
}

async function safeRestart() {
    try {
        showToast('正在进行语法预检...', 'success');
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'restart_server'})
        });
        var data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
            setTimeout(function() { window.location.reload(); }, 2000);
        } else {
            showToast(data.message, 'error');
        }
    } catch (e) {
        showToast('请求异常: ' + e, 'error');
    }
}

async function exportSnapshot() {
    try {
        showToast('正在打包导出快照，请稍候...', 'success');
        var res = await fetch('/api/action', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action: 'export_snapshot'})
        });
        var data = await res.json();
        if (data.status === 'ok') {
            showToast(data.message, 'success');
        } else {
            showToast(data.message, 'error');
        }
    } catch (e) {
        showToast('导出异常: ' + e, 'error');
    }
}
