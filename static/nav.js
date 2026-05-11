// TAiQ navigation — call initNav() on every page
function initNav({ requireRole = null } = {}) {
    const user = getUser();

    if (!user) {
        window.location.href = '/login';
        return;
    }

    if (requireRole && !hasRole(requireRole)) {
        window.location.href = '/';
        return;
    }

    _renderNav(user);
}

function _renderNav(user) {
    const linksEl   = document.getElementById('nav-links');
    const userArea  = document.getElementById('nav-user-area');
    if (!linksEl || !userArea) return;

    const currentPath = window.location.pathname;

    const links = [
        { href: '/dashboard',  label: 'Dashboard',  minRole: 'admin' },
        { href: '/',           label: 'Scraper',    minRole: 'user' },
        { href: '/taiq-work',  label: 'TAiQ Work',  minRole: 'user' },
        { href: '/users',      label: 'Users',      minRole: 'admin' },
        { href: '/audit',      label: 'Audit Logs', minRole: 'superadmin' },
    ];

    linksEl.innerHTML = links
        .filter(l => hasRole(l.minRole))
        .map(l => {
            const active = (l.href === '/' ? currentPath === '/' : currentPath.startsWith(l.href));
            return `<a href="${l.href}" class="nav-link${active ? ' active' : ''}">${l.label}</a>`;
        })
        .join('');

    const roleLabels = { superadmin: 'Super Admin', admin: 'Admin', user: 'User' };
    userArea.innerHTML = `
        <div class="nav-user">
            <span class="nav-username">${user.username}</span>
            <span class="role-badge role-${user.role}">${roleLabels[user.role] || user.role}</span>
            <button class="nav-logout-btn" onclick="logout()">Logout</button>
        </div>
    `;
}
