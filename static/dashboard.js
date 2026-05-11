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
        await Promise.all([loadStats(dateStr), loadTenders()]);
    }

    async function loadStats(dateStr) {
        statsRow.innerHTML = '<div class="stat-card stat-placeholder">Loading…</div>';
        actWrap.innerHTML  = '<p class="empty-msg">Loading…</p>';

        const res  = await authFetch(`/dashboard/stats?date=${dateStr}`);
        if (!res) return;
        const data = await res.json();

        // Stats cards
        if (!data.by_site || data.by_site.length === 0) {
            statsRow.innerHTML = '<div class="stat-card"><span class="stat-num">0</span><span class="stat-label">No runs today</span></div>';
        } else {
            allSites = new Set(data.by_site.map(s => s.site));
            statsRow.innerHTML = data.by_site.map(s => `
                <div class="stat-card">
                    <span class="stat-num">${s.count}</span>
                    <span class="stat-label">${s.site.toUpperCase()}</span>
                </div>
            `).join('') + `
                <div class="stat-card stat-total">
                    <span class="stat-num">${data.sessions.length}</span>
                    <span class="stat-label">Runs</span>
                </div>
            `;
            // Populate site filter
            filterSite.innerHTML = '<option value="">All Sites</option>' +
                [...allSites].map(s => `<option value="${s}">${s.toUpperCase()}</option>`).join('');
        }

        // Activity table
        if (!data.sessions || data.sessions.length === 0) {
            actWrap.innerHTML = '<p class="empty-msg">No runs on this date.</p>';
        } else {
            actWrap.innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>User</th>
                            <th>Site</th>
                            <th>Keywords</th>
                            <th>Tenders</th>
                            <th>Time</th>
                            <th>Download</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.sessions.map(s => `
                            <tr>
                                <td><strong>${s.username}</strong></td>
                                <td><span class="site-badge site-${s.site}">${s.site.toUpperCase()}</span></td>
                                <td class="keywords-cell">${s.keywords || '—'}</td>
                                <td>${s.tenders_found}</td>
                                <td class="time-cell">${_timeOnly(s.created_at)}</td>
                                <td>${s.zip_filename
                                    ? `<a class="dl-link" href="/download?name=${encodeURIComponent(s.zip_filename)}&token=${getToken()}" download>ZIP</a>`
                                    : '—'
                                }</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
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
            tendersGrid.innerHTML = '<p class="empty-msg">No tenders found for these filters.</p>';
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
});
