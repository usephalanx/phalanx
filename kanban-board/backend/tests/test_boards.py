"""Tests for board CRUD endpoints."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestCreateBoard:
    """Tests for POST /api/workspaces/{wid}/boards."""

    async def test_create_board_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Workspace member can create a board."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "Sprint Board", "description": "Sprint planning"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Sprint Board"
        assert data["description"] == "Sprint planning"
        assert data["workspace_id"] == workspace["id"]
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    async def test_create_board_no_description(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Board can be created without a description."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "Minimal Board"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Minimal Board"
        assert data["description"] is None

    async def test_create_board_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-member cannot create a board."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "Forbidden Board"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_create_board_unauthenticated(
        self,
        client: AsyncClient,
        workspace: dict[str, Any],
    ) -> None:
        """Unauthenticated request returns 401."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "No Auth Board"},
        )
        assert response.status_code == 401

    async def test_create_board_empty_name_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Board name must not be empty."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": ""},
            headers=auth_headers,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestListBoards:
    """Tests for GET /api/workspaces/{wid}/boards."""

    async def test_list_boards_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Workspace with no boards returns an empty list."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}/boards",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_boards_returns_workspace_boards(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
        board: dict[str, Any],
    ) -> None:
        """Member can list boards in the workspace."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}/boards",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == board["id"]

    async def test_list_boards_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-member cannot list boards."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}/boards",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestGetBoard:
    """Tests for GET /api/boards/{id}."""

    async def test_get_board_with_columns(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Get board returns board data with columns list."""
        response = await client.get(
            f"/api/boards/{board['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == board["id"]
        assert data["name"] == board["name"]
        assert "columns" in data
        assert isinstance(data["columns"], list)

    async def test_get_board_includes_created_columns(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
        column: dict[str, Any],
    ) -> None:
        """Get board returns nested columns."""
        response = await client.get(
            f"/api/boards/{board['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["columns"]) == 1
        assert data["columns"][0]["name"] == "To Do"

    async def test_get_board_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-existent board returns 404."""
        response = await client.get(
            "/api/boards/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_get_board_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Non-member cannot get board details."""
        response = await client.get(
            f"/api/boards/{board['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestUpdateBoard:
    """Tests for PUT /api/boards/{id}."""

    async def test_update_board_name(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Member can update the board name."""
        response = await client.put(
            f"/api/boards/{board['id']}",
            json={"name": "Updated Board"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Board"

    async def test_update_board_description(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Member can update the board description."""
        response = await client.put(
            f"/api/boards/{board['id']}",
            json={"description": "New description"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["description"] == "New description"

    async def test_update_board_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Updating non-existent board returns 404."""
        response = await client.put(
            "/api/boards/99999",
            json={"name": "Ghost Board"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_update_board_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Non-member cannot update a board."""
        response = await client.put(
            f"/api/boards/{board['id']}",
            json={"name": "Hacked"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestDeleteBoard:
    """Tests for DELETE /api/boards/{id}."""

    async def test_delete_board_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Member can delete a board."""
        response = await client.delete(
            f"/api/boards/{board['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        # Verify it's gone
        get_response = await client.get(
            f"/api/boards/{board['id']}",
            headers=auth_headers,
        )
        assert get_response.status_code == 404

    async def test_delete_board_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Deleting non-existent board returns 404."""
        response = await client.delete(
            "/api/boards/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_delete_board_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Non-member cannot delete a board."""
        response = await client.delete(
            f"/api/boards/{board['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403
