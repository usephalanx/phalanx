"""Tests for GET /api/cards/{card_id} endpoint."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestGetCard:
    """Tests for GET /api/cards/{card_id}."""

    async def test_get_card_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Workspace member can retrieve a single card by ID."""
        response = await client.get(
            f"/api/cards/{card['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == card["id"]
        assert data["title"] == card["title"]
        assert data["description"] == card["description"]
        assert data["column_id"] == card["column_id"]
        assert "position" in data
        assert "created_at" in data
        assert "updated_at" in data

    async def test_get_card_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-existent card returns 404."""
        response = await client.get(
            "/api/cards/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_get_card_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Non-member cannot retrieve a card."""
        response = await client.get(
            f"/api/cards/{card['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_get_card_unauthenticated(
        self,
        client: AsyncClient,
        card: dict[str, Any],
    ) -> None:
        """Unauthenticated request returns 401."""
        response = await client.get(
            f"/api/cards/{card['id']}",
        )
        assert response.status_code == 401

    async def test_get_card_reflects_updates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """GET returns the latest card data after an update."""
        await client.put(
            f"/api/cards/{card['id']}",
            json={"title": "Updated Title", "description": "Updated Desc"},
            headers=auth_headers,
        )
        response = await client.get(
            f"/api/cards/{card['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["description"] == "Updated Desc"
