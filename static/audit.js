document.addEventListener('DOMContentLoaded', () => {
    initNav({ requireRole: 'superadmin' });

    const tableWrap  = document.getElementById('audit-table-wrap');
    const pagination = document.getElementById('pagination');
    const totalEl    = document.getElementById('audit-total');
    const filterUser = document.getElementById('filter-user');

    let currentPage = 1;
    const PAGE_SIZE = 50;
    // username → id lookup built from results
    let userMap = {};

    document.getElementById('apply-filter').addEventListener('click', () => {
        currentPage = 1;
        loadLogs();
    });
    document.getElementById('clear-filter').addEventListener('click', () => {
        filterUser.value = '';
        currentPage = 1;
        loadLogs();
    });
    filterUser.addEventListener('keydown', e => {
        if (e.key === 'Enter') { currentPage = 1; loadLogs(); }
    });

    loadLogs();

    async function loadLogs() {
        tableWrap.innerHTML = '<p class="empty-msg">Loading…</p>';
        pagination.innerHTML = '';

        let url = `/superadmin/logs?page=${currentPage}&limit=${PAGE_SIZE}`;
        const usernameFilter = filterUser.value.trim().toLowerCase();

        const res = await authFetch(url);
        if (!res) return;
        const data = await res.json();

        const items = usernameFilter
            ? data.items.filter(i => (i.username || '').toLowerCase().includes(usernameFilter))
            : data.items;

        totalEl.textContent = `${data.total} total`;

        if (!items || items.length === 0) {
            tableWrap.innerHTML = '<p class="empty-msg">No activity logs found.</p>';
            return;
        }

        tableWrap.innerHTML = `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>User</th>
                        <th>Action</th>
                        <th>What happened</th>
                        <th>IP</th>
                    </tr>
                </thead>
                <tbody>
                    ${items.map(log => {
                        const { label, cls, text } = _describe(log);
                        return `
                            <tr>
                                <td class="time-cell">${_fmt(log.timestamp)}</td>
                                <td><strong>${_esc(log.username) || '—'}</strong></td>
                                <td><span class="action-badge ${cls}">${label}</span></td>
                                <td class="audit-desc">${text}</td>
                                <td class="time-cell">${log.ip_address || '—'}</td>
                            </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        `;

        // Pagination
        const totalPages = Math.ceil(data.total / PAGE_SIZE);
        if (totalPages > 1) {
            let pHtml = '';
            if (currentPage > 1) {
                pHtml += `<button class="page-btn" data-page="${currentPage - 1}">← Prev</button>`;
            }
            // Show window of pages
            const start = Math.max(1, currentPage - 2);
            const end   = Math.min(totalPages, currentPage + 2);
            for (let p = start; p <= end; p++) {
                pHtml += `<button class="page-btn${p === currentPage ? ' active' : ''}" data-page="${p}">${p}</button>`;
            }
            if (currentPage < totalPages) {
                pHtml += `<button class="page-btn" data-page="${currentPage + 1}">Next →</button>`;
            }
            pagination.innerHTML = pHtml;
            pagination.querySelectorAll('.page-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    currentPage = parseInt(btn.dataset.page);
                    loadLogs();
                });
            });
        }
    }

    // Turn a raw log row into a badge + plain-English sentence
    function _describe(log) {
        let d = {};
        try { d = JSON.parse(log.details_json || '{}'); } catch {}
        const t = s => `<em>“${_esc(s)}”</em>`;

        switch (log.action) {
            case 'login':
                return { label: 'Login', cls: 'action-login',
                         text: 'Logged into TAiQ' };

            case 'scrape_start': {
                const kws = Array.isArray(d.keywords) ? d.keywords.join(', ') : d.keywords;
                return { label: 'Scrape', cls: 'action-scrape',
                         text: `Started a manual scrape on ${_esc((d.site || '?').toUpperCase())}`
                               + (kws ? ` for keywords ${t(kws)}` : '') };
            }

            case 'taiq_run':
                return { label: 'TAiQ', cls: 'action-scrape',
                         text: 'Manually triggered the TAiQ daily scrape' };
            case 'taiq_stop':
                return { label: 'TAiQ', cls: 'action-danger',
                         text: `Stopped the running TAiQ scrape${d.run_id ? ` (run #${d.run_id})` : ''}` };

            case 'tender_approved': {
                const edited = d.previous_status && d.previous_status !== 'pending';
                return { label: 'Approved', cls: 'action-approve',
                         text: `${edited ? 'Changed the decision to APPROVED for' : 'Approved'} tender ${t(d.title || '?')}` };
            }
            case 'tender_rejected': {
                const edited = d.previous_status && d.previous_status !== 'pending';
                return { label: 'Rejected', cls: 'action-reject',
                         text: `${edited ? 'Changed the decision to REJECTED for' : 'Rejected'} tender ${t(d.title || '?')}` };
            }
            case 'tender_commented':
                return { label: 'Comment', cls: 'action-comment',
                         text: `Commented on tender ${t(d.title || '?')}` };

            case 'download_file':
                return { label: 'Download', cls: 'action-download',
                         text: `Downloaded file ${t(d.name || '?')}` };
            case 'download_tender_zip':
                return { label: 'Download', cls: 'action-download',
                         text: `Downloaded all documents of tender ${t(d.name || '?')} as a ZIP` };
            case 'download_report':
                return { label: 'Download', cls: 'action-download',
                         text: `Downloaded report ${t(d.name || '?')}` };

            case 'create_user':
                return { label: 'User', cls: 'action-default',
                         text: `Created a new ${_esc(d.role || 'user')} account ${t(d.new_user || '?')}` };
            case 'update_user': {
                const ch = d.changes || {};
                const parts = [];
                if (ch.username !== undefined)  parts.push(`renamed to ${t(ch.username)}`);
                if (ch.email !== undefined)     parts.push(`email changed to ${t(ch.email)}`);
                if (ch.is_active !== undefined) parts.push(ch.is_active ? 'account re-activated' : 'account deactivated');
                return { label: 'User', cls: 'action-default',
                         text: `Updated user #${d.target_id || '?'}${parts.length ? ' — ' + parts.join(', ') : ''}` };
            }
            case 'deactivate_user':
                return { label: 'User', cls: 'action-danger',
                         text: `Deactivated user account #${d.target_id || '?'}` };

            default: {
                // Unknown action — prettify it instead of showing raw jargon
                const nice    = log.action.replace(/_/g, ' ');
                const details = Object.entries(d)
                    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
                    .join(' · ');
                return { label: nice.charAt(0).toUpperCase() + nice.slice(1),
                         cls: 'action-default',
                         text: _esc(details) || '—' };
            }
        }
    }

    function _esc(s) {
        return String(s ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function _fmt(iso) {
        if (!iso) return '—';
        const [date, time] = iso.split('T');
        return `${date} ${time ? time.substring(0,5) : ''}`;
    }
});
