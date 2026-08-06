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
    document.getElementById('apply-filter').addEventListener('click', loadTenders);
    filterSite.addEventListener('change', loadTenders);

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
            const chipMap  = {
                complete: ['🤖 auto-run done', 'drc-chip-complete'],
                running:  ['🤖 running now',   'drc-chip-running'],
                failed:   ['🤖 run failed',    'drc-chip-failed'],
                stopped:  ['🤖 run stopped',   'drc-chip-stopped'],
            };
            const [chipLbl, chipCls] = chipMap[d.taiq_status] || ['🤖 no auto run', 'drc-chip-none'];
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
                <div class="drc-line">🤖 ${d.taiq} TAiQ &nbsp;·&nbsp; 👤 ${d.manual} manual</div>
                <div class="drc-line">🌐 ${d.sites_scanned} site${d.sites_scanned !== 1 ? 's' : ''} scanned · ${d.sites_with_results} gave results</div>
                <div class="drc-review">
                    <span class="drc-rv drc-rv-app" title="Approved">✅ ${d.approved}</span>
                    <span class="drc-rv drc-rv-rej" title="Rejected">❌ ${d.rejected}</span>
                    <span class="drc-rv drc-rv-pen" title="Awaiting review">⏳ ${d.pending} pending</span>
                    ${d.approval_rate !== null && d.approval_rate !== undefined
                        ? `<span class="drc-rv-rate">${d.approval_rate}% approved</span>` : ''}
                </div>
                <div class="drc-bar-track" title="${pct}% of this day's tenders reviewed">
                    <div class="drc-bar-fill" style="width:${pct}%"></div>
                </div>
                <div class="drc-bar-label">${reviewed} of ${d.scraped} reviewed</div>
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

    // ── Calendar ───────────────────────────────────────────────────────────

    function renderCalendar() {
        const months = ['January','February','March','April','May','June',
                        'July','August','September','October','November','December'];
        calLabel.textContent = `${months[calMonth]} ${calYear}`;

        const firstDay = new Date(calYear, calMonth, 1).getDay(); // 0=Sun
        const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();
        // Shift so Monday=0
        const startOffset = (firstDay + 6) % 7;

        let html = '';
        for (let i = 0; i < startOffset; i++) {
            html += '<div class="cal-day cal-empty"></div>';
        }
        for (let d = 1; d <= daysInMonth; d++) {
            const dateStr = `${calYear}-${String(calMonth + 1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
            const isToday    = dateStr === _fmtDate(today);
            const isSelected = dateStr === selectedDate;
            const hasData    = dataDates.has(dateStr);
            const isFuture   = dateStr > _fmtDate(today);
            let cls = 'cal-day';
            if (isToday)    cls += ' cal-today';
            if (isSelected) cls += ' cal-selected';
            if (hasData)    cls += ' cal-has-data';
            if (isFuture)   cls += ' cal-future';
            html += `<div class="${cls}" data-date="${dateStr}">${d}${hasData ? '<span class="cal-dot"></span>' : ''}</div>`;
        }
        calGrid.innerHTML = html;

        calGrid.querySelectorAll('.cal-day:not(.cal-empty):not(.cal-future)').forEach(el => {
            el.addEventListener('click', () => {
                selectedDate = el.dataset.date;
                renderCalendar();
                loadDay(selectedDate);
            });
        });
    }

    // ── TAiQ Sophisticated Scrape & Funnel Report Card ─────────────────────
    let trcSitesCache = [];

    const toggleBtn = document.getElementById('trc-toggle-details');
    const detailsCollapse = document.getElementById('trc-details-collapse');
    const siteSearchInput = document.getElementById('trc-site-search');

    if (toggleBtn && detailsCollapse) {
        toggleBtn.addEventListener('click', () => {
            const isHidden = detailsCollapse.style.display === 'none';
            detailsCollapse.style.display = isHidden ? 'block' : 'none';
            toggleBtn.textContent = isHidden ? 'Hide Details ▴' : 'Show Details ▾';
        });
    }

    if (siteSearchInput) {
        siteSearchInput.addEventListener('input', (e) => {
            const query = e.target.value.toLowerCase().trim();
            renderTrcSiteRows(trcSitesCache.filter(s => s.site.toLowerCase().includes(query)));
        });
    }

    async function loadTaiqReport(dateStr) {
        const dateLbl = document.getElementById('trc-date-label');
        const statusBadge = document.getElementById('trc-status-badge');
        const tilesContainer = document.getElementById('trc-stat-tiles');
        const funnelBar = document.getElementById('trc-funnel-bar');
        const funnelLegend = document.getElementById('trc-funnel-legend');
        const funnelTotalLbl = document.getElementById('trc-funnel-total-lbl');
        const kwList = document.getElementById('trc-keyword-list');

        if (dateLbl) dateLbl.textContent = _pretty(dateStr);

        const res = await authFetch(`/dashboard/taiq-report?date=${dateStr}`);
        if (!res) return;
        const data = await res.json();
        const run = data.run;
        const totals = data.totals || {};
        const sites = data.sites || [];
        const topKw = data.top_keywords || [];
        const trend = data.trend || {};
        trcSitesCache = sites;

        if (!run) {
            if (statusBadge) { statusBadge.className = 'trc-badge trc-badge-idle'; statusBadge.textContent = 'No Run Recorded'; }
            if (tilesContainer) tilesContainer.innerHTML = '<p class="empty-msg" style="grid-column: 1/-1;">No extraction run recorded for this date.</p>';
            if (funnelBar) funnelBar.innerHTML = '';
            if (funnelLegend) funnelLegend.innerHTML = '';
            return;
        }

        // Status badge
        if (statusBadge) {
            const st = run.status || 'complete';
            const map = {
                running:  ['Running Now', 'trc-badge-running'],
                complete: ['Completed',   'trc-badge-complete'],
                failed:   ['Failed',      'trc-badge-failed'],
                stopped:  ['Stopped',     'trc-badge-stopped']
            };
            const [lbl, cls] = map[st] || [st, 'trc-badge-idle'];
            statusBadge.className = `trc-badge ${cls}`;
            statusBadge.textContent = lbl;
        }

        // Stat tiles
        const listed = totals.listed || 0;
        const saved = totals.saved || run.total_tenders || 0;
        const rejectedTotal = totals.rejected_total || 0;
        const sitesScanned = totals.sites_scanned || sites.length || 0;

        const trendPct = trend.pct_change || 0;
        const trendSymbol = trendPct >= 0 ? '▲' : '▼';
        const trendClass = trendPct >= 0 ? 'trc-trend-up' : 'trc-trend-down';
        const trendHtml = trend.avg_7d ? `<span class="trc-trend ${trendClass}">${trendSymbol} ${Math.abs(trendPct)}% vs 7d avg</span>` : '';

        if (tilesContainer) {
            tilesContainer.innerHTML = `
                <div class="trc-tile">
                    <span class="trc-tile-icon">🌐</span>
                    <div class="trc-tile-body">
                        <span class="trc-tile-val">${sitesScanned}</span>
                        <span class="trc-tile-lbl">Sites Scanned</span>
                    </div>
                </div>
                <div class="trc-tile">
                    <span class="trc-tile-icon">🔍</span>
                    <div class="trc-tile-body">
                        <span class="trc-tile-val">${listed.toLocaleString()}</span>
                        <span class="trc-tile-lbl">Looked At</span>
                    </div>
                </div>
                <div class="trc-tile trc-tile-saved">
                    <span class="trc-tile-icon">✅</span>
                    <div class="trc-tile-body">
                        <span class="trc-tile-val">${saved.toLocaleString()} ${trendHtml}</span>
                        <span class="trc-tile-lbl">Tenders Saved</span>
                    </div>
                </div>
                <div class="trc-tile">
                    <span class="trc-tile-icon">🚫</span>
                    <div class="trc-tile-body">
                        <span class="trc-tile-val">${rejectedTotal.toLocaleString()}</span>
                        <span class="trc-tile-lbl">Filtered Out</span>
                    </div>
                </div>
            `;
        }

        // Rejection Funnel Bar & Legend
        const rej = totals.rejected || {};
        const reasonLabels = {
            title_miss: { label: "Keyword Not in Title", color: "#64748b" },
            negative:   { label: "Negative Keywords",    color: "#ef4444" },
            stale:      { label: "Older than Cutoff",    color: "#f59e0b" },
            no_date:    { label: "No Date Found",        color: "#64748b" },
            closed:     { label: "Archived / Expired",   color: "#a855f7" },
            duplicate:  { label: "Already Collected",    color: "#3b82f6" },
            error:      { label: "Scrape Error",         color: "#dc2626" },
        };

        if (funnelTotalLbl) funnelTotalLbl.textContent = `${rejectedTotal.toLocaleString()} total filtered out`;

        let barSegmentsHtml = '';
        let legendItemsHtml = '';

        if (rejectedTotal > 0) {
            for (const [rk, meta] of Object.entries(reasonLabels)) {
                const count = rej[rk] || 0;
                if (count > 0) {
                    const widthPct = Math.max(1, (count / rejectedTotal * 100).toFixed(1));
                    barSegmentsHtml += `<div class="trc-funnel-seg" style="width: ${widthPct}%; background-color: ${meta.color};" title="${meta.label}: ${count.toLocaleString()} (${widthPct}%)"></div>`;
                    legendItemsHtml += `
                        <div class="trc-legend-item">
                            <span class="trc-legend-dot" style="background-color: ${meta.color}"></span>
                            <span class="trc-legend-text">${meta.label}: <strong>${count.toLocaleString()}</strong></span>
                        </div>
                    `;
                }
            }
        } else {
            barSegmentsHtml = `<div class="trc-funnel-seg" style="width: 100%; background-color: #22c55e;" title="0 Filtered"></div>`;
            legendItemsHtml = `<div class="trc-legend-item"><span class="trc-legend-dot" style="background-color: #22c55e"></span><span>100% Retained / Clean Run</span></div>`;
        }

        if (funnelBar) funnelBar.innerHTML = barSegmentsHtml;
        if (funnelLegend) funnelLegend.innerHTML = legendItemsHtml;

        renderTrcSiteRows(sites);

        if (kwList) {
            if (topKw.length > 0) {
                kwList.innerHTML = topKw.map(k => `
                    <div class="trc-kw-item">
                        <span class="trc-kw-name">${_escapeHtml(k.keyword)}</span>
                        <span class="trc-kw-count">${k.count} tender${k.count !== 1 ? 's' : ''}</span>
                    </div>
                `).join('');
            } else {
                kwList.innerHTML = '<p class="empty-msg">No keyword matches recorded for this run.</p>';
            }
        }
    }

    function renderTrcSiteRows(sites) {
        const siteRowsContainer = document.getElementById('trc-site-rows');
        if (!siteRowsContainer) return;

        if (!sites || sites.length === 0) {
            siteRowsContainer.innerHTML = '<tr><td colspan="6" class="empty-msg">No website stats recorded.</td></tr>';
            return;
        }

        const badgeMap = {
            healthy:   ['Healthy',   'trc-sbadge-healthy'],
            low_yield: ['Low Yield', 'trc-sbadge-low'],
            warning:   ['Warning',   'trc-sbadge-warn'],
            idle:      ['Idle',      'trc-sbadge-idle']
        };

        siteRowsContainer.innerHTML = sites.map(s => {
            const [statusLbl, statusCls] = badgeMap[s.status] || [s.status, 'trc-sbadge-idle'];
            return `
                <tr>
                    <td><strong>${_escapeHtml(s.site)}</strong></td>
                    <td>${s.listed.toLocaleString()}</td>
                    <td><strong style="color: ${s.saved > 0 ? '#22c55e' : 'inherit'}">${s.saved.toLocaleString()}</strong></td>
                    <td>${s.rejected.toLocaleString()}</td>
                    <td>${s.yield_pct}%</td>
                    <td><span class="trc-sbadge ${statusCls}">${statusLbl}</span></td>
                </tr>
            `;
        }).join('');
    }

    // ── Data loading ───────────────────────────────────────────────────────

    async function loadDay(dateStr) {
        const isToday = dateStr === _fmtDate(today);
        dateLabel.textContent = isToday ? "Today's overview" : `Overview for ${_pretty(dateStr)}`;
        // Reset filters whenever the date changes so the dropdown is clean
        filterSite.value = '';
        filterKw.value   = '';
        // Stats must finish first — it populates the site dropdown before tenders load
        await loadStats(dateStr);
        await loadTaiqReport(dateStr);
        await loadTenders();
    }

    const _SITE_CFG = {
        ungm:   { icon: '🌐', label: 'UNGM',   cls: 'scard-ungm'   },
        devnet: { icon: '💼', label: 'DevNet', cls: 'scard-devnet' },
        ngobox: { icon: '📦', label: 'NGOBox', cls: 'scard-ngobox' },
        taiq:   { icon: '🤖', label: 'TAiQ',   cls: 'scard-taiq'   },
    };

    async function loadStats(dateStr) {
        statsRow.innerHTML = '';
        actWrap.innerHTML  = '<p class="empty-msg">Loading…</p>';

        const res  = await authFetch(`/dashboard/stats?date=${dateStr}`);
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
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:100%"></div></div>
                </div>`;

            const siteCards = siteTotals.map(s => {
                const cfg = _SITE_CFG[s.site] || { icon: '📌', label: s.site.toUpperCase(), cls: '' };
                const pct = grandTotal > 0 ? Math.max(Math.round((s.count / grandTotal) * 100), 5) : 5;
                return `
                    <div class="stat-card-v2 ${cfg.cls}">
                        <div class="sc2-icon">${cfg.icon}</div>
                        <span class="sc2-num">${s.count}</span>
                        <span class="sc2-label">${cfg.label}</span>
                        <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:${pct}%"></div></div>
                    </div>`;
            }).join('');

            const runsCard = `
                <div class="stat-card-v2 scard-runs">
                    <div class="sc2-icon">▶</div>
                    <span class="sc2-num">${data.sessions.length}</span>
                    <span class="sc2-label">Runs</span>
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:100%"></div></div>
                </div>`;

            statsRow.innerHTML = totalCard + siteCards + runsCard + _lastUpdateCardHtml();

            filterSite.innerHTML = '<option value="">All Sites</option>' +
                [...allSites].map(s => {
                    const lbl = _SITE_CFG[s]?.label || s.toUpperCase();
                    return `<option value="${s}">${lbl}</option>`;
                }).join('');
        }

        // ── Summary sentence ───────────────────────────────────────────────
        const summaryEl = document.getElementById('dash-summary');
        if (summaryEl) {
            if (grandTotal > 0) {
                const numSites = siteTotals.length;
                const isToday  = dateStr === _fmtDate(today);
                summaryEl.innerHTML =
                    `<strong>${grandTotal}</strong> tender${grandTotal !== 1 ? 's' : ''} found across ` +
                    `<strong>${numSites}</strong> site${numSites !== 1 ? 's' : ''} in ` +
                    `<strong>${data.sessions.length}</strong> run${data.sessions.length !== 1 ? 's' : ''} ` +
                    (isToday ? 'today' : `on ${_pretty(dateStr)}`);
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
                                <td><span class="site-badge site-${s.site}">${(_SITE_CFG[s.site]?.label || s.site).toUpperCase()}</span></td>
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
        const colors = { ungm:'#f59e0b', devnet:'#3b82f6', ngobox:'#10b981', taiq:'#7c3aed' };
        const labels = { ungm:'UNGM', devnet:'DevNet', ngobox:'NGOBox', taiq:'TAiQ' };
        if (!sites.length || total === 0) {
            panel.innerHTML = `
                <div class="sbd-title">Today at a glance</div>
                <p class="sbd-empty">No tenders found yet</p>`;
            return;
        }
        const rows = sites.map(s => {
            const pct   = Math.max(Math.round((s.count / total) * 100), 4);
            const color = colors[s.site] || '#64748b';
            const label = labels[s.site] || s.site.toUpperCase();
            return `
                <div class="sbd-row">
                    <span class="sbd-site-label">${label}</span>
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
        tendersGrid.innerHTML = '<p class="empty-msg">Loading…</p>';
        const site    = filterSite.value;
        const keyword = filterKw.value.trim();
        let url = `/dashboard/tenders?date=${selectedDate}`;
        if (site)    url += `&site=${encodeURIComponent(site)}`;
        if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;

        const res = await authFetch(url);
        if (!res) return;
        const tenders = await res.json();

        if (!tenders || tenders.length === 0) {
            const isFiltered = filterSite.value || filterKw.value.trim();
            tendersGrid.innerHTML = `
                <div class="dash-empty-state" style="grid-column:1/-1">
                    <span class="des-icon">${isFiltered ? '🔍' : '📭'}</span>
                    <span class="des-title">${isFiltered ? 'No matches' : 'No tenders yet'}</span>
                    <span class="des-sub">${isFiltered ? 'Try adjusting your site or keyword filters' : 'TAiQ and manual runs will populate this once they complete'}</span>
                </div>`;
            return;
        }

        tendersGrid.innerHTML = tenders.map(t => _tenderCard(t)).join('');
    }

    // ── Helpers ────────────────────────────────────────────────────────────

    function _tenderCard(t) {
        const fields = t.fields || {};
        const fieldRows = Object.entries(fields).slice(0, 3).map(([k, v]) => {
            const val = String(v).length > 55 ? String(v).substring(0,55) + '…' : String(v);
            return `<div class="card-field"><strong>${k}</strong><span>${val}</span></div>`;
        }).join('');

        const taiqTag = t.source === 'taiq'
            ? '<span class="taiq-source-tag">🤖 TAiQ Auto</span>' : '';

        const rvStatus = t.review_status || 'pending';
        const rvSource = t.source === 'taiq' ? 'taiq' : 'manual';
        const rvIcon   = rvStatus === 'approved' ? '✅' : rvStatus === 'rejected' ? '❌' : '⏳';
        const rvChip   = `<span class="card-review-chip chip-${rvStatus}">${rvIcon} ${rvStatus}</span>`;

        let actionsHtml = `<div class="card-actions">`;
        if (t.url) {
            actionsHtml += `<a href="${t.url}" target="_blank" class="card-link">View ↗</a>`;
        }
        if (t.tender_dir) {
            const dlUrl = `/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken())}`;
            actionsHtml += `<a href="${dlUrl}" class="card-dl-btn" download>⬇ Download All</a>`;
            actionsHtml += `<button class="card-files-btn" data-dir="${t.tender_dir}">📎 Files</button>`;
        }
        actionsHtml += `<a href="/status?open=${rvSource}:${t.id}" class="card-review-link">📝 Review</a>`;
        actionsHtml += `</div>`;

        return `
            <div class="result-card">
                <div class="card-meta">
                    <span class="site-badge site-${t.site}">${t.site.toUpperCase()}</span>
                    <span class="kw-tag">${t.keyword}</span>
                    ${taiqTag}
                    ${rvChip}
                </div>
                <h4>${t.title || 'Unknown Opportunity'}</h4>
                <div class="card-fields">${fieldRows}</div>
                ${actionsHtml}
            </div>
        `;
    }

    async function _toggleCardFiles(btn) {
        const card = btn.closest('.result-card');
        let list = card.querySelector('.card-files-list');
        if (list) {
            const hidden = list.style.display === 'none';
            list.style.display = hidden ? '' : 'none';
            btn.textContent = hidden ? '📎 Hide files' : '📎 Files';
            return;
        }
        btn.textContent = 'Loading…';
        btn.disabled = true;
        try {
            const token = getToken() || '';
            const dir = btn.dataset.dir;
            const res = await fetch(`/tender/files?dir=${encodeURIComponent(dir)}&token=${encodeURIComponent(token)}`);
            const files = await res.json();
            if (!Array.isArray(files) || files.length === 0) {
                btn.textContent = '📎 No files';
                return;
            }
            const token2 = getToken() || '';
            list = document.createElement('div');
            list.className = 'card-files-list';
            list.innerHTML = files.map(f => {
                const url = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(token2)}`;
                return `<div class="card-file-item">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <a href="${url}" download title="${f.name}">${f.name}</a>
                </div>`;
            }).join('');
            card.appendChild(list);
            btn.textContent = '📎 Hide files';
        } catch (_) {
            btn.textContent = '📎 Error';
        } finally {
            btn.disabled = false;
        }
    }

    tendersGrid.addEventListener('click', e => {
        const btn = e.target.closest('.card-files-btn');
        if (btn) _toggleCardFiles(btn);
    });

    function _fmtDate(d) {
        return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    }

    function _pretty(dateStr) {
        const [y,m,d] = dateStr.split('-');
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
