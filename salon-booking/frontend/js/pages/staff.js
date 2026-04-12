/**
 * Staff Management page module.
 * Table listing all staff with add/edit modal and active toggle.
 */
const StaffPage = (() => {
    'use strict';

    /** Current staff list. */
    let staffList = [];

    /**
     * Render the staff management page.
     * @param {HTMLElement} container
     */
    async function render(container) {
        container.innerHTML = `
            <div class="page-header">
                <h1>Staff Management</h1>
                <p>Manage your salon team members.</p>
            </div>
            <div class="table-toolbar">
                <div></div>
                <button class="btn btn-primary" id="add-staff-btn">+ Add Staff</button>
            </div>
            <div id="staff-table-container">
                <div class="loading-state"><div class="spinner"></div> Loading staff...</div>
            </div>
        `;

        document.getElementById('add-staff-btn').addEventListener('click', () => openStaffModal());
        await loadStaff();
    }

    /** Fetch staff from API and render the table. */
    async function loadStaff() {
        try {
            const data = await SalonAPI.listStaff();
            staffList = data.items || [];
            renderTable();
        } catch (err) {
            document.getElementById('staff-table-container').innerHTML = `
                <div class="empty-state">
                    <div class="icon">⚠️</div>
                    <h3>Failed to load staff</h3>
                    <p>${escapeHtml(err.message)}</p>
                </div>`;
        }
    }

    /** Render the staff data table. */
    function renderTable() {
        const container = document.getElementById('staff-table-container');
        if (staffList.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="icon">👥</div>
                    <h3>No staff members yet</h3>
                    <p>Click "Add Staff" to get started.</p>
                </div>`;
            return;
        }

        container.innerHTML = `
            <div class="data-table-wrapper">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Role</th>
                            <th>Email</th>
                            <th>Specialties</th>
                            <th>Status</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="staff-tbody"></tbody>
                </table>
            </div>
        `;

        const tbody = document.getElementById('staff-tbody');
        staffList.forEach(s => {
            const tr = document.createElement('tr');
            const specialties = (s.specialties || []).join(', ') || '—';
            const statusBadge = s.active
                ? '<span class="badge badge-active">Active</span>'
                : '<span class="badge badge-inactive">Inactive</span>';

            tr.innerHTML = `
                <td><strong>${escapeHtml(s.name)}</strong></td>
                <td>${escapeHtml(s.role)}</td>
                <td>${escapeHtml(s.email)}</td>
                <td style="max-width:200px;">${escapeHtml(specialties)}</td>
                <td>${statusBadge}</td>
                <td>
                    <button class="btn btn-sm btn-secondary edit-staff-btn" data-id="${s.id}">Edit</button>
                    <label class="toggle" title="${s.active ? 'Deactivate' : 'Activate'}">
                        <input type="checkbox" class="toggle-active-cb" data-id="${s.id}" ${s.active ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </td>
            `;
            tbody.appendChild(tr);
        });

        // Bind edit buttons
        tbody.querySelectorAll('.edit-staff-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const staff = staffList.find(s => s.id === parseInt(btn.dataset.id));
                if (staff) openStaffModal(staff);
            });
        });

        // Bind toggle switches
        tbody.querySelectorAll('.toggle-active-cb').forEach(cb => {
            cb.addEventListener('change', async () => {
                const id = parseInt(cb.dataset.id);
                try {
                    await SalonAPI.updateStaff(id, { active: cb.checked });
                    SalonApp.toast(`Staff member ${cb.checked ? 'activated' : 'deactivated'}.`, 'success');
                    await loadStaff();
                } catch (err) {
                    SalonApp.toast(`Failed: ${err.message}`, 'error');
                    cb.checked = !cb.checked;
                }
            });
        });
    }

    /**
     * Open the add/edit staff modal.
     * @param {object|null} staff - Staff object for editing, null for adding
     */
    function openStaffModal(staff = null) {
        const isEdit = !!staff;
        const title = isEdit ? 'Edit Staff Member' : 'Add Staff Member';

        const body = `
            <form id="staff-form">
                <div class="form-group">
                    <label for="sf-name">Name *</label>
                    <input type="text" id="sf-name" class="form-control" required
                           value="${isEdit ? escapeHtml(staff.name) : ''}" placeholder="Full name">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label for="sf-email">Email *</label>
                        <input type="email" id="sf-email" class="form-control" required
                               value="${isEdit ? escapeHtml(staff.email) : ''}" placeholder="name@salon.com">
                    </div>
                    <div class="form-group">
                        <label for="sf-phone">Phone</label>
                        <input type="tel" id="sf-phone" class="form-control"
                               value="${isEdit ? escapeHtml(staff.phone || '') : ''}" placeholder="555-0100">
                    </div>
                </div>
                <div class="form-group">
                    <label for="sf-role">Role *</label>
                    <select id="sf-role" class="form-control">
                        <option value="stylist" ${isEdit && staff.role === 'stylist' ? 'selected' : ''}>Stylist</option>
                        <option value="senior stylist" ${isEdit && staff.role === 'senior stylist' ? 'selected' : ''}>Senior Stylist</option>
                        <option value="colorist" ${isEdit && staff.role === 'colorist' ? 'selected' : ''}>Colorist</option>
                        <option value="nail technician" ${isEdit && staff.role === 'nail technician' ? 'selected' : ''}>Nail Technician</option>
                        <option value="manager" ${isEdit && staff.role === 'manager' ? 'selected' : ''}>Manager</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="sf-specialties">Specialties <small>(comma-separated)</small></label>
                    <input type="text" id="sf-specialties" class="form-control"
                           value="${isEdit ? (staff.specialties || []).join(', ') : ''}"
                           placeholder="haircut, coloring, highlights">
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" id="sf-cancel-btn">Cancel</button>
                    <button type="submit" class="btn btn-primary">${isEdit ? 'Save Changes' : 'Add Staff'}</button>
                </div>
            </form>
        `;

        SalonApp.openModal(title, body);

        document.getElementById('sf-cancel-btn').addEventListener('click', () => SalonApp.closeModal());

        document.getElementById('staff-form').addEventListener('submit', async (e) => {
            e.preventDefault();

            const name = document.getElementById('sf-name').value.trim();
            const email = document.getElementById('sf-email').value.trim();
            const phone = document.getElementById('sf-phone').value.trim() || null;
            const role = document.getElementById('sf-role').value;
            const specialtiesRaw = document.getElementById('sf-specialties').value.trim();
            const specialties = specialtiesRaw
                ? specialtiesRaw.split(',').map(s => s.trim()).filter(Boolean)
                : null;

            if (!name || !email) {
                SalonApp.toast('Name and email are required.', 'error');
                return;
            }

            try {
                if (isEdit) {
                    await SalonAPI.updateStaff(staff.id, { name, email, phone, role, specialties });
                    SalonApp.toast('Staff member updated.', 'success');
                } else {
                    await SalonAPI.createStaff({ name, email, phone, role, specialties });
                    SalonApp.toast('Staff member added.', 'success');
                }
                SalonApp.closeModal();
                await loadStaff();
            } catch (err) {
                SalonApp.toast(`Error: ${err.message}`, 'error');
            }
        });
    }

    /**
     * Escape HTML to prevent XSS.
     * @param {string} str
     * @returns {string}
     */
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    return { render };
})();
