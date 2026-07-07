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

    // ── Elements ───────────────────────────────────────────────────────────
    const monthLabel  = document.getElementById('rv-month-label');
    const tiles       = Array.from(document.querySelectorAll('.rv-tile'));
    const trendEl     = document.getElementById('rv-trend');
    const filterLabel = document.getElementById('rv-filter-label');
    const searchInput = document.getElementById('rv-search');
    const countLbl    = document.getElementById('rv-count-lbl');
    const listEl      = document.getElementById('rv-list');

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
            const res  = await authFetch(`/review/summary?month=${month}`);
            if (!res) return;
            const data = await res.json();
            monthLabel.textContent = _prettyMonth(month);
            document.getElementById('rv-num-scraped').textContent  = data.scraped  ?? 0;
            document.getElementById('rv-num-approved').textContent = data.approved ?? 0;
            document.getElementById('rv-num-rejected').textContent = data.rejected ?? 0;
            document.getElementById('rv-num-pending').textContent  = data.pending  ?? 0;
            _renderTrend(data.months || []);
        } catch { /* leave placeholders */ }
    }

    function _renderTrend(months) {
        if (!months.length) {
            trendEl.innerHTML = '<span class="empty-msg">No data yet.</span>';
            return;
        }
        const max = Math.max(...months.map(m => m.scraped), 1);
        trendEl.innerHTML = months.map(m => {
            const h  = x => Math.round((x / max) * 100);
            const on = m.month === month ? ' rv-bar-current' : '';
            return `
                <div class="rv-bar-col${on}" data-month="${m.month}" title="${_prettyMonth(m.month)} — ${m.scraped} scraped, ${m.approved} approved, ${m.rejected} rejected">
                    <div class="rv-bar-stack">
                        <div class="rv-bar-seg rv-seg-pending"  style="height:${h(m.pending)}%"></div>
                        <div class="rv-bar-seg rv-seg-rejected" style="height:${h(m.rejected)}%"></div>
                        <div class="rv-bar-seg rv-seg-approved" style="height:${h(m.approved)}%"></div>
                    </div>
                    <span class="rv-bar-lbl">${_shortMonth(m.month)}</span>
                    <span class="rv-bar-num">${m.scraped}</span>
                </div>`;
        }).join('');
        // Clicking a trend bar jumps to that month
        trendEl.querySelectorAll('.rv-bar-col').forEach(col => {
            col.addEventListener('click', () => {
                month = col.dataset.month;
                refresh();
            });
        });
    }

    // ── Tender list ────────────────────────────────────────────────────────
    async function loadList() {
        listEl.innerHTML = '<p class="empty-msg">Loading…</p>';
        try {
            let url = `/review/tenders?month=${month}`;
            if (statusFilter) url += `&status=${statusFilter}`;
            if (searchQ)      url += `&q=${encodeURIComponent(searchQ)}`;
            const res  = await authFetch(url);
            if (!res) return;
            const data = await res.json();
            tenders = data.tenders || [];
            countLbl.textContent = `${tenders.length} tender${tenders.length === 1 ? '' : 's'}`;
            if (!tenders.length) {
                listEl.innerHTML = '<p class="empty-msg">No tenders match this view.</p>';
                return;
            }
            listEl.innerHTML = tenders.map(_rowHtml).join('');
            listEl.querySelectorAll('.rv-row').forEach(row => {
                row.addEventListener('click', () =>
                    openModal(row.dataset.source, parseInt(row.dataset.id, 10)));
            });
        } catch {
            listEl.innerHTML = '<p class="empty-msg">Could not load tenders.</p>';
        }
    }

    function _rowHtml(t) {
        const st   = t.review_status || 'pending';
        const icon = st === 'approved' ? '✅' : st === 'rejected' ? '❌' : '⏳';
        // Regular users don't receive reviewer names (server redacts them)
        const byWho = t.reviewed_by_name
            ? ` by <strong>${_esc(t.reviewed_by_name)}</strong>` : '';
        const who  = st === 'pending'
            ? '<span class="rv-row-who rv-row-pending">awaiting review</span>'
            : `<span class="rv-row-who">${st}${byWho} · ${_prettyDT(t.reviewed_at)}</span>`;
        return `
            <div class="rv-row rv-row-${st}" data-source="${t.source}" data-id="${t.id}">
                <span class="rv-row-icon">${icon}</span>
                <div class="rv-row-main">
                    <span class="rv-row-title">${_esc(t.title || 'Unknown Opportunity')}</span>
                    <div class="rv-row-meta">
                        <span class="site-badge site-${_esc(t.site)}">${_esc((t.site || '').toUpperCase())}</span>
                        <span class="kw-tag">${_esc(t.keyword || '')}</span>
                        <span class="rv-row-src">${t.source === 'taiq' ? '🤖 TAiQ' : '🔍 Manual'}</span>
                        <span class="rv-row-date">${_prettyDT(t.found_at)}</span>
                    </div>
                </div>
                ${who}
            </div>`;
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
        mBadge.textContent = st === 'approved' ? '✅ Approved'
                           : st === 'rejected' ? '❌ Rejected' : '⏳ Pending';
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
                `${st === 'approved' ? '✅' : '❌'} <strong>${_esc(st)}</strong>${by}` +
                ` on ${_prettyDT(t.reviewed_at)}`;
        } else {
            mReviewInfo.classList.add('hidden');
        }

        // Overview fields
        const fields = t.fields || {};
        mFields.innerHTML = Object.keys(fields).length
            ? Object.entries(fields).map(([k, v]) =>
                `<div class="rv-field"><strong>${_esc(k)}</strong><span>${_esc(String(v))}</span></div>`).join('')
            : '<span class="empty-msg">No summary fields available.</span>';

        // Links
        let links = '';
        if (t.url)        links += `<a href="${_esc(t.url)}" target="_blank" class="card-link">View source ↗</a>`;
        if (t.tender_dir) links += `<a href="/download/tender?path=${encodeURIComponent(t.tender_dir)}&token=${encodeURIComponent(getToken() || '')}" class="card-dl-btn" download>⬇ Download All</a>`;
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
                const url = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(token)}`;
                return `<div class="card-file-item">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <a href="${url}" download title="${_esc(f.name)}">${_esc(f.name)}</a>
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
            const icon = c.action === 'approved' ? '✅'
                       : c.action === 'rejected' ? '❌' : '💬';
            const act  = c.action === 'comment' ? 'commented'
                       : `marked as <strong>${_esc(c.action)}</strong>`;
            const name = c.username ? _esc(c.username) : 'A team member';
            return `
                <div class="rv-comment rv-comment-${_esc(c.action)}">
                    <div class="rv-comment-head">
                        <span>${icon} <strong>${name}</strong> ${act}</span>
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
        const [y, m] = month.split('-').map(Number);
        const d = new Date(y, m - 1 + delta, 1);
        month = _monthStr(d);
        refresh();
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
