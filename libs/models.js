/* ChatApp model dropdown - selection, hide/restore, subagent providers */

function sendWithModel(model, event) {
    event.preventDefault();
    event.stopPropagation();
    var val = userInput.value.trim();
    var steps = parseInt(document.getElementById('max-steps').value) || 1;
    var parallel = document.getElementById('parallel-mode').checked;
    var isOffline = (model === '离线');
    var isDeepThink = (typeof deepThinkLevel !== 'undefined') ? deepThinkLevel : 0;
    postAction({action: 'send_message', text: val, models: [model], steps: steps, parallel: parallel, is_offline: isOffline, is_deep_think: isDeepThink});
    userInput.value = '';
    userInput.style.height = 'auto';
    document.getElementById('model-dropdown').style.display = 'none';
}

function hideModel(model, event) {
    event.preventDefault();
    event.stopPropagation();
    if (!hiddenModels.includes(model)) {
        hiddenModels.push(model);
        localStorage.setItem('llm_hidden_models', JSON.stringify(hiddenModels));
        var checkedBoxes = Array.from(document.querySelectorAll('.model-cb:checked')).map(function(cb) { return cb.value; });
        checkedBoxes = checkedBoxes.filter(function(m) { return m !== model; });
        localStorage.setItem('llm_selected_models', JSON.stringify(checkedBoxes));
        renderModelDropdown();
    }
}

function restoreModel(model, event) {
    event.preventDefault();
    event.stopPropagation();
    hiddenModels = hiddenModels.filter(function(m) { return m !== model; });
    localStorage.setItem('llm_hidden_models', JSON.stringify(hiddenModels));
    renderModelDropdown();
}

function renderModelDropdown() {
    var container = document.getElementById('model-dropdown');
    var checkedBoxes = Array.from(container.querySelectorAll('.model-cb:checked')).map(function(cb) { return cb.value; });
    // 修复：首次渲染或 DOM 中无已勾选项时，都从 localStorage 恢复
    // （场景：首次渲染时 AVAILABLE_MODELS 还是空数组，checkbox 未渲染;
    //  后端推送到达后再次调用 renderModelDropdown 时 isFirstModelRender 已为 false，
    //  从空 DOM 读取导致选择丢失）
    if (isFirstModelRender || checkedBoxes.length === 0) {
        var saved = localStorage.getItem('llm_selected_models');
        if (saved) { try { checkedBoxes = JSON.parse(saved); } catch(e) {} }
        // 不再硬编码默认模型，如果 localStorage 为空则保持空选择，用户需手动选择
        if (isFirstModelRender) {
            isFirstModelRender = false;
            var savedHidden = localStorage.getItem('llm_hidden_models');
            if (savedHidden) { try { hiddenModels = JSON.parse(savedHidden); } catch(e) {} }
        }
    }
    var models = new Set(AVAILABLE_MODELS);
    models.delete('默认模型');
    var sortedModels = Array.from(models).sort(function(a, b) {
        var countA = (globalModelStats[a] && globalModelStats[a].total) || 0;
        var countB = (globalModelStats[b] && globalModelStats[b].total) || 0;
        return countB - countA;
    });
    if (!globalSettings.enable_arc3) {
        sortedModels = sortedModels.filter(function(m) { return !stripComposite(m).startsWith('[ARC3]'); });
    }
    container.innerHTML = '';
    var visibleModels = sortedModels.filter(function(m) { return !hiddenModels.includes(m); });
    var hiddenModelsList = sortedModels.filter(function(m) { return hiddenModels.includes(m); });
    visibleModels.forEach(function(m) {
        var isChecked = checkedBoxes.includes(m) ? 'checked' : '';
        var stats = globalModelStats[m] || {total: 0, up: 0, down: 0};
        var prov = extractProvider(m) || ((window.MODEL_PROVIDERS && window.MODEL_PROVIDERS[m]) ? window.MODEL_PROVIDERS[m] : '');
        var modelDisplay = stripComposite(m);
        var provHtml = prov ? '<span style="font-size: 9px; padding: 1px 4px; background: #e9ecef; border-radius: 3px; color: #495057; margin-right: 6px; border: 1px solid #ced4da;">' + prov + '</span>' : '';
        var label = document.createElement('label');
        label.style.cssText = 'display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; cursor:pointer; min-width:340px;';
        label.innerHTML = '<span style="display:flex; align-items:center;"><span onclick="sendWithModel(\'' + m + '\', event)" style="cursor:pointer; padding:0 6px 0 0;" title="仅以此模型发送">🚀</span><input type="checkbox" class="model-cb" value="' + m + '" ' + isChecked + '> ' + provHtml + modelDisplay + '</span><span style="font-size: 10px; color: #888; margin-left: auto; margin-right: 10px;">总:' + stats.total + ' 好:' + stats.up + ' 差:' + stats.down + '</span><span onclick="hideModel(\'' + m + '\', event)" style="cursor:pointer; color:#dc3545; padding:0 4px;" title="隐藏模型">➖</span>';
        container.appendChild(label);
    });
    if (hiddenModelsList.length > 0) {
        var moreDivider = document.createElement('div');
        moreDivider.style.cssText = 'border-top:1px solid #eee; margin-top:8px; padding-top:8px;';
        var moreBtn = document.createElement('div');
        moreBtn.style.cssText = 'cursor:pointer; font-size:11px; color:#007bff; text-align:center;';
        moreBtn.innerText = isMoreModelsExpanded ? '收起更多模型 ▴' : '展开更多模型 ▾';
        moreBtn.onclick = function(e) { e.stopPropagation(); isMoreModelsExpanded = !isMoreModelsExpanded; renderModelDropdown(); };
        moreDivider.appendChild(moreBtn);
        if (isMoreModelsExpanded) {
            var hiddenContainer = document.createElement('div');
            hiddenContainer.style.cssText = 'margin-top:8px; padding:8px; background:#f9f9f9; border-radius:4px;';
            hiddenModelsList.forEach(function(m) {
                var prov = extractProvider(m) || ((window.MODEL_PROVIDERS && window.MODEL_PROVIDERS[m]) ? window.MODEL_PROVIDERS[m] : '');
                var modelDisplay = stripComposite(m);
                var provHtml = prov ? '<span style="font-size: 8px; padding: 1px 3px; background: #e9ecef; border-radius: 2px; color: #666; margin-right: 4px;">' + prov + '</span>' : '';
                var item = document.createElement('div');
                item.style.cssText = 'display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; font-size:12px; color:#666;';
                item.innerHTML = '<span>' + provHtml + modelDisplay + '</span><button type="button" onclick="restoreModel(\'' + m + '\', event)" style="border:none; background:transparent; cursor:pointer; color:#28a745; padding:0 4px;" title="恢复模型">➕</button>';
                hiddenContainer.appendChild(item);
            });
            moreDivider.appendChild(hiddenContainer);
        }
        container.appendChild(moreDivider);
    }
    updateSendButtonCount();
}

function updateSendButtonCount() {
    var steps = parseInt(document.getElementById('max-steps').value) || 1;
    var parallelLabel = document.getElementById('parallel-label');
    if (steps > 1) { parallelLabel.style.display = 'inline'; }
    else { parallelLabel.style.display = 'none'; document.getElementById('parallel-mode').checked = false; }
    var parallel = document.getElementById('parallel-mode').checked;
    var modelCheckboxes = document.querySelectorAll('.model-cb:checked');
    var modelCount = modelCheckboxes.length || 1;
    var count = modelCount * (parallel ? steps : 1);
    document.getElementById('send-button').innerText = '发送 (' + count + ')';
    var sliderContainer = document.getElementById('autopilot-slider-container');
    if (sliderContainer) {
        var hasArc3 = Array.from(modelCheckboxes).some(function(cb) { return stripComposite(cb.value).startsWith('[ARC3]'); });
        sliderContainer.style.display = hasArc3 ? 'inline' : 'none';
    }
}
