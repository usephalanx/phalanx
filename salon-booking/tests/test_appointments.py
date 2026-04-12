"""Tests for appointment CRUD endpoints and conflict validation."""

import pytest
from httpx import AsyncClient


async def _create_staff_and_service(client: AsyncClient) -> tuple[int, int]:
    """Helper: create a staff member and service, return their IDs."""
    staff_resp = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    service_resp = await client.post("/api/services", json={
        "name": "Haircut", "duration_minutes": 45,
        "price": 55.00, "category": "hair",
    })
    return staff_resp.json()["id"], service_resp.json()["id"]


def _appt_payload(staff_id: int, service_id: int, start: str = "2026-04-01T10:00:00") -> dict:
    """Helper: build an appointment creation payload."""
    return {
        "customer_name": "John Doe",
        "customer_email": "john@example.com",
        "customer_phone": "555-9999",
        "staff_id": staff_id,
        "service_id": service_id,
        "start_time": start,
        "notes": "First visit",
    }


@pytest.mark.asyncio
async def test_create_appointment(client: AsyncClient) -> None:
    """POST /api/appointments books a new appointment."""
    staff_id, service_id = await _create_staff_and_service(client)
    payload = _appt_payload(staff_id, service_id)

    resp = await client.post("/api/appointments", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["customer_name"] == "John Doe"
    assert data["status"] == "BOOKED"
    assert data["end_time"] == "2026-04-01T10:45:00"


@pytest.mark.asyncio
async def test_create_appointment_conflict(client: AsyncClient) -> None:
    """POST /api/appointments returns 409 on double-booking."""
    staff_id, service_id = await _create_staff_and_service(client)

    # Book first appointment
    await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))

    # Try overlapping appointment (starts during the first)
    resp = await client.post("/api/appointments", json=_appt_payload(
        staff_id, service_id, start="2026-04-01T10:30:00",
    ))
    assert resp.status_code == 409
    assert "conflict" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_appointment_no_conflict_adjacent(client: AsyncClient) -> None:
    """Adjacent appointments (end == start) should not conflict."""
    staff_id, service_id = await _create_staff_and_service(client)

    await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))

    # Starts exactly when first ends (10:45)
    resp = await client.post("/api/appointments", json=_appt_payload(
        staff_id, service_id, start="2026-04-01T10:45:00",
    ))
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_appointment_invalid_staff(client: AsyncClient) -> None:
    """POST /api/appointments with nonexistent staff returns 404."""
    _, service_id = await _create_staff_and_service(client)
    resp = await client.post("/api/appointments", json=_appt_payload(999, service_id))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_appointment_invalid_service(client: AsyncClient) -> None:
    """POST /api/appointments with nonexistent service returns 404."""
    staff_id, _ = await _create_staff_and_service(client)
    resp = await client.post("/api/appointments", json=_appt_payload(staff_id, 999))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_appointments(client: AsyncClient) -> None:
    """GET /api/appointments returns all appointments."""
    staff_id, service_id = await _create_staff_and_service(client)
    await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))

    resp = await client.get("/api/appointments")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_list_appointments_filter_date(client: AsyncClient) -> None:
    """GET /api/appointments?date=... filters by date."""
    staff_id, service_id = await _create_staff_and_service(client)
    await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))

    resp = await client.get("/api/appointments", params={"date": "2026-04-01"})
    assert resp.json()["total"] == 1

    resp2 = await client.get("/api/appointments", params={"date": "2026-04-02"})
    assert resp2.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_appointment(client: AsyncClient) -> None:
    """GET /api/appointments/{id} returns a single appointment."""
    staff_id, service_id = await _create_staff_and_service(client)
    r = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    appt_id = r.json()["id"]

    resp = await client.get(f"/api/appointments/{appt_id}")
    assert resp.status_code == 200
    assert resp.json()["customer_name"] == "John Doe"


@pytest.mark.asyncio
async def test_reschedule_appointment(client: AsyncClient) -> None:
    """PUT /api/appointments/{id} reschedules to a new time."""
    staff_id, service_id = await _create_staff_and_service(client)
    r = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    appt_id = r.json()["id"]

    resp = await client.put(f"/api/appointments/{appt_id}", json={
        "start_time": "2026-04-01T14:00:00",
    })
    assert resp.status_code == 200
    assert resp.json()["start_time"] == "2026-04-01T14:00:00"
    assert resp.json()["end_time"] == "2026-04-01T14:45:00"


@pytest.mark.asyncio
async def test_reschedule_appointment_conflict(client: AsyncClient) -> None:
    """PUT /api/appointments/{id} returns 409 if new time conflicts."""
    staff_id, service_id = await _create_staff_and_service(client)

    # Two appointments at different times
    r1 = await client.post("/api/appointments", json=_appt_payload(
        staff_id, service_id, start="2026-04-01T10:00:00",
    ))
    await client.post("/api/appointments", json=_appt_payload(
        staff_id, service_id, start="2026-04-01T14:00:00",
    ))

    # Try moving first into the second's slot
    appt_id = r1.json()["id"]
    resp = await client.put(f"/api/appointments/{appt_id}", json={
        "start_time": "2026-04-01T14:00:00",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_appointment(client: AsyncClient) -> None:
    """PATCH /api/appointments/{id}/cancel cancels the appointment."""
    staff_id, service_id = await _create_staff_and_service(client)
    r = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    appt_id = r.json()["id"]

    resp = await client.patch(f"/api/appointments/{appt_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_already_cancelled(client: AsyncClient) -> None:
    """PATCH cancel on already-cancelled returns 400."""
    staff_id, service_id = await _create_staff_and_service(client)
    r = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    appt_id = r.json()["id"]

    await client.patch(f"/api/appointments/{appt_id}/cancel")
    resp = await client.patch(f"/api/appointments/{appt_id}/cancel")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cancelled_slot_freed(client: AsyncClient) -> None:
    """After cancelling, the same time slot can be booked again."""
    staff_id, service_id = await _create_staff_and_service(client)
    r = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    appt_id = r.json()["id"]

    await client.patch(f"/api/appointments/{appt_id}/cancel")

    # Book same slot — should succeed since previous was cancelled
    resp = await client.post("/api/appointments", json=_appt_payload(staff_id, service_id))
    assert resp.status_code == 201
