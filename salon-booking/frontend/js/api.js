/**
 * API client module for the Salon Booking frontend.
 * Provides fetch wrappers for all backend REST endpoints.
 */
const SalonAPI = (() => {
    'use strict';

    const BASE = '/api';

    /**
     * Execute a fetch request and return parsed JSON or throw on error.
     * @param {string} url - Request URL
     * @param {RequestInit} [opts] - Fetch options
     * @returns {Promise<any>}
     */
    async function request(url, opts = {}) {
        const defaults = {
            headers: { 'Content-Type': 'application/json' },
        };
        const config = { ...defaults, ...opts };

        const response = await fetch(url, config);

        if (response.status === 204) return null;

        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const body = await response.json();
                detail = body.detail || detail;
            } catch { /* ignore parse errors */ }
            throw new Error(detail);
        }

        return response.json();
    }

    /* ---- Staff ---- */

    /** List staff members, optionally filtered by active status. */
    function listStaff(active = null) {
        const params = new URLSearchParams();
        if (active !== null) params.set('active', active);
        const qs = params.toString();
        return request(`${BASE}/staff${qs ? '?' + qs : ''}`);
    }

    /** Get a single staff member by ID. */
    function getStaff(staffId) {
        return request(`${BASE}/staff/${staffId}`);
    }

    /** Create a new staff member. */
    function createStaff(data) {
        return request(`${BASE}/staff`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Update an existing staff member. */
    function updateStaff(staffId, data) {
        return request(`${BASE}/staff/${staffId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    /** Soft-delete a staff member (set active=false). */
    function deleteStaff(staffId) {
        return request(`${BASE}/staff/${staffId}`, { method: 'DELETE' });
    }

    /* ---- Services ---- */

    /** List services with optional category and active filters. */
    function listServices(category = null, active = null) {
        const params = new URLSearchParams();
        if (category) params.set('category', category);
        if (active !== null) params.set('active', active);
        const qs = params.toString();
        return request(`${BASE}/services${qs ? '?' + qs : ''}`);
    }

    /** Get a single service by ID. */
    function getService(serviceId) {
        return request(`${BASE}/services/${serviceId}`);
    }

    /** Create a new service. */
    function createService(data) {
        return request(`${BASE}/services`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Update an existing service. */
    function updateService(serviceId, data) {
        return request(`${BASE}/services/${serviceId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    /** Soft-delete a service. */
    function deleteService(serviceId) {
        return request(`${BASE}/services/${serviceId}`, { method: 'DELETE' });
    }

    /* ---- Appointments ---- */

    /** List appointments with optional filters. */
    function listAppointments(filters = {}) {
        const params = new URLSearchParams();
        if (filters.staff_id) params.set('staff_id', filters.staff_id);
        if (filters.status) params.set('status', filters.status);
        if (filters.date) params.set('date', filters.date);
        const qs = params.toString();
        return request(`${BASE}/appointments${qs ? '?' + qs : ''}`);
    }

    /** Book a new appointment. */
    function createAppointment(data) {
        return request(`${BASE}/appointments`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Cancel an appointment. */
    function cancelAppointment(appointmentId) {
        return request(`${BASE}/appointments/${appointmentId}/cancel`, {
            method: 'PATCH',
        });
    }

    /* ---- Dashboard ---- */

    /** Get dashboard data for a specific date (default: today). */
    function getDashboard(date = null) {
        const params = new URLSearchParams();
        if (date) params.set('date', date);
        const qs = params.toString();
        return request(`${BASE}/dashboard${qs ? '?' + qs : ''}`);
    }

    /* ---- Public API ---- */
    return {
        listStaff,
        getStaff,
        createStaff,
        updateStaff,
        deleteStaff,
        listServices,
        getService,
        createService,
        updateService,
        deleteService,
        listAppointments,
        createAppointment,
        cancelAppointment,
        getDashboard,
    };
})();
