/**
 * Services page module.
 * Card grid of services with add/edit modal and active toggle.
 */
const ServicesPage = (() => {
    'use strict';

    /** Current services list. */
    let servicesList = [];

    /**
     * Render the services management page.
     * @param {HTMLElement} container
     */
    async function render(container) {
        container.innerHTML = `
            <div class="page-header">
                <h1>Service Catalog</h1>
                <p>Manage the services your salon offers.</p>
            </div>
            <div class="table-toolbar">
                <div></div>
                <button class="btn btn-primary" id="add-service-btn">+ Add Service</button>
            </div>
            <div id="services-grid-container">
                <div class="loading-state"><div class="spinner"></div> Loading services...</div>
            </div>
        `;

        document.getElementById('add-service-btn').addEventListener('click', () => openServiceModal());
        await loadServices();
    }

    /** Fetch services and render the grid. */
    async function loadServices() {
        try {
            const data = await SalonAPI.listServices();
            servicesList = data.items || [];
            renderGrid();
        } catch (err) {
            document.getElementById('services-grid-container').innerHTML = `
                <div class="empty-state">
                    <div class="icon">⚠️</div>
                    <h3>Failed to load services</h3>
                    <p>${escapeHtml(err.message)}</p>
                </div>`;
        }
    }

    /** Render the service card grid. */
    function renderGrid() {
        const container = document.getElementById('services-grid-container');
        if (servicesList.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="icon">💅</div>
                    <h3>No services yet</h3>
                    <p>Click "Add Service" to create your first service.</p>
                </div>`;
            return;
        }

        container.innerHTML = '<div class="service-grid" id="service-grid"></div>';
        const grid = document.getElementById('service-grid');

        servicesList.forEach(svc => {
            const card = document.createElement('div');
            card.className = 'service-card';
            if (!svc.active) card.style.opacity = '0.6';

            card.innerHTML = `
                <div class="svc-actions">
                    <button class="btn-icon edit-svc-btn" data-id="${svc.id}" title="Edit">✏️</button>
                </div>
                <span class="svc-category">${escapeHtml(svc.category)}</span>
                <h3>${escapeHtml(svc.name)}</h3>
                <p class="svc-desc">${escapeHtml(svc.description || 'No description provided.')}</p>
                <div class="svc-footer">
                    <span class="svc-price">$${svc.price.toFixed(2)}</span>
                    <span class="svc-duration">${svc.duration_minutes} min</span>
                </div>
                <div class="svc-status" style="margin-top:0.75rem;display:flex;align-items:center;gap:0.5rem;">
                    <label class="toggle" title="${svc.active ? 'Deactivate' : 'Activate'}">
                        <input type="checkbox" class="toggle-svc-cb" data-id="${svc.id}" ${svc.active ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                    <span style="font-size:0.8rem;color:#71717a;">${svc.active ? 'Active' : 'Inactive'}</span>
                </div>
            `;
            grid.appendChild(card);
        });

        // Bind edit buttons
        grid.querySelectorAll('.edit-svc-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const svc = servicesList.find(s => s.id === parseInt(btn.dataset.id));
                if (svc) openServiceModal(svc);
            });
        });

        // Bind toggle switches
        grid.querySelectorAll('.toggle-svc-cb').forEach(cb => {
            cb.addEventListener('change', async () => {
                const id = parseInt(cb.dataset.id);
                try {
                    await SalonAPI.updateService(id, { active: cb.checked });
                    SalonApp.toast(`Service ${cb.checked ? 'activated' : 'deactivated'}.`, 'success');
                    await loadServices();
                } catch (err) {
                    SalonApp.toast(`Failed: ${err.message}`, 'error');
                    cb.checked = !cb.checked;
                }
            });
        });
    }

    /**
     * Open the add/edit service modal.
     * @param {object|null} svc - Service object for editing, null for adding
     */
    function openServiceModal(svc = null) {
        const isEdit = !!svc;
        const title = isEdit ? 'Edit Service' : 'Add Service';

        const body = `
            <form id="service-form">
                <div class="form-group">
                    <label for="svc-name">Name *</label>
                    <input type="text" id="svc-name" class="form-control" required
                           value="${isEdit ? escapeHtml(svc.name) : ''}" placeholder="e.g. Haircut">
                </div>
                <div class="form-group">
                    <label for="svc-desc">Description</label>
                    <textarea id="svc-desc" class="form-control" rows="3"
                              placeholder="Describe the service...">${isEdit ? escapeHtml(svc.description || '') : ''}</textarea>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label for="svc-duration">Duration (minutes) *</label>
                        <input type="number" id="svc-duration" class="form-control" min="5" step="5" required
                               value="${isEdit ? svc.duration_minutes : '30'}" placeholder="30">
                    </div>
                    <div class="form-group">
                        <label for="svc-price">Price ($) *</label>
                        <input type="number" id="svc-price" class="form-control" min="0" step="0.01" required
                               value="${isEdit ? svc.price : ''}" placeholder="0.00">
                    </div>
                </div>
                <div class="form-group">
                    <label for="svc-category">Category *</label>
                    <select id="svc-category" class="form-control">
                        <option value="hair" ${isEdit && svc.category === 'hair' ? 'selected' : ''}>Hair</option>
                        <option value="nails" ${isEdit && svc.category === 'nails' ? 'selected' : ''}>Nails</option>
                        <option value="grooming" ${isEdit && svc.category === 'grooming' ? 'selected' : ''}>Grooming</option>
                        <option value="spa" ${isEdit && svc.category === 'spa' ? 'selected' : ''}>Spa</option>
                        <option value="other" ${isEdit && svc.category === 'other' ? 'selected' : ''}>Other</option>
                    </select>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" id="svc-cancel-btn">Cancel</button>
                    <button type="submit" class="btn btn-primary">${isEdit ? 'Save Changes' : 'Add Service'}</button>
                </div>
            </form>
        `;

        SalonApp.openModal(title, body);

        document.getElementById('svc-cancel-btn').addEventListener('click', () => SalonApp.closeModal());

        document.getElementById('service-form').addEventListener('submit', async (e) => {
            e.preventDefault();

            const name = document.getElementById('svc-name').value.trim();
            const description = document.getElementById('svc-desc').value.trim() || null;
            const duration_minutes = parseInt(document.getElementById('svc-duration').value);
            const price = parseFloat(document.getElementById('svc-price').value);
            const category = document.getElementById('svc-category').value;

            if (!name || !duration_minutes || isNaN(price)) {
                SalonApp.toast('Please fill in all required fields.', 'error');
                return;
            }

            try {
                if (isEdit) {
                    await SalonAPI.updateService(svc.id, { name, description, duration_minutes, price, category });
                    SalonApp.toast('Service updated.', 'success');
                } else {
                    await SalonAPI.createService({ name, description, duration_minutes, price, category });
                    SalonApp.toast('Service added.', 'success');
                }
                SalonApp.closeModal();
                await loadServices();
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
