"""JWT token creation, decoding, and password hashing utilities.

All token operations use python-jose with HS256 (configurable via settings).
Password hashing uses passlib with the bcrypt scheme.
"""

from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt_lib
from fastapi import HTTPException, status
from jose import JWTError, jwt

# ---------------------------------------------------------------------------
# passlib + bcrypt >= 4.1 compatibility shim
#
# bcrypt 4.1+ removed ``bcrypt.__about__`` and bcrypt 4.2+ raises
# ``ValueError`` for passwords exceeding 72 bytes.  passlib's self-test
# probes with a long secret, so we patch ``hashpw`` / ``checkpw`` to
# silently truncate at the bcrypt limit (72 bytes) and inject the
# missing ``__about__`` attribute.
# ---------------------------------------------------------------------------
_BCRYPT_MAX_LEN = 72

if not hasattr(_bcrypt_lib, "__about__"):  # pragma: no cover
    _bcrypt_lib.__about__ = type(  # type: ignore[attr-defined]
        "__about__", (), {"__version__": _bcrypt_lib.__version__}
    )()

_orig_hashpw = _bcrypt_lib.hashpw
_orig_checkpw = _bcrypt_lib.checkpw


def _safe_hashpw(  # type: ignore[no-untyped-def]
    password: bytes, salt: bytes, **kwargs
) -> bytes:
    """Wrap ``bcrypt.hashpw`` to truncate passwords longer than 72 bytes."""
    if isinstance(password, memoryview):
        password = bytes(password)
    return _orig_hashpw(password[:_BCRYPT_MAX_LEN], salt, **kwargs)


def _safe_checkpw(  # type: ignore[no-untyped-def]
    password: bytes, hashed_password: bytes, **kwargs
) -> bool:
    """Wrap ``bcrypt.checkpw`` to truncate passwords longer than 72 bytes."""
    if isinstance(password, memoryview):
        password = bytes(password)
    return _orig_checkpw(password[:_BCRYPT_MAX_LEN], hashed_password, **kwargs)


_bcrypt_lib.hashpw = _safe_hashpw  # type: ignore[assignment]
_bcrypt_lib.checkpw = _safe_checkpw  # type: ignore[assignment]

from passlib.context import CryptContext  # noqa: E402

from app.config import get_settings  # noqa: E402

# ---------------------------------------------------------------------------
# Password hashing (passlib bcrypt)
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Hash a plaintext password using passlib bcrypt.

    Args:
        plain: The raw password string.

    Returns:
        The bcrypt-hashed password as a string.
    """
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Args:
        plain: The raw password string.
        hashed: The bcrypt hash to compare against.

    Returns:
        True if the password matches, False otherwise.
    """
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT creation
# ---------------------------------------------------------------------------

_REFRESH_TOKEN_EXPIRE_DAYS = 7


def create_access_token(user_id: int, email: str) -> str:
    """Create a JWT access token with a configurable expiry.

    The default lifetime is controlled by ``ACCESS_TOKEN_EXPIRE_MINUTES``
    in application settings (30 minutes by default).

    Args:
        user_id: The database primary-key of the user.
        email: The user's email address (embedded in the token payload).

    Returns:
        An encoded JWT string.
    """
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes,
    )
    payload: dict[str, object] = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    """Create a JWT refresh token with a 7-day expiry.

    Args:
        user_id: The database primary-key of the user.

    Returns:
        An encoded JWT string.
    """
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# ---------------------------------------------------------------------------
# JWT decoding / validation
# ---------------------------------------------------------------------------


def decode_token(token: str) -> dict[str, object]:
    """Decode and validate a JWT token.

    Raises ``HTTPException(401)`` when the token is expired, malformed,
    or missing the ``sub`` claim.

    Args:
        token: The raw JWT string (without the "Bearer " prefix).

    Returns:
        The decoded token payload as a dictionary.

    Raises:
        HTTPException: 401 if the token is invalid or expired.
    """
    settings = get_settings()
    try:
        payload: dict[str, object] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("sub") is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
        ) from exc
