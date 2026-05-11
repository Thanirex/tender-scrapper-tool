// TAiQ auth utilities — included on every page
const TOKEN_KEY = 'taiiq_token';

function getToken() {
    return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
}

function getUser() {
    const token = getToken();
    if (!token) return null;
    try {
        const payload = token.split('.')[1];
        const decoded = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
        // Check expiry
        if (decoded.exp && decoded.exp * 1000 < Date.now()) {
            clearToken();
            return null;
        }
        return decoded;
    } catch {
        clearToken();
        return null;
    }
}

function authHeaders() {
    const token = getToken();
    return token ? { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };
}

async function authFetch(url, options = {}) {
    const headers = { ...authHeaders(), ...(options.headers || {}) };
    const res = await fetch(url, { ...options, headers });
    if (res.status === 401) {
        clearToken();
        window.location.href = '/login';
        return null;
    }
    return res;
}

function logout() {
    clearToken();
    window.location.href = '/login';
}

// Role hierarchy for comparison
const ROLE_RANK = { user: 0, admin: 1, superadmin: 2 };

function hasRole(minRole) {
    const user = getUser();
    if (!user) return false;
    return (ROLE_RANK[user.role] || 0) >= (ROLE_RANK[minRole] || 0);
}
