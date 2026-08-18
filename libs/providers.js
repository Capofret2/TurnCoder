/* ChatApp provider management modal - add/edit/delete providers and models */

var _providerData = [];
var _providerTestResults = {};
var _webSearchProviders = [];
var _wsDragIdx = -1;

function openProviderModal() {
    fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'get_providers'})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.status === 'ok') {
            _providerData = d.providers || [];
            var ws = d.web_search || {};
            if (Array.isArray(ws)) {
                _webSearchProviders = ws;
            } else if (ws && typeof ws === 'object' && ws.provider) {
                _webSearchProviders = [ws];
            } else {
                _webSearchProviders = [];
            }
            _providerTestResults = {};
            renderProviderList();
            document.getElementById('provider-modal').style.display = 'flex';
        } else {
            showToast(d.message || '加载供应商列表失败', 'error');
        }
    })
    .catch(function(e) {
        showToast('网络错误: ' + e.message, 'error');
    });
}

function closeProviderModal() {
    document.getElementById('provider-modal').style.display = 'none';
}

function _renderWebSearchCard(container) {
    var card = document.createElement('div');
    card.className = 'provider-card';

    var hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex; align-items:center; justify-content:space-between; margin-bottom:var(--md-sys-spacing-2);';
    hdr.innerHTML = '<span style="font-weight:500;">搜索供应商 (按优先级排序)</span>' +
        '<button onclick="_addWebSearchProvider()" class="prov-btn-add-model" title="添加搜索供应商" style="padding:2px 8px;">+</button>';
    card.appendChild(hdr);

    if (_webSearchProviders.length === 0) {
        var hint = document.createElement('div');
        hint.style.cssText = 'color:#888; font-size:12px; padding:8px 0;';
        hint.textContent = '未配置搜索供应商，将使用内置 Serper 默认密钥';
        card.appendChild(hint);
    }

    _webSearchProviders.forEach(function(sp, idx) {
        var row = document.createElement('div');
        row.className = 'prov-row';
        row.draggable = true;
        row.dataset.wsIdx = idx;
        row.style.cssText = 'flex-wrap:wrap; gap:4px; padding:6px; border:1px solid var(--md-sys-color-outline-variant,#ddd); border-radius:8px; margin-bottom:6px; cursor:grab; transition:background .15s;';
        row.ondragstart = function(e) { _wsDragIdx = idx; e.dataTransfer.effectAllowed = 'move'; row.style.opacity = '0.5'; };
        row.ondragend = function() { row.style.opacity = '1'; };
        row.ondragover = function(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; row.style.background = 'var(--md-sys-color-surface-container-high,#e8e8e8)'; };
        row.ondragleave = function() { row.style.background = ''; };
        row.ondrop = function(e) {
            e.preventDefault(); row.style.background = '';
            if (_wsDragIdx < 0 || _wsDragIdx === idx) return;
            var item = _webSearchProviders.splice(_wsDragIdx, 1)[0];
            _webSearchProviders.splice(idx, 0, item);
            _wsDragIdx = -1;
            renderProviderList();
        };

        // 行1：拖拽柄 + 序号 + 名称 + 删除
        var line1 = document.createElement('div');
        line1.style.cssText = 'display:flex; align-items:center; gap:4px; width:100%;';
        line1.innerHTML =
            '<span style="cursor:grab; color:#aaa; font-size:14px;" title="拖拽排序">' + mdIcon('drag_indicator', 16) + '</span>' +
            '<span style="color:#888; font-size:11px; min-width:16px; text-align:center;">#' + (idx + 1) + '</span>' +
            '<input type="text" id="ws-name-' + idx + '" value="' + _escAttr(sp.name || '') + '" placeholder="名称" class="prov-input" style="flex:1; min-width:80px;">' +
            '<select id="ws-type-' + idx + '" class="prov-input" style="width:90px; min-height:36px;">' +
            '<option value="serper"' + (sp.provider === 'serper' || !sp.provider ? ' selected' : '') + '>Serper</option>' +
            '<option value="exa"' + (sp.provider === 'exa' ? ' selected' : '') + '>Exa</option></select>' +
            '<button onclick="_removeWebSearchProvider(' + idx + ')" class="prov-btn-danger" title="删除">' + mdIcon('delete', 16) + '</button>';
        row.appendChild(line1);

        // 行2：API Key
        var line2 = document.createElement('div');
        line2.style.cssText = 'display:flex; align-items:center; gap:4px; width:100%; margin-top:4px;';
        line2.innerHTML =
            '<span class="prov-label" style="min-width:30px;">Key</span>' +
            '<input type="password" id="ws-key-' + idx + '" value="' + _escAttr(sp.api_key || '') + '" placeholder="API 密钥" class="prov-input" style="flex:1;">' +
            '<button onclick="_toggleKeyVis(\'ws-key-' + idx + '\')" class="prov-btn-eye" title="显示/隐藏">' + mdIcon('visibility', 16) + '</button>';
        row.appendChild(line2);

        card.appendChild(row);
    });

    container.appendChild(card);
}

function _addWebSearchProvider() {
    _syncSearchInputs();
    _webSearchProviders.push({name: '', provider: 'serper', api_key: ''});
    renderProviderList();
}

function _removeWebSearchProvider(idx) {
    _syncSearchInputs();
    _webSearchProviders.splice(idx, 1);
    renderProviderList();
}

function renderProviderList() {
    var container = document.getElementById('provider-list');
    container.innerHTML = '';

    _renderWebSearchCard(container);

    _providerData.forEach(function(prov, idx) {
        var card = document.createElement('div');
        card.className = 'provider-card';

        // Header: name input + delete button
        var header = document.createElement('div');
        header.className = 'prov-row';
        header.innerHTML =
            '<input type="text" id="prov-name-' + idx + '" value="' + _escAttr(prov.name || '') + '" placeholder="供应商名称" class="prov-input prov-name-input">' +
            '<button onclick="removeProvider(' + idx + ')" class="prov-btn-danger" title="删除此供应商">' + mdIcon('delete', 16) + '</button>';
        card.appendChild(header);

        // URL row
        var urlRow = document.createElement('div');
        urlRow.className = 'prov-row';
        urlRow.innerHTML =
            '<span class="prov-label">URL</span>' +
            '<input type="text" id="prov-url-' + idx + '" value="' + _escAttr(prov.api_url || '') + '" placeholder="https://api.example.com/v1/chat/completions" class="prov-input">';
        card.appendChild(urlRow);

        // Key row + toggle + test
        var testResult = _providerTestResults[idx];
        var testCls = 'prov-btn-test';
        var testText = '测试';
        if (testResult === 'ok') { testCls = 'prov-btn-test prov-test-ok'; testText = mdIcon('check', 16); }
        else if (testResult === 'fail') { testCls = 'prov-btn-test prov-test-fail'; testText = mdIcon('close', 16); }
        else if (testResult === 'testing') { testCls = 'prov-btn-test prov-test-ing'; testText = mdIcon('hourglass', 16); }

        var keyRow = document.createElement('div');
        keyRow.className = 'prov-row';
        keyRow.innerHTML =
            '<span class="prov-label">Key</span>' +
            '<input type="password" id="prov-key-' + idx + '" value="' + _escAttr(prov.api_key || '') + '" placeholder="API 密钥" class="prov-input">' +
            '<button onclick="_toggleKeyVis(\'prov-key-' + idx + '\')" class="prov-btn-eye" title="显示/隐藏">' + mdIcon('visibility', 16) + '</button>' +
            '<button onclick="testProvider(' + idx + ')" class="' + testCls + '" title="测试连通性">' + testText + '</button>';
        card.appendChild(keyRow);

        // Models section
        var modelsDiv = document.createElement('div');
        modelsDiv.className = 'prov-models-section';
        var modelsLabel = document.createElement('div');
        modelsLabel.className = 'prov-models-label';
        modelsLabel.textContent = '模型 (' + (prov.models || []).length + ')';
        modelsDiv.appendChild(modelsLabel);

        var modelsList = document.createElement('div');
        modelsList.className = 'prov-models-list';
        (prov.models || []).forEach(function(m, mi) {
            var modelName = typeof m === 'string' ? m : (m.name || JSON.stringify(m));
            var modelPrice = (typeof m === 'object' && m.price_per_call) ? m.price_per_call : '';
            var tag = document.createElement('span');
            tag.className = 'prov-model-tag';
            tag.innerHTML = _escHtml(modelName) +
                ' <input type="number" step="0.01" min="0" value="' + modelPrice + '" placeholder="$" style="width:42px;font-size:10px;padding:1px 3px;border:1px solid #ddd;border-radius:2px;margin:0 4px;" onchange="_updateModelPrice(' + idx + ',' + mi + ',this.value)" title="每次调用价格">' +
                '<span class="prov-model-remove" onclick="removeModel(' + idx + ',' + mi + ')">&times;</span>';
            modelsList.appendChild(tag);
        });
        modelsDiv.appendChild(modelsList);

        // Add model input
        var addRow = document.createElement('div');
        addRow.className = 'prov-row';
        addRow.innerHTML =
            '<input type="text" id="prov-add-model-' + idx + '" placeholder="新模型名称" class="prov-input" onkeydown="if(event.key===\'Enter\')addModel(' + idx + ')">' +
            '<button onclick="addModel(' + idx + ')" class="prov-btn-add-model">+</button>';
        modelsDiv.appendChild(addRow);

        card.appendChild(modelsDiv);
        container.appendChild(card);
    });

    // Add provider button
    var addDiv = document.createElement('div');
    addDiv.style.cssText = 'text-align:center; padding:15px 0;';
    addDiv.innerHTML = '<button onclick="addProvider()" class="prov-btn-add">+ 添加供应商</button>';
    container.appendChild(addDiv);
}

function addProvider() {
    _providerData.push({ name: '', api_url: '', api_key: '', models: [] });
    renderProviderList();
    var container = document.getElementById('provider-list');
    container.scrollTop = container.scrollHeight;
}

/**
 * Drop a provider from the editing buffer. The last native confirm() in the app.
 *
 * Worth knowing before touching the wording: this only mutates _providerData in
 * memory. Nothing reaches disk until 保存 is pressed, so closing the dialog
 * discards it. The gate therefore does not guard against data loss — it guards
 * against a mis-click in a dense list being carried into a later save, which is
 * why the message says so rather than implying finality.
 */
async function removeProvider(idx) {
    var name = _providerData[idx].name || '未命名';
    var ok = await showConfirmModal(
        '从列表中移除供应商「' + name + '」？\n\n'
        + '点击「保存」后生效；直接关闭本窗口则本次改动全部作废。',
        '移除');
    if (!ok) return;
    _providerData.splice(idx, 1);
    delete _providerTestResults[idx];
    renderProviderList();
}

function addModel(provIdx) {
    var input = document.getElementById('prov-add-model-' + provIdx);
    var name = (input.value || '').trim();
    if (!name) return;
    if (!_providerData[provIdx].models) _providerData[provIdx].models = [];
    _providerData[provIdx].models.push(name);
    input.value = '';
    renderProviderList();
}

function removeModel(provIdx, modelIdx) {
    _providerData[provIdx].models.splice(modelIdx, 1);
    renderProviderList();
}

function _updateModelPrice(provIdx, modelIdx, value) {
    var price = parseFloat(value) || 0;
    var model = _providerData[provIdx].models[modelIdx];
    if (price > 0) {
        if (typeof model === 'string') {
            _providerData[provIdx].models[modelIdx] = { name: model, price_per_call: price };
        } else {
            model.price_per_call = price;
        }
    } else {
        if (typeof model === 'object') {
            delete model.price_per_call;
            var keys = Object.keys(model).filter(function(k) { return k !== 'name'; });
            if (keys.length === 0) {
                _providerData[provIdx].models[modelIdx] = model.name;
            }
        }
    }
}

function _toggleKeyVis(inputId) {
    var el = document.getElementById(inputId);
    el.type = el.type === 'password' ? 'text' : 'password';
}

function testProvider(idx) {
    _syncProviderInputs();
    _providerTestResults[idx] = 'testing';
    renderProviderList();
    var prov = _providerData[idx];
    fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'test_provider', api_url: prov.api_url, api_key: prov.api_key})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        _providerTestResults[idx] = d.reachable ? 'ok' : 'fail';
        renderProviderList();
        if (d.message) showToast(d.message, d.reachable ? 'success' : 'error');
    })
    .catch(function() {
        _providerTestResults[idx] = 'fail';
        renderProviderList();
        showToast('请求异常', 'error');
    });
}

function saveProviders() {
    _syncProviderInputs();
    _syncSearchInputs();
    // Validate: remove providers with empty name AND empty url (accidental adds)
    _providerData = _providerData.filter(function(p) {
        return (p.name || '').trim() || (p.api_url || '').trim();
    });
    fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'save_providers', providers: _providerData, web_search: _webSearchProviders})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.status === 'ok') {
            showToast('供应商配置已保存并生效', 'success');
            closeProviderModal();
            // Immediately update model dropdown from returned state
            if (d.state) {
                if (d.state.available_models) AVAILABLE_MODELS = d.state.available_models;
                if (d.state.model_providers) window.MODEL_PROVIDERS = d.state.model_providers;
                renderModelDropdown();
            }
            // Also push to other connected clients
            postAction({action: 'ping'});
        } else {
            showToast(d.message || '保存失败', 'error');
        }
    })
    .catch(function(e) {
        showToast('保存失败: ' + e.message, 'error');
    });
}

function _syncProviderInputs() {
    for (var i = 0; i < _providerData.length; i++) {
        var nameEl = document.getElementById('prov-name-' + i);
        var urlEl = document.getElementById('prov-url-' + i);
        var keyEl = document.getElementById('prov-key-' + i);
        if (nameEl) _providerData[i].name = nameEl.value;
        if (urlEl) _providerData[i].api_url = urlEl.value;
        if (keyEl) _providerData[i].api_key = keyEl.value;
    }
}

function _syncSearchInputs() {
    for (var i = 0; i < _webSearchProviders.length; i++) {
        var nameEl = document.getElementById('ws-name-' + i);
        var typeEl = document.getElementById('ws-type-' + i);
        var keyEl = document.getElementById('ws-key-' + i);
        if (nameEl) _webSearchProviders[i].name = nameEl.value.trim();
        if (typeEl) _webSearchProviders[i].provider = typeEl.value || 'serper';
        if (keyEl) _webSearchProviders[i].api_key = keyEl.value.trim();
    }
    // 移除完全空白的条目
    _webSearchProviders = _webSearchProviders.filter(function(sp) {
        return (sp.name || '').trim() || (sp.api_key || '').trim();
    });
}

function _escAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}