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

/* ==========================================================================
   List order and provider grouping.
   --------------------------------------------------------------------------
   Both preferences live in localStorage rather than global_settings, for the
   same reason the colour scheme does: this is a per-device browsing preference,
   and global_settings round-trips through the server on every change.

   All three orders fall back to the name as a secondary key. Without it, the
   usage order leaves every never-used model — usually most of them, all at
   zero — in whatever sequence Array.sort happens to produce, which reads as
   "the list is in a different order every time I open it".
   ========================================================================== */
var MDL_SORTS = [
    {id: 'usage', name: '用量'},
    {id: 'name', name: '名称'},
    {id: 'provider', name: '供应商'}
];
var _mdlLastProv = null;

function _mdlSort() { return localStorage.getItem('llm_sort_mode') || 'usage'; }
function _mdlGrouped() { return localStorage.getItem('llm_group_by_provider') === '1'; }

function _mdlSetSort(v) {
    localStorage.setItem('llm_sort_mode', v);
    renderModelDropdown();
}

function _mdlToggleGroup() {
    localStorage.setItem('llm_group_by_provider', _mdlGrouped() ? '0' : '1');
    renderModelDropdown();
}

/** Provider for a model, with a bucket for the ones that resolve to nothing —
    an empty string would render a nameless header. */
function _mdlProvOf(m) {
    return extractProvider(m)
        || ((window.MODEL_PROVIDERS && window.MODEL_PROVIDERS[m]) ? window.MODEL_PROVIDERS[m] : '')
        || '未分类';
}

function _mdlComparator() {
    var mode = _mdlSort();
    var byName = function(a, b) { return stripComposite(a).localeCompare(stripComposite(b)); };
    var byUsage = function(a, b) {
        var ca = (globalModelStats[a] && globalModelStats[a].total) || 0;
        var cb = (globalModelStats[b] && globalModelStats[b].total) || 0;
        return cb - ca || byName(a, b);
    };
    var byProv = function(a, b) { return _mdlProvOf(a).localeCompare(_mdlProvOf(b)) || byUsage(a, b); };
    var base = mode === 'name' ? byName : (mode === 'provider' ? byProv : byUsage);
    if (!_mdlGrouped()) return base;
    // Grouping forces the provider to be the primary key whatever the chosen
    // order is. Otherwise one provider's models get scattered by the other key
    // and, since a header closes as soon as the provider changes, the same
    // provider would open a second and third header further down the list.
    return function(a, b) { return _mdlProvOf(a).localeCompare(_mdlProvOf(b)) || base(a, b); };
}

/** The chip row at the top of the popover. Also resets the group-header cursor,
    so it must be emitted before the rows it governs. */
function _mdlControls() {
    _mdlLastProv = null;
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;flex-wrap:wrap;align-items:center;gap:var(--md-sys-spacing-1);'
        + 'padding-bottom:var(--md-sys-spacing-2);margin-bottom:var(--md-sys-spacing-2);'
        + 'border-bottom:1px solid var(--md-sys-color-outline-variant);';
    var lb = document.createElement('span');
    lb.style.cssText = 'font-size:var(--md-sys-typescale-label-small-size);'
        + 'color:var(--md-sys-color-on-surface-variant);margin-right:var(--md-sys-spacing-1);';
    lb.textContent = '排序';
    row.appendChild(lb);
    var cur = _mdlSort();
    MDL_SORTS.forEach(function(s) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'md-chip' + (cur === s.id ? ' md-chip--selected' : '');
        b.style.minHeight = '26px';
        b.textContent = s.name;
        // stopPropagation: the document-level handler in main.js closes this
        // popover on any click it judges to be outside, and a rebuild mid-click
        // is exactly the shape that confuses that test.
        b.onclick = function(e) { e.stopPropagation(); _mdlSetSort(s.id); };
        row.appendChild(b);
    });
    var g = document.createElement('button');
    g.type = 'button';
    g.className = 'md-chip' + (_mdlGrouped() ? ' md-chip--selected' : '');
    g.style.cssText = 'min-height:26px;margin-left:auto;';
    g.textContent = '按供应商分组';
    g.onclick = function(e) { e.stopPropagation(); _mdlToggleGroup(); };
    row.appendChild(g);
    return row;
}

/** Emit a header when the provider changes. No-op while grouping is off. */
function _mdlMaybeHeader(container, m) {
    if (!_mdlGrouped()) return;
    var p = _mdlProvOf(m);
    if (p === _mdlLastProv) return;
    _mdlLastProv = p;
    var h = document.createElement('div');
    h.style.cssText = 'font-size:var(--md-sys-typescale-label-small-size);'
        + 'font-weight:var(--md-sys-typescale-label-small-weight);'
        + 'letter-spacing:var(--md-sys-typescale-label-small-tracking);'
        + 'color:var(--md-sys-color-on-surface-variant);'
        + 'margin:var(--md-sys-spacing-2) 0 var(--md-sys-spacing-1);';
    h.textContent = p;
    container.appendChild(h);
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
    var sortedModels = Array.from(models).sort(_mdlComparator());
    if (!globalSettings.enable_arc3) {
        sortedModels = sortedModels.filter(function(m) { return !stripComposite(m).startsWith('[ARC3]'); });
    }
    container.innerHTML = '';
    // Before the rows, and not only for visual order: _mdlControls resets the
    // group-header cursor. Called after them, the first provider's header would
    // be skipped because the cursor still holds the previous render's last value.
    container.appendChild(_mdlControls());
    var visibleModels = sortedModels.filter(function(m) { return !hiddenModels.includes(m); });
    var hiddenModelsList = sortedModels.filter(function(m) { return hiddenModels.includes(m); });
    visibleModels.forEach(function(m) {
        _mdlMaybeHeader(container, m);
        var isChecked = checkedBoxes.includes(m) ? 'checked' : '';
        var stats = globalModelStats[m] || {total: 0, up: 0, down: 0};
        var prov = extractProvider(m) || ((window.MODEL_PROVIDERS && window.MODEL_PROVIDERS[m]) ? window.MODEL_PROVIDERS[m] : '');
        var modelDisplay = stripComposite(m);
        var provHtml = prov ? '<span style="font-size: var(--md-sys-typescale-label-small-size); padding: 1px var(--md-sys-spacing-1); background: var(--md-sys-color-secondary-container); color: var(--md-sys-color-on-secondary-container); border-radius: var(--md-sys-shape-corner-extra-small); margin-right: var(--md-sys-spacing-1);">' + prov + '</span>' : '';
        var label = document.createElement('label');
        label.style.cssText = 'display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; cursor:pointer; min-width:340px;';
        label.innerHTML = '<span style="display:flex; align-items:center;"><span onclick="sendWithModel(\'' + m + '\', event)" style="cursor:pointer; padding:0 6px 0 0; color:var(--md-sys-color-primary);" title="仅以此模型发送">' + mdIcon('bolt', 16) + '</span><input type="checkbox" class="model-cb" value="' + m + '" ' + isChecked + '> ' + provHtml + modelDisplay + '</span><span style="font-size: var(--md-sys-typescale-label-small-size); color: var(--md-sys-color-on-surface-variant); margin-left: auto; margin-right: var(--md-sys-spacing-3);">总:' + stats.total + ' 好:' + stats.up + ' 差:' + stats.down + '</span><span onclick="hideModel(\'' + m + '\', event)" style="cursor:pointer; color:var(--md-sys-color-error); padding:0 var(--md-sys-spacing-1);" title="隐藏模型">' + mdIcon('remove', 16) + '</span>';
        container.appendChild(label);
    });
    if (hiddenModelsList.length > 0) {
        var moreDivider = document.createElement('div');
        moreDivider.style.cssText = 'border-top:1px solid var(--md-sys-color-outline-variant);'
            + 'margin-top:var(--md-sys-spacing-2); padding-top:var(--md-sys-spacing-2);';
        var moreBtn = document.createElement('div');
        // primary, not #007bff. That exact literal is one of the three inline
        // overrides UI_CONVENTIONS section five records, so it is a recurring slip
        // in this codebase rather than a one-off; the token also follows the accent.
        moreBtn.style.cssText = 'cursor:pointer; font-size:var(--md-sys-typescale-label-small-size);'
            + 'color:var(--md-sys-color-primary); text-align:center;';
        moreBtn.innerText = isMoreModelsExpanded ? '收起更多模型 ▴' : '展开更多模型 ▾';
        moreBtn.onclick = function(e) { e.stopPropagation(); isMoreModelsExpanded = !isMoreModelsExpanded; renderModelDropdown(); };
        moreDivider.appendChild(moreBtn);
        if (isMoreModelsExpanded) {
            var hiddenContainer = document.createElement('div');
            hiddenContainer.style.cssText = 'margin-top:var(--md-sys-spacing-2);'
                + 'padding:var(--md-sys-spacing-2);'
                + 'background:var(--md-sys-color-surface-container);'
                + 'border-radius:var(--md-sys-shape-corner-extra-small);';
            hiddenModelsList.forEach(function(m) {
                var prov = extractProvider(m) || ((window.MODEL_PROVIDERS && window.MODEL_PROVIDERS[m]) ? window.MODEL_PROVIDERS[m] : '');
                var modelDisplay = stripComposite(m);
                // Same pair as the visible rows' badge above. Left as literals, this
                // list would keep a pale badge on a dark container — one concept with
                // two implementations, and only one of them fixed.
                var provHtml = prov ? '<span style="font-size: var(--md-sys-typescale-label-small-size); padding: 1px var(--md-sys-spacing-1); background: var(--md-sys-color-secondary-container); color: var(--md-sys-color-on-secondary-container); border-radius: var(--md-sys-shape-corner-extra-small); margin-right: var(--md-sys-spacing-1);">' + prov + '</span>' : '';
                var item = document.createElement('div');
                item.style.cssText = 'display:flex; justify-content:space-between; align-items:center;'
                    + 'margin-bottom:var(--md-sys-spacing-1);'
                    + 'font-size:var(--md-sys-typescale-body-small-size);'
                    + 'color:var(--md-sys-color-on-surface-variant);';
                item.innerHTML = '<span>' + provHtml + modelDisplay + '</span><button type="button" class="md-icon-button md-icon-button--compact" onclick="restoreModel(\'' + m + '\', event)" style="color:var(--md-sys-color-tertiary);" title="恢复模型">' + mdIcon('add', 16) + '</button>';
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
