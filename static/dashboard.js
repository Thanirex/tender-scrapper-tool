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
    const kpiRow      = document.getElementById('kpi-row');
    const actWrap     = document.getElementById('activity-wrap');
    const tendersGrid = document.getElementById('tenders-grid');
    const dateLabel   = document.getElementById('selected-date-label');
    const filterSite  = document.getElementById('filter-site');
    const filterKw    = document.getElementById('filter-keyword');

    // Greet by name — the sidebar already says *where* you are, the header
    // says *who* you are and what this page is reporting on.
    const welcomeEl = document.getElementById('welcome-name');
    if (welcomeEl) welcomeEl.textContent = (getUser() || {}).username || 'there';

    // Shared state across the KPI row, the chart and the sources panel, so a
    // single fetch of each feeds every consumer.
    let overview     = null;   // all-time totals   (/dashboard/overview)
    let reportSeries = [];     // per-day series    (/dashboard/report)
    let periodSites  = [];     // by_site for the calendar selection

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
        // The Last Run KPI is driven by this poll, so redraw the row whenever
        // a poll lands — it may well arrive after the row first rendered.
        renderKpiRow();
        clearInterval(taiqPollTimer);
        taiqPollTimer = setInterval(loadTaiqStatus, run && run.status === 'running' ? 8000 : 60000);
    }

    // ── KPI row ────────────────────────────────────────────────────────────
    // Four executive-level figures, each with an explicit scope label. The
    // scope matters: Total Tenders is all-time, Scraped is today only. The
    // old per-site cards claimed "All time" while showing the selected date's
    // counts, which is exactly the contradiction these labels prevent.

    function _lastRunKpi() {
        const r = latestTaiqRun;
        let timeStr = '—', dateStr = 'No runs yet', statusLbl = 'Idle', tone = 'idle';
        if (r) {
            const isRunning = r.status === 'running';
            const iso = (isRunning ? r.started_at : (r.finished_at || r.started_at)) || '';
            if (iso.includes('T')) {
                dateStr = _pretty(iso.split('T')[0]);
                timeStr = iso.split('T')[1].substring(0, 5);
            }
            const map = {
                running:  ['Running',   'running'],
                complete: ['Completed', 'complete'],
                failed:   ['Failed',    'failed'],
                stopped:  ['Stopped',   'stopped'],
            };
            [statusLbl, tone] = map[r.status] || [r.status, 'idle'];
        }
        return { timeStr, dateStr, statusLbl, tone };
    }

    function _kpiCard({ tone, glyph, label, value, sub, foot }) {
        return `
            <div class="kpi-card kpi-${tone}">
                <div class="kpi-icon">${icon(glyph)}</div>
                <div class="kpi-body">
                    <span class="kpi-label">${label}</span>
                    <span class="kpi-value">${value}</span>
                    <span class="kpi-sub">${sub}</span>
                </div>
                ${foot || ''}
            </div>`;
    }

    // A real delta or nothing at all. Decorative percentages with no baseline
    // behind them are worse than no trend indicator.
    function _kpiDelta(curr, prev, unit) {
        if (prev === null || prev === undefined) return '';
        const diff = curr - prev;
        if (diff === 0) return `<span class="kpi-delta kpi-delta-flat">no change ${unit}</span>`;
        const cls = diff > 0 ? 'kpi-delta-up' : 'kpi-delta-down';
        const arrow = diff > 0 ? '↑' : '↓';
        return `<span class="kpi-delta ${cls}">${arrow} ${Math.abs(diff)} ${unit}</span>`;
    }

    function renderKpiRow() {
        if (!kpiRow) return;

        const todayRow = reportSeries[0] || null;
        const yestRow  = reportSeries[1] || null;
        const lr       = _lastRunKpi();

        const totalAll   = overview ? overview.total_tenders  : null;
        const pendingAll = overview ? overview.pending_review : null;
        const scrapedToday = todayRow ? todayRow.scraped : null;

        const cards = [
            _kpiCard({
                tone: 'total', glyph: 'layers',
                label: 'Total Tenders',
                value: totalAll === null ? '—' : totalAll.toLocaleString(),
                sub:   'All time · every source',
                foot:  scrapedToday
                    ? `<span class="kpi-delta kpi-delta-up">↑ ${scrapedToday} today</span>`
                    : '<span class="kpi-delta kpi-delta-flat">no new tenders today</span>',
            }),
            _kpiCard({
                tone: 'scraped', glyph: 'inbox',
                label: 'Tenders Scraped',
                value: scrapedToday === null ? '—' : scrapedToday,
                sub:   'Today',
                foot:  yestRow ? _kpiDelta(todayRow.scraped, yestRow.scraped, 'vs yesterday') : '',
            }),
            _kpiCard({
                tone: pendingAll ? 'pending-live' : 'pending', glyph: 'eye',
                label: 'Pending Review',
                value: pendingAll === null ? '—' : pendingAll,
                sub:   pendingAll ? 'Needs your attention' : 'Everything reviewed',
                foot:  pendingAll
                    ? '<a class="kpi-link" href="/status">Review now →</a>' : '',
            }),
            _kpiCard({
                tone: 'lastrun', glyph: 'clock',
                label: 'Last Run',
                value: lr.timeStr,
                sub:   lr.dateStr,
                foot:  `<span class="kpi-run-status kpi-run-${lr.tone}">
                            <span class="kpi-run-dot"></span>${lr.statusLbl}
                        </span>`,
            }),
        ];
        kpiRow.innerHTML = cards.join('');
    }

    async function loadOverview() {
        const res = await authFetch('/dashboard/overview');
        if (!res) return;
        overview = await res.json();
        renderKpiRow();
        renderSources();
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
    // The primary number gets a radial anchor so the eye lands on it before
    // reading anything else; the ring encodes review progress for that day.

    const _RING_R = 34;
    const _RING_C = 2 * Math.PI * _RING_R;

    function _radial(value, pct, label, tone) {
        const dash = (Math.max(0, Math.min(100, pct)) / 100) * _RING_C;
        // A zero-length dash still paints a dot under stroke-linecap:round,
        // which reads as "1% done" on a day with nothing to show. Omit the arc.
        const arc = dash > 0 ? `
                    <circle cx="40" cy="40" r="${_RING_R}" class="drc-ring-fill drc-ring-${tone}"
                            stroke-dasharray="${dash.toFixed(1)} ${(_RING_C - dash).toFixed(1)}"
                            transform="rotate(-90 40 40)"/>` : '';
        // The caption sits below the ring, not inside it: fitting it in the
        // centre forced it down to a barely-legible size.
        return `
            <div class="drc-radial">
                <div class="drc-ring-wrap">
                    <svg viewBox="0 0 80 80" class="drc-ring" aria-hidden="true">
                        <circle cx="40" cy="40" r="${_RING_R}" class="drc-ring-track"/>${arc}
                    </svg>
                    <span class="drc-num">${value}</span>
                </div>
                <span class="drc-num-label">${label}</span>
            </div>`;
    }

    function renderDailyReport() {
        const row = document.getElementById('daily-report-row');
        if (!row) return;
        const days = reportSeries.slice(0, 3);
        if (!days.length) { row.innerHTML = ''; return; }

        const names = ['Today', 'Yesterday'];
        row.innerHTML = days.map((d, i) => {
            const name     = names[i] || _pretty(d.date);
            const sub      = names[i] ? _pretty(d.date) : '';
            const reviewed = d.approved + d.rejected;
            const pct      = d.scraped ? Math.round(100 * reviewed / d.scraped) : 0;
            const downloaded = d.downloaded ?? 0;
            const chipMap  = {
                complete: ['Run complete', 'drc-chip-complete', 'complete'],
                running:  ['Running now',  'drc-chip-running',  'running'],
                failed:   ['Run failed',   'drc-chip-failed',   'failed'],
                stopped:  ['Run stopped',  'drc-chip-stopped',  'stopped'],
            };
            const [chipLbl, chipCls, tone] = chipMap[d.taiq_status] || ['No data run', 'drc-chip-none', 'none'];
            return `
            <div class="day-report-card${i === 0 ? ' drc-today' : ''}">
                <div class="drc-head">
                    <span class="drc-day">${name}${sub ? ` <span class="drc-date">· ${sub}</span>` : ''}</span>
                    <span class="drc-chip ${chipCls}">${chipLbl}</span>
                </div>
                <div class="drc-body">
                    ${_radial(d.scraped, pct, `tender${d.scraped !== 1 ? 's' : ''} scraped`, tone)}
                    <div class="drc-meta">
                        <div class="drc-meta-row">
                            <span class="drc-meta-k">${icon('globe')}Sites scanned</span>
                            <b>${d.sites_scanned}</b>
                        </div>
                        <div class="drc-meta-row">
                            <span class="drc-meta-k">${icon('key')}Keywords</span>
                            <b>${(d.keywords ?? 0).toLocaleString()}</b>
                        </div>
                        <div class="drc-meta-row">
                            <span class="drc-meta-k">${icon('bot')}TAiQ</span>
                            <b>${d.taiq}</b>
                        </div>
                        <div class="drc-meta-row">
                            <span class="drc-meta-k">${icon('user')}Manual</span>
                            <b>${d.manual}</b>
                        </div>
                    </div>
                </div>
                <div class="drc-review">
                    <span class="drc-rv drc-rv-app" title="Approved">${icon('check')}${d.approved}</span>
                    <span class="drc-rv drc-rv-rej" title="Rejected">${icon('cross')}${d.rejected}</span>
                    <span class="drc-rv drc-rv-pen" title="Awaiting review">${icon('hourglass')}${d.pending} pending</span>
                    ${d.approval_rate !== null && d.approval_rate !== undefined
                        ? `<span class="drc-rv-rate">${d.approval_rate}% approved</span>` : ''}
                </div>
                <div class="drc-foot">
                    <span title="${reviewed} of ${d.scraped} tenders reviewed">${icon('clipboard')}${reviewed} Reviewed</span>
                    <span title="${downloaded} of ${d.scraped} tenders have documents saved">${icon('download')}${downloaded} Downloaded</span>
                </div>
            </div>`;
        }).join('');
    }

    // ── Performance overview chart ─────────────────────────────────────────
    const perfTabs  = document.getElementById('perf-tabs');
    const perfRange = document.getElementById('perf-range');
    const perfChart = document.getElementById('perf-chart');
    let perfMetric  = 'scraped';

    const _METRIC_LABEL = {
        scraped:       'tenders scraped',
        sites_scanned: 'sites scanned',
        keywords:      'keywords',
    };

    if (perfTabs) {
        perfTabs.addEventListener('click', e => {
            const btn = e.target.closest('.perf-tab');
            if (!btn) return;
            perfTabs.querySelectorAll('.perf-tab').forEach(b => {
                const on = b === btn;
                b.classList.toggle('active', on);
                b.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            perfMetric = btn.dataset.metric;
            renderPerfChart();
        });
    }
    if (perfRange) {
        perfRange.addEventListener('change', () => loadReport(parseInt(perfRange.value, 10)));
    }

    // Hand-rolled inline SVG — the page already avoids chart CDNs, and an
    // area+line over at most 30 points needs no library.
    function renderPerfChart() {
        if (!perfChart) return;
        const pts = reportSeries.slice().reverse();   // oldest → newest
        if (!pts.length) {
            perfChart.innerHTML = '<p class="empty-msg">No run history yet.</p>';
            return;
        }

        const W = 640, H = 220, PL = 38, PR = 14, PT = 18, PB = 30;
        const vals = pts.map(d => Number(d[perfMetric]) || 0);
        const rawMax = Math.max(...vals);
        const max = rawMax > 0 ? _niceCeil(rawMax) : 10;
        const iw = W - PL - PR, ih = H - PT - PB;
        const x = i => PL + (pts.length === 1 ? iw / 2 : (i * iw) / (pts.length - 1));
        const y = v => PT + ih - (v / max) * ih;

        const line = vals.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        const area = `${line} L${x(vals.length - 1).toFixed(1)},${(PT + ih).toFixed(1)} L${x(0).toFixed(1)},${(PT + ih).toFixed(1)} Z`;

        // 4 horizontal gridlines with value labels
        const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => {
            const v = Math.round(max * f);
            const yy = y(max * f);
            return `<line class="pc-grid" x1="${PL}" y1="${yy.toFixed(1)}" x2="${W - PR}" y2="${yy.toFixed(1)}"/>
                    <text class="pc-ytick" x="${PL - 8}" y="${(yy + 3.5).toFixed(1)}">${v}</text>`;
        }).join('');

        // Only label a few dates so the axis never crowds at 30 days
        const step = Math.max(1, Math.ceil(pts.length / 7));
        const xLabels = pts.map((d, i) => {
            if (i % step !== 0 && i !== pts.length - 1) return '';
            const [, m, dd] = d.date.split('-');
            const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(m, 10) - 1];
            return `<text class="pc-xtick" x="${x(i).toFixed(1)}" y="${H - 8}">${parseInt(dd, 10)} ${mon}</text>`;
        }).join('');

        const dots = vals.map((v, i) => {
            const last = i === vals.length - 1;
            return `<circle class="pc-dot${last ? ' pc-dot-last' : ''}" cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="${last ? 5 : 3.2}">
                        <title>${_pretty(pts[i].date)} — ${v} ${_METRIC_LABEL[perfMetric]}</title>
                    </circle>`;
        }).join('');

        const lastV = vals[vals.length - 1];
        const lx = Math.min(x(vals.length - 1), W - PR - 34);
        const callout = `
            <g class="pc-callout" transform="translate(${lx.toFixed(1)},${Math.max(y(lastV) - 26, PT + 2).toFixed(1)})">
                <rect x="-24" y="-13" width="48" height="20" rx="6"/>
                <text x="0" y="1.5">${lastV}</text>
            </g>`;

        perfChart.innerHTML = `
            <svg viewBox="0 0 ${W} ${H}" class="perf-svg" role="img"
                 aria-label="${_METRIC_LABEL[perfMetric]} over the last ${pts.length} days">
                <defs>
                    <linearGradient id="pcFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%"   stop-color="var(--accent)" stop-opacity="0.26"/>
                        <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
                    </linearGradient>
                </defs>
                ${ticks}
                <path class="pc-area" d="${area}"/>
                <path class="pc-line" d="${line}"/>
                ${dots}
                ${callout}
                ${xLabels}
            </svg>`;
    }

    function _niceCeil(n) {
        const mag  = Math.pow(10, Math.floor(Math.log10(n)));
        const step = n / mag <= 2 ? 0.5 * mag : n / mag <= 5 ? mag : 2 * mag;
        return Math.ceil(n / step) * step;
    }

    // One fetch, three consumers: KPI deltas, the activity cards, the chart.
    async function loadReport(days = 7) {
        const res = await authFetch(`/dashboard/report?days=${days}`);
        if (!res) return;
        const { days: rows } = await res.json();
        reportSeries = rows || [];
        renderDailyReport();
        renderPerfChart();
        renderKpiRow();
    }

    // ── Top sources ────────────────────────────────────────────────────────
    // Fifteen equally-weighted site cards do not scale past a handful of
    // sources. A ranked list does, and it makes "which source produces the
    // most?" answerable at a glance.
    const srcList   = document.getElementById('src-list');
    const srcMore   = document.getElementById('src-more');
    const srcScope  = document.getElementById('sources-scope');
    let srcExpanded = false;

    if (srcScope) {
        srcScope.addEventListener('change', () => { srcExpanded = false; renderSources(); });
    }
    if (srcMore) {
        srcMore.addEventListener('click', () => { srcExpanded = !srcExpanded; renderSources(); });
    }

    function renderSources() {
        if (!srcList) return;
        const allTime = !srcScope || srcScope.value === 'all';
        const rows = (allTime ? (overview && overview.by_site) : periodSites) || [];
        const sorted = rows.slice().sort((a, b) => b.count - a.count).filter(r => r.count > 0);

        if (!sorted.length) {
            srcList.innerHTML = `<p class="empty-msg">${
                allTime ? 'No tenders recorded yet.' : 'No tenders in the selected period.'
            }</p>`;
            if (srcMore) srcMore.hidden = true;
            return;
        }

        const top  = sorted[0].count;
        const show = srcExpanded ? sorted : sorted.slice(0, 5);
        srcList.innerHTML = show.map((s, i) => {
            const key   = String(s.site || '').toLowerCase();
            const label = _siteShortLabel(s);
            const color = _siteColor(key);
            const pct   = Math.max(Math.round((s.count / top) * 100), 3);
            return `
                <div class="src-row">
                    <span class="src-rank">${i + 1}</span>
                    <span class="src-dot" style="background:${color}"></span>
                    <span class="src-name" title="${_escapeHtml(_siteLabel(s))}">${_escapeHtml(label)}</span>
                    <span class="src-bar"><span class="src-bar-fill" style="width:${pct}%;background:${color}"></span></span>
                    <span class="src-count">${s.count}</span>
                </div>`;
        }).join('');

        if (srcMore) {
            srcMore.hidden = sorted.length <= 5;
            srcMore.textContent = srcExpanded
                ? 'Show top 5' : `View all ${sorted.length} sources`;
        }
    }

    // ── Quick actions ──────────────────────────────────────────────────────
    (function renderQuickActions() {
        const wrap = document.getElementById('qa-list');
        if (!wrap) return;
        const actions = [
            { href: '/',          tone: 'a', glyph: 'play',  title: 'Run Scraper',  sub: 'Start a new data extraction', role: 'user'  },
            { href: '/taiq-work', tone: 'b', glyph: 'bot',   title: 'TAiQ Status',  sub: 'Check system health & current status', role: 'user' },
            { href: '/status',    tone: 'c', glyph: 'file',  title: 'View Reports', sub: 'Check analytics & insights', role: 'user' },
            { href: '/users',     tone: 'd', glyph: 'users', title: 'Manage Users', sub: 'Add or manage team access', role: 'admin' },
        ].filter(a => hasRole(a.role));

        wrap.innerHTML = actions.map(a => `
            <a class="qa-item qa-${a.tone}" href="${a.href}">
                <span class="qa-icon">${icon(a.glyph)}</span>
                <span class="qa-text">
                    <strong>${a.title}</strong>
                    <span>${a.sub}</span>
                </span>
                <span class="qa-arrow" aria-hidden="true">→</span>
            </a>`).join('');
    })();

    loadOverview();
    loadReport(parseInt((perfRange && perfRange.value) || '7', 10));
    // Stays fresh across midnight and while a run is in progress.
    setInterval(() => {
        loadOverview();
        loadReport(parseInt((perfRange && perfRange.value) || '7', 10));
    }, 5 * 60 * 1000);

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
        ungm:   { label: 'UNGM'   },
        devnet: { label: 'DevNet' },
        ngobox: { label: 'NGOBox' },
        taiq:   { label: 'TAiQ'   },
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
        actWrap.innerHTML = '<p class="empty-msg">Loading…</p>';

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

        // ── Sources for the selected period ────────────────────────────────
        // These counts are scoped to the calendar selection, so they feed the
        // Top Sources panel's "Selected period" mode — never a card claiming
        // "All time", which is what the KPI row is for.
        const siteTotals = data.by_site || [];
        const grandTotal = siteTotals.reduce((s, x) => s + x.count, 0);
        periodSites = siteTotals;
        renderSources();

        if (siteTotals.length > 0) {
            allSites = new Set(siteTotals.map(s => s.site));

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
                    <span class="des-icon">${icon('calendar')}</span>
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
                                    ${_statusDot(s)}<strong>${s.source === 'taiq' ? icon('bot', 'ic-inline') : ''}${s.username}</strong>
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
                    <span class="des-icon">${icon(isFiltered ? 'search' : 'tray')}</span>
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
        const rvIcon   = icon(rvStatus === 'approved' ? 'check'
                            : rvStatus === 'rejected' ? 'cross' : 'hourglass');

        if (pillsEl) {
            pillsEl.innerHTML = `
                <span class="site-badge site-${tender.site}" title="${_escapeHtml(_siteLabel(tender))}">${_escapeHtml(_siteShortLabel(tender))}</span>
                <span class="kw-tag">${_escapeHtml(tender.keyword || '')}</span>
                <span class="td-mpill td-mpill-${rvStatus}">${rvIcon} ${rvStatus}</span>
                ${tender.source === 'taiq' ? `<span class="taiq-source-tag">${icon('bot')}TAiQ Auto</span>` : ''}
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
            linksHtml += `<a href="${dlUrl}" class="td-modal-btn secondary-btn" download>${icon('download')}Download All Files</a>`;
        }
        linksHtml += `<a href="/status?open=${rvSource}:${tender.id}" class="td-modal-btn secondary-btn">${icon('edit')}Review</a>`;
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
        const rvIcon   = icon(rvStatus === 'approved' ? 'check'
                            : rvStatus === 'rejected' ? 'cross' : 'hourglass');
        const taiqTag  = t.source === 'taiq'
            ? `<span class="taiq-source-tag">${icon('bot')}TAiQ</span>` : '';

        const menuItems = [
            `<button type="button" class="card-inspect-btn" data-id="${t.id}">${icon('search')}Inspect</button>`,
            t.url ? `<a href="${t.url}" target="_blank" rel="noopener">${icon('link')}View source</a>` : '',
            t.tender_dir
                ? `<a href="/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken())}" download>${icon('download')}Download all files</a>`
                : '',
            `<a href="/status?open=${rvSource}:${t.id}">${icon('edit')}Review</a>`,
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
