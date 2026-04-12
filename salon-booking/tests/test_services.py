"""Tests for service CRUD endpoints."""

import pytest
from httpx import AsyncClient


SAMPLE_SERVICE = {
    "name": "Haircut",
    "description": "Professional haircut",
    "duration_minutes": 45,
    "price": 55.00,
    "category": "hair",
}


@pytest.mark.asyncio
async def test_create_service(client: AsyncClient) -> None:
    """POST /api/services creates a new service."""
    resp = await client.post("/api/services", json=SAMPLE_SERVICE)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Haircut"
    assert data["duration_minutes"] == 45
    assert data["price"] == 55.00
    assert data["active"] is True


@pytest.mark.asyncio
async def test_list_services(client: AsyncClient) -> None:
    """GET /api/services returns all services."""
    await client.post("/api/services", json=SAMPLE_SERVICE)
    await client.post("/api/services", json={
        **SAMPLE_SERVICE, "name": "Coloring", "category": "hair",
    })

    resp = await client.get("/api/services")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_list_services_filter_category(client: AsyncClient) -> None:
    """GET /api/services?category=nails filters by category."""
    await client.post("/api/services", json=SAMPLE_SERVICE)
    await client.post("/api/services", json={
        "name": "Manicure", "duration_minutes": 30,
        "price": 25.00, "category": "nails",
    })

    resp = await client.get("/api/services", params={"category": "nails"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Manicure"


@pytest.mark.asyncio
async def test_get_service(client: AsyncClient) -> None:
    """GET /api/services/{id} returns a single service."""
    r = await client.post("/api/services", json=SAMPLE_SERVICE)
    sid = r.json()["id"]

    resp = await client.get(f"/api/services/{sid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Haircut"


@pytest.mark.asyncio
async def test_get_service_not_found(client: AsyncClient) -> None:
    """GET /api/services/999 returns 404."""
    resp = await client.get("/api/services/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_service(client: AsyncClient) -> None:
    """PUT /api/services/{id} updates service fields."""
    r = await client.post("/api/services", json=SAMPLE_SERVICE)
    sid = r.json()["id"]

    resp = await client.put(f"/api/services/{sid}", json={"price": 65.00})
    assert resp.status_code == 200
    assert resp.json()["price"] == 65.00


@pytest.mark.asyncio
async def test_deactivate_service(client: AsyncClient) -> None:
    """DELETE /api/services/{id} soft-deletes the service."""
    r = await client.post("/api/services", json=SAMPLE_SERVICE)
    sid = r.json()["id"]

    resp = await client.delete(f"/api/services/{sid}")
    assert resp.status_code == 204

    detail = await client.get(f"/api/services/{sid}")
    assert detail.json()["active"] is False


@pytest.mark.asyncio
async def test_create_service_invalid_duration(client: AsyncClient) -> None:
    """POST /api/services with zero duration returns 422."""
    resp = await client.post("/api/services", json={
        **SAMPLE_SERVICE, "duration_minutes": 0,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_service_negative_price(client: AsyncClient) -> None:
    """POST /api/services with negative price returns 422."""
    resp = await client.post("/api/services", json={
        **SAMPLE_SERVICE, "price": -10,
    })
    assert resp.status_code == 422
