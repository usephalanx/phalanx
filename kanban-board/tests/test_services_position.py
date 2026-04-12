"""Tests for fractional indexing position service."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board import Board
from app.models.column import Column
from app.models.user import User
from app.models.workspace import Workspace
from app.services.position import calculate_position, rebalance_positions


def test_calculate_position_first_item() -> None:
    """First item in an empty list gets position 1024.0."""
    assert calculate_position(None, None) == 1024.0


def test_calculate_position_insert_at_start() -> None:
    """Insert before the first item returns half of next_pos."""
    assert calculate_position(None, 1024.0) == 512.0


def test_calculate_position_append_at_end() -> None:
    """Append after the last item returns prev_pos + 1024."""
    assert calculate_position(1024.0, None) == 2048.0


def test_calculate_position_between_two() -> None:
    """Insert between two items returns the midpoint."""
    assert calculate_position(1024.0, 2048.0) == 1536.0


def test_calculate_position_small_gap() -> None:
    """Midpoint works even for small gaps."""
    assert calculate_position(100.0, 101.0) == 100.5


@pytest.mark.asyncio
async def test_rebalance_positions(db_session: AsyncSession) -> None:
    """Rebalance renumbers positions at 1024 increments."""
    user = User(email="rebalance@test.com", hashed_password="h", display_name="Reb")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    # Create columns with irregular positions
    for pos in [1, 2, 3]:
        db_session.add(Column(title=f"Col {pos}", board_id=board.id, position=pos))
    await db_session.flush()

    items = await rebalance_positions(db_session, Column, "board_id", board.id)
    assert len(items) == 3
    assert [item.position for item in items] == [1024.0, 2048.0, 3072.0]


@pytest.mark.asyncio
async def test_rebalance_preserves_order(db_session: AsyncSession) -> None:
    """Rebalance preserves the original ordering of items."""
    user = User(email="order@test.com", hashed_password="h", display_name="Ord")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    db_session.add(Column(title="First", board_id=board.id, position=5))
    db_session.add(Column(title="Second", board_id=board.id, position=10))
    await db_session.flush()

    items = await rebalance_positions(db_session, Column, "board_id", board.id)
    assert items[0].title == "First"
    assert items[1].title == "Second"
