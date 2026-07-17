/* ChatApp provider management modal - add/edit/delete providers and models */

var _providerData = [];
var _providerTestResults = {};

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

function renderProviderList() {
    var container = document.getElementById('provider-list');
    container.innerHTML = '';

    _providerData.forEach(function(prov, idx) {
        var card = document.createElement('div');
        card.className = 'provider-card';

        // Header: name input + delete button
        var header = document.createElement('div');
        header.className = 'prov-row';
        header.innerHTML =
            '<input type="text" id="prov-name-' + idx + '" value="' + _escAttr(prov.name || '') + '" placeholder="供应商名称" class="prov-input prov-name-input">' +
            '<button onclick="removeProvider(' + idx + ')" class="prov-btn-danger" title="删除此供应商">🗑️</button>';
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
        if (testResult === 'ok') { testCls = 'prov-btn-test prov-test-ok'; testText = '✓'; }
        else if (testResult === 'fail') { testCls = 'prov-btn-test prov-test-fail'; testText = '✗'; }
        else if (testResult === 'testing') { testCls = 'prov-btn-test prov-test-ing'; testText = '…'; }

        var keyRow = document.createElement('div');
        keyRow.className = 'prov-row';
        keyRow.innerHTML =
            '<span class="prov-label">Key</span>' +
            '<input type="password" id="prov-key-' + idx + '" value="' + _escAttr(prov.api_key || '') + '" placeholder="API 密钥" class="prov-input">' +
            '<button onclick="_toggleKeyVis(\'prov-key-' + idx + '\')" class="prov-btn-eye" title="显示/隐藏">👁</button>' +
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

function removeProvider(idx) {
    var name = _providerData[idx].name || '未命名';
    if (!confirm('确定删除供应商「' + name + '」？')) return;
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
    // Validate: remove providers with empty name AND empty url (accidental adds)
    _providerData = _providerData.filter(function(p) {
        return (p.name || '').trim() || (p.api_url || '').trim();
    });
    fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'save_providers', providers: _providerData})
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

function _escAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}