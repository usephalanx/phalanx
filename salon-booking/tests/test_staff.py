"""Tests for staff CRUD endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_staff(client: AsyncClient) -> None:
    """POST /api/staff creates a new staff member."""
    payload = {
        "name": "Test Stylist",
        "email": "test@salon.com",
        "phone": "555-1234",
        "role": "stylist",
        "specialties": ["haircut", "coloring"],
    }
    resp = await client.post("/api/staff", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Stylist"
    assert data["email"] == "test@salon.com"
    assert data["specialties"] == ["haircut", "coloring"]
    assert data["active"] is True
    assert "id" in data


@pytest.mark.asyncio
async def test_list_staff(client: AsyncClient) -> None:
    """GET /api/staff returns all staff."""
    await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    await client.post("/api/staff", json={
        "name": "Bob", "email": "bob@salon.com", "role": "colorist",
    })

    resp = await client.get("/api/staff")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_list_staff_filter_active(client: AsyncClient) -> None:
    """GET /api/staff?active=true filters by active status."""
    r1 = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    staff_id = r1.json()["id"]
    await client.post("/api/staff", json={
        "name": "Bob", "email": "bob@salon.com", "role": "stylist",
    })
    # Deactivate Alice
    await client.delete(f"/api/staff/{staff_id}")

    resp = await client.get("/api/staff", params={"active": "true"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["name"] == "Bob"


@pytest.mark.asyncio
async def test_get_staff_detail(client: AsyncClient) -> None:
    """GET /api/staff/{id} returns staff with schedules."""
    r = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    staff_id = r.json()["id"]

    resp = await client.get(f"/api/staff/{staff_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Alice"
    assert "schedules" in data


@pytest.mark.asyncio
async def test_get_staff_not_found(client: AsyncClient) -> None:
    """GET /api/staff/999 returns 404."""
    resp = await client.get("/api/staff/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_staff(client: AsyncClient) -> None:
    """PUT /api/staff/{id} updates staff fields."""
    r = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    staff_id = r.json()["id"]

    resp = await client.put(f"/api/staff/{staff_id}", json={"name": "Alice Updated"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alice Updated"


@pytest.mark.asyncio
async def test_deactivate_staff(client: AsyncClient) -> None:
    """DELETE /api/staff/{id} soft-deletes the staff member."""
    r = await client.post("/api/staff", json={
        "name": "Alice", "email": "alice@salon.com", "role": "stylist",
    })
    staff_id = r.json()["id"]

    resp = await client.delete(f"/api/staff/{staff_id}")
    assert resp.status_code == 204

    # Verify soft-deleted
    detail = await client.get(f"/api/staff/{staff_id}")
    assert detail.json()["active"] is False


@pytest.mark.asyncio
async def test_create_staff_validation_error(client: AsyncClient) -> None:
    """POST /api/staff with invalid email returns 422."""
    resp = await client.post("/api/staff", json={
        "name": "Bad",
        "email": "not-an-email",
        "role": "stylist",
    })
    assert resp.status_code == 422
