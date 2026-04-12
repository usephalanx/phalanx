/**
 * Dashboard page module.
 * Shows today's appointments in a timeline/card layout grouped by hour,
 * with summary counts (total, completed, upcoming) at the top.
 */
const DashboardPage = (() => {
    'use strict';

    /**
     * Format a datetime string to a short time like "09:30 AM".
     * @param {string} isoString
     * @returns {string}
     */
    function formatTime(isoString) {
        const d = new Date(isoString);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    /**
     * Return the CSS class for a status badge.
     * @param {string} status
     * @returns {string}
     */
    function badgeClass(status) {
        const map = {
            BOOKED: 'badge-booked',
            CONFIRMED: 'badge-confirmed',
            COMPLETED: 'badge-completed',
            CANCELLED: 'badge-cancelled',
        };
        return map[status] || 'badge-booked';
    }

    /**
     * Render the dashboard page into the given container element.
     * @param {HTMLElement} container
     */
    async function render(container) {
        container.innerHTML = `
            <div class="page-header">
                <h1>Dashboard</h1>
                <p id="dash-date-label">Loading...</p>
            </div>
            <div class="summary-cards" id="dash-summary"></div>
            <div class="timeline" id="dash-timeline">
                <div class="loading-state"><div class="spinner"></div> Loading appointments...</div>
            </div>
        `;

        try {
            const today = new Date().toISOString().slice(0, 10);
            const data = await SalonAPI.getDashboard(today);

            // Also fetch all appointments for today to get status counts
            const apptData = await SalonAPI.listAppointments({ date: today });

            renderSummary(data, apptData);
            renderTimeline(data);
            document.getElementById('dash-date-label').textContent =
                formatDateLabel(data.date);
        } catch (err) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="icon">⚠️</div>
                    <h3>Failed to load dashboard</h3>
                    <p>${err.message}</p>
                </div>`;
        }
    }

    /**
     * Format a date string to a human-readable label.
     * @param {string} dateStr - ISO date string (YYYY-MM-DD)
     * @returns {string}
     */
    function formatDateLabel(dateStr) {
        const d = new Date(dateStr + 'T00:00:00');
        return d.toLocaleDateString(undefined, {
            weekday: 'long',
            year: 'numeric',
            month: 'long',
            day: 'numeric',
        });
    }

    /**
     * Render the summary cards section.
     * @param {object} dashData - Dashboard API response
     * @param {object} apptData - Appointments list response
     */
    function renderSummary(dashData, apptData) {
        const items = apptData.items || [];
        const total = dashData.total_appointments;
        const completed = items.filter(a => a.status === 'COMPLETED').length;
        const upcoming = items.filter(a => a.status === 'BOOKED' || a.status === 'CONFIRMED').length;

        document.getElementById('dash-summary').innerHTML = `
            <div class="summary-card pink">
                <div class="label">Total Bookings</div>
                <div class="value">${total}</div>
            </div>
            <div class="summary-card green">
                <div class="label">Completed</div>
                <div class="value">${completed}</div>
            </div>
            <div class="summary-card gold">
                <div class="label">Upcoming</div>
                <div class="value">${upcoming}</div>
            </div>
        `;
    }

    /**
     * Render the timeline of hour blocks.
     * @param {object} data - Dashboard API response
     */
    function renderTimeline(data) {
        const el = document.getElementById('dash-timeline');
        if (!data.hour_blocks || data.hour_blocks.length === 0) {
            el.innerHTML = `
                <div class="empty-state">
                    <div class="icon">📅</div>
                    <h3>No hours to display</h3>
                    <p>Check back during working hours.</p>
                </div>`;
            return;
        }

        el.innerHTML = data.hour_blocks.map(block => {
            if (block.appointments.length === 0) {
                return `
                    <div class="hour-block">
                        <div class="hour-label">${block.hour}</div>
                        <div class="hour-empty">Available</div>
                    </div>`;
            }

            const cards = block.appointments.map(a => `
                <div class="appt-card">
                    <div class="time-range">${formatTime(a.start_time)} — ${formatTime(a.end_time)}</div>
                    <div class="customer">${escapeHtml(a.customer_name)}</div>
                    <div class="detail">Service #${a.service_id}</div>
                    <div class="detail">Staff #${a.staff_id}</div>
                    <span class="badge ${badgeClass(a.status)}">${a.status}</span>
                </div>
            `).join('');

            return `
                <div class="hour-block">
                    <div class="hour-label">${block.hour}</div>
                    <div class="hour-appointments">${cards}</div>
                </div>`;
        }).join('');
    }

    /**
     * Escape HTML to prevent XSS in user-provided content.
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
