document.addEventListener('DOMContentLoaded', () => {

    const today      = new Date();
    let calYear      = today.getFullYear();
    let calMonth     = today.getMonth();
    let selectedDate = null;
    // Map of date → status for calendar dots
    let dateStatuses = {};
    let pollTimer    = null;
    let allTenders   = [];
    let currentRunId = null;

    const calGrid        = document.getElementById('cal-grid');
    const calLabel       = document.getElementById('cal-month-label');
    const runningBanner  = document.getElementById('running-banner');
    const trbProgressTxt = document.getElementById('trb-progress-text');
    const trbKwNum       = document.getElementById('trb-kw-num');
    const trbTdNum       = document.getElementById('trb-td-num');
    const trbStopBtn     = document.getElementById('trb-stop-btn');
    const emptyState     = document.getElementById('taiq-empty-state');
    const runSummary     = document.getElementById('taiq-run-summary');
    const filterRow      = document.getElementById('taiq-filter-row');
    const tendersGrid    = document.getElementById('taiq-tenders-grid');
    const filterKw       = document.getElementById('taiq-filter-kw');
    const countLbl       = document.getElementById('taiq-count-lbl');

    const isSuperadmin  = getUser()?.role === 'superadmin';
    const canSeeLogs    = hasRole('admin');   // admin + superadmin
    const triggerBar    = document.getElementById('taiq-trigger-bar');
    const ttbRunBtn     = document.getElementById('ttb-run-btn');

    // ── Logs panel refs ────────────────────────────────────────────────────
    const logsCard      = document.getElementById('taiq-logs-card');
    const logsBody      = document.getElementById('tlc-body');
    const logsLines     = document.getElementById('tlc-lines');
    const logsLiveBadge = document.getElementById('tlc-live-badge');
    const logsToggleBtn = document.getElementById('tlc-toggle-btn');
    let logPollTimer    = null;
    let logsVisible     = true;
    let lastLogRunId    = null;

    if (canSeeLogs) {
        logsCard.classList.remove('hidden');
        logsToggleBtn.addEventListener('click', () => {
            logsVisible = !logsVisible;
            logsBody.style.display = logsVisible ? '' : 'none';
            logsToggleBtn.textContent = logsVisible ? 'Hide' : 'Show';
        });
    }

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

    filterKw.addEventListener('input', applyFilter);

    // ── Trigger bar (superadmin only) ─────────────────────────────────────
    if (isSuperadmin) {
        triggerBar.classList.remove('hidden');
        ttbRunBtn.addEventListener('click', async () => {
            if (!confirm('Trigger a manual TAiQ scrape run now?')) return;
            ttbRunBtn.disabled = true;
            ttbRunBtn.textContent = '⏳ Starting…';
            try {
                const res = await authFetch('/taiq/run', { method: 'POST' });
                if (!res) { ttbRunBtn.disabled = false; ttbRunBtn.textContent = '▶ Run Now'; return; }
                const data = await res.json();
                if (data.ok) {
                    ttbRunBtn.textContent = '✓ Started';
                    // Start polling immediately so the banner appears
                    if (!pollTimer) pollTimer = setInterval(checkStatus, 8000);
                    checkStatus();
                } else {
                    alert(data.error || 'Could not start the run.');
                    ttbRunBtn.disabled = false;
                    ttbRunBtn.textContent = '▶ Run Now';
                }
            } catch {
                ttbRunBtn.disabled = false;
                ttbRunBtn.textContent = '▶ Run Now';
            }
        });
    }

    // ── Stop button (superadmin only) ──────────────────────────────────────
    if (isSuperadmin) {
        trbStopBtn.classList.remove('hidden');
        trbStopBtn.addEventListener('click', async () => {
            if (!confirm('Stop the current TAiQ scrape? Tenders found so far will be saved.')) return;
            trbStopBtn.disabled = true;
            trbStopBtn.textContent = '⏳ Stopping…';
            try {
                const res = await authFetch('/taiq/stop', { method: 'POST' });
                if (!res) return;
                const data = await res.json();
                if (data.ok) {
                    trbStopBtn.textContent = '✓ Stop sent';
                    trbProgressTxt.textContent = 'Stop signal sent — finishing current tender…';
                } else {
                    trbStopBtn.textContent = '⏹ Stop';
                    trbStopBtn.disabled = false;
                    alert(data.error || 'Could not stop the run.');
                }
            } catch {
                trbStopBtn.textContent = '⏹ Stop';
                trbStopBtn.disabled = false;
            }
        });
    }

    // ── Bootstrap ──────────────────────────────────────────────────────────
    loadHistory();
    checkStatus();

    // ── History + calendar ─────────────────────────────────────────────────

    async function loadHistory() {
        const res = await authFetch('/taiq/history');
        if (!res) return;
        const data = await res.json();
        dateStatuses = {};
        (data.dates || []).forEach(d => { dateStatuses[d.date] = d.status; });
        renderCalendar();
    }

    function renderCalendar() {
        const MONTHS = ['January','February','March','April','May','June',
                        'July','August','September','October','November','December'];
        calLabel.textContent = `${MONTHS[calMonth]} ${calYear}`;

        const firstDay    = new Date(calYear, calMonth, 1).getDay();
        const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();
        const todayStr    = _fmtDate(today);
        let html = '';

        for (let i = 0; i < firstDay; i++) html += '<div class="taiq-cal-day taiq-cal-empty"></div>';

        for (let d = 1; d <= daysInMonth; d++) {
            const ds = `${calYear}-${String(calMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
            const status   = dateStatuses[ds];
            const isToday  = ds === todayStr;
            const isSel    = ds === selectedDate;
            const isFuture = ds > todayStr;
            let cls = 'taiq-cal-day';
            if (isToday)  cls += ' taiq-cal-today';
            if (isSel)    cls += ' taiq-cal-selected';
            if (isFuture) cls += ' taiq-cal-future';
            if (status)   cls += ` taiq-cal-has-data`;

            const dot = status
                ? `<span class="taiq-cal-dot dot-${status}"></span>`
                : '';
            html += `<div class="${cls}" data-date="${ds}">${d}${dot}</div>`;
        }
        calGrid.innerHTML = html;

        calGrid.querySelectorAll('.taiq-cal-day:not(.taiq-cal-empty):not(.taiq-cal-future)').forEach(el => {
            el.addEventListener('click', () => {
                if (!dateStatuses[el.dataset.date]) return;
                selectedDate = el.dataset.date;
                renderCalendar();
                loadDateData(selectedDate);
            });
        });
    }

    // ── Status check (running banner) ──────────────────────────────────────

    async function checkStatus() {
        const res = await authFetch('/taiq/status');
        if (!res) return;
        const data = await res.json();
        const run  = data.run;

        if (run && run.status === 'running') {
            showRunningBanner(run);
            if (isSuperadmin) { ttbRunBtn.disabled = true; ttbRunBtn.textContent = '▶ Run Now'; }
            if (!pollTimer) pollTimer = setInterval(checkStatus, 8000);
            // Start live log poll only when run id changes (avoids restart on every 8s tick)
            if (canSeeLogs && lastLogRunId !== run.id) _startLogPoll(run.id);
        } else {
            runningBanner.classList.add('hidden');
            if (isSuperadmin) { ttbRunBtn.disabled = false; ttbRunBtn.textContent = '▶ Run Now'; }
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            _stopLogPoll();
            // Refresh calendar when a run just finished (any terminal status)
            if (run && ['complete', 'failed', 'stopped'].includes(run.status)) {
                loadHistory();
                if (selectedDate === run.run_date) loadDateData(selectedDate);
            }
        }
    }

    function showRunningBanner(run) {
        runningBanner.classList.remove('hidden');
        const done  = run.keywords_done || 0;
        const total = run.total_keywords || '?';
        trbKwNum.textContent = `${done} / ${total}`;
        trbTdNum.textContent = run.total_tenders || 0;

        if (run.stop_requested) {
            trbProgressTxt.textContent = 'Stop signal sent — finishing current tender…';
            if (isSuperadmin) { trbStopBtn.textContent = '✓ Stop sent'; trbStopBtn.disabled = true; }
        } else {
            const curKw = run.current_keyword
                ? ` · "${run.current_keyword}"`
                : '';
            trbProgressTxt.textContent =
                `Scanning all sites — ${done} of ${total} keyword/site combinations done, `
                + `${run.total_tenders || 0} tenders found${curKw}`;
            if (isSuperadmin) { trbStopBtn.textContent = '⏹ Stop'; trbStopBtn.disabled = false; }
        }
    }

    // ── Date data loading ──────────────────────────────────────────────────

    async function loadDateData(dateStr) {
        emptyState.classList.add('hidden');
        runSummary.classList.remove('hidden');
        filterRow.classList.remove('hidden');
        runSummary.querySelector('#trs-date').textContent = _pretty(dateStr);
        tendersGrid.innerHTML = '<p class="empty-msg">Loading…</p>';

        const res = await authFetch(`/taiq/date/${dateStr}`);
        if (!res) return;
        const data = await res.json();

        if (!data.run) {
            runSummary.classList.add('hidden');
            filterRow.classList.add('hidden');
            tendersGrid.innerHTML = '<p class="empty-msg">No run data found for this date.</p>';
            return;
        }

        renderRunSummary(data.run);
        currentRunId = data.run.id;
        allTenders   = data.tenders || [];
        applyFilter();
        if (canSeeLogs) loadLogs(data.run.id);
    }

    function renderRunSummary(run) {
        const badgeEl = document.getElementById('trs-status-badge');
        badgeEl.textContent = run.status.charAt(0).toUpperCase() + run.status.slice(1);
        badgeEl.className   = 'status-badge';
        if (run.status === 'complete')      badgeEl.classList.add('success');
        else if (run.status === 'failed')   badgeEl.classList.add('danger');
        else if (run.status === 'stopped')  badgeEl.classList.add('stopped');
        else if (run.status === 'running')  badgeEl.classList.add('pulse');

        document.getElementById('trs-kw-done').textContent =
            `${run.keywords_done || 0} / ${run.total_keywords || 0}`;
        document.getElementById('trs-tenders').textContent = run.total_tenders || 0;
        document.getElementById('trs-duration').textContent = _duration(run.started_at, run.finished_at);

        const errEl = document.getElementById('trs-error');
        if (run.error_msg) {
            errEl.textContent = run.error_msg;
            errEl.classList.remove('hidden');
        } else {
            errEl.classList.add('hidden');
        }
    }

    function applyFilter() {
        const kw = filterKw.value.trim().toLowerCase();
        const filtered = kw
            ? allTenders.filter(t =>
                (t.keyword || '').toLowerCase().includes(kw) ||
                (t.title || '').toLowerCase().includes(kw))
            : allTenders;
        countLbl.textContent = `${filtered.length} tender${filtered.length !== 1 ? 's' : ''}`;
        if (filtered.length === 0) {
            tendersGrid.innerHTML = '<p class="empty-msg">No tenders match your filter.</p>';
        } else {
            tendersGrid.innerHTML = filtered.map(t => _tenderCard(t)).join('');
        }
    }

    // ── Logs panel ─────────────────────────────────────────────────────────

    function _startLogPoll(runId) {
        if (!canSeeLogs) return;
        _stopLogPoll();
        lastLogRunId = runId;
        loadLogs(runId, true);
        logPollTimer = setInterval(() => loadLogs(runId, true), 3000);
    }

    function _stopLogPoll() {
        if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; }
        logsLiveBadge.classList.add('hidden');
    }

    async function loadLogs(runId, isLive = false) {
        if (!canSeeLogs) return;
        const url = runId != null ? `/taiq/logs?run_id=${runId}` : '/taiq/logs';
        const res = await authFetch(url);
        if (!res) return;
        const data = await res.json();
        if (!data || !Array.isArray(data.lines)) return;

        const atBottom = logsLines.scrollHeight - logsLines.scrollTop
                         <= logsLines.clientHeight + 40;

        logsLines.innerHTML = data.lines.length
            ? data.lines.map(_formatLogLine).join('\n')
            : '<span class="log-muted">No log entries yet.</span>';

        if (data.live) {
            logsLiveBadge.classList.remove('hidden');
        } else {
            logsLiveBadge.classList.add('hidden');
        }

        // Auto-scroll to bottom if user was already at the bottom
        if (atBottom || isLive) {
            logsLines.scrollTop = logsLines.scrollHeight;
        }
    }

    function _formatLogLine(line) {
        const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        let cls = '';
        if (/✅|complete|✓ Saved/.test(line))                         cls = 'log-success';
        else if (/❌|FAILED|Error|error/.test(line))                   cls = 'log-error';
        else if (/⚠️|Skipping|No publication date/.test(line))        cls = 'log-warn';
        else if (/⏩ Duplicate|📅 Skipping/.test(line))               cls = 'log-skip';
        else if (/▶️ Keyword:|▶️ Processing/.test(line))              cls = 'log-keyword';
        else if (/📊 Summaris|→ Summaris/.test(line))                 cls = 'log-info';
        else if (/⏹ Stop|stopped/.test(line))                         cls = 'log-stop';
        return cls ? `<span class="${cls}">${esc}</span>` : esc;
    }

    // ── File download helpers ──────────────────────────────────────────────

    tendersGrid.addEventListener('click', e => {
        const btn = e.target.closest('.card-files-btn');
        if (btn) _toggleCardFiles(btn);
    });

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
            const dir   = btn.dataset.dir;
            const res   = await fetch(`/tender/files?dir=${encodeURIComponent(dir)}&token=${encodeURIComponent(token)}`);
            const files = await res.json();
            if (!Array.isArray(files) || files.length === 0) {
                btn.textContent = '📎 No files';
                btn.disabled = false;
                return;
            }
            list = document.createElement('div');
            list.className = 'card-files-list';
            list.innerHTML = files.map(f => {
                const url = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(getToken() || '')}`;
                return `<div class="card-file-item">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <a href="${url}" download title="${f.name}">${f.name}</a>
                </div>`;
            }).join('');
            card.appendChild(list);
            btn.textContent = '📎 Hide files';
            btn.disabled = false;
        } catch (_) {
            btn.textContent = '📎 Error';
            btn.disabled = false;
        }
    }

    // ── Card renderer ──────────────────────────────────────────────────────

    function _tenderCard(t) {
        const fields    = t.fields || {};
        const fieldRows = Object.entries(fields).slice(0, 3).map(([k, v]) => {
            const val = String(v).length > 55 ? String(v).substring(0, 55) + '…' : String(v);
            return `<div class="card-field"><strong>${k}</strong><span>${val}</span></div>`;
        }).join('');

        const rvStatus = t.review_status || 'pending';
        const rvIcon   = rvStatus === 'approved' ? '✅' : rvStatus === 'rejected' ? '❌' : '⏳';
        const rvChip   = `<span class="card-review-chip chip-${rvStatus}">${rvIcon} ${rvStatus}</span>`;

        let actionsHtml = '<div class="card-actions">';
        if (t.url) {
            actionsHtml += `<a href="${t.url}" target="_blank" class="card-link">View ↗</a>`;
        }
        if (t.tender_dir) {
            const dlUrl = `/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken() || '')}`;
            actionsHtml += `<a href="${dlUrl}" class="card-dl-btn" download>⬇ Download All</a>`;
            actionsHtml += `<button class="card-files-btn" data-dir="${t.tender_dir}">📎 Files</button>`;
        }
        actionsHtml += `<a href="/status?open=taiq:${t.id}" class="card-review-link">📝 Review</a>`;
        actionsHtml += '</div>';

        return `
            <div class="result-card">
                <div class="card-meta">
                    <span class="site-badge site-${t.site}">${(t.site || '').toUpperCase()}</span>
                    <span class="kw-tag">${t.keyword || ''}</span>
                    ${rvChip}
                </div>
                <h4>${t.title || 'Unknown Opportunity'}</h4>
                <div class="card-fields">${fieldRows}</div>
                ${actionsHtml}
            </div>
        `;
    }

    // ── Utility ────────────────────────────────────────────────────────────

    function _fmtDate(d) {
        return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    }

    function _pretty(ds) {
        if (!ds) return '—';
        const [y, m, d] = ds.split('-');
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        return `${parseInt(d)} ${months[parseInt(m)-1]} ${y}`;
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
