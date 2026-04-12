"""Tests for SQLAlchemy models — CRUD operations and relationships."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board import Board
from app.models.card import Card
from app.models.column import Column
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember


@pytest.mark.asyncio
async def test_create_user(db_session: AsyncSession) -> None:
    """Insert a User and verify fields are persisted."""
    user = User(email="alice@example.com", hashed_password="fakehash123")
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(User).where(User.email == "alice@example.com"))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.email == "alice@example.com"
    assert fetched.hashed_password == "fakehash123"


@pytest.mark.asyncio
async def test_user_email_unique(db_session: AsyncSession) -> None:
    """Duplicate emails raise an IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(User(email="dup@test.com", hashed_password="h1"))
    await db_session.flush()

    db_session.add(User(email="dup@test.com", hashed_password="h2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_create_workspace(db_session: AsyncSession) -> None:
    """Create a workspace linked to an owner."""
    user = User(email="owner@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="My Team", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    result = await db_session.execute(select(Workspace).where(Workspace.id == ws.id))
    fetched = result.scalar_one()
    assert fetched.name == "My Team"
    assert fetched.owner_id == user.id


@pytest.mark.asyncio
async def test_workspace_member(db_session: AsyncSession) -> None:
    """Add a member to a workspace with a role."""
    user = User(email="member@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="Team WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    member = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="admin")
    db_session.add(member)
    await db_session.flush()

    result = await db_session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.workspace_id == ws.id,
        )
    )
    fetched = result.scalar_one()
    assert fetched.role == "admin"


@pytest.mark.asyncio
async def test_create_board(db_session: AsyncSession) -> None:
    """Create a board in a workspace."""
    user = User(email="board-owner@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="Dev Team", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Sprint Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    result = await db_session.execute(select(Board).where(Board.id == board.id))
    fetched = result.scalar_one()
    assert fetched.title == "Sprint Board"
    assert fetched.workspace_id == ws.id


@pytest.mark.asyncio
async def test_create_column(db_session: AsyncSession) -> None:
    """Create a column within a board."""
    user = User(email="col-user@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    col = Column(title="To Do", board_id=board.id, position=0)
    db_session.add(col)
    await db_session.flush()

    result = await db_session.execute(select(Column).where(Column.id == col.id))
    fetched = result.scalar_one()
    assert fetched.title == "To Do"
    assert fetched.position == 0


@pytest.mark.asyncio
async def test_create_card(db_session: AsyncSession) -> None:
    """Create a card in a column with an assignee."""
    user = User(email="card-user@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    col = Column(title="In Progress", board_id=board.id, position=1)
    db_session.add(col)
    await db_session.flush()

    card = Card(
        title="Implement auth",
        description="Add JWT-based authentication",
        column_id=col.id,
        position=0,
        assignee_id=user.id,
    )
    db_session.add(card)
    await db_session.flush()

    result = await db_session.execute(select(Card).where(Card.id == card.id))
    fetched = result.scalar_one()
    assert fetched.title == "Implement auth"
    assert fetched.description == "Add JWT-based authentication"
    assert fetched.assignee_id == user.id
    assert fetched.position == 0


@pytest.mark.asyncio
async def test_card_without_assignee(db_session: AsyncSession) -> None:
    """Cards can be created without an assignee."""
    user = User(email="no-assign@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    col = Column(title="Backlog", board_id=board.id, position=0)
    db_session.add(col)
    await db_session.flush()

    card = Card(title="Unassigned task", column_id=col.id, position=0)
    db_session.add(card)
    await db_session.flush()

    result = await db_session.execute(select(Card).where(Card.id == card.id))
    fetched = result.scalar_one()
    assert fetched.assignee_id is None


@pytest.mark.asyncio
async def test_cascade_delete_workspace_boards(db_session: AsyncSession) -> None:
    """Deleting a workspace cascades to boards, columns, and cards."""
    user = User(email="cascade@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="Cascade WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    col = Column(title="Col", board_id=board.id, position=0)
    db_session.add(col)
    await db_session.flush()

    card = Card(title="Card", column_id=col.id, position=0)
    db_session.add(card)
    await db_session.flush()

    await db_session.delete(ws)
    await db_session.flush()

    boards = (await db_session.execute(select(Board))).scalars().all()
    columns = (await db_session.execute(select(Column))).scalars().all()
    cards = (await db_session.execute(select(Card))).scalars().all()
    assert len(boards) == 0
    assert len(columns) == 0
    assert len(cards) == 0


@pytest.mark.asyncio
async def test_multiple_columns_ordering(db_session: AsyncSession) -> None:
    """Columns are ordered by position within a board."""
    user = User(email="order@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    board = Board(title="Board", workspace_id=ws.id)
    db_session.add(board)
    await db_session.flush()

    for i, title in enumerate(["Done", "In Progress", "To Do"]):
        db_session.add(Column(title=title, board_id=board.id, position=2 - i))
    await db_session.flush()

    result = await db_session.execute(
        select(Column).where(Column.board_id == board.id).order_by(Column.position)
    )
    cols = result.scalars().all()
    assert [c.title for c in cols] == ["To Do", "In Progress", "Done"]
