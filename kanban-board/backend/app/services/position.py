"""Fractional indexing for drag-and-drop ordering of columns and cards."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Default gap between positions for new and rebalanced items
_POSITION_GAP = 1024.0


def calculate_position(
    prev_pos: float | None,
    next_pos: float | None,
) -> float:
    """Calculate a position value between two adjacent items.

    - If both are None (first item), returns _POSITION_GAP (1024.0).
    - If prev_pos is None (insert at start), returns next_pos / 2.
    - If next_pos is None (append at end), returns prev_pos + _POSITION_GAP.
    - Otherwise, returns the midpoint.
    """
    if prev_pos is None and next_pos is None:
        return _POSITION_GAP
    if prev_pos is None:
        return next_pos / 2  # type: ignore[operator]
    if next_pos is None:
        return prev_pos + _POSITION_GAP
    return (prev_pos + next_pos) / 2


async def rebalance_positions(
    db: AsyncSession,
    model_class: type,
    parent_fk_column: str,
    parent_id: int,
) -> list:
    """Renumber all items under a parent at even _POSITION_GAP increments.

    Returns the list of rebalanced items ordered by their new position.
    """
    fk_attr = getattr(model_class, parent_fk_column)
    result = await db.execute(
        select(model_class)
        .where(fk_attr == parent_id)
        .order_by(model_class.position)
    )
    items = result.scalars().all()

    for idx, item in enumerate(items):
        new_pos = (idx + 1) * _POSITION_GAP
        if item.position != new_pos:
            await db.execute(
                update(model_class)
                .where(model_class.id == item.id)
                .values(position=new_pos)
            )
            item.position = new_pos

    return list(items)
