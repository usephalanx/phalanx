"""Column router — CRUD and reorder endpoints for board columns."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.board import Board
from app.models.column import Column
from app.models.user import User
from app.schemas.column import (
    ColumnCreate,
    ColumnReorderRequest,
    ColumnResponse,
    ColumnUpdate,
)
from app.services.permissions import get_current_user, require_workspace_member
from app.services.position import calculate_position

router = APIRouter(prefix="/api", tags=["columns"])


@router.post(
    "/boards/{board_id}/columns",
    response_model=ColumnResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_column(
    board_id: int,
    body: ColumnCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ColumnResponse:
    """Create a new column in the given board.

    The column is appended at the end by default (position calculated from
    the last existing column).

    Args:
        board_id: The board database ID.
        body: Column creation data including name and optional color/wip_limit.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The created column data.

    Raises:
        HTTPException: If the board is not found or the user lacks access.
    """
    board = await _get_board_or_404(board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    # Find the last column position for this board
    result = await db.execute(
        select(Column)
        .where(Column.board_id == board_id)
        .order_by(Column.position.desc())
        .limit(1)
    )
    last_column = result.scalar_one_or_none()
    last_pos = last_column.position if last_column else None

    position = calculate_position(last_pos, None)

    column = Column(
        name=body.name,
        board_id=board_id,
        position=position,
        color=body.color,
        wip_limit=body.wip_limit,
    )
    db.add(column)
    await db.flush()
    await db.refresh(column)

    return ColumnResponse.model_validate(column)


@router.put(
    "/columns/{column_id}",
    response_model=ColumnResponse,
)
async def update_column(
    column_id: int,
    body: ColumnUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ColumnResponse:
    """Update a column's name, color, or WIP limit.

    Args:
        column_id: The column database ID.
        body: Fields to update.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The updated column data.

    Raises:
        HTTPException: If the column is not found or the user lacks access.
    """
    column = await _get_column_or_404(column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    if body.name is not None:
        column.name = body.name
    if body.color is not None:
        column.color = body.color
    if body.wip_limit is not None:
        column.wip_limit = body.wip_limit

    await db.flush()
    await db.refresh(column)
    return ColumnResponse.model_validate(column)


@router.delete(
    "/columns/{column_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_column(
    column_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a column and all its cards.

    Args:
        column_id: The column database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Raises:
        HTTPException: If the column is not found or the user lacks access.
    """
    column = await _get_column_or_404(column_id, db)
    board = await _get_board_or_404(column.board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    await db.delete(column)
    await db.flush()


@router.patch(
    "/columns/reorder",
    response_model=list[ColumnResponse],
)
async def reorder_columns(
    body: ColumnReorderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ColumnResponse]:
    """Reorder columns by providing their IDs in the desired order.

    All column IDs must belong to the same board. The positions are
    reassigned at 1024-unit intervals in the order specified.

    Args:
        body: List of column IDs in the desired order.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The reordered columns with updated positions.

    Raises:
        HTTPException: If any column is not found, columns span multiple boards,
            or the user lacks access.
    """
    if not body.column_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="column_ids must not be empty",
        )

    # Load all requested columns
    result = await db.execute(
        select(Column).where(Column.id.in_(body.column_ids))
    )
    columns_map: dict[int, Column] = {col.id: col for col in result.scalars().all()}

    # Verify all IDs were found
    missing = set(body.column_ids) - set(columns_map.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Columns not found: {sorted(missing)}",
        )

    # Verify all columns belong to the same board
    board_ids = {col.board_id for col in columns_map.values()}
    if len(board_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All columns must belong to the same board",
        )

    board_id = board_ids.pop()
    board = await _get_board_or_404(board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    # Reassign positions in the specified order
    gap = 1024.0
    ordered_columns: list[Column] = []
    for idx, col_id in enumerate(body.column_ids):
        col = columns_map[col_id]
        col.position = (idx + 1) * gap
        ordered_columns.append(col)

    await db.flush()

    # Refresh and return
    for col in ordered_columns:
        await db.refresh(col)

    return [ColumnResponse.model_validate(col) for col in ordered_columns]


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
