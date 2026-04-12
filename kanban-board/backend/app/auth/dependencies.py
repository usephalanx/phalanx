"""FastAPI dependency that extracts and validates the Bearer token."""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import decode_token
from app.database import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate the current user from a Bearer token.

    This FastAPI dependency:
    1. Reads the ``Authorization: Bearer <token>`` header via OAuth2.
    2. Decodes and validates the JWT.
    3. Looks up the user in the database by the token's ``sub`` claim.

    Args:
        token: The JWT extracted from the Authorization header.
        db: The async database session (injected).

    Returns:
        The authenticated :class:`User` ORM instance.

    Raises:
        HTTPException: 401 if the token is invalid or the user is not found.
    """
    payload = decode_token(token)
    user_id = int(payload["sub"])  # type: ignore[arg-type]

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user
