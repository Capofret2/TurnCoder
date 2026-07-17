/* ChatApp utility functions - markdown rendering and notifications */

// Protect math formulas and code blocks during Markdown rendering
function renderMarkdownProtected(text) {
    if (!text) return "";
    var codeBlocks = [];
    var mathBlocks = [];
    var safeHtmlTags = [];

    // 1. Extract fenced code blocks as placeholders (must be first to protect their content)
    var t = text.replace(/`{3}[\s\S]*?`{3}/g, function(match) {
        codeBlocks.push(match);
        return '%%%CODEBLOCK_' + (codeBlocks.length - 1) + '%%%';
    });

    // 2. Extract inline code as placeholders
    t = t.replace(/`[^`\n]*`/g, function(match) {
        codeBlocks.push(match);
        return '%%%CODEBLOCK_' + (codeBlocks.length - 1) + '%%%';
    });

    // 3. Extract math formulas (after code extraction to avoid matching $ inside code)
    t = t.replace(/\$\$[\s\S]*?\$\$/g, function(match) {
        mathBlocks.push(match);
        return '%%%MATHBLOCK_' + (mathBlocks.length - 1) + '%%%';
    });
    t = t.replace(/\$[^$\n]+\$/g, function(match) {
        mathBlocks.push(match);
        return '%%%MATHINLINE_' + (mathBlocks.length - 1) + '%%%';
    });

    // 4. Preserve whitelisted HTML tags (del/ins for diff and correction rendering)
    t = t.replace(/<(\/?)(del|ins)(\s[^>]*)?>/gi, function(match) {
        safeHtmlTags.push(match);
        return '%%%SAFEHTML_' + (safeHtmlTags.length - 1) + '%%%';
    });

    // 5. Escape < to prevent raw HTML from being rendered as DOM elements
    // Only < is escaped; > is preserved for markdown blockquote syntax
    t = t.replace(/</g, '&lt;');

    // 6. Restore whitelisted HTML tags
    safeHtmlTags.forEach(function(tag, i) {
        t = t.replace('%%%SAFEHTML_' + i + '%%%', tag);
    });

    // 7. Restore code blocks (marked.js will process their syntax and escape content internally)
    codeBlocks.forEach(function(code, i) {
        t = t.replace('%%%CODEBLOCK_' + i + '%%%', function() { return code; });
    });

    // 8. Render Markdown
    var html = marked.parse(t);

    // 9. Restore math formulas (must be after marked.parse to prevent markdown mangling)
    mathBlocks.forEach(function(math, i) {
        html = html.replace('%%%MATHBLOCK_' + i + '%%%', function() { return math; });
        html = html.replace('%%%MATHINLINE_' + i + '%%%', function() { return math; });
    });

    return html;
}

// Configure marked.js with highlight.js code highlighting
(function() {
    var renderer = new marked.Renderer();
    // 禁用 GFM 的 ~~strikethrough~~ 渲染，原样保留波浪号
    renderer.del = function(text) {
        if (typeof text === 'object' && text !== null) text = text.text || text.raw || '';
        return '~~' + text + '~~';
    };
    renderer.code = function(code, language) {
        if (typeof code === 'object' && code !== null) {
            language = code.lang;
            code = code.text;
        }
        var validLanguage = (language && typeof language === 'string' && hljs.getLanguage(language)) ? language : 'plaintext';
        var highlightedCode = code;
        try {
            highlightedCode = hljs.highlight(code, { language: validLanguage, ignoreIllegals: true }).value;
        } catch (e) {
            console.warn("Code highlight failed:", e);
        }
        return '<pre><code class="hljs language-' + validLanguage + '">' + highlightedCode + '</code></pre>';
    };
    if (marked.use) marked.use({ renderer: renderer });
    marked.setOptions({ renderer: renderer, breaks: true });
})();

// Shared message type classification (used by visualization + context manager)
// Must stay consistent with message_toggle.py batch_context_manage
function classifyMessageType(m) {
    var isThinking = (m.model_name && m.model_name.endsWith('(思考过程)')) || m.cc_type === 'thinking';
    if (isThinking) return 'thinking';
    if (m.role === 'assistant') return 'assistant';
    var isTool = m.is_tool_result || m.tool_use_id || (m.role === 'user' && m.content && (m.content.startsWith('**Tool Result**') || m.content.startsWith('**Tool Error**') || m.content.startsWith('**审稿 [') || m.content.startsWith('**规划 [') || m.content.startsWith('**聚合规划 [')));
    if (isTool) return 'tool_result';
    return 'user';
}

// Composite model ID helpers: "Provider::Model" format
function stripComposite(m) {
    if (!m) return '';
    var idx = m.indexOf('::');
    return idx > 0 ? m.substring(idx + 2) : m;
}

function extractProvider(m) {
    if (!m) return '';
    var idx = m.indexOf('::');
    return idx > 0 ? m.substring(0, idx) : '';
}

function formatModelDisplay(m) {
    if (!m) return '';
    var idx = m.indexOf('::');
    if (idx > 0) {
        return '[' + m.substring(0, idx) + '] ' + m.substring(idx + 2);
    }
    return '[' + m + ']';
}

// Toast notification system
function showToast(message, type) {
    type = type || 'error';
    var container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 90; display: flex; flex-direction: column; gap: 10px; pointer-events: none; max-height: calc(100vh - 120px); overflow-y: auto;';
        document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    var bgColor = type === 'error' ? '#f8d7da' : '#d4edda';
    var textColor = type === 'error' ? '#721c24' : '#155724';
    var borderColor = type === 'error' ? '#f5c6cb' : '#c3e6cb';
    toast.style.cssText = 'background-color: ' + bgColor + '; color: ' + textColor + '; border: 1px solid ' + borderColor + '; padding: 12px 16px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); display: flex; align-items: flex-start; justify-content: space-between; min-width: 250px; max-width: 350px; pointer-events: auto; font-size: 13px; transition: opacity 0.3s;';
    var msgSpan = document.createElement('span');
    msgSpan.innerText = message;
    msgSpan.style.cssText = 'margin-right: 15px; word-break: break-word; line-height: 1.4;';
    var closeBtn = document.createElement('span');
    closeBtn.innerHTML = '&times;';
    closeBtn.style.cssText = 'cursor: pointer; font-size: 20px; font-weight: bold; line-height: 1; color: inherit; opacity: 0.7; padding-left: 5px;';
    closeBtn.onclick = function() { toast.style.opacity = '0'; setTimeout(function() { toast.remove(); }, 300); };
    toast.appendChild(msgSpan);
    toast.appendChild(closeBtn);
    container.appendChild(toast);
    setTimeout(function() { if (toast.parentNode) { toast.style.opacity = '0'; setTimeout(function() { toast.remove(); }, 300); } }, 8000);
}

// Non-blocking prompt modal (replaces native window.prompt)
function showPromptModal(message, defaultValue) {
    return new Promise(function(resolve) {
        var overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;justify-content:center;align-items:center;z-index:9999;';
        var box = document.createElement('div');
        box.style.cssText = 'background:#fff;border-radius:8px;padding:20px 24px;min-width:320px;max-width:450px;box-shadow:0 8px 32px rgba(0,0,0,0.25);';
        var msgEl = document.createElement('div');
        msgEl.style.cssText = 'margin-bottom:12px;font-size:14px;color:#333;white-space:pre-wrap;line-height:1.5;';
        msgEl.textContent = message;
        var input = document.createElement('input');
        input.type = 'text';
        input.value = defaultValue || '';
        input.style.cssText = 'width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:4px;font-size:14px;box-sizing:border-box;margin-bottom:16px;';
        var btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;';
        var cancelBtn = document.createElement('button');
        cancelBtn.textContent = '取消';
        cancelBtn.style.cssText = 'padding:6px 16px;border:1px solid #ccc;border-radius:4px;background:#fff;cursor:pointer;font-size:13px;';
        var okBtn = document.createElement('button');
        okBtn.textContent = '确认';
        okBtn.style.cssText = 'padding:6px 16px;border:none;border-radius:4px;background:#007bff;color:#fff;cursor:pointer;font-size:13px;';
        cancelBtn.onclick = function() { overlay.remove(); resolve(null); };
        okBtn.onclick = function() { overlay.remove(); resolve(input.value); };
        input.onkeydown = function(e) { if (e.key === 'Enter') { overlay.remove(); resolve(input.value); } if (e.key === 'Escape') { overlay.remove(); resolve(null); } };
        btnRow.appendChild(cancelBtn);
        btnRow.appendChild(okBtn);
        box.appendChild(msgEl);
        box.appendChild(input);
        box.appendChild(btnRow);
        overlay.appendChild(box);
        overlay.onclick = function(e) { if (e.target === overlay) { overlay.remove(); resolve(null); } };
        document.body.appendChild(overlay);
        setTimeout(function() { input.focus(); input.select(); }, 50);
    });
}
