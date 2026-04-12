"""Tests for column CRUD and reorder endpoints."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestCreateColumn:
    """Tests for POST /api/boards/{bid}/columns."""

    async def test_create_column_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Workspace member can create a column in a board."""
        response = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "To Do", "color": "#ff0000", "wip_limit": 5},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "To Do"
        assert data["color"] == "#ff0000"
        assert data["wip_limit"] == 5
        assert data["board_id"] == board["id"]
        assert data["position"] > 0

    async def test_create_column_minimal(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Column can be created with name only."""
        response = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Backlog"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Backlog"
        assert data["color"] is None
        assert data["wip_limit"] is None

    async def test_create_column_positions_increment(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Each new column gets a higher position than the previous one."""
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Column 1"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Column 2"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201
        assert col2.json()["position"] > col1.json()["position"]

    async def test_create_column_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Non-member cannot create a column."""
        response = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Forbidden"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_create_column_board_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Creating a column on a non-existent board returns 404."""
        response = await client.post(
            "/api/boards/99999/columns",
            json={"name": "Ghost Column"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_create_column_empty_name_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Column name must not be empty."""
        response = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": ""},
            headers=auth_headers,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestUpdateColumn:
    """Tests for PUT /api/columns/{id}."""

    async def test_update_column_name(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Member can update the column name."""
        response = await client.put(
            f"/api/columns/{column['id']}",
            json={"name": "In Progress"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["name"] == "In Progress"

    async def test_update_column_color_and_wip(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Member can update color and WIP limit."""
        response = await client.put(
            f"/api/columns/{column['id']}",
            json={"color": "#00ff00", "wip_limit": 3},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["color"] == "#00ff00"
        assert data["wip_limit"] == 3

    async def test_update_column_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Updating non-existent column returns 404."""
        response = await client.put(
            "/api/columns/99999",
            json={"name": "Ghost Column"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_update_column_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Non-member cannot update a column."""
        response = await client.put(
            f"/api/columns/{column['id']}",
            json={"name": "Hacked"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestDeleteColumn:
    """Tests for DELETE /api/columns/{id}."""

    async def test_delete_column_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Member can delete a column."""
        response = await client.delete(
            f"/api/columns/{column['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 204

    async def test_delete_column_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Deleting non-existent column returns 404."""
        response = await client.delete(
            "/api/columns/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_delete_column_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Non-member cannot delete a column."""
        response = await client.delete(
            f"/api/columns/{column['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestReorderColumns:
    """Tests for PATCH /api/columns/reorder."""

    async def test_reorder_columns_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Reorder reverses column positions correctly."""
        # Create three columns
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Col A"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Col B"},
            headers=auth_headers,
        )
        col3 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Col C"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201
        assert col3.status_code == 201

        ids = [col3.json()["id"], col1.json()["id"], col2.json()["id"]]

        response = await client.patch(
            "/api/columns/reorder",
            json={"column_ids": ids},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        # Verify order matches requested
        assert data[0]["id"] == ids[0]
        assert data[1]["id"] == ids[1]
        assert data[2]["id"] == ids[2]
        # Verify positions are strictly ascending
        assert data[0]["position"] < data[1]["position"] < data[2]["position"]

    async def test_reorder_columns_missing_ids(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Reorder with non-existent column IDs returns 404."""
        response = await client.patch(
            "/api/columns/reorder",
            json={"column_ids": [99998, 99999]},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_reorder_columns_empty_list(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Reorder with empty column_ids list returns 422."""
        response = await client.patch(
            "/api/columns/reorder",
            json={"column_ids": []},
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_reorder_columns_non_member_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Non-member cannot reorder columns."""
        col = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Col X"},
            headers=auth_headers,
        )
        assert col.status_code == 201

        response = await client.patch(
            "/api/columns/reorder",
            json={"column_ids": [col.json()["id"]]},
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_reorder_columns_mixed_boards_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Reorder with columns from different boards returns 400."""
        # Create two boards
        board1 = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "Board 1"},
            headers=auth_headers,
        )
        board2 = await client.post(
            f"/api/workspaces/{workspace['id']}/boards",
            json={"name": "Board 2"},
            headers=auth_headers,
        )
        assert board1.status_code == 201
        assert board2.status_code == 201

        col1 = await client.post(
            f"/api/boards/{board1.json()['id']}/columns",
            json={"name": "Board1 Col"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board2.json()['id']}/columns",
            json={"name": "Board2 Col"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        response = await client.patch(
            "/api/columns/reorder",
            json={"column_ids": [col1.json()["id"], col2.json()["id"]]},
            headers=auth_headers,
        )
        assert response.status_code == 400
