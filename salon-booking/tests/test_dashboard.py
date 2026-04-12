"""Tests for the dashboard endpoint."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_dashboard_empty(client: AsyncClient) -> None:
    """GET /api/dashboard with no appointments returns empty hour blocks."""
    resp = await client.get("/api/dashboard", params={"date": "2026-04-01"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_appointments"] == 0
    assert data["date"] == "2026-04-01"
    assert len(data["hour_blocks"]) == 12  # 08:00 – 19:00


@pytest.mark.asyncio
async def test_dashboard_with_appointments(client: AsyncClient) -> None:
    """GET /api/dashboard groups appointments by hour."""
    # Create staff and service
    staff_resp = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    service_resp = await client.post("/api/services", json={
        "name": "Haircut", "duration_minutes": 45,
        "price": 55.00, "category": "hair",
    })
    staff_id = staff_resp.json()["id"]
    service_id = service_resp.json()["id"]

    # Book two appointments in different hours
    await client.post("/api/appointments", json={
        "customer_name": "John", "customer_email": "john@example.com",
        "staff_id": staff_id, "service_id": service_id,
        "start_time": "2026-04-01T10:00:00",
    })
    await client.post("/api/appointments", json={
        "customer_name": "Jane", "customer_email": "jane@example.com",
        "staff_id": staff_id, "service_id": service_id,
        "start_time": "2026-04-01T14:00:00",
    })

    resp = await client.get("/api/dashboard", params={"date": "2026-04-01"})
    data = resp.json()
    assert data["total_appointments"] == 2

    # Find the 10:00 and 14:00 blocks
    blocks_with_appts = [b for b in data["hour_blocks"] if b["appointments"]]
    assert len(blocks_with_appts) == 2


@pytest.mark.asyncio
async def test_dashboard_excludes_cancelled(client: AsyncClient) -> None:
    """Dashboard should not include cancelled appointments."""
    staff_resp = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    service_resp = await client.post("/api/services", json={
        "name": "Haircut", "duration_minutes": 45,
        "price": 55.00, "category": "hair",
    })
    staff_id = staff_resp.json()["id"]
    service_id = service_resp.json()["id"]

    r = await client.post("/api/appointments", json={
        "customer_name": "John", "customer_email": "john@example.com",
        "staff_id": staff_id, "service_id": service_id,
        "start_time": "2026-04-01T10:00:00",
    })
    appt_id = r.json()["id"]
    await client.patch(f"/api/appointments/{appt_id}/cancel")

    resp = await client.get("/api/dashboard", params={"date": "2026-04-01"})
    assert resp.json()["total_appointments"] == 0
