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

        const actionLabels = {
            login:          'Login',
            scrape_start:   'Scrape Start',
            create_user:    'Create User',
            update_user:    'Update User',
            deactivate_user:'Deactivate User',
        };

        tableWrap.innerHTML = `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>User</th>
                        <th>Action</th>
                        <th>Details</th>
                        <th>IP</th>
                    </tr>
                </thead>
                <tbody>
                    ${items.map(log => {
                        let details = '';
                        try {
                            const d = JSON.parse(log.details_json || '{}');
                            details = Object.entries(d)
                                .map(([k,v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
                                .join(' · ');
                        } catch {}
                        const actionCls = log.action === 'login' ? 'action-login'
                                        : log.action.startsWith('scrape') ? 'action-scrape'
                                        : log.action.includes('deactivate') ? 'action-danger'
                                        : 'action-default';
                        return `
                            <tr>
                                <td class="time-cell">${_fmt(log.timestamp)}</td>
                                <td><strong>${log.username || '—'}</strong></td>
                                <td><span class="action-badge ${actionCls}">${actionLabels[log.action] || log.action}</span></td>
                                <td class="details-cell">${details || '—'}</td>
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

    function _fmt(iso) {
        if (!iso) return '—';
        const [date, time] = iso.split('T');
        return `${date} ${time ? time.substring(0,5) : ''}`;
    }
});
