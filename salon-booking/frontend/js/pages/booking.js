/**
 * Booking page module.
 * Multi-step form: select service → select staff → pick date/time → customer info → confirm.
 */
const BookingPage = (() => {
    'use strict';

    /** Booking state across steps. */
    let state = {
        step: 1,
        services: [],
        staff: [],
        selectedService: null,
        selectedStaff: null,
        selectedDate: null,
        selectedSlot: null,
        customer: { name: '', email: '', phone: '' },
        existingAppointments: [],
    };

    const TOTAL_STEPS = 5;
    const STEP_LABELS = ['Service', 'Staff', 'Date & Time', 'Your Info', 'Confirm'];

    /**
     * Render the booking page into the given container.
     * @param {HTMLElement} container
     */
    async function render(container) {
        resetState();
        container.innerHTML = buildShell();
        await loadServices();
        renderStep();
    }

    /** Reset the booking state. */
    function resetState() {
        state = {
            step: 1,
            services: [],
            staff: [],
            selectedService: null,
            selectedStaff: null,
            selectedDate: null,
            selectedSlot: null,
            customer: { name: '', email: '', phone: '' },
            existingAppointments: [],
        };
    }

    /**
     * Build the outer shell HTML with stepper and step panels.
     * @returns {string}
     */
    function buildShell() {
        return `
            <div class="page-header">
                <h1>Book an Appointment</h1>
                <p>Follow the steps below to schedule your visit.</p>
            </div>
            <div class="stepper" id="booking-stepper"></div>
            <div id="booking-step-content"></div>
        `;
    }

    /** Load services from the API. */
    async function loadServices() {
        try {
            const data = await SalonAPI.listServices(null, true);
            state.services = data.items || [];
        } catch {
            state.services = [];
        }
    }

    /** Load active staff from the API. */
    async function loadStaff() {
        try {
            const data = await SalonAPI.listStaff(true);
            state.staff = data.items || [];
        } catch {
            state.staff = [];
        }
    }

    /** Render the current step. */
    function renderStep() {
        renderStepper();
        const content = document.getElementById('booking-step-content');
        if (!content) return;

        switch (state.step) {
            case 1: renderServiceStep(content); break;
            case 2: renderStaffStep(content); break;
            case 3: renderDateTimeStep(content); break;
            case 4: renderCustomerStep(content); break;
            case 5: renderConfirmStep(content); break;
        }
    }

    /** Render the stepper indicator. */
    function renderStepper() {
        const el = document.getElementById('booking-stepper');
        if (!el) return;

        el.innerHTML = STEP_LABELS.map((label, i) => {
            const num = i + 1;
            let cls = '';
            if (num === state.step) cls = 'active';
            else if (num < state.step) cls = 'completed';

            const connector = (i < STEP_LABELS.length - 1)
                ? '<div class="step-connector"></div>'
                : '';

            return `
                <div class="step ${cls}">
                    <span class="step-number">${num}</span>
                    ${label}
                </div>${connector}`;
        }).join('');
    }

    /** Navigate to the next step. */
    function nextStep() {
        if (state.step < TOTAL_STEPS) {
            state.step++;
            renderStep();
        }
    }

    /** Navigate to the previous step. */
    function prevStep() {
        if (state.step > 1) {
            state.step--;
            renderStep();
        }
    }

    /**
     * Step 1: Select a service.
     * @param {HTMLElement} container
     */
    function renderServiceStep(container) {
        if (state.services.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="icon">💇</div>
                    <h3>No services available</h3>
                    <p>Please add services before booking.</p>
                </div>`;
            return;
        }

        container.innerHTML = `
            <h2 style="margin-bottom:1rem;">Choose a Service</h2>
            <div class="select-grid" id="svc-select-grid"></div>
            <div class="form-actions">
                <button class="btn btn-primary" id="svc-next-btn" disabled>Next →</button>
            </div>
        `;

        const grid = document.getElementById('svc-select-grid');
        state.services.forEach(svc => {
            const card = document.createElement('div');
            card.className = 'select-card' + (state.selectedService?.id === svc.id ? ' selected' : '');
            card.dataset.id = svc.id;
            card.innerHTML = `
                <div class="card-title">${escapeHtml(svc.name)}</div>
                <div class="card-meta">${escapeHtml(svc.description || '')} · ${svc.duration_minutes} min</div>
                <div class="card-price">$${svc.price.toFixed(2)}</div>
            `;
            card.addEventListener('click', () => selectService(svc));
            grid.appendChild(card);
        });

        updateNextButton('svc-next-btn', !!state.selectedService);
        document.getElementById('svc-next-btn').addEventListener('click', async () => {
            await loadStaff();
            nextStep();
        });
    }

    /**
     * Select a service and highlight its card.
     * @param {object} svc
     */
    function selectService(svc) {
        state.selectedService = svc;
        document.querySelectorAll('#svc-select-grid .select-card').forEach(c => {
            c.classList.toggle('selected', parseInt(c.dataset.id) === svc.id);
        });
        updateNextButton('svc-next-btn', true);
    }

    /**
     * Step 2: Select a staff member.
     * @param {HTMLElement} container
     */
    function renderStaffStep(container) {
        const staffList = state.staff;

        if (staffList.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="icon">👤</div>
                    <h3>No staff available</h3>
                    <p>Please add active staff members.</p>
                </div>
                <div class="form-actions">
                    <button class="btn btn-secondary" id="staff-back-btn">← Back</button>
                </div>`;
            document.getElementById('staff-back-btn').addEventListener('click', prevStep);
            return;
        }

        container.innerHTML = `
            <h2 style="margin-bottom:1rem;">Choose a Stylist</h2>
            <div class="select-grid" id="staff-select-grid"></div>
            <div class="form-actions">
                <button class="btn btn-secondary" id="staff-back-btn">← Back</button>
                <button class="btn btn-primary" id="staff-next-btn" disabled>Next →</button>
            </div>
        `;

        const grid = document.getElementById('staff-select-grid');
        staffList.forEach(s => {
            const card = document.createElement('div');
            card.className = 'select-card' + (state.selectedStaff?.id === s.id ? ' selected' : '');
            card.dataset.id = s.id;
            const specialties = (s.specialties || []).join(', ') || 'General';
            card.innerHTML = `
                <div class="card-title">${escapeHtml(s.name)}</div>
                <div class="card-meta">${escapeHtml(s.role)}</div>
                <div class="card-meta" style="font-size:0.8rem;margin-top:0.25rem;">${escapeHtml(specialties)}</div>
            `;
            card.addEventListener('click', () => selectStaff(s));
            grid.appendChild(card);
        });

        updateNextButton('staff-next-btn', !!state.selectedStaff);
        document.getElementById('staff-back-btn').addEventListener('click', prevStep);
        document.getElementById('staff-next-btn').addEventListener('click', () => nextStep());
    }

    /**
     * Select a staff member and highlight its card.
     * @param {object} s
     */
    function selectStaff(s) {
        state.selectedStaff = s;
        document.querySelectorAll('#staff-select-grid .select-card').forEach(c => {
            c.classList.toggle('selected', parseInt(c.dataset.id) === s.id);
        });
        updateNextButton('staff-next-btn', true);
    }

    /**
     * Step 3: Pick date and available time slot.
     * @param {HTMLElement} container
     */
    function renderDateTimeStep(container) {
        const todayStr = new Date().toISOString().slice(0, 10);
        const selectedDate = state.selectedDate || todayStr;

        container.innerHTML = `
            <h2 style="margin-bottom:1rem;">Pick a Date &amp; Time</h2>
            <div class="form-group">
                <label for="booking-date">Date</label>
                <input type="date" id="booking-date" class="form-control"
                       value="${selectedDate}" min="${todayStr}">
            </div>
            <div id="slots-container">
                <div class="loading-state"><div class="spinner"></div> Loading available slots...</div>
            </div>
            <div class="form-actions">
                <button class="btn btn-secondary" id="dt-back-btn">← Back</button>
                <button class="btn btn-primary" id="dt-next-btn" disabled>Next →</button>
            </div>
        `;

        document.getElementById('dt-back-btn').addEventListener('click', prevStep);
        document.getElementById('dt-next-btn').addEventListener('click', () => nextStep());

        const dateInput = document.getElementById('booking-date');
        dateInput.addEventListener('change', () => {
            state.selectedDate = dateInput.value;
            state.selectedSlot = null;
            loadSlots(dateInput.value);
        });

        state.selectedDate = selectedDate;
        loadSlots(selectedDate);
    }

    /**
     * Load and render available time slots for the given date.
     * @param {string} dateStr - YYYY-MM-DD
     */
    async function loadSlots(dateStr) {
        const slotsContainer = document.getElementById('slots-container');
        if (!slotsContainer) return;

        slotsContainer.innerHTML = '<div class="loading-state"><div class="spinner"></div> Loading...</div>';

        try {
            // Fetch existing appointments for this staff on this date
            const apptData = await SalonAPI.listAppointments({
                staff_id: state.selectedStaff.id,
                date: dateStr,
            });
            state.existingAppointments = apptData.items || [];
        } catch {
            state.existingAppointments = [];
        }

        const slots = generateSlots(dateStr);

        if (slots.length === 0) {
            slotsContainer.innerHTML = `
                <div class="empty-state">
                    <div class="icon">⏰</div>
                    <h3>No available slots</h3>
                    <p>Try a different date or staff member.</p>
                </div>`;
            return;
        }

        slotsContainer.innerHTML = `
            <label style="font-size:0.85rem;font-weight:600;color:#52525b;">Available Times</label>
            <div class="slots-grid" id="slots-grid"></div>
        `;

        const grid = document.getElementById('slots-grid');
        slots.forEach(slot => {
            const btn = document.createElement('button');
            btn.className = 'slot-btn' + (slot.available ? '' : ' disabled');
            if (state.selectedSlot === slot.time) btn.classList.add('selected');
            btn.textContent = slot.label;
            btn.disabled = !slot.available;

            if (slot.available) {
                btn.addEventListener('click', () => selectSlot(slot.time));
            }
            grid.appendChild(btn);
        });
    }

    /**
     * Generate time slots from 09:00 to 17:00 in 30-minute increments,
     * marking unavailable ones that conflict with existing appointments.
     * @param {string} dateStr - YYYY-MM-DD
     * @returns {Array<{time: string, label: string, available: boolean}>}
     */
    function generateSlots(dateStr) {
        const duration = state.selectedService?.duration_minutes || 30;
        const slots = [];

        for (let h = 9; h < 17; h++) {
            for (let m = 0; m < 60; m += 30) {
                const slotStart = new Date(`${dateStr}T${pad(h)}:${pad(m)}:00`);
                const slotEnd = new Date(slotStart.getTime() + duration * 60000);

                // Don't offer slots that would extend past 17:00
                const endOfDay = new Date(`${dateStr}T17:00:00`);
                if (slotEnd > endOfDay) continue;

                // Don't offer slots in the past
                if (slotStart < new Date()) {
                    slots.push({
                        time: `${pad(h)}:${pad(m)}`,
                        label: formatSlotLabel(h, m),
                        available: false,
                    });
                    continue;
                }

                const available = !hasConflict(slotStart, slotEnd);
                slots.push({
                    time: `${pad(h)}:${pad(m)}`,
                    label: formatSlotLabel(h, m),
                    available,
                });
            }
        }
        return slots;
    }

    /**
     * Check if a proposed slot conflicts with any existing appointment.
     * @param {Date} start
     * @param {Date} end
     * @returns {boolean}
     */
    function hasConflict(start, end) {
        return state.existingAppointments.some(a => {
            if (a.status === 'CANCELLED') return false;
            const aStart = new Date(a.start_time);
            const aEnd = new Date(a.end_time);
            return start < aEnd && aStart < end;
        });
    }

    /**
     * Select a time slot.
     * @param {string} time - e.g. "09:30"
     */
    function selectSlot(time) {
        state.selectedSlot = time;
        document.querySelectorAll('#slots-grid .slot-btn').forEach(btn => {
            btn.classList.toggle('selected', btn.textContent === formatSlotLabelFromTime(time));
        });
        updateNextButton('dt-next-btn', true);
    }

    /**
     * Step 4: Customer information.
     * @param {HTMLElement} container
     */
    function renderCustomerStep(container) {
        container.innerHTML = `
            <h2 style="margin-bottom:1rem;">Your Information</h2>
            <div class="form-group">
                <label for="cust-name">Full Name *</label>
                <input type="text" id="cust-name" class="form-control" placeholder="Jane Doe"
                       value="${escapeHtml(state.customer.name)}" required>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label for="cust-email">Email *</label>
                    <input type="email" id="cust-email" class="form-control" placeholder="jane@example.com"
                           value="${escapeHtml(state.customer.email)}" required>
                </div>
                <div class="form-group">
                    <label for="cust-phone">Phone</label>
                    <input type="tel" id="cust-phone" class="form-control" placeholder="555-0100"
                           value="${escapeHtml(state.customer.phone)}">
                </div>
            </div>
            <div class="form-actions">
                <button class="btn btn-secondary" id="cust-back-btn">← Back</button>
                <button class="btn btn-primary" id="cust-next-btn">Review →</button>
            </div>
        `;

        document.getElementById('cust-back-btn').addEventListener('click', prevStep);
        document.getElementById('cust-next-btn').addEventListener('click', () => {
            const name = document.getElementById('cust-name').value.trim();
            const email = document.getElementById('cust-email').value.trim();
            const phone = document.getElementById('cust-phone').value.trim();

            if (!name || !email) {
                SalonApp.toast('Please fill in name and email.', 'error');
                return;
            }

            state.customer = { name, email, phone };
            nextStep();
        });
    }

    /**
     * Step 5: Confirmation review and submit.
     * @param {HTMLElement} container
     */
    function renderConfirmStep(container) {
        const svc = state.selectedService;
        const staff = state.selectedStaff;

        container.innerHTML = `
            <h2 style="margin-bottom:1rem;">Confirm Your Booking</h2>
            <div class="confirmation-card" style="text-align:left;max-width:100%;">
                <div class="details">
                    <div class="row"><span class="lbl">Service</span><span>${escapeHtml(svc.name)}</span></div>
                    <div class="row"><span class="lbl">Duration</span><span>${svc.duration_minutes} min</span></div>
                    <div class="row"><span class="lbl">Price</span><span>$${svc.price.toFixed(2)}</span></div>
                    <div class="row"><span class="lbl">Stylist</span><span>${escapeHtml(staff.name)}</span></div>
                    <div class="row"><span class="lbl">Date</span><span>${state.selectedDate}</span></div>
                    <div class="row"><span class="lbl">Time</span><span>${state.selectedSlot}</span></div>
                    <div class="row"><span class="lbl">Name</span><span>${escapeHtml(state.customer.name)}</span></div>
                    <div class="row"><span class="lbl">Email</span><span>${escapeHtml(state.customer.email)}</span></div>
                    ${state.customer.phone ? `<div class="row"><span class="lbl">Phone</span><span>${escapeHtml(state.customer.phone)}</span></div>` : ''}
                </div>
            </div>
            <div class="form-actions" style="margin-top:1.5rem;">
                <button class="btn btn-secondary" id="confirm-back-btn">← Back</button>
                <button class="btn btn-gold" id="confirm-submit-btn">✓ Book Appointment</button>
            </div>
        `;

        document.getElementById('confirm-back-btn').addEventListener('click', prevStep);
        document.getElementById('confirm-submit-btn').addEventListener('click', submitBooking);
    }

    /** Submit the booking to the API. */
    async function submitBooking() {
        const btn = document.getElementById('confirm-submit-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Booking...';
        }

        const startTime = `${state.selectedDate}T${state.selectedSlot}:00`;

        try {
            const result = await SalonAPI.createAppointment({
                customer_name: state.customer.name,
                customer_email: state.customer.email,
                customer_phone: state.customer.phone || null,
                staff_id: state.selectedStaff.id,
                service_id: state.selectedService.id,
                start_time: startTime,
            });

            renderSuccess(result);
        } catch (err) {
            SalonApp.toast(`Booking failed: ${err.message}`, 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '✓ Book Appointment';
            }
        }
    }

    /**
     * Render the success confirmation after a successful booking.
     * @param {object} appointment - The created appointment object
     */
    function renderSuccess(appointment) {
        const container = document.getElementById('booking-step-content');
        if (!container) return;

        // Hide stepper
        const stepper = document.getElementById('booking-stepper');
        if (stepper) stepper.style.display = 'none';

        container.innerHTML = `
            <div class="confirmation-card">
                <div class="check-icon">✓</div>
                <h2>Booking Confirmed!</h2>
                <p style="color:#71717a;">Your appointment has been scheduled successfully.</p>
                <div class="details">
                    <div class="row"><span class="lbl">Confirmation #</span><span>${appointment.id}</span></div>
                    <div class="row"><span class="lbl">Service</span><span>${escapeHtml(state.selectedService.name)}</span></div>
                    <div class="row"><span class="lbl">Stylist</span><span>${escapeHtml(state.selectedStaff.name)}</span></div>
                    <div class="row"><span class="lbl">Date</span><span>${state.selectedDate}</span></div>
                    <div class="row"><span class="lbl">Time</span><span>${state.selectedSlot}</span></div>
                    <div class="row"><span class="lbl">Status</span><span class="badge badge-booked">${appointment.status}</span></div>
                </div>
                <button class="btn btn-primary" id="book-another-btn" style="margin-top:1.5rem;">Book Another</button>
            </div>
        `;

        document.getElementById('book-another-btn').addEventListener('click', () => {
            if (stepper) stepper.style.display = 'flex';
            render(document.getElementById('app'));
        });

        SalonApp.toast('Appointment booked successfully!', 'success');
    }

    /* ---- Utilities ---- */

    function pad(n) { return n.toString().padStart(2, '0'); }

    function formatSlotLabel(h, m) {
        const suffix = h >= 12 ? 'PM' : 'AM';
        const hour12 = h > 12 ? h - 12 : (h === 0 ? 12 : h);
        return `${hour12}:${pad(m)} ${suffix}`;
    }

    function formatSlotLabelFromTime(time) {
        const [hStr, mStr] = time.split(':');
        return formatSlotLabel(parseInt(hStr), parseInt(mStr));
    }

    function updateNextButton(id, enabled) {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = !enabled;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    return { render };
})();
