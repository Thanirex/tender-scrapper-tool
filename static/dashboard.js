document.addEventListener('DOMContentLoaded', () => {
    initNav({ requireRole: 'admin' });

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

    async function loadTaiqStatus() {
        const res = await authFetch('/taiq/status');
        if (!res) return;
        const { run } = await res.json();
        renderTaiqWidget(run);
        clearInterval(taiqPollTimer);
        taiqPollTimer = setInterval(loadTaiqStatus, run && run.status === 'running' ? 8000 : 60000);
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

    // ── Data loading ───────────────────────────────────────────────────────

    async function loadDay(dateStr) {
        const isToday = dateStr === _fmtDate(today);
        dateLabel.textContent = isToday ? "Today's overview" : `Overview for ${_pretty(dateStr)}`;
        // Reset filters whenever the date changes so the dropdown is clean
        filterSite.value = '';
        filterKw.value   = '';
        // Stats must finish first — it populates the site dropdown before tenders load
        await loadStats(dateStr);
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
                <div class="stat-card-v2 scard-runs" style="grid-column:1/-1;text-align:center">
                    <div class="sc2-icon">📋</div>
                    <span class="sc2-num">0</span>
                    <span class="sc2-label">No activity for this date</span>
                    <div class="sc2-bar-track"><div class="sc2-bar-fill" style="width:0%"></div></div>
                </div>`;
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

            statsRow.innerHTML = totalCard + siteCards + runsCard;

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

        let actionsHtml = '';
        if (t.url || t.tender_dir) {
            actionsHtml = `<div class="card-actions">`;
            if (t.url) {
                actionsHtml += `<a href="${t.url}" target="_blank" class="card-link">View ↗</a>`;
            }
            if (t.tender_dir) {
                const dlUrl = `/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken())}`;
                actionsHtml += `<a href="${dlUrl}" class="card-dl-btn" download>⬇ Download All</a>`;
                actionsHtml += `<button class="card-files-btn" data-dir="${t.tender_dir}">📎 Files</button>`;
            }
            actionsHtml += `</div>`;
        }

        return `
            <div class="result-card">
                <div class="card-meta">
                    <span class="site-badge site-${t.site}">${t.site.toUpperCase()}</span>
                    <span class="kw-tag">${t.keyword}</span>
                    ${taiqTag}
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
