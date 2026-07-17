/* ChatApp code monitoring - file scan, save, code context stats */

async function refreshCodeContextStats() {
    if (codeConfig.paths.length === 0) {
        codeTokens = 0;
        document.getElementById('code-token-display').innerText = '';
        updateTokenEst();
        return;
    }
    try {
        var res = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({action: 'get_code_context', client_sid: window.localSid}) });
        var data = await res.json();
        codeTokens = data.tokens || 0;
        document.getElementById('code-token-display').innerText = ' (' + codeTokens.toFixed(2) + 'k)';
        updateTokenEst();
    } catch(e) { console.error(e); }
}

function openCodeMonitor() {
    document.getElementById('cm-paths').value = codeConfig.paths.join('\n');
    document.querySelectorAll('#cm-extensions input[type="checkbox"]').forEach(function(cb) {
        cb.checked = codeConfig.extensions.includes(cb.value);
    });
    document.getElementById('cm-custom-ext').value = codeConfig.custom_extensions || '';
    document.getElementById('cm-tree-limit').value = codeConfig.tree_limit_k !== undefined ? codeConfig.tree_limit_k : 10;
    if (codeConfig.paths.length > 0) { scanCodeFiles(); }
    else { document.getElementById('cm-file-list').innerHTML = '请配置路径后扫描。'; }
    document.getElementById('code-monitor-modal').style.display = 'flex';
}

function toggleAllFiles(checked) {
    document.querySelectorAll('.cm-file-cb').forEach(function(cb) { cb.checked = checked; });
}

async function scanCodeFiles() {
    var paths = document.getElementById('cm-paths').value.split('\n').map(function(p) { return p.trim(); }).filter(function(p) { return p; });
    var checkboxes = document.querySelectorAll('#cm-extensions input[type="checkbox"]:checked');
    var exts = Array.from(checkboxes).map(function(cb) { return cb.value; });
    var custom = document.getElementById('cm-custom-ext').value;
    if (custom) exts = exts.concat(custom.split(',').map(function(e) { return e.trim(); }).filter(function(e) { return e; }));
    if (paths.length === 0 || exts.length === 0) {
        document.getElementById('cm-file-list').innerHTML = '路径和后缀不能为空。';
        return;
    }
    document.getElementById('cm-file-list').innerHTML = '扫描中...';
    try {
        var res = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({action: 'scan_files', paths: paths, extensions: exts}) });
        var data = await res.json();
        var list = document.getElementById('cm-file-list');
        list.innerHTML = '';
        if (data.files && data.files.length > 0) {
            data.files.forEach(function(f) {
                var isChecked = codeConfig.selected_files.includes(f.abs_path) ? 'checked' : '';
                list.innerHTML += '<div style="margin-bottom: 6px; word-break: break-all;"><label style="cursor:pointer;"><input type="checkbox" class="cm-file-cb" value="' + f.abs_path + '" ' + isChecked + '> ' + f.display + '</label></div>';
            });
        } else {
            list.innerHTML = '未找到匹配的文件。';
        }
    } catch(e) {
        document.getElementById('cm-file-list').innerHTML = '扫描失败: ' + e;
    }
}

async function saveCodeMonitor() {
    codeConfig.paths = document.getElementById('cm-paths').value.split('\n').map(function(p) { return p.trim(); }).filter(function(p) { return p; });
    var checkboxes = document.querySelectorAll('#cm-extensions input[type="checkbox"]:checked');
    var exts = Array.from(checkboxes).map(function(cb) { return cb.value; });
    codeConfig.custom_extensions = document.getElementById('cm-custom-ext').value;
    if (codeConfig.custom_extensions) exts = exts.concat(codeConfig.custom_extensions.split(',').map(function(e) { return e.trim(); }).filter(function(e) { return e; }));
    codeConfig.extensions = exts;
    codeConfig.tree_limit_k = parseFloat(document.getElementById('cm-tree-limit').value) || 10;
    var fileCbs = document.querySelectorAll('.cm-file-cb:checked');
    codeConfig.selected_files = Array.from(fileCbs).map(function(cb) { return cb.value; });
    localStorage.setItem('llm_code_config', JSON.stringify(codeConfig));
    await postAction({action: 'update_code_config', config: codeConfig});
    await refreshCodeContextStats();
    document.getElementById('code-monitor-modal').style.display = 'none';
}
