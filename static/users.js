document.addEventListener('DOMContentLoaded', () => {
    initNav({ requireRole: 'admin' });

    const tableWrap = document.getElementById('users-table-wrap');
    const overlay   = document.getElementById('modal-overlay');
    const modalTitle = document.getElementById('modal-title');
    const modalSave  = document.getElementById('modal-save');
    const modalError = document.getElementById('modal-error');
    const roleWrap   = document.getElementById('m-role-wrap');
    const pwWrap     = document.getElementById('m-password-wrap');
    const roleSelect = document.getElementById('m-role');

    let editingId = null;
    const currentUser = getUser();

    // Superadmins can create admins; restrict role dropdown for plain admins
    if (currentUser && currentUser.role !== 'superadmin') {
        roleWrap.style.display = 'none';
    }

    document.getElementById('create-user-btn').addEventListener('click', () => openModal());
    document.getElementById('modal-close').addEventListener('click',  closeModal);
    document.getElementById('modal-cancel').addEventListener('click', closeModal);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
    document.getElementById('modal-save').addEventListener('click', saveUser);

    loadUsers();

    async function loadUsers() {
        tableWrap.innerHTML = '<p class="empty-msg">Loading…</p>';
        const res = await authFetch('/admin/users');
        if (!res) return;
        const users = await res.json();

        if (!users || users.length === 0) {
            tableWrap.innerHTML = '<p class="empty-msg">No users yet. Create one to get started.</p>';
            return;
        }

        tableWrap.innerHTML = `
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Username</th>
                        <th>Email</th>
                        <th>Role</th>
                        <th>Created</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${users.map(u => _userRow(u)).join('')}
                </tbody>
            </table>
        `;

        tableWrap.querySelectorAll('.edit-btn').forEach(btn => {
            btn.addEventListener('click', () => openModal(btn.dataset));
        });
        tableWrap.querySelectorAll('.deactivate-btn').forEach(btn => {
            btn.addEventListener('click', () => toggleActive(parseInt(btn.dataset.id), false));
        });
        tableWrap.querySelectorAll('.activate-btn').forEach(btn => {
            btn.addEventListener('click', () => toggleActive(parseInt(btn.dataset.id), true));
        });
    }

    function _userRow(u) {
        const isSelf = u.id === parseInt(currentUser.sub);
        const canEdit = !isSelf || currentUser.role === 'superadmin';
        const activeLabel = u.is_active
            ? '<span class="status-chip active">Active</span>'
            : '<span class="status-chip inactive">Inactive</span>';
        const actions = isSelf ? '<span class="text-muted">You</span>' : `
            <button class="action-btn edit-btn"
                data-id="${u.id}" data-username="${u.username}"
                data-email="${u.email}" data-role="${u.role}">Edit</button>
            ${u.is_active
                ? `<button class="action-btn danger-btn deactivate-btn" data-id="${u.id}">Deactivate</button>`
                : `<button class="action-btn activate-btn" data-id="${u.id}">Activate</button>`
            }
        `;
        const roleLabels = { superadmin: 'Super Admin', admin: 'Admin', user: 'User' };
        return `
            <tr>
                <td><strong>${u.username}</strong></td>
                <td>${u.email}</td>
                <td><span class="role-badge role-${u.role}">${roleLabels[u.role] || u.role}</span></td>
                <td class="time-cell">${u.created_at ? u.created_at.split('T')[0] : '—'}</td>
                <td>${activeLabel}</td>
                <td class="actions-cell">${actions}</td>
            </tr>
        `;
    }

    function openModal(data = null) {
        editingId = data ? parseInt(data.id) : null;
        modalTitle.textContent = editingId ? 'Edit User' : 'Create User';
        modalSave.textContent  = editingId ? 'Save' : 'Create';
        modalError.classList.add('hidden');
        modalError.textContent = '';

        document.getElementById('m-username').value = data?.username || '';
        document.getElementById('m-email').value    = data?.email    || '';
        document.getElementById('m-password').value = '';
        if (roleSelect) roleSelect.value = data?.role || 'user';

        // When editing, password is optional
        const pwLabel = pwWrap.querySelector('label');
        if (pwLabel) pwLabel.textContent = editingId ? 'New Password (leave blank to keep)' : 'Password';

        overlay.classList.remove('hidden');
        document.getElementById('m-username').focus();
    }

    function closeModal() {
        overlay.classList.add('hidden');
        editingId = null;
    }

    async function saveUser() {
        modalError.classList.add('hidden');
        const username = document.getElementById('m-username').value.trim();
        const email    = document.getElementById('m-email').value.trim();
        const password = document.getElementById('m-password').value;
        const role     = roleSelect?.value || 'user';

        if (!username || !email) {
            modalError.textContent = 'Username and email are required.';
            modalError.classList.remove('hidden');
            return;
        }
        if (!editingId && !password) {
            modalError.textContent = 'Password is required for new users.';
            modalError.classList.remove('hidden');
            return;
        }

        modalSave.disabled = true;

        let res;
        if (editingId) {
            const body = { username, email };
            if (password) body.password = password; // not a standard UpdateUserRequest field — fine to ignore server-side
            res = await authFetch(`/admin/users/${editingId}`, {
                method: 'PUT',
                body: JSON.stringify({ username, email }),
            });
        } else {
            res = await authFetch('/admin/users', {
                method: 'POST',
                body: JSON.stringify({ username, email, password, role }),
            });
        }

        modalSave.disabled = false;
        if (!res) return;

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            modalError.textContent = err.detail || 'An error occurred.';
            modalError.classList.remove('hidden');
            return;
        }

        closeModal();
        loadUsers();
    }

    async function toggleActive(id, active) {
        const res = await authFetch(`/admin/users/${id}`, {
            method: 'PUT',
            body: JSON.stringify({ is_active: active }),
        });
        if (res && res.ok) loadUsers();
    }
});
