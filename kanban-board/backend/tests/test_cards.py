"""Tests for card CRUD and move endpoints."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestCreateCard:
    """Tests for POST /api/columns/{column_id}/cards."""

    async def test_create_card_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Workspace member can create a card with all fields."""
        response = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={
                "title": "Implement login",
                "description": "Build the login form",
                "assignee_id": None,
            },
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Implement login"
        assert data["description"] == "Build the login form"
        assert data["column_id"] == column["id"]
        assert data["position"] > 0

    async def test_create_card_minimal(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Card can be created with title only."""
        response = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Quick task"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Quick task"
        assert data["description"] is None
        assert data["assignee_id"] is None

    async def test_create_card_positions_increment(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Each new card gets a higher position than the previous one."""
        card1 = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Card 1"},
            headers=auth_headers,
        )
        card2 = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Card 2"},
            headers=auth_headers,
        )
        assert card1.status_code == 201
        assert card2.status_code == 201
        assert card2.json()["position"] > card1.json()["position"]

    async def test_create_card_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Non-member cannot create a card."""
        response = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Forbidden"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_create_card_column_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Creating a card in a non-existent column returns 404."""
        response = await client.post(
            "/api/columns/99999/cards",
            json={"title": "Ghost Card"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_create_card_empty_title_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Card title must not be empty."""
        response = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": ""},
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_create_card_unauthenticated(
        self,
        client: AsyncClient,
        column: dict[str, Any],
    ) -> None:
        """Unauthenticated request returns 401."""
        response = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "No Auth"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
class TestListCards:
    """Tests for GET /api/columns/{column_id}/cards."""

    async def test_list_cards_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Empty column returns an empty list."""
        response = await client.get(
            f"/api/columns/{column['id']}/cards",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_cards_returns_cards(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Cards are returned ordered by position."""
        await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "First"},
            headers=auth_headers,
        )
        await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Second"},
            headers=auth_headers,
        )

        response = await client.get(
            f"/api/columns/{column['id']}/cards",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["title"] == "First"
        assert data[1]["title"] == "Second"
        assert data[0]["position"] < data[1]["position"]

    async def test_list_cards_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Non-member cannot list cards."""
        response = await client.get(
            f"/api/columns/{column['id']}/cards",
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_list_cards_column_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Listing cards for a non-existent column returns 404."""
        response = await client.get(
            "/api/columns/99999/cards",
            headers=auth_headers,
        )
        assert response.status_code == 404


@pytest.mark.asyncio
class TestUpdateCard:
    """Tests for PUT /api/cards/{card_id}."""

    async def test_update_card_title(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Member can update the card title."""
        response = await client.put(
            f"/api/cards/{card['id']}",
            json={"title": "Updated Title"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Updated Title"

    async def test_update_card_description(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Member can update the card description."""
        response = await client.put(
            f"/api/cards/{card['id']}",
            json={"description": "New description"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["description"] == "New description"

    async def test_update_card_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Updating a non-existent card returns 404."""
        response = await client.put(
            "/api/cards/99999",
            json={"title": "Ghost"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_update_card_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Non-member cannot update a card."""
        response = await client.put(
            f"/api/cards/{card['id']}",
            json={"title": "Hacked"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_update_card_empty_title_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Card title cannot be set to empty."""
        response = await client.put(
            f"/api/cards/{card['id']}",
            json={"title": ""},
            headers=auth_headers,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestDeleteCard:
    """Tests for DELETE /api/cards/{card_id}."""

    async def test_delete_card_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Member can delete a card, and it's gone afterwards."""
        response = await client.delete(
            f"/api/cards/{card['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        # Verify it's gone
        get_response = await client.put(
            f"/api/cards/{card['id']}",
            json={"title": "Should fail"},
            headers=auth_headers,
        )
        assert get_response.status_code == 404

    async def test_delete_card_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Deleting a non-existent card returns 404."""
        response = await client.delete(
            "/api/cards/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_delete_card_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Non-member cannot delete a card."""
        response = await client.delete(
            f"/api/cards/{card['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestMoveCard:
    """Tests for PATCH /api/cards/{card_id}/move."""

    async def test_move_card_within_same_column(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Card can be repositioned within the same column."""
        # Create two cards
        card1 = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Card A"},
            headers=auth_headers,
        )
        card2 = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "Card B"},
            headers=auth_headers,
        )
        assert card1.status_code == 201
        assert card2.status_code == 201

        # Move card2 to position 0 (before card1)
        response = await client.patch(
            f"/api/cards/{card2.json()['id']}/move",
            json={
                "target_column_id": column["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["column_id"] == column["id"]

        # Verify the order: Card B should now be before Card A
        list_resp = await client.get(
            f"/api/columns/{column['id']}/cards",
            headers=auth_headers,
        )
        cards = list_resp.json()
        assert cards[0]["title"] == "Card B"
        assert cards[1]["title"] == "Card A"
        assert cards[0]["position"] < cards[1]["position"]

    async def test_move_card_to_different_column(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Card can be moved to a different column on the same board."""
        # Create two columns
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "To Do"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Done"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        # Create a card in column 1
        card = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "Movable Card"},
            headers=auth_headers,
        )
        assert card.status_code == 201

        # Move it to column 2 at position 0
        response = await client.patch(
            f"/api/cards/{card.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["column_id"] == col2.json()["id"]

        # Verify it's no longer in column 1
        list_response = await client.get(
            f"/api/columns/{col1.json()['id']}/cards",
            headers=auth_headers,
        )
        assert list_response.status_code == 200
        assert len(list_response.json()) == 0

        # Verify it's in column 2
        list_response = await client.get(
            f"/api/columns/{col2.json()['id']}/cards",
            headers=auth_headers,
        )
        assert list_response.status_code == 200
        assert len(list_response.json()) == 1
        assert list_response.json()[0]["title"] == "Movable Card"

    async def test_move_card_cross_board_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Moving a card to a column on a different board returns 400."""
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
            json={"name": "Col A"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board2.json()['id']}/columns",
            json={"name": "Col B"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        card = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "Cross-board card"},
            headers=auth_headers,
        )
        assert card.status_code == 201

        response = await client.patch(
            f"/api/cards/{card.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 400

    async def test_move_card_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Moving a non-existent card returns 404."""
        response = await client.patch(
            "/api/cards/99999/move",
            json={
                "target_column_id": column["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_move_card_target_column_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
    ) -> None:
        """Moving to a non-existent column returns 404."""
        response = await client.patch(
            f"/api/cards/{card['id']}/move",
            json={
                "target_column_id": 99999,
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_move_card_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        card: dict[str, Any],
        column: dict[str, Any],
    ) -> None:
        """Non-member cannot move a card."""
        response = await client.patch(
            f"/api/cards/{card['id']}/move",
            json={
                "target_column_id": column["id"],
                "position": 0,
            },
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_move_card_preserves_other_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Moving a card preserves its title, description, and assignee."""
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Source"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Dest"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        card = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "Important", "description": "Do not lose me"},
            headers=auth_headers,
        )
        assert card.status_code == 201

        response = await client.patch(
            f"/api/cards/{card.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Important"
        assert data["description"] == "Do not lose me"
        assert data["column_id"] == col2.json()["id"]

    async def test_move_card_reorders_destination_column(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Moving a card into a column reorders all cards at even intervals."""
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Source"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Dest"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        # Create three cards in destination column
        card_a = await client.post(
            f"/api/columns/{col2.json()['id']}/cards",
            json={"title": "A"},
            headers=auth_headers,
        )
        card_b = await client.post(
            f"/api/columns/{col2.json()['id']}/cards",
            json={"title": "B"},
            headers=auth_headers,
        )
        card_c = await client.post(
            f"/api/columns/{col2.json()['id']}/cards",
            json={"title": "C"},
            headers=auth_headers,
        )
        assert card_a.status_code == 201
        assert card_b.status_code == 201
        assert card_c.status_code == 201

        # Create a card in source column and move it to position 1 in dest
        card_x = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "X"},
            headers=auth_headers,
        )
        assert card_x.status_code == 201

        response = await client.patch(
            f"/api/cards/{card_x.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 1,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200

        # Verify final order in destination: A, X, B, C
        list_resp = await client.get(
            f"/api/columns/{col2.json()['id']}/cards",
            headers=auth_headers,
        )
        assert list_resp.status_code == 200
        titles = [c["title"] for c in list_resp.json()]
        assert titles == ["A", "X", "B", "C"]

        # Verify positions are evenly spaced at 1024 intervals
        positions = [c["position"] for c in list_resp.json()]
        assert positions == [1024.0, 2048.0, 3072.0, 4096.0]

    async def test_move_card_reorders_source_column(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Moving a card out of a column rebalances positions in the source."""
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Source"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Dest"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        # Create three cards in source column
        card_a = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "A"},
            headers=auth_headers,
        )
        card_b = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "B"},
            headers=auth_headers,
        )
        card_c = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "C"},
            headers=auth_headers,
        )
        assert card_a.status_code == 201
        assert card_b.status_code == 201
        assert card_c.status_code == 201

        # Move middle card (B) to destination
        response = await client.patch(
            f"/api/cards/{card_b.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200

        # Source column should have A, C with rebalanced positions
        list_resp = await client.get(
            f"/api/columns/{col1.json()['id']}/cards",
            headers=auth_headers,
        )
        assert list_resp.status_code == 200
        titles = [c["title"] for c in list_resp.json()]
        assert titles == ["A", "C"]

        positions = [c["position"] for c in list_resp.json()]
        assert positions == [1024.0, 2048.0]

    async def test_move_card_position_clamped_to_end(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        board: dict[str, Any],
    ) -> None:
        """Position exceeding column length inserts at the end."""
        col1 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Source"},
            headers=auth_headers,
        )
        col2 = await client.post(
            f"/api/boards/{board['id']}/columns",
            json={"name": "Dest"},
            headers=auth_headers,
        )
        assert col1.status_code == 201
        assert col2.status_code == 201

        # Create one card in dest
        existing = await client.post(
            f"/api/columns/{col2.json()['id']}/cards",
            json={"title": "Existing"},
            headers=auth_headers,
        )
        assert existing.status_code == 201

        # Create a card in source
        card_x = await client.post(
            f"/api/columns/{col1.json()['id']}/cards",
            json={"title": "X"},
            headers=auth_headers,
        )
        assert card_x.status_code == 201

        # Move to position 999 (way past end) — should clamp to end
        response = await client.patch(
            f"/api/cards/{card_x.json()['id']}/move",
            json={
                "target_column_id": col2.json()["id"],
                "position": 999,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200

        # X should be at the end
        list_resp = await client.get(
            f"/api/columns/{col2.json()['id']}/cards",
            headers=auth_headers,
        )
        titles = [c["title"] for c in list_resp.json()]
        assert titles == ["Existing", "X"]

    async def test_move_card_negative_position_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        card: dict[str, Any],
        column: dict[str, Any],
    ) -> None:
        """Negative position is rejected with 422."""
        response = await client.patch(
            f"/api/cards/{card['id']}/move",
            json={
                "target_column_id": column["id"],
                "position": -1,
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_move_card_same_column_reorder_three(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        column: dict[str, Any],
    ) -> None:
        """Three cards reordered within the same column get even positions."""
        card_a = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "A"},
            headers=auth_headers,
        )
        card_b = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "B"},
            headers=auth_headers,
        )
        card_c = await client.post(
            f"/api/columns/{column['id']}/cards",
            json={"title": "C"},
            headers=auth_headers,
        )
        assert card_a.status_code == 201
        assert card_b.status_code == 201
        assert card_c.status_code == 201

        # Move C to position 0 — new order should be C, A, B
        response = await client.patch(
            f"/api/cards/{card_c.json()['id']}/move",
            json={
                "target_column_id": column["id"],
                "position": 0,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200

        list_resp = await client.get(
            f"/api/columns/{column['id']}/cards",
            headers=auth_headers,
        )
        titles = [c["title"] for c in list_resp.json()]
        assert titles == ["C", "A", "B"]

        positions = [c["position"] for c in list_resp.json()]
        assert positions == [1024.0, 2048.0, 3072.0]
