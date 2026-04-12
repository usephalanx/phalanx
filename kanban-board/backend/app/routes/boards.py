"""Board router — CRUD endpoints for Kanban boards within workspaces."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.board import Board
from app.models.user import User
from app.schemas.board import (
    BoardCreate,
    BoardDetailResponse,
    BoardResponse,
    BoardUpdate,
)
from app.schemas.column import ColumnWithCardsResponse
from app.services.permissions import get_current_user, require_workspace_member

router = APIRouter(prefix="/api", tags=["boards"])


@router.post(
    "/workspaces/{workspace_id}/boards",
    response_model=BoardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_board(
    workspace_id: int,
    body: BoardCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BoardResponse:
    """Create a new board in the given workspace.

    Args:
        workspace_id: The workspace database ID.
        body: Board creation data including name and optional description.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The created board data.

    Raises:
        HTTPException: If the user is not a member of the workspace.
    """
    await require_workspace_member(workspace_id, current_user, db)

    board = Board(
        name=body.name,
        description=body.description,
        workspace_id=workspace_id,
    )
    db.add(board)
    await db.flush()
    await db.refresh(board)

    return BoardResponse.model_validate(board)


@router.get(
    "/workspaces/{workspace_id}/boards",
    response_model=list[BoardResponse],
)
async def list_boards(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BoardResponse]:
    """List all boards in a workspace.

    Args:
        workspace_id: The workspace database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        A list of boards belonging to the workspace.

    Raises:
        HTTPException: If the user is not a member of the workspace.
    """
    await require_workspace_member(workspace_id, current_user, db)

    result = await db.execute(
        select(Board)
        .where(Board.workspace_id == workspace_id)
        .order_by(Board.created_at.desc())
    )
    boards = result.scalars().all()
    return [BoardResponse.model_validate(b) for b in boards]


@router.get(
    "/boards/{board_id}",
    response_model=BoardDetailResponse,
)
async def get_board(
    board_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BoardDetailResponse:
    """Get a board by ID, including its columns and cards.

    Args:
        board_id: The board database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The board data with nested columns and cards.

    Raises:
        HTTPException: If the board is not found or the user lacks access.
    """
    board = await _get_board_or_404(board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    columns_data = [
        ColumnWithCardsResponse.model_validate(col) for col in board.columns
    ]

    return BoardDetailResponse(
        id=board.id,
        workspace_id=board.workspace_id,
        name=board.name,
        description=board.description,
        created_at=board.created_at,
        updated_at=board.updated_at,
        columns=columns_data,
    )


@router.put(
    "/boards/{board_id}",
    response_model=BoardResponse,
)
async def update_board(
    board_id: int,
    body: BoardUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BoardResponse:
    """Update a board's name and/or description.

    Args:
        board_id: The board database ID.
        body: Fields to update.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Returns:
        The updated board data.

    Raises:
        HTTPException: If the board is not found or the user lacks access.
    """
    board = await _get_board_or_404(board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    if body.name is not None:
        board.name = body.name
    if body.description is not None:
        board.description = body.description

    await db.flush()
    await db.refresh(board)
    return BoardResponse.model_validate(board)


@router.delete(
    "/boards/{board_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_board(
    board_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a board and all its columns and cards.

    Args:
        board_id: The board database ID.
        current_user: The authenticated user (must be a workspace member).
        db: The async database session.

    Raises:
        HTTPException: If the board is not found or the user lacks access.
    """
    board = await _get_board_or_404(board_id, db)
    await require_workspace_member(board.workspace_id, current_user, db)

    await db.delete(board)
    await db.flush()


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
