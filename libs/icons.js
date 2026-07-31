/* TurnCoder — local Material-style icon set.
 *
 * Outlined glyphs on the 24dp Material grid: 1.8 stroke, round caps and joins,
 * no fill. Stroke is `currentColor`, so an icon inherits the colour of whatever
 * hosts it and follows a theme change for free. No network request, no font.
 *
 * Names mirror Material Symbols so that swapping in official path data later is
 * a value-only change.
 *
 * Usage: el.innerHTML = mdIcon('content_copy') + ' 复制';
 *
 * Always keep the text label. toggleCodeBlock in codeblocks.js and
 * toggleAutopilot in actions.js branch on button text, so an icon-only button
 * would silently break their state detection.
 */

var MD_ICON_GLYPHS = {
    /* clipboard / editing */
    content_copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V5.5A1.5 1.5 0 0 0 13.5 4h-8A1.5 1.5 0 0 0 4 5.5v8A1.5 1.5 0 0 0 5.5 15H9"/>',
    edit: '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M14 6l4 4"/>',
    brush: '<path d="M4 20c0-2.6 1.6-4 3.5-4S11 17.2 11 19c0 .7-.5 1-1 1z"/><path d="M9.5 16.5L19.5 6.5l-2-2L7.5 14.5z"/>',
    delete: '<path d="M5 7h14"/><path d="M10 4h4"/><path d="M6.5 7l.9 12.1A2 2 0 0 0 9.4 21h5.2a2 2 0 0 0 2-1.9L17.5 7"/><path d="M10.5 11v6M13.5 11v6"/>',
    delete_sweep: '<path d="M3 7h8M3 11h8M3 15h5"/><path d="M13 9h7l-.7 10.1a2 2 0 0 1-2 1.9h-1.6a2 2 0 0 1-2-1.9z"/>',
    undo: '<path d="M4 10h9.5a5 5 0 0 1 0 10H10"/><path d="M4 10l4-4M4 10l4 4"/>',
    swap: '<path d="M4 8h13l-3.2-3.2M20 16H7l3.2 3.2"/>',
    save: '<path d="M5 5.8A1.8 1.8 0 0 1 6.8 4h8.4L20 8.8v9.4A1.8 1.8 0 0 1 18.2 20H6.8A1.8 1.8 0 0 1 5 18.2z"/><path d="M8.5 4v5h7"/><circle cx="12.5" cy="14.5" r="2.2"/>',

    /* visibility / rating */
    visibility: '<path d="M12 5.2c-5 0-8.6 4.2-9.6 6.8 1 2.6 4.6 6.8 9.6 6.8s8.6-4.2 9.6-6.8C20.6 9.4 17 5.2 12 5.2z"/><circle cx="12" cy="12" r="3"/>',
    visibility_off: '<path d="M4 4l16 16"/><path d="M9.8 5.5A9.7 9.7 0 0 1 12 5.2c5 0 8.6 4.2 9.6 6.8a13.4 13.4 0 0 1-2.5 3.7"/><path d="M6.4 7.7A13.3 13.3 0 0 0 2.4 12c1 2.6 4.6 6.8 9.6 6.8a9.6 9.6 0 0 0 3.4-.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    thumb_up: '<path d="M7.5 21V10.2L11.4 3l1 .5A2.6 2.6 0 0 1 13.6 6.3L13 10.2h5a2 2 0 0 1 2 2.3l-1.1 6.4A2 2 0 0 1 16.9 21z"/><path d="M7.5 10.2H4V21h3.5"/>',
    thumb_down: '<path d="M16.5 3v10.8L12.6 21l-1-.5A2.6 2.6 0 0 1 10.4 17.7L11 13.8H6a2 2 0 0 1-2-2.3l1.1-6.4A2 2 0 0 1 7.1 3z"/><path d="M16.5 13.8H20V3h-3.5"/>',
    star: '<path d="M12 4.2l2.5 5.1 5.6.8-4.1 4 1 5.6-5-2.7-5 2.7 1-5.6-4.1-4 5.6-.8z"/>',
    push_pin: '<path d="M12 3l5 5-2.2 1v4.2L18 16H6l3.2-2.8V9L7 8z"/><path d="M12 16v5"/>',

    /* disclosure / navigation */
    expand_more: '<path d="M7 10l5 5 5-5"/>',
    expand_less: '<path d="M7 14l5-5 5 5"/>',
    chevron_right: '<path d="M10 6.5l5.5 5.5L10 17.5"/>',
    arrow_upward: '<path d="M12 20V5"/><path d="M6 11l6-6 6 6"/>',
    arrow_downward: '<path d="M12 4v15"/><path d="M6 13l6 6 6-6"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    drag_indicator: '<circle cx="9.5" cy="6" r="1.3"/><circle cx="14.5" cy="6" r="1.3"/><circle cx="9.5" cy="12" r="1.3"/><circle cx="14.5" cy="12" r="1.3"/><circle cx="9.5" cy="18" r="1.3"/><circle cx="14.5" cy="18" r="1.3"/>',
    more_vert: '<circle cx="12" cy="5.4" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="18.6" r="1.4"/>',
    open_in_new: '<path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/><path d="M18 14.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3.5"/>',

    /* decisions */
    check: '<path d="M5 12.8l4.6 4.6L19 7"/>',
    check_circle: '<circle cx="12" cy="12" r="8.2"/><path d="M8.4 12.3l2.5 2.5 4.7-5.1"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    block: '<circle cx="12" cy="12" r="8.2"/><path d="M6.4 6.4l11.2 11.2"/>',
    error: '<circle cx="12" cy="12" r="8.2"/><path d="M12 7.8v5.4M12 16.4h.01"/>',
    warning: '<path d="M12 4.2l8.8 15.6H3.2z"/><path d="M12 9.6v4.4M12 17h.01"/>',
    hourglass: '<path d="M7 4h10M7 20h10"/><path d="M8 4v3.2l4 4 4-4V4M8 20v-3.2l4-4 4 4V20"/>',

    /* execution */
    play_arrow: '<path d="M8 5l11 7-11 7z"/>',
    skip_next: '<path d="M6 5l9.5 7L6 19z"/><path d="M18 5v14"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v4h-4"/>',
    terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5M13 14.5h4"/>',
    settings: '<circle cx="12" cy="12" r="3.2"/><path d="M12 4v2.6M12 17.4V20M4 12h2.6M17.4 12H20M6.3 6.3l1.9 1.9M15.8 15.8l1.9 1.9M17.7 6.3l-1.9 1.9M8.2 15.8l-1.9 1.9"/>',
    build: '<path d="M14.5 6a3.5 3.5 0 0 1 4.9 4.2l-8.1 8.1a2.5 2.5 0 1 1-3.5-3.5l8.1-8.1"/><path d="M5 5l3 3"/>',
    bolt: '<path d="M13.2 3L6 13.2h4.8L9.6 21l7.4-10.2h-4.8z"/>',
    lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.2 10.5V7.8a3.8 3.8 0 0 1 7.6 0v2.7"/>',

    /* content types */
    description: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h5"/>',
    book: '<path d="M4.5 5a2 2 0 0 1 2-2H11v18H6.5a2 2 0 0 1-2-2z"/><path d="M19.5 5a2 2 0 0 0-2-2H13v18h4.5a2 2 0 0 0 2-2z"/>',
    folder: '<path d="M4 7.2a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
    folder_open: '<path d="M4 7.2a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2H4z"/><path d="M4 9.2h17.2l-2 8.4a2 2 0 0 1-2 1.6H6.5a2 2 0 0 1-2-1.6z"/>',
    create_new_folder: '<path d="M4 7.2a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M12 10.6v5.4M9.3 13.3h5.4"/>',
    inventory: '<path d="M4 8l8-4 8 4v8l-8 4-8-4z"/><path d="M4 8l8 4 8-4M12 12v8"/>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1.2"/><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><path d="M9.5 12h5"/>',
    image: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 16.5l-5.2-5.2L9.5 17.5"/>',
    photo_camera: '<path d="M4 8h3l1.6-2.2h6.8L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13.5" r="3.2"/>',
    bar_chart: '<path d="M6 20V11M12 20V5M18 20v-6"/>',
    grid_view: '<rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/>',
    dns: '<rect x="4" y="4" width="16" height="6" rx="2"/><rect x="4" y="14" width="16" height="6" rx="2"/><path d="M8 7h.01M8 17h.01"/>',
    search: '<circle cx="11" cy="11" r="6"/><path d="M15.4 15.4L20 20"/>',
    psychology: '<path d="M7.4 15.4A4.2 4.2 0 0 1 8 7.2 5.2 5.2 0 0 1 17.4 8.4 3.6 3.6 0 0 1 16.6 15.4z"/><circle cx="7.2" cy="18.8" r="1.2"/><circle cx="10.4" cy="20.6" r="0.9"/>',
    smart_toy: '<rect x="4" y="8" width="16" height="11" rx="3.2"/><path d="M12 8V4.8"/><circle cx="9.2" cy="13.2" r="1"/><circle cx="14.8" cy="13.2" r="1"/>',
    local_fire: '<path d="M12 3.2s4.8 3.9 4.8 8a4.8 4.8 0 0 1-9.6 0c0-1.9 1-3 2-3.9 0 1.9 1 2.9 1.9 2.9s1-3.9-1.1-7z"/>',
    payments: '<rect x="3" y="6" width="18" height="12" rx="2.4"/><circle cx="12" cy="12" r="2.6"/><path d="M7 12h.01M17 12h.01"/>',
    add: '<path d="M12 5v14M5 12h14"/>',
    remove: '<path d="M5 12h14"/>'
};

var _mdIconCache = {};

/**
 * Build an inline SVG icon string.
 * @param {string} name key into MD_ICON_GLYPHS (Material Symbols naming)
 * @param {number} [size=18] rendered edge length in px
 * @returns {string} SVG markup, or '' when the name is unknown
 */
function mdIcon(name, size) {
    var px = size || 18;
    var key = name + '@' + px;
    var hit = _mdIconCache[key];
    if (hit !== undefined) return hit;
    var glyph = MD_ICON_GLYPHS[name];
    if (!glyph) {
        console.warn('mdIcon: unknown glyph "' + name + '"');
        _mdIconCache[key] = '';
        return '';
    }
    var svg = '<svg class="md-icon" width="' + px + '" height="' + px + '"'
        + ' viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
        + ' stroke-linecap="round" stroke-linejoin="round"'
        + ' aria-hidden="true" focusable="false">' + glyph + '</svg>';
    _mdIconCache[key] = svg;
    return svg;
}

window.mdIcon = mdIcon;
window.MD_ICON_GLYPHS = MD_ICON_GLYPHS;