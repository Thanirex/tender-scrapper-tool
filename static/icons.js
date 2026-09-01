// TAiQ icon set — inline stroke SVG, no icon font and no CDN, so an icon can
// never fail to load or arrive a frame late. Load this BEFORE nav.js and any
// page script that calls icon(): both read ICON_PATHS from here.
//
// Every path is drawn on a 24×24 grid with no fill, so a single stroke weight
// and `currentColor` keep the whole set visually consistent. Add new icons
// here rather than inlining SVG at the call site.

const ICON_PATHS = {
    // ── Navigation ──────────────────────────────────────────────────────────
    home:    '<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/>',
    search:  '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    bot:     '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4"/><circle cx="9" cy="14" r="1.2"/><circle cx="15" cy="14" r="1.2"/>',
    pulse:   '<path d="M3 12h4l2.5-7 5 14L17 12h4"/>',
    users:   '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M17 5.2a3.5 3.5 0 0 1 0 6.6"/><path d="M18.5 20a6.4 6.4 0 0 0-3-5.4"/>',
    file:    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',

    // ── KPI row ─────────────────────────────────────────────────────────────
    layers:  '<path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z"/><path d="m3 12 9 4.5L21 12"/><path d="m3 16.5 9 4.5 9-4.5"/>',
    inbox:   '<path d="M12 3v9"/><path d="m8.5 8.5 3.5 3.5 3.5-3.5"/><path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
    eye:     '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    clock:   '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',

    // ── Daily activity ──────────────────────────────────────────────────────
    globe:   '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18Z"/>',
    key:     '<circle cx="7.5" cy="15.5" r="3.5"/><path d="m10 13 8.5-8.5"/><path d="m15.5 7.5 2 2"/><path d="m18 5 2 2"/>',
    user:    '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',

    // ── Review states ───────────────────────────────────────────────────────
    check:      '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9.5"/>',
    cross:      '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/>',
    hourglass:  '<path d="M7 3h10"/><path d="M7 21h10"/><path d="M17 3v4l-5 5 5 5v4"/><path d="M7 3v4l5 5-5 5v4"/>',
    clipboard:  '<path d="M9 4H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-2"/><rect x="9" y="2.5" width="6" height="3.5" rx="1"/><path d="m9.5 13.5 2 2 3.5-3.5"/>',
    download:   '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',

    // ── Actions ─────────────────────────────────────────────────────────────
    play:    '<circle cx="12" cy="12" r="9"/><path d="m10 8.5 6 3.5-6 3.5Z"/>',
    edit:    '<path d="M12 20h8"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
    link:    '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',

    // ── Value banner ────────────────────────────────────────────────────────
    sparkle: '<path d="m12 3 1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Z"/><path d="m18.6 16.4.6 1.6 1.6.6-1.6.6-.6 1.6-.6-1.6-1.6-.6 1.6-.6Z"/>',
    target:  '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.3"/>',
    chart:   '<path d="M3 20h18"/><path d="M6 20v-6"/><path d="M12 20V6"/><path d="M18 20v-9"/>',

    // ── Review modal ────────────────────────────────────────────────────────
    chat:      '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/>',
    paperclip: '<path d="M20 11.5 12 19.5a5 5 0 0 1-7-7l8.5-8.5a3.4 3.4 0 0 1 4.8 4.8L9.7 17.4a1.8 1.8 0 0 1-2.5-2.5l7.8-7.8"/>',
    close:     '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',

    // ── Empty states ────────────────────────────────────────────────────────
    calendar: '<rect x="3.5" y="5" width="17" height="16" rx="2"/><path d="M3.5 10h17"/><path d="M8 3v4"/><path d="M16 3v4"/>',
    tray:     '<path d="M4 13h4l1.5 2.5h5L16 13h4"/><path d="M6 4h12l3 9v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Z"/>',
};

// `cls` lets the caller size and colour the icon from CSS; everything inherits
// currentColor so an icon always matches the text it sits beside.
function icon(name, cls = '', strokeWidth = 1.8) {
    const d = ICON_PATHS[name];
    if (!d) return '';
    return `<svg class="ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round"
        stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
}
