document.addEventListener('DOMContentLoaded', () => {
    initNav();   // all roles can view the dashboard

    const today = new Date();
    let selectedDate = _fmtDate(today);
    let calYear      = today.getFullYear();
    let calMonth     = today.getMonth(); // 0-indexed
    let dataDates    = new Set();
    let allSites     = new Set();

    const calGrid     = document.getElementById('cal-grid');
    const calLabel    = document.getElementById('cal-month-label');
    const statsRow    = document.getElementById('stats-row');
    const actWrap     = document.getElementById('activity-wrap');
    const tendersGrid = document.getElementById('tenders-grid');
    const dateLabel   = document.getElementById('selected-date-label');
    const filterSite  = document.getElementById('filter-site');
    const filterKw    = document.getElementById('filter-keyword');

    document.getElementById('cal-prev').addEventListener('click', () => {
        calMonth--;
        if (calMonth < 0) { calMonth = 11; calYear--; }
        renderCalendar();
    });
    document.getElementById('cal-next').addEventListener('click', () => {
        calMonth++;
        if (calMonth > 11) { calMonth = 0; calYear++; }
        renderCalendar();
    });
    let isUpdatingDropdown = false;
    let currentTendersRequestId = 0;

    document.getElementById('apply-filter').addEventListener('click', loadTenders);
    filterSite.addEventListener('change', () => {
        if (!isUpdatingDropdown) loadTenders();
    });

    // ── TAiQ status widget ─────────────────────────────────────────────────
    let taiqPollTimer = null;
    let latestTaiqRun = null;

    async function loadTaiqStatus() {
        const res = await authFetch('/taiq/status');
        if (!res) return;
        const { run } = await res.json();
        latestTaiqRun = run;
        renderTaiqWidget(run);
        // Refresh the "Last Tender Update" stat card in place (stats row may
        // have been rendered before this poll returned)
        const luCard = document.getElementById('last-update-card');
        if (luCard) luCard.outerHTML = _lastUpdateCardHtml();
        clearInterval(taiqPollTimer);
        taiqPollTimer = setInterval(loadTaiqStatus, run && run.status === 'running' ? 8000 : 60000);
    }

    // ── "Last Tender Update" stat card ─────────────────────────────────────
    function _lastUpdateCardHtml() {
        const r = latestTaiqRun;
        let timeStr = '—', dateStr = 'No runs yet', statusLbl = '—', color = '#94a3b8';
        if (r) {
            const isRunning = r.status === 'running';
            const iso = (isRunning ? r.started_at : (r.finished_at || r.started_at)) || '';
            if (iso.includes('T')) {
                dateStr = _pretty(iso.split('T')[0]);
                timeStr = iso.split('T')[1].substring(0, 5);
            }
            const map = {
                running:  ['Running',   '#f59e0b'],
                complete: ['Completed', '#22c55e'],
                failed:   ['Failed',    '#ef4444'],
                stopped:  ['Stopped',   '#a855f7'],
            };
            [statusLbl, color] = map[r.status] || [r.status, '#94a3b8'];
        }
        return `
            <div class="stat-card-v2 scard-lastupdate" id="last-update-card">
                <div class="sc2-icon">🕒</div>
                <span class="sc2-num sc2-num-time">${timeStr}</span>
                <span class="sc2-label">Last Tender Update · ${dateStr}</span>
                <span class="lu-status" style="color:${color}">
                    <span class="lu-status-dot" style="background:${color}"></span>${statusLbl}
                </span>
            </div>`;
    }

    function renderTaiqWidget(run) {
        const dot   = document.getElementById('dtc-dot');
        const desc  = document.getElementById('dtc-desc');
        const pills = document.getElementById('dtc-pills');

        dot.className = 'dtc-dot';

        if (!run) {
            dot.classList.add('dtc-dot-idle');
            desc.textContent = 'No runs recorded yet · Scheduled daily at 7:00 AM IST';
            pills.innerHTML  = '';
            return;
        }

        const isToday = run.run_date === _fmtDate(today);
        const prefix  = isToday ? 'Today' : _pretty(run.run_date);

        if (run.status === 'running') {
            dot.classList.add('dtc-dot-running');
            const curKw = run.current_keyword ? ` · ${run.current_keyword}` : '';
            desc.textContent = `Running now${curKw}`;
            pills.innerHTML  = `
                <span class="dtc-pill dtc-pill-kw">${run.keywords_done || 0} / ${run.total_keywords || '?'} keywords</span>
                <span class="dtc-pill dtc-pill-td">${run.total_tenders || 0} tenders found</span>`;
        } else if (run.status === 'complete') {
            dot.classList.add('dtc-dot-complete');
            desc.textContent = `${prefix}: Complete`;
            pills.innerHTML  = `
                <span class="dtc-pill dtc-pill-kw">${run.keywords_done || 0} / ${run.total_keywords || 0} keywords</span>
                <span class="dtc-pill dtc-pill-td">${run.total_tenders || 0} tenders</span>
                <span class="dtc-pill">${_duration(run.started_at, run.finished_at)}</span>`;
        } else if (run.status === 'failed') {
            dot.classList.add('dtc-dot-failed');
            const errShort = run.error_msg
                ? run.error_msg.substring(0, 70) + (run.error_msg.length > 70 ? '…' : '')
                : 'Unknown error';
            desc.textContent = `${prefix}: Failed — ${errShort}`;
            pills.innerHTML  = '';
        } else if (run.status === 'stopped') {
            dot.classList.add('dtc-dot-stopped');
            desc.textContent = `${prefix}: Stopped`;
            pills.innerHTML  = `
                <span class="dtc-pill dtc-pill-kw">${run.keywords_done || 0} / ${run.total_keywords || 0} keywords</span>
                <span class="dtc-pill dtc-pill-td">${run.total_tenders || 0} tenders</span>`;
        } else {
            dot.classList.add('dtc-dot-idle');
            desc.textContent = `${prefix}: ${run.status}`;
            pills.innerHTML  = '';
        }
    }

    loadTaiqStatus();

    // ── Last 3 days report cards ───────────────────────────────────────────
    async function loadDailyReport() {
        const row = document.getElementById('daily-report-row');
        if (!row) return;
        const res = await authFetch('/dashboard/report?days=3');
        if (!res) return;
        const { days } = await res.json();
        if (!days || !days.length) { row.innerHTML = ''; return; }

        const names = ['Today', 'Yesterday'];
        row.innerHTML = days.map((d, i) => {
            const name     = names[i] || _pretty(d.date);
            const sub      = names[i] ? _pretty(d.date) : '';
            const reviewed = d.approved + d.rejected;
            const pct      = d.scraped ? Math.round(100 * reviewed / d.scraped) : 0;
            const downloaded = d.downloaded ?? 0;
            const dlPct    = d.scraped ? Math.round(100 * downloaded / d.scraped) : 0;
            const chipMap  = {
                complete: ['Run complete', 'drc-chip-complete'],
                running:  ['Running now',  'drc-chip-running'],
                failed:   ['Run failed',   'drc-chip-failed'],
                stopped:  ['Run stopped',  'drc-chip-stopped'],
            };
            const [chipLbl, chipCls] = chipMap[d.taiq_status] || ['No data run', 'drc-chip-none'];
            return `
            <div class="day-report-card${i === 0 ? ' drc-today' : ''}">
                <div class="drc-head">
                    <span class="drc-day">${name}${sub ? ` <span class="drc-date">· ${sub}</span>` : ''}</span>
                    <span class="drc-chip ${chipCls}">${chipLbl}</span>
                </div>
                <div class="drc-main">
                    <span class="drc-num">${d.scraped}</span>
                    <span class="drc-num-label">tender${d.scraped !== 1 ? 's' : ''} scraped</span>
                </div>
                <div class="drc-line">🤖 TAiQ: <b>${d.taiq}</b> &nbsp;·&nbsp; 👤 Manual: <b>${d.manual}</b></div>
                <div class="drc-line">🌐 Sites scanned: <b>${d.sites_scanned}</b> &nbsp;·&nbsp; Keywords: <b>${d.keywords ?? 0}</b></div>
                <div class="drc-review">
                    <span class="drc-rv drc-rv-app" title="Approved">✅ ${d.approved}</span>
                    <span class="drc-rv drc-rv-rej" title="Rejected">❌ ${d.rejected}</span>
                    <span class="drc-rv drc-rv-pen" title="Awaiting review">⏳ ${d.pending} pending</span>
                    ${d.approval_rate !== null && d.approval_rate !== undefined
                        ? `<span class="drc-rv-rate">${d.approval_rate}% approved</span>` : ''}
                </div>
                <div class="drc-bars">
                    <div class="drc-bar-track" title="${pct}% of this day's tenders reviewed">
                        <div class="drc-bar-fill" style="width:${pct}%"></div>
                    </div>
                    <div class="drc-bar-label">${reviewed} of ${d.scraped} reviewed</div>
                    <div class="drc-bar-track" title="${dlPct}% of this day's tenders have documents saved">
                        <div class="drc-bar-fill drc-bar-fill-dl" style="width:${dlPct}%"></div>
                    </div>
                    <div class="drc-bar-label">${downloaded} of ${d.scraped} downloaded</div>
                </div>
            </div>`;
        }).join('');
    }

    loadDailyReport();
    setInterval(loadDailyReport, 5 * 60 * 1000);   // stays fresh across midnight

    // Load dates that have data, then bootstrap
    authFetch('/dashboard/dates')
        .then(r => r && r.json())
        .then(dates => {
            if (dates) dataDates = new Set(dates);
            renderCalendar();
            loadDay(selectedDate);
        });

    // ── Calendar & Date Range Animation ─────────────────────────────────────
    let rangeStartDate = null;
    let rangeEndDate   = null;
    let hoverDate      = null;
    let isCustomRangeMode = false;
    let loadedTendersCache = [];

    const customBox = document.getElementById('cal-custom-box');
    const fromInput = document.getElementById('cal-from-date');
    const toInput   = document.getElementById('cal-to-date');
    const applyCustomBtn = document.getElementById('cal-apply-custom');

    // Preset Chip Click Listeners
    const presetRow = document.getElementById('cal-presets-row');
    if (presetRow) {
        presetRow.addEventListener('click', (e) => {
            const btn = e.target.closest('.cal-preset-chip');
            if (!btn || btn.id === 'cal-apply-custom') return;

            const wasActive = btn.classList.contains('active');
            const preset    = btn.dataset.preset;
            const now       = new Date();

            // If user clicks an ALREADY ACTIVE pill (except if switching to normal today), deselect it & reset to default Today
            if (wasActive && preset !== 'today') {
                presetRow.querySelectorAll('.cal-preset-chip').forEach(b => b.classList.remove('active'));
                const todayBtn = presetRow.querySelector('[data-preset="today"]');
                if (todayBtn) todayBtn.classList.add('active');

                isCustomRangeMode = false;
                if (customBox) customBox.style.display = 'none';
                selectedDate   = _fmtDate(now);
                rangeStartDate = null;
                rangeEndDate   = null;

                renderCalendar();
                loadDay(selectedDate);
                return;
            }

            presetRow.querySelectorAll('.cal-preset-chip').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            if (preset === 'custom') {
                isCustomRangeMode = true;
                if (customBox) customBox.style.display = 'flex';
                if (!rangeStartDate || !rangeEndDate) {
                    const s = new Date(now); s.setDate(s.getDate() - 6);
                    rangeStartDate = _fmtDate(s);
                    rangeEndDate   = _fmtDate(now);
                }
                if (fromInput) fromInput.value = rangeStartDate;
                if (toInput)   toInput.value   = rangeEndDate;
                selectedDate = null;
            } else {
                // Normal preset mode
                isCustomRangeMode = false;
                if (customBox) customBox.style.display = 'none';

                if (preset === 'today') {
                    selectedDate   = _fmtDate(now);
                    rangeStartDate = null;
                    rangeEndDate   = null;
                } else if (preset === 'yesterday') {
                    const y = new Date(now); y.setDate(y.getDate() - 1);
                    selectedDate   = _fmtDate(y);
                    rangeStartDate = null;
                    rangeEndDate   = null;
                } else if (preset === '7days') {
                    const s = new Date(now); s.setDate(s.getDate() - 6);
                    rangeStartDate = _fmtDate(s);
                    rangeEndDate   = _fmtDate(now);
                    selectedDate   = null;
                } else if (preset === '30days') {
                    const s = new Date(now); s.setDate(s.getDate() - 29);
                    rangeStartDate = _fmtDate(s);
                    rangeEndDate   = _fmtDate(now);
                    selectedDate   = null;
                }
            }

            renderCalendar();
            if (rangeStartDate && rangeEndDate) {
                loadDayRange(rangeStartDate, rangeEndDate);
            } else {
                loadDay(selectedDate);
            }
        });
    }

    function _onCustomDateInputsChanged() {
        const fVal = fromInput ? fromInput.value : '';
        const tVal = toInput ? toInput.value : '';
        if (fVal && tVal) {
            rangeStartDate = fVal < tVal ? fVal : tVal;
            rangeEndDate   = fVal < tVal ? tVal : fVal;
            selectedDate   = null;
            renderCalendar();
            loadDayRange(rangeStartDate, rangeEndDate);
        }
    }

    if (applyCustomBtn) {
        applyCustomBtn.addEventListener('click', _onCustomDateInputsChanged);
    }
    if (fromInput) {
        fromInput.addEventListener('change', _onCustomDateInputsChanged);
    }
    if (toInput) {
        toInput.addEventListener('change', _onCustomDateInputsChanged);
    }

    function renderCalendar() {
        const months = ['January','February','March','April','May','June',
                        'July','August','September','October','November','December'];
        calLabel.textContent = `${months[calMonth]} ${calYear}`;

        const firstDay = new Date(calYear, calMonth, 1).getDay(); // 0=Sun
        const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();
        const startOffset = (firstDay + 6) % 7;

        let html = '';
        for (let i = 0; i < startOffset; i++) {
            html += '<div class="cal-day cal-empty"></div>';
        }
        for (let d = 1; d <= daysInMonth; d++) {
            const dateStr = `${calYear}-${String(calMonth + 1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
            const isToday    = dateStr === _fmtDate(today);
            const isSelected = selectedDate ? (dateStr === selectedDate) : false;
            const hasData    = dataDates.has(dateStr);
            const isFuture   = dateStr > _fmtDate(today);

            let cls = 'cal-day';
            if (isToday)    cls += ' cal-today';
            if (isSelected) cls += ' cal-selected';
            if (hasData)    cls += ' cal-has-data';
            if (isFuture)   cls += ' cal-future';

            if (rangeStartDate && rangeEndDate) {
                if (dateStr === rangeStartDate) cls += ' cal-range-start';
                else if (dateStr === rangeEndDate) cls += ' cal-range-end';
                else if (dateStr > rangeStartDate && dateStr < rangeEndDate) cls += ' cal-range-mid';
            } else if (rangeStartDate && !rangeEndDate) {
                if (dateStr === rangeStartDate) cls += ' cal-range-start';
                else if (hoverDate) {
                    const min = rangeStartDate < hoverDate ? rangeStartDate : hoverDate;
                    const max = rangeStartDate < hoverDate ? hoverDate : rangeStartDate;
                    if (dateStr >= min && dateStr <= max) cls += ' cal-hover-range';
                }
            }

            html += `<div class="${cls}" data-date="${dateStr}">${d}${hasData ? '<span class="cal-dot"></span>' : ''}</div>`;
        }
        calGrid.innerHTML = html;

        const dayEls = calGrid.querySelectorAll('.cal-day:not(.cal-empty):not(.cal-future)');
        dayEls.forEach(el => {
            el.addEventListener('mouseenter', () => {
                if (isCustomRangeMode && rangeStartDate && !rangeEndDate) {
                    hoverDate = el.dataset.date;
                    renderCalendar();
                }
            });

            el.addEventListener('click', () => {
                const clickedDate = el.dataset.date;

                if (!isCustomRangeMode) {
                    // Normal single date click mode
                    selectedDate   = clickedDate;
                    rangeStartDate = null;
                    rangeEndDate   = null;
                    if (presetRow) presetRow.querySelectorAll('.cal-preset-chip').forEach(b => b.classList.remove('active'));
                    renderCalendar();
                    loadDay(selectedDate);
                    return;
                }

                // Custom Range click mode
                if (!rangeStartDate || (rangeStartDate && rangeEndDate)) {
                    rangeStartDate = clickedDate;
                    rangeEndDate   = null;
                    selectedDate   = null;
                    if (fromInput) fromInput.value = rangeStartDate;
                    if (toInput)   toInput.value   = '';
                    renderCalendar();
                } else {
                    if (clickedDate < rangeStartDate) {
                        rangeEndDate   = rangeStartDate;
                        rangeStartDate = clickedDate;
                    } else if (clickedDate === rangeStartDate) {
                        rangeEndDate = null;
                        selectedDate = clickedDate;
                    } else {
                        rangeEndDate = clickedDate;
                    }
                    if (fromInput) fromInput.value = rangeStartDate;
                    if (toInput)   toInput.value   = rangeEndDate || rangeStartDate;
                    hoverDate = null;
                    selectedDate = null;
                    renderCalendar();
                    if (rangeStartDate && rangeEndDate) {
                        loadDayRange(rangeStartDate, rangeEndDate);
                    } else {
                        loadDay(rangeStartDate);
                    }
                }
            });
        });

        calGrid.addEventListener('mouseleave', () => {
            if (isCustomRangeMode && rangeStartDate && !rangeEndDate && hoverDate) {
                hoverDate = null;
                renderCalendar();
            }
        });
    }

    async function loadDayRange(startDate, endDate) {
        dateLabel.textContent = `Overview for ${_pretty(startDate)} – ${_pretty(endDate)}`;
        await loadStats(null, startDate, endDate);
        filterSite.value = '';
        filterKw.value   = '';
        await loadTenders();
    }


    // ── Data loading ───────────────────────────────────────────────────────

    async function loadDay(dateStr) {
        const isToday = dateStr === _fmtDate(today);
        dateLabel.textContent = isToday ? "Today's overview" : `Overview for ${_pretty(dateStr)}`;
        // Stats must finish first — it populates the site dropdown before tenders load
        await loadStats(dateStr);
        // Reset filters whenever the date changes so the dropdown is clean
        filterSite.value = '';
        filterKw.value   = '';
        await loadTenders();
    }

    // Only these four carry bespoke branding. Every other site is described by
    // its `display_name` in sites_config.json, which the server sends with each
    // row — so a newly added site shows up correctly with no change here.
    const _SITE_CFG = {
        ungm:   { icon: '🌐', label: 'UNGM',   cls: 'scard-ungm'   },
        devnet: { icon: '💼', label: 'DevNet', cls: 'scard-devnet' },
        ngobox: { icon: '📦', label: 'NGOBox', cls: 'scard-ngobox' },
        taiq:   { icon: '🤖', label: 'TAiQ',   cls: 'scard-taiq'   },
    };

    // Stable per-site accent colour so every site gets a distinct, consistent
    // hue without anyone having to maintain a colour table.
    const _SITE_COLORS = { ungm: '#f59e0b', devnet: '#3b82f6', ngobox: '#10b981', taiq: '#7c3aed' };

    function _siteColor(key) {
        if (_SITE_COLORS[key]) return _SITE_COLORS[key];
        let h = 0;
        for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 360;
        return `hsl(${h}, 62%, 48%)`;
    }

    // `row` is a by_site entry from /dashboard/stats; display_name comes from
    // the team's sites_config.json.
    function _siteLabel(row) {
        const key = String(row.site || '').toLowerCase();
        return _SITE_CFG[key]?.label || row.display_name || key.toUpperCase();
    }

    // Config display names are descriptive ("Mercy Corps Tenders (RFPs)") and
    // too long for a stat card or a badge, so trim them down to the org name.
    const _GENERIC_TAIL = /\s+(tenders?|procurement|solicitations?|bids?|jobs?|rfps?|eois?)$/i;

    function _siteShortLabel(row) {
        const key = String(row.site || '').toLowerCase();
        if (_SITE_CFG[key]) return _SITE_CFG[key].label;

        let name = String(row.display_name || key.toUpperCase());

        // A mid-string all-caps acronym is the name people actually use —
        // "International Solar Alliance (ISA) Procurement" → "ISA". A trailing
        // bracket is a category ("… (RFPs)"), so it is dropped instead.
        const acronym = name.match(/\(([A-Z0-9]{2,6})\)(?!\s*$)/);
        if (acronym) return acronym[1];

        name = name.split(' (')[0].split(' — ')[0].split(' & ')[0].trim();
        while (_GENERIC_TAIL.test(name)) name = name.replace(_GENERIC_TAIL, '').trim();
        if (!name) name = key.toUpperCase();

        if (name.length > 24) {
            const words = name.split(/\s+/);
            // An all-caps leading token is the org's own short name.
            if (/^[A-Z0-9-]{2,10}$/.test(words[0])) return words[0];
            name = words.slice(0, 3).join(' ');
        }
        return name.length > 24 ? name.slice(0, 23).trimEnd() + '…' : name;
    }

    async function loadStats(dateStr, startDate = null, endDate = null) {
        statsRow.innerHTML = '';
        actWrap.innerHTML  = '<p class="empty-msg">Loading…</p>';

        const runActivitySection = document.getElementById('run-activity-section');
        const isMultiDay = Boolean(startDate && endDate && startDate !== endDate);
        if (runActivitySection) {
            runActivitySection.style.display = isMultiDay ? 'none' : 'block';
        }

        const url = (startDate && endDate)
            ? `/dashboard/stats?start_date=${startDate}&end_date=${endDate}`
            : `/dashboard/stats?date=${dateStr}`;

        const res  = await authFetch(url);
        if (!res) return;
        const data = await res.json();

        // ── Stat cards v2 ──────────────────────────────────────────────────
        const siteTotals = data.by_site || [];
        const grandTotal = siteTotals.reduce((s, x) => s + x.count, 0);

        if (siteTotals.length === 0) {
            statsRow.innerHTML = `
                <div class="stat-card-v2 scard-runs" style="text-align:center">
                    <div class="sc2-icon">📋</div>
                    <span class="sc2-num">0</span>
                    <span class="sc2-label">No activity for this date</span>
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:0%"></div></div>
                </div>` + _lastUpdateCardHtml();
        } else {
            allSites = new Set(siteTotals.map(s => s.site));

            const totalCard = `
                <div class="stat-card-v2 scard-total">
                    <div class="sc2-icon">📊</div>
                    <span class="sc2-num">${grandTotal}</span>
                    <span class="sc2-label">Total Tenders</span>
                    <span class="sc2-sub">All time</span>
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:100%"></div></div>
                </div>`;

            const siteCards = siteTotals.map(s => {
                const key = String(s.site || '').toLowerCase();
                const cfg = _SITE_CFG[key];
                const label = _siteShortLabel(s);
                const icon = cfg?.icon || '📌';
                const pct = grandTotal > 0 ? Math.max(Math.round((s.count / grandTotal) * 100), 5) : 5;
                // Sites without a bespoke class get their accent inline, so they
                // never render as an unstyled card.
                const accent = cfg ? '' : ` style="--scard-accent:${_siteColor(key)}"`;
                return `
                    <div class="stat-card-v2 ${cfg?.cls || 'scard-generic'}"${accent}>
                        <div class="sc2-icon">${icon}</div>
                        <span class="sc2-num">${s.count}</span>
                        <span class="sc2-label" title="${_escapeHtml(_siteLabel(s))}">${_escapeHtml(label)}</span>
                        <span class="sc2-sub">All time</span>
                        <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:${pct}%"></div></div>
                    </div>`;
            }).join('');

            const runsCard = `
                <div class="stat-card-v2 scard-runs">
                    <div class="sc2-icon">▶</div>
                    <span class="sc2-num">${data.sessions.length}</span>
                    <span class="sc2-label">Runs</span>
                    <span class="sc2-sub">Selected period</span>
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:100%"></div></div>
                </div>`;

            statsRow.innerHTML = totalCard + siteCards + runsCard + _lastUpdateCardHtml();

            const currentVal = filterSite.value;
            isUpdatingDropdown = true;
            const labelByKey = Object.fromEntries(
                siteTotals.map(s => [String(s.site || '').toLowerCase(), _siteLabel(s)])
            );
            filterSite.innerHTML = '<option value="">All Sites</option>' +
                [...allSites].map(s => {
                    const lbl = labelByKey[s] || s.toUpperCase();
                    return `<option value="${s}">${_escapeHtml(lbl)}</option>`;
                }).join('');
            filterSite.value = currentVal || '';
            isUpdatingDropdown = false;
        }

        // ── Summary sentence ───────────────────────────────────────────────
        const summaryEl = document.getElementById('dash-summary');
        if (summaryEl) {
            if (grandTotal > 0) {
                const numSites = siteTotals.length;
                const dateText = (startDate && endDate)
                    ? `between ${_pretty(startDate)} and ${_pretty(endDate)}`
                    : (dateStr === _fmtDate(today) ? 'today' : `on ${_pretty(dateStr)}`);
                summaryEl.innerHTML =
                    `<strong>${grandTotal}</strong> tender${grandTotal !== 1 ? 's' : ''} found across ` +
                    `<strong>${numSites}</strong> site${numSites !== 1 ? 's' : ''} in ` +
                    `<strong>${data.sessions.length}</strong> run${data.sessions.length !== 1 ? 's' : ''} ` +
                    dateText;
            } else {
                summaryEl.textContent = '';
            }
        }

        // ── Side breakdown chart ───────────────────────────────────────────
        renderSideBreakdown(data);

        // ── Activity table ─────────────────────────────────────────────────
        if (!data.sessions || data.sessions.length === 0) {
            actWrap.innerHTML = `
                <div class="dash-empty-state">
                    <span class="des-icon">🗓️</span>
                    <span class="des-title">No runs on this date</span>
                    <span class="des-sub">Manual scrapes and TAiQ runs will appear here</span>
                </div>`;
        } else {
            const _statusDot = s => {
                const cols = { complete:'#22c55e', running:'#f59e0b', failed:'#ef4444', stopped:'#a855f7' };
                const c = cols[s.status] || '#94a3b8';
                return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c};margin-right:5px;vertical-align:middle;flex-shrink:0"></span>`;
            };
            const _truncKw = kw => {
                if (!kw) return '—';
                const parts = kw.split(',').map(k => k.trim()).filter(Boolean);
                if (parts.length <= 2) return parts.join(', ');
                return `${parts.slice(0, 2).join(', ')} <span style="color:var(--text-light)">+${parts.length - 2} more</span>`;
            };
            actWrap.innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>User</th>
                            <th>Site</th>
                            <th>Keywords</th>
                            <th style="text-align:center">Tenders</th>
                            <th>Started</th>
                            <th>Download</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.sessions.map(s => `
                            <tr${s.source === 'taiq' ? ' class="taiq-activity-row"' : ''}>
                                <td style="white-space:nowrap">
                                    ${_statusDot(s)}<strong>${s.source === 'taiq' ? '🤖 ' : ''}${s.username}</strong>
                                </td>
                                <td><span class="site-badge site-${s.site}" title="${_escapeHtml(_siteLabel(s))}">${_escapeHtml(_siteShortLabel(s))}</span></td>
                                <td class="keywords-cell">${_truncKw(s.keywords)}</td>
                                <td style="text-align:center"><strong>${s.tenders_found}</strong></td>
                                <td class="time-cell">${_timeOnly(s.created_at)}</td>
                                <td>${s.source === 'taiq'
                                    ? `<a class="dl-link" href="/taiq-work">View →</a>`
                                    : s.zip_filename
                                        ? `<a class="dl-link" href="/download?name=${encodeURIComponent(s.zip_filename)}&token=${getToken()}" download>ZIP</a>`
                                        : `<span style="color:var(--text-light);font-size:0.8rem">—</span>`
                                }</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>`;
        }
    }

    function renderSideBreakdown(data) {
        const panel = document.getElementById('side-breakdown-panel');
        if (!panel) return;
        const sites = data.by_site || [];
        const total = sites.reduce((s, x) => s + x.count, 0);
        if (!sites.length || total === 0) {
            panel.innerHTML = `
                <div class="sbd-title">Today at a glance</div>
                <p class="sbd-empty">No tenders found yet</p>`;
            return;
        }
        const rows = sites.map(s => {
            const pct   = Math.max(Math.round((s.count / total) * 100), 4);
            const color = _siteColor(String(s.site || '').toLowerCase());
            const label = _siteShortLabel(s);
            return `
                <div class="sbd-row">
                    <span class="sbd-site-label" title="${_escapeHtml(label)}">${_escapeHtml(label)}</span>
                    <div class="sbd-bar-track">
                        <div class="sbd-bar-fill" style="width:${pct}%;background:${color}"></div>
                    </div>
                    <span class="sbd-count">${s.count}</span>
                </div>`;
        }).join('');
        panel.innerHTML = `
            <div class="sbd-title">Today at a glance</div>
            ${rows}
            <div class="sbd-footer">
                <span>${data.sessions.length} run${data.sessions.length !== 1 ? 's' : ''}</span>
                <strong style="color:var(--text-main)">${total} total</strong>
            </div>`;
    }

    async function loadTenders() {
        const reqId = ++currentTendersRequestId;
        tendersGrid.innerHTML = '<p class="empty-msg">Loading…</p>';
        const site    = filterSite.value;
        const keyword = filterKw.value.trim();
        let url = (rangeStartDate && rangeEndDate)
            ? `/dashboard/tenders?start_date=${rangeStartDate}&end_date=${rangeEndDate}`
            : `/dashboard/tenders?date=${selectedDate || _fmtDate(today)}`;
        if (site)    url += `&site=${encodeURIComponent(site)}`;
        if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;

        const res = await authFetch(url);
        if (!res || reqId !== currentTendersRequestId) return;
        const tenders = await res.json();
        if (reqId !== currentTendersRequestId) return;
        loadedTendersCache = tenders || [];

        if (!tenders || tenders.length === 0) {
            const isFiltered = filterSite.value || filterKw.value.trim();
            tendersGrid.innerHTML = `
                <div class="dash-empty-state">
                    <span class="des-icon">${isFiltered ? '🔍' : '📭'}</span>
                    <span class="des-title">${isFiltered ? 'No tenders found' : 'No tenders yet'}</span>
                    <span class="des-sub">${isFiltered ? 'Try adjusting your filters or keywords' : 'TAiQ and manual runs will populate this once they complete'}</span>
                </div>`;
            return;
        }

        tendersGrid.innerHTML = `
            <div class="table-wrap">
                <table class="tenders-table">
                    <thead>
                        <tr>
                            <th>Keyword</th>
                            <th>Site</th>
                            <th>Tender Title</th>
                            <th>Published On</th>
                            <th>Status</th>
                            <th style="text-align:right">Action</th>
                        </tr>
                    </thead>
                    <tbody>${tenders.map(t => _tenderRow(t)).join('')}</tbody>
                </table>
            </div>`;
    }

    // ── Tender Inspection Modal ─────────────────────────────────────────────
    const tdModalOverlay = document.getElementById('td-modal-overlay');
    const tdModalClose   = document.getElementById('td-modal-close');

    if (tdModalClose) {
        tdModalClose.addEventListener('click', () => {
            if (tdModalOverlay) tdModalOverlay.style.display = 'none';
        });
    }
    if (tdModalOverlay) {
        tdModalOverlay.addEventListener('click', (e) => {
            if (e.target === tdModalOverlay) tdModalOverlay.style.display = 'none';
        });
    }

    function openTenderDetailModal(tenderId) {
        const tender = loadedTendersCache.find(t => String(t.id) === String(tenderId));
        if (!tender) return;

        const titleEl = document.getElementById('td-modal-title');
        const pillsEl = document.getElementById('td-modal-meta-pills');
        const bodyEl  = document.getElementById('td-modal-body');

        if (titleEl) titleEl.textContent = tender.title || 'Opportunity Details';

        const rvStatus = tender.review_status || 'pending';
        const rvIcon   = rvStatus === 'approved' ? '✅' : rvStatus === 'rejected' ? '❌' : '⏳';

        if (pillsEl) {
            pillsEl.innerHTML = `
                <span class="site-badge site-${tender.site}" title="${_escapeHtml(_siteLabel(tender))}">${_escapeHtml(_siteShortLabel(tender))}</span>
                <span class="kw-tag">${_escapeHtml(tender.keyword || '')}</span>
                <span class="td-mpill td-mpill-${rvStatus}">${rvIcon} ${rvStatus}</span>
                ${tender.source === 'taiq' ? '<span class="taiq-source-tag">🤖 TAiQ Auto</span>' : ''}
            `;
        }

        const fields = tender.fields || {};
        let fieldsGridHtml = '';
        if (Object.keys(fields).length > 0) {
            fieldsGridHtml = `
                <div class="td-section-title">Summary & Extracted Fields</div>
                <div class="td-fields-grid">
                    ${Object.entries(fields).map(([k, v]) => `
                        <div class="td-field-card">
                            <span class="td-fc-label">${_escapeHtml(k)}</span>
                            <span class="td-fc-val">${_escapeHtml(String(v))}</span>
                        </div>
                    `).join('')}
                </div>
            `;
        }

        const rvSource = tender.source === 'taiq' ? 'taiq' : 'manual';

        let linksHtml = `<div class="td-actions-row">`;
        if (tender.url) {
            linksHtml += `<a href="${tender.url}" target="_blank" class="td-modal-btn primary-btn">View Source Page ↗</a>`;
        }
        if (tender.tender_dir) {
            const dlUrl = `/download/tender?path=${encodeURIComponent(tender.tender_dir)}&token=${encodeURIComponent(getToken())}`;
            linksHtml += `<a href="${dlUrl}" class="td-modal-btn secondary-btn" download>⬇ Download All Files</a>`;
        }
        linksHtml += `<a href="/status?open=${rvSource}:${tender.id}" class="td-modal-btn secondary-btn">📝 Review</a>`;
        linksHtml += `</div>`;

        const filesHtml = tender.tender_dir
            ? `<div class="td-section-title">Documents</div>
               <div class="card-files-list" id="td-modal-files"></div>`
            : '';

        if (bodyEl) {
            bodyEl.innerHTML = `
                <div class="td-info-row">
                    <span><strong>Found At:</strong> ${tender.found_at || '—'}</span>
                    <span><strong>Published Date:</strong> ${tender.published_date || '—'}</span>
                </div>
                ${fieldsGridHtml}
                ${filesHtml}
                ${linksHtml}
            `;
        }

        if (tender.tender_dir) {
            const filesEl = document.getElementById('td-modal-files');
            if (filesEl) _renderTenderFiles(tender.tender_dir, filesEl);
        }

        if (tdModalOverlay) tdModalOverlay.style.display = 'flex';
    }

    // ── Helpers ────────────────────────────────────────────────────────────

    function _tenderRow(t) {
        const rvStatus = t.review_status || 'pending';
        const rvSource = t.source === 'taiq' ? 'taiq' : 'manual';
        const rvIcon   = rvStatus === 'approved' ? '✅' : rvStatus === 'rejected' ? '❌' : '⏳';
        const taiqTag  = t.source === 'taiq'
            ? '<span class="taiq-source-tag">🤖 TAiQ</span>' : '';

        const menuItems = [
            `<button type="button" class="card-inspect-btn" data-id="${t.id}">🔍 Inspect</button>`,
            t.url ? `<a href="${t.url}" target="_blank" rel="noopener">View source ↗</a>` : '',
            t.tender_dir
                ? `<a href="/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken())}" download>⬇ Download all files</a>`
                : '',
            `<a href="/status?open=${rvSource}:${t.id}">📝 Review</a>`,
        ].filter(Boolean).join('');

        return `
            <tr data-id="${t.id}">
                <td class="tt-kw">${_escapeHtml(t.keyword || '—')}</td>
                <td><span class="site-badge site-${t.site}" title="${_escapeHtml(_siteLabel(t))}">${_escapeHtml(_siteShortLabel(t))}</span></td>
                <td>
                    <span class="tt-title" title="${_escapeHtml(t.title || '')}">${_escapeHtml(t.title || 'Unknown Opportunity')}</span>
                    ${taiqTag}
                </td>
                <td class="tt-date">${_escapeHtml(t.published_date || '—')}</td>
                <td><span class="tt-status tt-status-${rvStatus}">${rvIcon} ${rvStatus}</span></td>
                <td style="text-align:right">
                    <span class="row-menu-wrap">
                        <button type="button" class="row-menu-btn" aria-haspopup="true" aria-label="Row actions">⋮</button>
                        <span class="row-menu">${menuItems}</span>
                    </span>
                </td>
            </tr>`;
    }

    // The row has no space for a file list, so the documents live in the
    // detail modal and are fetched the first time it opens.
    async function _renderTenderFiles(dir, container) {
        container.innerHTML = '<span class="empty-msg">Loading files…</span>';
        try {
            const token = getToken() || '';
            const res = await fetch(
                `/tender/files?dir=${encodeURIComponent(dir)}&token=${encodeURIComponent(token)}`
            );
            const files = await res.json();
            if (!Array.isArray(files) || files.length === 0) {
                container.innerHTML = '<span class="empty-msg">No files saved for this tender.</span>';
                return;
            }
            container.innerHTML = files.map(f => {
                const url = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(token)}`;
                return `<div class="card-file-item">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <a href="${url}" download title="${_escapeHtml(f.name)}">${_escapeHtml(f.name)}</a>
                </div>`;
            }).join('');
        } catch (_) {
            container.innerHTML = '<span class="empty-msg">Could not load files.</span>';
        }
    }

    function _closeRowMenus(except) {
        document.querySelectorAll('.row-menu.open').forEach(m => {
            if (m !== except) m.classList.remove('open');
        });
    }

    tendersGrid.addEventListener('click', e => {
        const menuBtn = e.target.closest('.row-menu-btn');
        if (menuBtn) {
            e.stopPropagation();
            const menu = menuBtn.parentElement.querySelector('.row-menu');
            _closeRowMenus(menu);
            menu.classList.toggle('open');
            return;
        }

        const inspectBtn = e.target.closest('.card-inspect-btn');
        if (inspectBtn) {
            _closeRowMenus();
            openTenderDetailModal(inspectBtn.dataset.id);
            return;
        }

        // Links inside the menu act normally; anywhere else on the row opens
        // the detail modal.
        if (e.target.closest('.row-menu')) return;
        const row = e.target.closest('tr[data-id]');
        if (row) openTenderDetailModal(row.dataset.id);
    });

    document.addEventListener('click', () => _closeRowMenus());
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') _closeRowMenus();
    });

    // Was called in six places but never defined, so every call threw a
    // ReferenceError — which aborted the tender detail modal's render.
    function _escapeHtml(str) {
        return String(str ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[ch]);
    }

    function _fmtDate(d) {
        return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    }

    function _pretty(dateStr) {
        if (!dateStr || typeof dateStr !== 'string') return '—';
        const parts = dateStr.split('-');
        if (parts.length < 3) return dateStr;
        const [y,m,d] = parts;
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        return `${parseInt(d)} ${months[parseInt(m)-1]} ${y}`;
    }

    function _timeOnly(isoStr) {
        if (!isoStr) return '—';
        const t = isoStr.split('T')[1];
        if (!t) return '—';
        return t.substring(0, 5);
    }

    function _duration(start, end) {
        if (!start || !end) return '—';
        try {
            const ms = new Date(end) - new Date(start);
            if (ms < 0) return '—';
            const h = Math.floor(ms / 3600000);
            const m = Math.floor((ms % 3600000) / 60000);
            if (h > 0) return `${h}h ${m}m`;
            return `${m}m`;
        } catch { return '—'; }
    }
});
