// TAiQ Status page — monthly review dashboard (approve / reject / comment)
document.addEventListener('DOMContentLoaded', () => {

    const user = getUser();
    if (!user) return;   // initNav already redirects

    // ── State ──────────────────────────────────────────────────────────────
    let month        = _monthStr(new Date());   // "YYYY-MM"
    let statusFilter = '';                      // '' | approved | rejected | pending
    let searchQ      = '';
    let tenders      = [];
    let current      = null;                    // tender open in the modal
    let searchTimer  = null;
    let sortKey      = 'found_at';
    let sortDir      = -1;                      // -1 desc, 1 asc
    let trendRange   = 6;                       // months shown in the trend chart

    // ── Elements ───────────────────────────────────────────────────────────
    const monthLabel  = document.getElementById('rv-month-label');
    const tiles       = Array.from(document.querySelectorAll('.rv-tile'));
    const trendEl     = document.getElementById('rv-trend');
    const filterLabel = document.getElementById('rv-filter-label');
    const searchInput = document.getElementById('rv-search');
    const countLbl    = document.getElementById('rv-count-lbl');
    const tbodyEl     = document.getElementById('rv-tbody');
    const sortHeaders = Array.from(document.querySelectorAll('.rv-th-sort'));

    const overlay     = document.getElementById('rv-modal-overlay');
    const mBadge      = document.getElementById('rvm-badge');
    const mSite       = document.getElementById('rvm-site');
    const mKw         = document.getElementById('rvm-kw');
    const mClose      = document.getElementById('rvm-close');
    const mTitle      = document.getElementById('rvm-title');
    const mReviewInfo = document.getElementById('rvm-reviewinfo');
    const mFields     = document.getElementById('rvm-fields');
    const mLinks      = document.getElementById('rvm-links');
    const mFiles      = document.getElementById('rvm-files');
    const mComments   = document.getElementById('rvm-comments');
    const mDecision   = document.getElementById('rvm-decision');
    const mDecTitle   = document.getElementById('rvm-decision-title');
    const mInput      = document.getElementById('rvm-comment-input');
    const mApprove    = document.getElementById('rvm-approve');
    const mReject     = document.getElementById('rvm-reject');
    const mComment    = document.getElementById('rvm-comment');
    const mErr        = document.getElementById('rvm-err');
    const mLocked     = document.getElementById('rvm-locked');
    const mLockedText = document.getElementById('rvm-locked-text');
    const mEdit       = document.getElementById('rvm-edit');

    // ── Wiring ─────────────────────────────────────────────────────────────
    document.getElementById('rv-month-prev').addEventListener('click', () => _shiftMonth(-1));
    document.getElementById('rv-month-next').addEventListener('click', () => _shiftMonth(1));

    const trendRangeSel = document.getElementById('rv-trend-range');
    if (trendRangeSel) {
        trendRangeSel.addEventListener('change', () => {
            trendRange = parseInt(trendRangeSel.value, 10) || 6;
            loadSummary();
        });
    }

    tiles.forEach(tile => tile.addEventListener('click', () => {
        statusFilter = tile.dataset.status;
        tiles.forEach(t => t.classList.toggle('active', t === tile));
        _updateFilterLabel();
        loadList();
    }));

    searchInput.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            searchQ = searchInput.value.trim();
            loadList();
        }, 300);
    });

    sortHeaders.forEach(th => th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (sortKey === key) { sortDir = -sortDir; }
        else                 { sortKey = key; sortDir = key === 'found_at' ? -1 : 1; }
        _renderTable();
    }));

    mClose.addEventListener('click', closeModal);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !overlay.classList.contains('hidden')) closeModal();
    });

    mApprove.addEventListener('click', () => _decide('approved'));
    mReject .addEventListener('click', () => _decide('rejected'));
    mComment.addEventListener('click', _commentOnly);
    mEdit   .addEventListener('click', () => {
        mLocked.classList.add('hidden');
        mDecision.classList.remove('hidden');
        mDecTitle.textContent = 'Edit decision';
        mInput.focus();
    });

    // ── Bootstrap ──────────────────────────────────────────────────────────
    refresh().then(() => {
        // Deep link: /status?open=taiq:123
        const open = new URLSearchParams(window.location.search).get('open');
        if (open) {
            const [src, id] = open.split(':');
            if ((src === 'taiq' || src === 'manual') && id) openModal(src, parseInt(id, 10));
        }
    });

    async function refresh() {
        await Promise.all([loadSummary(), loadList()]);
    }

    // ── Summary + trend ────────────────────────────────────────────────────
    async function loadSummary() {
        try {
            const res  = await authFetch(`/review/summary?month=${month}&months=${trendRange}`);
            if (!res) return;
            const data = await res.json();
            monthLabel.textContent = _prettyMonth(month);
            document.getElementById('rv-num-scraped').textContent  = data.scraped  ?? 0;
            document.getElementById('rv-num-approved').textContent = data.approved ?? 0;
            document.getElementById('rv-num-rejected').textContent = data.rejected ?? 0;
            document.getElementById('rv-num-pending').textContent  = data.pending  ?? 0;
            _renderTileIcons();
            _renderTileTrends(data);
            _renderMetrics(data);
            _renderTrend(data.months || []);
        } catch { /* leave placeholders */ }
    }

    // Modal section headings carry their icon as a data-icon name so the
    // markup stays declarative and the SVG still comes from the one registry.
    document.querySelectorAll('.rv-section-title[data-icon]').forEach(el => {
        el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon));
    });

    function _renderTileIcons() {
        const map = {
            scraped:  'inbox',
            approved: 'check',
            rejected: 'cross',
            pending:  'clock',
        };
        Object.entries(map).forEach(([k, name]) => {
            const el = document.getElementById(`rv-ic-${k}`);
            if (el && !el.childElementCount) el.innerHTML = icon(name);
        });
        [['reviewed', 'clipboard'], ['avgtime', 'clock'], ['rate', 'chart']].forEach(([k, name]) => {
            const el = document.getElementById(`rv-mi-${k}`);
            if (el && !el.childElementCount) el.innerHTML = icon(name);
        });
    }

    // ── Tile sparkline + month-over-month delta ────────────────────────────
    // Both come from the `months` history already in the summary response, so
    // the trend line and the percentage are the same real data the chart uses.

    function _sparkline(values, tone) {
        const pts = values.filter(v => v !== null && v !== undefined);
        if (pts.length < 2) return '';
        const W = 64, H = 22, P = 2;
        const max = Math.max(...pts), min = Math.min(...pts);
        const span = max - min || 1;
        const x = i => P + (i * (W - 2 * P)) / (pts.length - 1);
        const y = v => H - P - ((v - min) / span) * (H - 2 * P);
        const d = pts.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        return `
            <svg class="rv-spark rv-spark-${tone}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
                <path d="${d}"/>
                <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(pts[pts.length - 1]).toFixed(1)}" r="2"/>
            </svg>`;
    }

    // A percentage needs a baseline. When the previous month is absent or zero
    // there is no honest comparison to draw, so nothing is shown.
    function _momDelta(months, key) {
        const prevMonth = _shiftMonthStr(month, -1);
        const cur  = months.find(m => m.month === month);
        const prev = months.find(m => m.month === prevMonth);
        if (!cur || !prev) return '';
        const a = cur[key] ?? 0, b = prev[key] ?? 0;
        const label = _shortMonth(prevMonth);
        if (b === 0) return a === 0 ? '' : `<span class="rv-delta-flat">new vs ${label}</span>`;
        const pct = Math.round(((a - b) / b) * 100);
        if (pct === 0) return `<span class="rv-delta-flat">no change vs ${label}</span>`;
        const up = pct > 0;
        return `<span class="rv-delta-${up ? 'up' : 'down'}">${up ? '↑' : '↓'} ${Math.abs(pct)}% vs ${label}</span>`;
    }

    function _renderTileTrends(d) {
        const months = d.months || [];
        const TONES = { scraped: 'neutral', approved: 'approved', rejected: 'rejected', pending: 'pending' };

        Object.keys(TONES).forEach(key => {
            const spark = document.getElementById(`rv-spark-${key}`);
            const delta = document.getElementById(`rv-delta-${key}`);
            if (spark) spark.innerHTML = _sparkline(months.map(m => m[key] ?? 0), TONES[key]);
            if (delta) delta.innerHTML = _momDelta(months, key);
        });

        // Pending is a live queue, not a monthly outcome: how long the oldest
        // item has waited says more than a month-over-month percentage.
        const pd = document.getElementById('rv-delta-pending');
        if (pd) {
            const h = d.oldest_pending_hours;
            if (!d.pending)      pd.innerHTML = '<span class="rv-delta-flat">queue clear</span>';
            else if (h == null)  pd.innerHTML = '';
            else {
                const age = h < 1  ? `${Math.round(h * 60)} min`
                          : h < 48 ? `${Math.round(h)} hrs`
                                   : `${Math.round(h / 24)} days`;
                const stale = h >= 168;   // a week
                pd.innerHTML = `<span class="rv-delta-${stale ? 'warn' : 'flat'}"
                    title="Longest-waiting pending tender">oldest waiting ${age}</span>`;
            }
        }
    }

    function _renderMetrics(d) {
        const reviewed = document.getElementById('rv-m-reviewed');
        const avgtime  = document.getElementById('rv-m-avgtime');
        const rate     = document.getElementById('rv-m-rate');

        reviewed.textContent = d.scraped ? `${d.decided ?? 0} of ${d.scraped}` : '—';

        if (d.avg_review_hours == null)      avgtime.textContent = '—';
        else if (d.avg_review_hours < 1)     avgtime.textContent = `${Math.round(d.avg_review_hours * 60)} min`;
        else if (d.avg_review_hours < 48)    avgtime.textContent = `${d.avg_review_hours} hrs`;
        else avgtime.textContent = `${(d.avg_review_hours / 24).toFixed(1)} days`;

        rate.textContent = d.approval_rate == null ? '—' : `${d.approval_rate}%`;
    }

    // Stacked bars in SVG so the chart can carry a real value axis and
    // gridlines — a bar height means nothing without a scale to read it against.
    let trendMonths = [];

    function _renderTrend(months) {
        trendMonths = months;
        if (!months.length) {
            trendEl.innerHTML = '<span class="empty-msg">No data yet.</span>';
            return;
        }

        // The viewBox is sized to the container's actual pixel width so one
        // SVG unit is one CSS pixel. Scaling a fixed viewBox to full width
        // stretched the chart to ~325px tall and magnified the axis text with
        // it; this keeps both at the size they were designed for.
        const W = Math.max(Math.round(trendEl.clientWidth) || 900, 420);
        const H = 194, PL = 40, PR = 14, PT = 22, PB = 28;
        const iw = W - PL - PR, ih = H - PT - PB;
        const max = _niceMax(Math.max(...months.map(m => m.scraped), 1));
        const y   = v => PT + ih - (v / max) * ih;

        // Cap the column width and centre the group, so two months don't sit
        // marooned at opposite ends of the plot area.
        const slot  = Math.min(iw / months.length, 120);
        const x0    = PL + (iw - slot * months.length) / 2;
        const barW  = Math.min(56, slot * 0.55);
        const cx    = i => x0 + slot * i + slot / 2;

        const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => {
            const v = Math.round(max * f), yy = y(max * f);
            return `<line class="rvc-grid" x1="${PL}" y1="${yy.toFixed(1)}" x2="${W - PR}" y2="${yy.toFixed(1)}"/>
                    <text class="rvc-ytick" x="${PL - 10}" y="${(yy + 3.5).toFixed(1)}">${v}</text>`;
        }).join('');

        const bars = months.map((m, i) => {
            const x = cx(i) - barW / 2;
            const segs = [
                ['approved', m.approved],
                ['rejected', m.rejected],
                ['pending',  m.pending],
            ];
            // Stack upward from the baseline: approved, then rejected, then
            // pending — the three always sum to scraped.
            let acc = 0;
            const rects = segs.map(([tone, val]) => {
                if (!val) return '';
                const h  = (val / max) * ih;
                const yy = PT + ih - h - acc;
                acc += h;
                return `<rect class="rvc-seg rvc-seg-${tone}" x="${x.toFixed(1)}" y="${yy.toFixed(1)}"
                              width="${barW.toFixed(1)}" height="${h.toFixed(1)}"/>`;
            }).join('');

            const isCur = m.month === month;
            return `
                <g class="rvc-col${isCur ? ' rvc-col-current' : ''}" data-month="${m.month}" role="button" tabindex="0">
                    <title>${_prettyMonth(m.month)} — ${m.scraped} scraped · ${m.approved} approved · ${m.rejected} rejected · ${m.pending} pending</title>
                    <rect class="rvc-hit" x="${(cx(i) - slot / 2).toFixed(1)}" y="${PT}" width="${slot.toFixed(1)}" height="${ih}"/>
                    ${rects}
                    <text class="rvc-total" x="${cx(i).toFixed(1)}" y="${(y(m.scraped) - 8).toFixed(1)}">${m.scraped}</text>
                    <text class="rvc-xtick" x="${cx(i).toFixed(1)}" y="${H - 12}">${_shortMonth(m.month).toUpperCase()}</text>
                </g>`;
        }).join('');

        trendEl.innerHTML = `
            <svg viewBox="0 0 ${W} ${H}" class="rvc-svg" role="img"
                 aria-label="Tenders scraped per month, split by review status">
                ${ticks}
                <line class="rvc-axis" x1="${PL}" y1="${PT + ih}" x2="${W - PR}" y2="${PT + ih}"/>
                ${bars}
            </svg>`;

        // Clicking a column jumps to that month
        trendEl.querySelectorAll('.rvc-col').forEach(col => {
            const go = () => { month = col.dataset.month; refresh(); };
            col.addEventListener('click', go);
            col.addEventListener('keydown', e => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
            });
        });
    }

    // The viewBox is tied to the container's pixel width, so a resize has to
    // redraw rather than just rescale.
    let trendResizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(trendResizeTimer);
        trendResizeTimer = setTimeout(() => {
            if (trendMonths.length) _renderTrend(trendMonths);
        }, 150);
    });

    // The axis draws four gridlines, so the top of the scale must divide by 4
    // into whole tenders — otherwise the ticks read 8 / 15 / 23 instead of
    // 6 / 12 / 18. Counts are integers, so the step must be one too.
    const _AXIS_STEPS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50,
                         60, 75, 100, 125, 150, 200, 250, 300, 400, 500, 750, 1000];

    function _niceMax(n) {
        for (const step of _AXIS_STEPS) {
            if (step * 4 >= n) return step * 4;
        }
        return Math.ceil(n / 4) * 4;
    }

    // ── Tender table ───────────────────────────────────────────────────────
    async function loadList() {
        tbodyEl.innerHTML = '<tr><td colspan="8" class="empty-msg">Loading…</td></tr>';
        try {
            let url = `/review/tenders?month=${month}`;
            if (statusFilter) url += `&status=${statusFilter}`;
            if (searchQ)      url += `&q=${encodeURIComponent(searchQ)}`;
            const res  = await authFetch(url);
            if (!res) return;
            const data = await res.json();
            tenders = data.tenders || [];
            countLbl.textContent = `${tenders.length} tender${tenders.length === 1 ? '' : 's'}`;
            _renderTable();
        } catch {
            tbodyEl.innerHTML = '<tr><td colspan="8" class="empty-msg">Could not load tenders.</td></tr>';
        }
    }

    const _STATUS_ORDER = { pending: 0, rejected: 1, approved: 2 };

    function _renderTable() {
        // Sort indicator on headers
        sortHeaders.forEach(th => {
            th.classList.toggle('sorted', th.dataset.sort === sortKey);
            th.dataset.dir = th.dataset.sort === sortKey ? (sortDir === 1 ? 'asc' : 'desc') : '';
        });

        if (!tenders.length) {
            tbodyEl.innerHTML = '<tr><td colspan="8" class="empty-msg">No tenders match this view.</td></tr>';
            return;
        }

        const sorted = [...tenders].sort((a, b) => {
            let va, vb;
            if (sortKey === 'review_status') {
                va = _STATUS_ORDER[a.review_status || 'pending'];
                vb = _STATUS_ORDER[b.review_status || 'pending'];
            } else if (sortKey === 'id') {
                va = a.id; vb = b.id;
            } else {
                va = String(a[sortKey] ?? '').toLowerCase();
                vb = String(b[sortKey] ?? '').toLowerCase();
            }
            if (va < vb) return -sortDir;
            if (va > vb) return  sortDir;
            return 0;
        });

        tbodyEl.innerHTML = sorted.map(_rowTr).join('');
        tbodyEl.querySelectorAll('tr[data-id]').forEach(row => {
            row.addEventListener('click', () =>
                openModal(row.dataset.source, parseInt(row.dataset.id, 10)));
        });
    }

    function _rowTr(t) {
        const st    = t.review_status || 'pending';
        const byWho = t.reviewed_by_name ? ` · ${_esc(t.reviewed_by_name)}` : '';
        const statusPill = st === 'pending'
            ? `<span class="rv-pill rv-pill-pending" title="Awaiting review">${icon('hourglass')}Pending</span>`
            : st === 'approved'
                ? `<span class="rv-pill rv-pill-approved" title="Approved${byWho ? ' by' + byWho.slice(2) : ''} on ${_prettyDT(t.reviewed_at)}">${icon('check')}Approved${byWho}</span>`
                : `<span class="rv-pill rv-pill-rejected" title="Rejected${byWho ? ' by' + byWho.slice(2) : ''} on ${_prettyDT(t.reviewed_at)}">${icon('cross')}Rejected${byWho}</span>`;
        const idTag = `${t.source === 'taiq' ? 'T' : 'M'}-${t.id}`;
        const method = t.source === 'taiq'
            ? `${icon('bot')}TAiQ` : `${icon('search')}Manual`;
        return `
            <tr class="rv-tr rv-tr-${st}" data-source="${t.source}" data-id="${t.id}">
                <td class="rv-td-id">${idTag}</td>
                <td class="rv-td-title" title="${_esc(t.title || '')}">${_esc(t.title || 'Unknown Opportunity')}</td>
                <td><span class="site-badge site-${_esc(t.site)}">${_esc((t.site || '').toUpperCase())}</span></td>
                <td><span class="kw-tag">${_esc(t.keyword || '')}</span></td>
                <td class="rv-td-src">${method}</td>
                <td class="rv-td-date">${_prettyDT(t.found_at)}</td>
                <td>${statusPill}</td>
                <td class="rv-td-act">
                    <button type="button" class="rv-row-open" aria-label="Open review for ${idTag}">
                        ${icon('edit')}
                    </button>
                </td>
            </tr>`;
    }

    // ── Modal ──────────────────────────────────────────────────────────────
    async function openModal(source, id) {
        try {
            const res = await authFetch(`/review/tender/${source}/${id}`);
            if (!res || !res.ok) return;
            const data = await res.json();
            current = data.tender;
            _renderModal(data.tender, data.comments || []);
            overlay.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
        } catch { /* ignore */ }
    }

    function closeModal() {
        overlay.classList.add('hidden');
        document.body.style.overflow = '';
        current = null;
        // Clear deep-link param so refresh doesn't reopen
        if (window.location.search.includes('open=')) {
            history.replaceState(null, '', '/status');
        }
    }

    function _renderModal(t, comments) {
        const st = t.review_status || 'pending';
        mBadge.innerHTML = st === 'approved' ? `${icon('check')}Approved`
                         : st === 'rejected' ? `${icon('cross')}Rejected`
                                             : `${icon('hourglass')}Pending`;
        mBadge.className   = `rv-status-badge rv-badge-${st}`;
        mSite.textContent  = (t.site || '').toUpperCase();
        mSite.className    = `site-badge site-${t.site}`;
        mKw.textContent    = t.keyword || '';
        mTitle.textContent = t.title || 'Unknown Opportunity';

        if (st !== 'pending') {
            const by = t.reviewed_by_name
                ? ` by <strong>${_esc(t.reviewed_by_name)}</strong>` : '';
            mReviewInfo.classList.remove('hidden');
            mReviewInfo.innerHTML =
                `${icon(st === 'approved' ? 'check' : 'cross')}<strong>${_esc(st)}</strong>${by}` +
                ` on ${_prettyDT(t.reviewed_at)}`;
        } else {
            mReviewInfo.classList.add('hidden');
        }

        // Overview fields — long values span the full width so words never clip
        const fields = t.fields || {};
        mFields.innerHTML = Object.keys(fields).length
            ? Object.entries(fields).map(([k, v]) => {
                const val  = String(v);
                const wide = val.length > 90 ? ' rv-field-wide' : '';
                return `<div class="rv-field${wide}"><strong>${_esc(k)}</strong><span>${_esc(val)}</span></div>`;
            }).join('')
            : '<span class="empty-msg">No summary fields available.</span>';

        // Links
        let links = '';
        if (t.url)        links += `<a href="${_esc(t.url)}" target="_blank" class="card-link">View source ↗</a>`;
        if (t.tender_dir) links += `<a href="/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken() || '')}" class="rv-dl-all" download>${icon('download')}Download All Files</a>`;
        mLinks.innerHTML = links;

        _loadFiles(t.tender_dir);
        _renderComments(comments);

        // Decision panel vs locked bar
        mInput.value = '';
        mErr.classList.add('hidden');
        _setBtnsBusy(false);
        if (st === 'pending') {
            mDecision.classList.remove('hidden');
            mLocked.classList.add('hidden');
            mDecTitle.textContent = 'Your review';
        } else {
            mDecision.classList.add('hidden');
            mLocked.classList.remove('hidden');
            const canEdit = hasRole('admin') ||
                            String(t.reviewed_by || '') === String(user.sub);
            mLockedText.innerHTML =
                `Decision recorded${canEdit ? '' : ' — only admins or the original reviewer can change it'}.`;
            mEdit.classList.toggle('hidden', !canEdit);
        }
    }

    async function _loadFiles(dir) {
        if (!dir) {
            mFiles.innerHTML = '<span class="empty-msg">No files for this tender.</span>';
            return;
        }
        mFiles.innerHTML = '<span class="empty-msg">Loading files…</span>';
        try {
            const token = getToken() || '';
            const res   = await fetch(`/tender/files?dir=${encodeURIComponent(dir)}&token=${encodeURIComponent(token)}`);
            const files = await res.json();
            if (!Array.isArray(files) || !files.length) {
                mFiles.innerHTML = '<span class="empty-msg">No files for this tender.</span>';
                return;
            }
            mFiles.innerHTML = files.map(f => {
                const url  = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(token)}`;
                const ext  = (f.name.split('.').pop() || '').toLowerCase();
                const kind = ['xlsx', 'xls', 'csv'].includes(ext) ? 'xls'
                           : ext === 'pdf'                        ? 'pdf'
                           : ['doc', 'docx'].includes(ext)        ? 'doc'
                           : ext === 'zip'                        ? 'zip' : 'file';
                const tag  = kind === 'file'
                    ? (ext ? ext.slice(0, 4).toUpperCase() : 'FILE')
                    : kind.toUpperCase();
                return `<div class="rv-file-card">
                    <span class="rv-file-ico ico-${kind}">${tag}</span>
                    <span class="rv-file-name" title="${_esc(f.name)}">${_esc(f.name)}</span>
                    <a href="${url}" class="rv-file-view" download>View</a>
                </div>`;
            }).join('');
        } catch {
            mFiles.innerHTML = '<span class="empty-msg">Could not load files.</span>';
        }
    }

    function _renderComments(comments) {
        if (!comments.length) {
            mComments.innerHTML = '<span class="empty-msg">No comments yet.</span>';
            return;
        }
        mComments.innerHTML = comments.map(c => {
            // Not named `icon` — that would shadow the global icon() helper.
            const glyph = icon(c.action === 'approved' ? 'check'
                             : c.action === 'rejected' ? 'cross' : 'chat');
            const act  = c.action === 'comment' ? 'commented'
                       : `marked as <strong>${_esc(c.action)}</strong>`;
            const name = c.username ? _esc(c.username) : 'A team member';
            return `
                <div class="rv-comment rv-comment-${_esc(c.action)}">
                    <div class="rv-comment-head">
                        <span>${glyph} <strong>${name}</strong> ${act}</span>
                        <span class="rv-comment-ts">${_prettyDT(c.created_at)}</span>
                    </div>
                    <div class="rv-comment-body">${_esc(c.comment)}</div>
                </div>`;
        }).join('');
    }

    // ── Actions ────────────────────────────────────────────────────────────
    async function _decide(status) {
        if (!current) return;
        const comment = mInput.value.trim();
        if (!comment) {
            _showErr(`Please add a comment explaining why this is being ${status}.`);
            mInput.focus();
            return;
        }
        _setBtnsBusy(true);
        try {
            const res = await authFetch(
                `/review/tender/${current.source}/${current.id}/decision`,
                { method: 'POST', body: JSON.stringify({ status, comment }) });
            if (!res) return;
            const data = await res.json();
            if (!res.ok || !data.ok) {
                _showErr(data.error || 'Could not save the decision.');
                _setBtnsBusy(false);
                return;
            }
            current = data.tender;
            _renderModal(data.tender, data.comments || []);
            refresh();   // update tiles + list behind the modal
        } catch {
            _showErr('Network error — please try again.');
            _setBtnsBusy(false);
        }
    }

    async function _commentOnly() {
        if (!current) return;
        const comment = mInput.value.trim();
        if (!comment) {
            _showErr('Write a comment first.');
            mInput.focus();
            return;
        }
        _setBtnsBusy(true);
        try {
            const res = await authFetch(
                `/review/tender/${current.source}/${current.id}/comment`,
                { method: 'POST', body: JSON.stringify({ comment }) });
            if (!res) return;
            const data = await res.json();
            if (!res.ok || !data.ok) {
                _showErr(data.error || 'Could not save the comment.');
            } else {
                mInput.value = '';
                mErr.classList.add('hidden');
                _renderComments(data.comments || []);
            }
        } catch {
            _showErr('Network error — please try again.');
        }
        _setBtnsBusy(false);
    }

    function _showErr(msg) {
        mErr.textContent = msg;
        mErr.classList.remove('hidden');
    }

    function _setBtnsBusy(busy) {
        [mApprove, mReject, mComment].forEach(b => b.disabled = busy);
    }

    // ── Utilities ──────────────────────────────────────────────────────────
    function _shiftMonth(delta) {
        month = _shiftMonthStr(month, delta);
        refresh();
    }

    function _shiftMonthStr(m, delta) {
        const [y, mo] = m.split('-').map(Number);
        return _monthStr(new Date(y, mo - 1 + delta, 1));
    }

    function _monthStr(d) {
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    }

    const _MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    function _prettyMonth(m) {
        const [y, mo] = m.split('-');
        return `${['January','February','March','April','May','June','July','August','September','October','November','December'][parseInt(mo) - 1]} ${y}`;
    }

    function _shortMonth(m) {
        return _MONTHS[parseInt(m.split('-')[1]) - 1];
    }

    function _prettyDT(s) {
        if (!s) return '—';
        const [d, t] = s.split('T');
        if (!d) return s;
        const [y, mo, day] = d.split('-');
        const time = t ? ` ${t.slice(0, 5)}` : '';
        return `${parseInt(day)} ${_MONTHS[parseInt(mo) - 1]} ${y}${time}`;
    }

    function _updateFilterLabel() {
        filterLabel.textContent = statusFilter
            ? `${statusFilter.charAt(0).toUpperCase()}${statusFilter.slice(1)} tenders`
            : 'All tenders';
    }

    function _esc(s) {
        return String(s ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
});
