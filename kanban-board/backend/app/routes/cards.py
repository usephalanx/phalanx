"""Card router — CRUD and move endpoints for cards within columns."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.board import Board
from app.models.card import Card
from app.models.column import Column
from app.models.user import User
from app.schemas.card import CardCreate, CardMoveRequest, CardResponse, CardUpdate
from app.services.permissions import get_current_user, require_workspace_member
from app.services.position import calculate_position
from app.services.websocket import manager

router = APIRouter(prefix="/api", tags=["cards"])

# Default gap between positions when rebalancing
_POSITION_GAP = 1024.0


# ── Column-scoped endpoints ─────────────────────────────────────────────────


@router.post(
    "/columns/{column_id}/cards",
    response_model=CardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_card(
    column_id: int,
    body: CardCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardResponse:
    """Create a new card in a column, appended at the end.

    Args:
        column_id: The column database ID.
        body: Card creation data including title and optional description/assignee.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The created card data.

    Raises:
        HTTPException: If the column is not found or the user lacks access.
    """
    column = await _get_column_or_404(column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    # Find the last card position in this column
    result = await db.execute(
        select(Card)
        .where(Card.column_id == column_id)
        .order_by(Card.position.desc())
        .limit(1)
    )
    last_card = result.scalar_one_or_none()
    last_pos = last_card.position if last_card else None

    position = calculate_position(last_pos, None)

    card = Card(
        title=body.title,
        description=body.description,
        column_id=column_id,
        position=position,
        assignee_id=body.assignee_id,
    )
    db.add(card)
    await db.flush()
    await db.refresh(card)

    response = CardResponse.model_validate(card)

    # Broadcast card_created event to the board room
    await manager.broadcast(board.id, {
        "event": "card_created",
        "data": response.model_dump(mode="json"),
    })

    return response


@router.get(
    "/columns/{column_id}/cards",
    response_model=list[CardResponse],
)
async def list_cards(
    column_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CardResponse]:
    """List all cards in a column, ordered by position.

    Args:
        column_id: The column database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        A list of cards in the column sorted by position.

    Raises:
        HTTPException: If the column is not found or the user lacks access.
    """
    column = await _get_column_or_404(column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    result = await db.execute(
        select(Card)
        .where(Card.column_id == column_id)
        .order_by(Card.position.asc())
    )
    cards = result.scalars().all()
    return [CardResponse.model_validate(c) for c in cards]


# ── Card-scoped endpoints ───────────────────────────────────────────────────


@router.get(
    "/cards/{card_id}",
    response_model=CardResponse,
)
async def get_card(
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardResponse:
    """Get a single card by ID.

    Args:
        card_id: The card database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The card data.

    Raises:
        HTTPException: If the card is not found or the user lacks access.
    """
    card = await _get_card_or_404(card_id, db)
    column = await _get_column_or_404(card.column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    return CardResponse.model_validate(card)


@router.put(
    "/cards/{card_id}",
    response_model=CardResponse,
)
async def update_card(
    card_id: int,
    body: CardUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardResponse:
    """Update a card's title, description, or assignee.

    Args:
        card_id: The card database ID.
        body: Fields to update.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The updated card data.

    Raises:
        HTTPException: If the card is not found or the user lacks access.
    """
    card = await _get_card_or_404(card_id, db)
    column = await _get_column_or_404(card.column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    if body.title is not None:
        card.title = body.title
    if body.description is not None:
        card.description = body.description
    if body.assignee_id is not None:
        card.assignee_id = body.assignee_id

    await db.flush()
    await db.refresh(card)

    response = CardResponse.model_validate(card)

    # Broadcast card_updated event to the board room
    await manager.broadcast(board.id, {
        "event": "card_updated",
        "data": response.model_dump(mode="json"),
    })

    return response


@router.delete(
    "/cards/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_card(
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a card.

    Args:
        card_id: The card database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Raises:
        HTTPException: If the card is not found or the user lacks access.
    """
    card = await _get_card_or_404(card_id, db)
    column = await _get_column_or_404(card.column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    card_id_val = card.id
    column_id_val = card.column_id

    await db.delete(card)
    await db.flush()

    # Broadcast card_deleted event to the board room
    await manager.broadcast(board.id, {
        "event": "card_deleted",
        "data": {"id": card_id_val, "column_id": column_id_val},
    })


@router.patch(
    "/cards/{card_id}/move",
    response_model=CardResponse,
)
async def move_card(
    card_id: int,
    body: CardMoveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardResponse:
    """Move a card to a different column and/or position.

    Atomically updates the card's column_id and recalculates positions for
    all cards in both the source and destination columns.  The ``position``
    field in the request body is a zero-based index indicating where the card
    should be inserted in the target column's ordered list.

    Position updates for every affected card happen in a single database
    flush (transaction) to prevent race conditions during drag-and-drop.

    Args:
        card_id: The card database ID.
        body: Move data containing target_column_id and integer position.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The card data with its updated column and position.

    Raises:
        HTTPException: If the card, source column, or target column is not
            found, if target column is on a different board, or if the user
            lacks workspace membership.
    """
    card = await _get_card_or_404(card_id, db)
    source_column = await _get_column_or_404(card.column_id, db)
    source_board = await _get_board_or_404(source_column.board_id, db)
    await require_workspace_member(source_board.workspace_id, current_user, db)

    # Validate target column exists and belongs to the same board
    target_column = await _get_column_or_404(body.target_column_id, db)
    if target_column.board_id != source_column.board_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target column must belong to the same board",
        )

    source_column_id = card.column_id
    target_column_id = body.target_column_id
    moving_across_columns = source_column_id != target_column_id

    # ── Reorder destination column ──────────────────────────────────────
    # Load all cards in the target column (excluding the moving card),
    # ordered by current position.
    dest_result = await db.execute(
        select(Card)
        .where(Card.column_id == target_column_id, Card.id != card.id)
        .order_by(Card.position.asc())
    )
    dest_cards: list[Card] = list(dest_result.scalars().all())

    # Clamp position to valid range
    insert_idx = min(body.position, len(dest_cards))

    # Insert the moved card at the requested index
    dest_cards.insert(insert_idx, card)

    # Update the card's column
    card.column_id = target_column_id

    # Assign evenly-spaced positions to all cards in the destination column
    for idx, c in enumerate(dest_cards):
        c.position = (idx + 1) * _POSITION_GAP

    # ── Reorder source column (if different) ────────────────────────────
    if moving_across_columns:
        src_result = await db.execute(
            select(Card)
            .where(Card.column_id == source_column_id, Card.id != card.id)
            .order_by(Card.position.asc())
        )
        src_cards: list[Card] = list(src_result.scalars().all())

        for idx, c in enumerate(src_cards):
            c.position = (idx + 1) * _POSITION_GAP

    # Single flush ensures atomicity — all position writes commit together
    await db.flush()
    await db.refresh(card)

    response = CardResponse.model_validate(card)

    # Broadcast card_moved event to the board room
    await manager.broadcast(source_board.id, {
        "event": "card_moved",
        "data": {
            **response.model_dump(mode="json"),
            "source_column_id": source_column_id,
        },
    })

    return response


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_board_or_404(board_id: int, db: AsyncSession) -> Board:
    """Fetch a board by ID or raise 404.

    Args:
        board_id: The board database ID.
        db: The async database session.

    Returns:
        The Board ORM instance.

    Raises:
        HTTPException: If the board does not exist.
    """
    result = await db.execute(select(Board).where(Board.id == board_id))
    board = result.scalar_one_or_none()

    if board is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )
    return board


async def _get_column_or_404(column_id: int, db: AsyncSession) -> Column:
    """Fetch a column by ID or raise 404.

    Args:
        column_id: The column database ID.
        db: The async database session.

    Returns:
        The Column ORM instance.

    Raises:
        HTTPException: If the column does not exist.
    """
    result = await db.execute(select(Column).where(Column.id == column_id))
    column = result.scalar_one_or_none()

    if column is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Column not found",
        )
    return column


async def _get_card_or_404(card_id: int, db: AsyncSession) -> Card:
    """Fetch a card by ID or raise 404.

    Args:
        card_id: The card database ID.
        db: The async database session.

    Returns:
        The Card ORM instance.

    Raises:
        HTTPException: If the card does not exist.
    """
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()

    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found",
        )
    return card
