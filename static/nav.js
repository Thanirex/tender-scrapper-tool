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

// Navigation lives in the left sidebar. The top bar keeps branding and the
// user menu only, so the links are not duplicated in two places.
const NAV_LINKS = [
    { href: '/dashboard',  label: 'Dashboard',  minRole: 'user',       icon: 'home'   },
    { href: '/',           label: 'Scraper',    minRole: 'user',       icon: 'search' },
    { href: '/taiq-work',  label: 'TAiQ Work',  minRole: 'user',       icon: 'bot'    },
    { href: '/status',     label: 'Status',     minRole: 'user',       icon: 'pulse'  },
    { href: '/users',      label: 'Users',      minRole: 'admin',      icon: 'users'  },
    { href: '/audit',      label: 'Audit Logs', minRole: 'superadmin', icon: 'file'   },
];

// Icons come from the shared registry in icons.js, which every page loads
// before this file — one icon set for the whole app rather than a per-page copy.
function _navIcon(name) {
    return icon(name, 'side-link-icon', 1.7);
}

function _isActive(href, currentPath) {
    return href === '/' ? currentPath === '/' : currentPath.startsWith(href);
}

function _renderNav(user) {
    const userArea = document.getElementById('nav-user-area');
    const currentPath = window.location.pathname;
    const allowed = NAV_LINKS.filter(l => hasRole(l.minRole));

    // The old top-bar link strip is retired; the sidebar owns navigation.
    const linksEl = document.getElementById('nav-links');
    if (linksEl) linksEl.remove();

    _renderSidebar(allowed, currentPath);

    if (!userArea) return;

    const roleLabels = { superadmin: 'Super Admin', admin: 'Admin', user: 'User' };
    const teamId = (user.team_id || 'cnk').toLowerCase();
    const teamName = (user.team_name || teamId).toUpperCase();
    const initial = (user.username || '?').trim().charAt(0).toUpperCase();

    userArea.innerHTML = `
        <div class="nav-user">
            <a class="portal-btn" href="/dashboard">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M14 4h6v6"/><path d="M20 4 11 13"/>
                    <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>
                </svg>${teamName} PORTAL
            </a>
            <span class="nav-username">${user.username}</span>
            <span class="role-badge role-${user.role}">${roleLabels[user.role] || user.role}</span>
            <div class="nav-avatar-wrap">
                <button class="nav-avatar" id="nav-avatar-btn" type="button"
                        aria-haspopup="true" aria-expanded="false"
                        title="${user.username}">${initial}</button>
                <div class="nav-avatar-menu" id="nav-avatar-menu">
                    <div class="nam-head">
                        <strong>${user.username}</strong>
                        <span>${roleLabels[user.role] || user.role} · ${teamName}</span>
                    </div>
                    <button class="nam-item" type="button" onclick="logout()">Log out</button>
                </div>
            </div>
        </div>
    `;

    const avatarBtn  = document.getElementById('nav-avatar-btn');
    const avatarMenu = document.getElementById('nav-avatar-menu');
    if (avatarBtn && avatarMenu) {
        avatarBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = avatarMenu.classList.toggle('open');
            avatarBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
        document.addEventListener('click', () => {
            avatarMenu.classList.remove('open');
            avatarBtn.setAttribute('aria-expanded', 'false');
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                avatarMenu.classList.remove('open');
                avatarBtn.setAttribute('aria-expanded', 'false');
            }
        });
    }
}

function _renderSidebar(links, currentPath) {
    if (document.getElementById('app-sidebar')) return;

    const aside = document.createElement('aside');
    aside.className = 'app-sidebar';
    aside.id = 'app-sidebar';
    aside.innerHTML = `
        <nav class="side-nav" aria-label="Main navigation">
            ${links.map(l => `
                <a href="${l.href}" class="side-link${_isActive(l.href, currentPath) ? ' active' : ''}"
                   ${_isActive(l.href, currentPath) ? 'aria-current="page"' : ''}>
                    ${_navIcon(l.icon)}<span class="side-link-label">${l.label}</span>
                </a>`).join('')}
        </nav>`;
    document.body.appendChild(aside);
    document.body.classList.add('has-sidebar');

    // Mobile: a button in the top bar slides the sidebar in.
    const navLeft = document.querySelector('.nav-left');
    if (navLeft && !document.getElementById('sidebar-toggle')) {
        const btn = document.createElement('button');
        btn.className = 'sidebar-toggle';
        btn.id = 'sidebar-toggle';
        btn.type = 'button';
        btn.setAttribute('aria-label', 'Toggle navigation');
        btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.9" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>`;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            aside.classList.toggle('open');
        });
        navLeft.prepend(btn);
        document.addEventListener('click', (e) => {
            if (aside.classList.contains('open') && !aside.contains(e.target)) {
                aside.classList.remove('open');
            }
        });
    }
}
