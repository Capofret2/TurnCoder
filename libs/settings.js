/* ChatApp settings panel - open/close/save global settings */

function openSettingsModal() {
    if (document.getElementById('set-developer-mode')) document.getElementById('set-developer-mode').checked = globalSettings.developer_mode;

    if (document.getElementById('set-starred')) document.getElementById('set-starred').checked = globalSettings.enable_starred;

    if (document.getElementById('set-tool-lower-bound')) document.getElementById('set-tool-lower-bound').checked = globalSettings.enable_tool_lower_bound;
    if (document.getElementById('set-tool-desc-enforcement')) document.getElementById('set-tool-desc-enforcement').checked = globalSettings.enable_tool_description_enforcement;
    if (document.getElementById('set-cc-simulate')) document.getElementById('set-cc-simulate').checked = globalSettings.enable_tool_simulate;
    if (document.getElementById('set-partial-read')) document.getElementById('set-partial-read').checked = !!globalSettings.enable_partial_read;
    if (document.getElementById('set-descriptor-tool-calls')) document.getElementById('set-descriptor-tool-calls').checked = globalSettings.enable_descriptor_tool_calls;

    if (document.getElementById('set-routing-token')) document.getElementById('set-routing-token').checked = !!globalSettings.enable_routing_token;

    if (document.getElementById('set-ttfb-retry')) document.getElementById('set-ttfb-retry').checked = !!globalSettings.enable_ttfb_retry;
    if (document.getElementById('set-cache-control')) document.getElementById('set-cache-control').checked = !!globalSettings.enable_cache_control;
    if (document.getElementById('set-multiturn-format')) document.getElementById('set-multiturn-format').checked = !!globalSettings.enable_multiturn_format;
    if (document.getElementById('set-prefix-lock')) document.getElementById('set-prefix-lock').checked = !!globalSettings.enable_prefix_lock;
    if (document.getElementById('set-enable_thinking_retry')) document.getElementById('set-enable_thinking_retry').checked = !!globalSettings.enable_thinking_retry;
    if (document.getElementById('set-show-all-autoread')) document.getElementById('set-show-all-autoread').checked = !!globalSettings.enable_show_all_autoread;
    if (document.getElementById('set-streaming')) document.getElementById('set-streaming').checked = !!globalSettings.enable_stream;
    if (document.getElementById('set-truncation-detection')) document.getElementById('set-truncation-detection').checked = !!globalSettings.enable_truncation_detection;
    if (document.getElementById('set-pure-mode')) document.getElementById('set-pure-mode').checked = globalSettings.enable_pure_mode;

    if (document.getElementById('set-reverse-context')) document.getElementById('set-reverse-context').checked = globalSettings.enable_reverse_context;
    if (document.getElementById('set-anthropic-protocol')) document.getElementById('set-anthropic-protocol').checked = globalSettings.enable_anthropic_protocol;
    if (document.getElementById('set-cc-inject')) document.getElementById('set-cc-inject').checked = globalSettings.enable_tool_inject;
    if (document.getElementById('set-auto-update')) document.getElementById('set-auto-update').checked = !!globalSettings.enable_auto_update;
    if (document.getElementById('set-bulk-logging')) document.getElementById('set-bulk-logging').checked = globalSettings.enable_bulk_logging;


    if (document.getElementById('set-billing-sync-button')) document.getElementById('set-billing-sync-button').checked = globalSettings.enable_billing_sync_button;
    if (document.getElementById('set-style-filter')) document.getElementById('set-style-filter').checked = globalSettings.enable_style_filter;
    if (document.getElementById('set-planned-tools')) document.getElementById('set-planned-tools').checked = !!globalSettings.enable_planned_tools;
    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettingsModal() {
    document.getElementById('settings-modal').style.display = 'none';
}

function saveSettings() {
    if (document.getElementById('set-developer-mode')) globalSettings.developer_mode = document.getElementById('set-developer-mode').checked;

    if (document.getElementById('set-starred')) globalSettings.enable_starred = document.getElementById('set-starred').checked;

    if (document.getElementById('set-tool-lower-bound')) globalSettings.enable_tool_lower_bound = document.getElementById('set-tool-lower-bound').checked;
    if (document.getElementById('set-tool-desc-enforcement')) globalSettings.enable_tool_description_enforcement = document.getElementById('set-tool-desc-enforcement').checked;
    if (document.getElementById('set-cc-simulate')) globalSettings.enable_tool_simulate = document.getElementById('set-cc-simulate').checked;
    if (document.getElementById('set-partial-read')) globalSettings.enable_partial_read = document.getElementById('set-partial-read').checked;
    if (document.getElementById('set-descriptor-tool-calls')) globalSettings.enable_descriptor_tool_calls = document.getElementById('set-descriptor-tool-calls').checked;

    if (document.getElementById('set-routing-token')) globalSettings.enable_routing_token = document.getElementById('set-routing-token').checked;

    if (document.getElementById('set-ttfb-retry')) globalSettings.enable_ttfb_retry = document.getElementById('set-ttfb-retry').checked;
    if (document.getElementById('set-cache-control')) globalSettings.enable_cache_control = document.getElementById('set-cache-control').checked;
    if (document.getElementById('set-multiturn-format')) globalSettings.enable_multiturn_format = document.getElementById('set-multiturn-format').checked;
    if (document.getElementById('set-prefix-lock')) globalSettings.enable_prefix_lock = document.getElementById('set-prefix-lock').checked;
    if (document.getElementById('set-enable_thinking_retry')) globalSettings.enable_thinking_retry = document.getElementById('set-enable_thinking_retry').checked;
    if (document.getElementById('set-show-all-autoread')) globalSettings.enable_show_all_autoread = document.getElementById('set-show-all-autoread').checked;
    if (document.getElementById('set-streaming')) {
        var _streamOn = document.getElementById('set-streaming').checked;
        globalSettings.enable_stream = _streamOn;
        globalSettings.force_no_stream = !_streamOn;
    }
    if (document.getElementById('set-truncation-detection')) globalSettings.enable_truncation_detection = document.getElementById('set-truncation-detection').checked;
    if (document.getElementById('set-pure-mode')) globalSettings.enable_pure_mode = document.getElementById('set-pure-mode').checked;

    if (document.getElementById('set-reverse-context')) globalSettings.enable_reverse_context = document.getElementById('set-reverse-context').checked;
    if (document.getElementById('set-anthropic-protocol')) globalSettings.enable_anthropic_protocol = document.getElementById('set-anthropic-protocol').checked;
    if (document.getElementById('set-cc-inject')) globalSettings.enable_tool_inject = document.getElementById('set-cc-inject').checked;
    if (document.getElementById('set-auto-update')) globalSettings.enable_auto_update = document.getElementById('set-auto-update').checked;
    if (document.getElementById('set-bulk-logging')) globalSettings.enable_bulk_logging = document.getElementById('set-bulk-logging').checked;

    if (document.getElementById('set-billing-sync-button')) globalSettings.enable_billing_sync_button = document.getElementById('set-billing-sync-button').checked;
    if (document.getElementById('set-style-filter')) globalSettings.enable_style_filter = document.getElementById('set-style-filter').checked;
    if (document.getElementById('set-planned-tools')) globalSettings.enable_planned_tools = document.getElementById('set-planned-tools').checked;
    applySettingsUI();
    postAction({action: 'update_global_settings', settings: globalSettings});
}

function applySettingsUI() {
    // Hardcoded settings — no UI toggle, always applied
    globalSettings.enable_correction = false;
    globalSettings.enable_queue = false;
    globalSettings.enable_steps = false;
    globalSettings.enable_arc3 = false;
    globalSettings.auto_hide_env_obs = false;
    globalSettings.swap_system_messages = false;

    globalSettings.enable_autopilot = true;
    globalSettings.enable_deep_think_ui = true;
    globalSettings.enable_custom_websearch = true;
    globalSettings.enable_custom_webfetch = true;
    globalSettings.enable_custom_webfetch_jina = true;
    globalSettings.enable_webfetch_headless = true;
    globalSettings.enable_webfetch_file_mode = true;
    globalSettings.enable_bottom_tabs = true;

    // Developer mode: force settings when OFF
    var devMode = globalSettings.developer_mode;
    if (!devMode) {
        globalSettings.enable_anthropic_protocol = true;
        globalSettings.enable_tool_inject = true;
        globalSettings.enable_starred = false;
        globalSettings.enable_reverse_context = false;
        globalSettings.enable_tool_lower_bound = false;
        globalSettings.enable_tool_simulate = true;
        globalSettings.enable_routing_token = false;
        globalSettings.enable_descriptor_tool_calls = true;
    }

    document.getElementById('steps-container').style.display = globalSettings.enable_steps ? 'inline-block' : 'none';
    if (document.getElementById('code-monitor-btn')) document.getElementById('code-monitor-btn').style.display = globalSettings.enable_tool_inject ? 'none' : 'inline-block';
    if (document.getElementById('deep-think-label')) {
        document.getElementById('deep-think-label').style.display = globalSettings.enable_deep_think_ui ? 'inline' : 'none';
    }
    if (!globalSettings.enable_steps) {
        document.getElementById('max-steps').value = 1;
        updateSendButtonCount();
    }
    if (document.getElementById('autopilot-container')) {
        document.getElementById('autopilot-container').style.display = globalSettings.enable_autopilot ? 'inline-block' : 'none';
    }
    if (document.getElementById('autopilot-btn')) {
        document.getElementById('autopilot-btn').style.display = globalSettings.enable_autopilot ? 'inline-block' : 'none';
    }
    renderModelDropdown();

    // Developer mode UI visibility controls
    var devButtons = document.getElementById('dev-buttons-row');
    if (devButtons) devButtons.style.display = devMode ? 'flex' : 'none';
    var imgUploadBtn = document.getElementById('image-upload-btn');
    if (imgUploadBtn) imgUploadBtn.style.display = 'inline-block';
    document.querySelectorAll('.dev-only-setting').forEach(function(el) {
        el.style.display = devMode ? '' : 'none';
    });
    var advToggle = document.getElementById('adv-opt-toggle');
    if (advToggle) advToggle.style.display = devMode ? '' : 'none';
    // Bottom tab bar visibility
    var bottomTabBar = document.getElementById('bottom-tab-bar');
    if (bottomTabBar) bottomTabBar.style.display = globalSettings.enable_bottom_tabs ? '' : 'none';
    if (globalSettings.enable_bottom_tabs && typeof renderBottomTabs === 'function') renderBottomTabs();
    // Force chat re-render to update conditional buttons (annotation etc)
    if (typeof lastChatHash !== 'undefined') lastChatHash = '';
}

function openManualModal() {
    document.getElementById('manual-modal').style.display = 'flex';
}

function closeManualModal() {
    document.getElementById('manual-modal').style.display = 'none';
}
