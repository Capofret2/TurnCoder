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
    // M3 snackbar: inverse surface so the notice reads against any background.
    // The error/success distinction moves to a 4px leading edge, which keeps the
    // inverse treatment intact instead of swapping the whole fill.
    var toast = document.createElement('div');
    var accent = type === 'error' ? 'var(--md-sys-color-error)' : 'var(--md-sys-color-success)';
    toast.style.cssText = 'background: var(--md-sys-color-inverse-surface);'
        + ' color: var(--md-sys-color-inverse-on-surface);'
        + ' border: none; border-left: 4px solid ' + accent + ';'
        + ' padding: var(--md-sys-spacing-3) var(--md-sys-spacing-4);'
        + ' border-radius: var(--md-sys-shape-corner-extra-small);'
        + ' box-shadow: var(--md-sys-elevation-level3);'
        + ' display: flex; align-items: flex-start; justify-content: space-between; gap: var(--md-sys-spacing-3);'
        + ' min-width: 250px; max-width: 380px; min-height: 48px; box-sizing: border-box;'
        + ' pointer-events: auto;'
        + ' font-size: var(--md-sys-typescale-body-medium-size);'
        + ' letter-spacing: var(--md-sys-typescale-body-medium-tracking);'
        + ' transition: opacity var(--md-sys-motion-duration-short4) var(--md-sys-motion-easing-standard-accelerate);';
    var msgSpan = document.createElement('span');
    msgSpan.innerText = message;
    msgSpan.style.cssText = 'word-break: break-word; line-height: 1.4; align-self: center;';
    var closeBtn = document.createElement('span');
    closeBtn.innerHTML = mdIcon('close', 18);
    closeBtn.style.cssText = 'cursor: pointer; line-height: 1; color: inherit; opacity: 0.8;'
        + ' width: 24px; height: 24px; flex-shrink: 0;'
        + ' display: inline-flex; align-items: center; justify-content: center;'
        + ' border-radius: var(--md-sys-shape-corner-full);';
    closeBtn.onclick = function() { toast.style.opacity = '0'; setTimeout(function() { toast.remove(); }, 300); };
    toast.appendChild(msgSpan);
    toast.appendChild(closeBtn);
    container.appendChild(toast);
    setTimeout(function() { if (toast.parentNode) { toast.style.opacity = '0'; setTimeout(function() { toast.remove(); }, 300); } }, 8000);
}

/* ===== Overlay keyboard behaviour, defined once for every overlay ==========
 *
 * Six of the eight overlays in this app had no Escape handling and none had a
 * focus trap, so Tab walked out of a modal into the page it was covering — which
 * is visually obscured, making the focus ring impossible to follow.
 *
 * One document-level listener plus a topmost-overlay test, rather than wiring
 * each open function: that would mean editing settings.js, providers.js,
 * codemonitor.js and three sites in main.js, and every future overlay would
 * have to remember to opt in.
 *
 * Escape clicks the overlay's own close control instead of removing the node.
 * That runs whatever cleanup the close button already performs (closeSettingsModal
 * persists, closeSessionManager tears down its grid) rather than establishing a
 * second, divergent close path.
 *
 * showPromptModal and showConfirmModal are deliberately absent from the
 * selector. Their Escape has to resolve a Promise, and this handler is
 * registered first — removing their node here would leave the caller awaiting
 * forever.
 * ======================================================================== */
var MD_OVERLAY_SELECTOR = [
    '.md-modal-overlay', '.sm-overlay', '#edit-modal', '#code-monitor-modal',
    '#ctx-mgr-overlay', '#waterfall-overlay'
].join(',');

/** The visible overlay stacked highest, or null. */
function _mdTopmostOverlay() {
    var open = [];
    document.querySelectorAll(MD_OVERLAY_SELECTOR).forEach(function(el) {
        var cs = getComputedStyle(el);
        if (cs.display !== 'none' && cs.visibility !== 'hidden') open.push(el);
    });
    if (!open.length) return null;
    // z-index first, DOM order as the tiebreak, which puts a dynamically
    // appended overlay above a declared one sharing the same level.
    var best = open[0], bestZ = -Infinity;
    open.forEach(function(el) {
        var z = parseInt(getComputedStyle(el).zIndex, 10);
        if (isNaN(z)) z = 0;
        if (z >= bestZ) { bestZ = z; best = el; }
    });
    return best;
}

/** Tabbable descendants, in document order. offsetParent filters hidden ones. */
function _mdOverlayFocusables(root) {
    var sel = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
    return Array.prototype.filter.call(root.querySelectorAll(sel), function(el) {
        return !el.disabled && el.offsetParent !== null;
    });
}

document.addEventListener('keydown', function(e) {
    if (e.defaultPrevented) return;
    if (e.key !== 'Escape' && e.key !== 'Tab') return;
    var ov = _mdTopmostOverlay();
    if (!ov) return;
    if (e.key === 'Escape') {
        e.preventDefault();
        var closer = ov.querySelector('.md-modal-close, .sm-close, [data-overlay-close]');
        if (closer) { closer.click(); return; }
        // No close control: the two declared modals are toggled by display and
        // the rest are built and thrown away by their opener.
        if (ov.id === 'edit-modal' || ov.id === 'code-monitor-modal') {
            ov.style.display = 'none';
        } else {
            ov.remove();
        }
        return;
    }
    var f = _mdOverlayFocusables(ov);
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    // Focus outside the overlay entirely: pull it back in rather than letting
    // the page behind receive the tab.
    if (!ov.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
    }
    if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
    }
});

// Non-blocking prompt modal (replaces native window.prompt)
function showPromptModal(message, defaultValue) {
    return new Promise(function(resolve) {
        // M3 basic dialog. Action area follows the spec ordering: the dismissive
        // action sits left as a text button, the confirming action right as filled.
        var overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;'
            + 'background:color-mix(in srgb, var(--md-sys-color-scrim) 32%, transparent);'
            + 'display:flex;justify-content:center;align-items:center;z-index:9999;';
        var box = document.createElement('div');
        box.style.cssText = 'background:var(--md-sys-color-surface-container-high);'
            + 'color:var(--md-sys-color-on-surface);'
            + 'border-radius:var(--md-sys-shape-corner-extra-large);'
            + 'padding:var(--md-sys-spacing-6);min-width:320px;max-width:450px;'
            + 'box-shadow:var(--md-sys-elevation-level3);';
        var msgEl = document.createElement('div');
        msgEl.style.cssText = 'margin-bottom:var(--md-sys-spacing-4);'
            + 'font-size:var(--md-sys-typescale-body-large-size);'
            + 'letter-spacing:var(--md-sys-typescale-body-large-tracking);'
            + 'color:var(--md-sys-color-on-surface-variant);white-space:pre-wrap;line-height:1.5;';
        msgEl.textContent = message;
        var input = document.createElement('input');
        input.type = 'text';
        // Mirrors showConfirmModal's confirm-cancel / confirm-ok. Without these
        // nothing that routes through this dialog can be covered by a DOM case:
        // the elements were unaddressable, which is why the existing
        // reject-approval assertion depended on a native prompt() being
        // synchronous.
        input.dataset.testid = 'prompt-input';
        input.value = defaultValue || '';
        input.style.cssText = 'width:100%;padding:var(--md-sys-spacing-2) var(--md-sys-spacing-3);'
            + 'border:none;border-bottom:1px solid var(--md-sys-color-outline);'
            + 'border-radius:var(--md-sys-shape-corner-extra-small) var(--md-sys-shape-corner-extra-small) 0 0;'
            + 'background:var(--md-sys-color-surface-container-highest);'
            + 'color:var(--md-sys-color-on-surface);outline:none;'
            + 'font-family:inherit;font-size:var(--md-sys-typescale-body-large-size);'
            + 'min-height:40px;box-sizing:border-box;margin-bottom:var(--md-sys-spacing-6);';
        var btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:var(--md-sys-spacing-2);';
        var _mdBtnBase = 'min-height:40px;border-radius:var(--md-sys-shape-corner-full);cursor:pointer;'
            + 'font-family:inherit;font-size:var(--md-sys-typescale-label-large-size);'
            + 'font-weight:var(--md-sys-typescale-label-large-weight);'
            + 'letter-spacing:var(--md-sys-typescale-label-large-tracking);';
        var cancelBtn = document.createElement('button');
        cancelBtn.dataset.testid = 'prompt-cancel';
        cancelBtn.textContent = '取消';
        cancelBtn.style.cssText = _mdBtnBase + 'padding:0 var(--md-sys-spacing-3);border:none;'
            + 'background:transparent;color:var(--md-sys-color-primary);';
        var okBtn = document.createElement('button');
        okBtn.dataset.testid = 'prompt-ok';
        okBtn.textContent = '确认';
        okBtn.style.cssText = _mdBtnBase + 'padding:0 var(--md-sys-spacing-6);border:none;'
            + 'background:var(--md-sys-color-primary);color:var(--md-sys-color-on-primary);';
        cancelBtn.onclick = function() { overlay.remove(); resolve(null); };
        okBtn.onclick = function() { overlay.remove(); resolve(input.value); };
        // preventDefault on both: this key press continues bubbling to the
        // document-level overlay handler above, which would then find this
        // overlay already gone and close whatever is stacked beneath it —
        // one press dismissing two dialogs. defaultPrevented is the only
        // coordination between the two handlers.
        input.onkeydown = function(e) {
            if (e.key === 'Enter') { e.preventDefault(); overlay.remove(); resolve(input.value); }
            if (e.key === 'Escape') { e.preventDefault(); overlay.remove(); resolve(null); }
        };
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

/**
 * Yes/no dialog for consequential actions. Resolves true only on confirm.
 *
 * Shares showPromptModal's chrome on purpose — same scrim, same dialog shape,
 * same dismissive-left/confirming-right action order — so the two read as one
 * component family. It differs in three places, all of them because the caller
 * is about to destroy something:
 *
 *   - no input, so nothing can be mistaken for a field to fill in;
 *   - the confirming action is the error pair rather than primary;
 *   - focus lands on cancel, so a stray Return dismisses instead of commits.
 *
 * @param {string} message body text, rendered as plain text
 * @param {string} [confirmLabel='确认'] label for the destructive action
 * @returns {Promise<boolean>}
 */
function showConfirmModal(message, confirmLabel) {
    return new Promise(function(resolve) {
        var overlay = document.createElement('div');
        overlay.className = 'md-confirm-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;'
            + 'background:color-mix(in srgb, var(--md-sys-color-scrim) 32%, transparent);'
            + 'display:flex;justify-content:center;align-items:center;z-index:9999;';
        var box = document.createElement('div');
        box.style.cssText = 'background:var(--md-sys-color-surface-container-high);'
            + 'color:var(--md-sys-color-on-surface);'
            + 'border-radius:var(--md-sys-shape-corner-extra-large);'
            + 'padding:var(--md-sys-spacing-6);min-width:320px;max-width:450px;'
            + 'box-shadow:var(--md-sys-elevation-level3);';
        var msgEl = document.createElement('div');
        msgEl.style.cssText = 'margin-bottom:var(--md-sys-spacing-6);'
            + 'font-size:var(--md-sys-typescale-body-large-size);'
            + 'letter-spacing:var(--md-sys-typescale-body-large-tracking);'
            + 'color:var(--md-sys-color-on-surface-variant);white-space:pre-wrap;line-height:1.5;';
        msgEl.textContent = message;
        var btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:var(--md-sys-spacing-2);';
        var cancelBtn = document.createElement('button');
        cancelBtn.className = 'md-button md-button--text';
        cancelBtn.dataset.testid = 'confirm-cancel';
        cancelBtn.textContent = '取消';
        var okBtn = document.createElement('button');
        okBtn.className = 'md-button md-button--danger';
        okBtn.dataset.testid = 'confirm-ok';
        okBtn.textContent = confirmLabel || '确认';
        var done = function(v) { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
        var onKey = function(e) {
            if (e.key === 'Escape') { e.preventDefault(); done(false); }
        };
        cancelBtn.onclick = function() { done(false); };
        okBtn.onclick = function() { done(true); };
        document.addEventListener('keydown', onKey);
        btnRow.appendChild(cancelBtn);
        btnRow.appendChild(okBtn);
        box.appendChild(msgEl);
        box.appendChild(btnRow);
        overlay.appendChild(box);
        overlay.onclick = function(e) { if (e.target === overlay) done(false); };
        document.body.appendChild(overlay);
        setTimeout(function() { cancelBtn.focus(); }, 50);
    });
}
