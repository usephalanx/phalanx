"""Unit tests for app.auth.jwt — JWT creation, decoding, and password hashing."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from jose import jwt

from app.auth.jwt import (
    _REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import get_settings

settings = get_settings()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestHashPassword:
    """Tests for hash_password()."""

    def test_returns_string(self) -> None:
        """hash_password returns a non-empty string."""
        hashed = hash_password("mypassword")
        assert isinstance(hashed, str)
        assert len(hashed) > 0

    def test_hash_differs_from_plain(self) -> None:
        """The hash should not equal the plaintext."""
        plain = "secret123"
        hashed = hash_password(plain)
        assert hashed != plain

    def test_different_calls_produce_different_hashes(self) -> None:
        """Two calls with the same input produce different hashes (random salt)."""
        h1 = hash_password("samepassword")
        h2 = hash_password("samepassword")
        assert h1 != h2


class TestVerifyPassword:
    """Tests for verify_password()."""

    def test_correct_password_returns_true(self) -> None:
        """verify_password returns True for the correct plaintext."""
        hashed = hash_password("goodpassword")
        assert verify_password("goodpassword", hashed) is True

    def test_wrong_password_returns_false(self) -> None:
        """verify_password returns False for an incorrect plaintext."""
        hashed = hash_password("goodpassword")
        assert verify_password("wrongpassword", hashed) is False

    def test_empty_password(self) -> None:
        """Empty string can be hashed and verified."""
        hashed = hash_password("")
        assert verify_password("", hashed) is True
        assert verify_password("notempty", hashed) is False


# ---------------------------------------------------------------------------
# Access token
# ---------------------------------------------------------------------------


class TestCreateAccessToken:
    """Tests for create_access_token()."""

    def test_returns_string(self) -> None:
        """create_access_token returns a non-empty JWT string."""
        token = create_access_token(user_id=1, email="test@example.com")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_payload_contains_required_claims(self) -> None:
        """The decoded payload contains sub, email, exp, and type."""
        token = create_access_token(user_id=42, email="alice@example.com")
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        assert payload["sub"] == "42"
        assert payload["email"] == "alice@example.com"
        assert payload["type"] == "access"
        assert "exp" in payload

    def test_expiry_matches_settings(self) -> None:
        """The token expiry should be approximately ACCESS_TOKEN_EXPIRE_MINUTES from now."""
        before = datetime.now(timezone.utc).replace(microsecond=0)
        token = create_access_token(user_id=1, email="t@t.com")
        after = datetime.now(timezone.utc) + timedelta(seconds=1)

        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

        expected_min = before + timedelta(minutes=settings.access_token_expire_minutes)
        expected_max = after + timedelta(minutes=settings.access_token_expire_minutes)
        assert expected_min <= exp <= expected_max


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------


class TestCreateRefreshToken:
    """Tests for create_refresh_token()."""

    def test_returns_string(self) -> None:
        """create_refresh_token returns a non-empty JWT string."""
        token = create_refresh_token(user_id=1)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_payload_contains_required_claims(self) -> None:
        """The decoded payload contains sub, exp, and type=refresh."""
        token = create_refresh_token(user_id=7)
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        assert payload["sub"] == "7"
        assert payload["type"] == "refresh"
        assert "exp" in payload
        # Refresh tokens should NOT contain email
        assert "email" not in payload

    def test_expiry_is_7_days(self) -> None:
        """The refresh token should expire in approximately 7 days."""
        before = datetime.now(timezone.utc).replace(microsecond=0)
        token = create_refresh_token(user_id=1)
        after = datetime.now(timezone.utc) + timedelta(seconds=1)

        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

        expected_min = before + timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS)
        expected_max = after + timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS)
        assert expected_min <= exp <= expected_max


# ---------------------------------------------------------------------------
# Token decoding
# ---------------------------------------------------------------------------


class TestDecodeToken:
    """Tests for decode_token()."""

    def test_decode_valid_access_token(self) -> None:
        """A valid access token decodes successfully."""
        token = create_access_token(user_id=10, email="decode@example.com")
        payload = decode_token(token)
        assert payload["sub"] == "10"
        assert payload["email"] == "decode@example.com"
        assert payload["type"] == "access"

    def test_decode_valid_refresh_token(self) -> None:
        """A valid refresh token decodes successfully."""
        token = create_refresh_token(user_id=20)
        payload = decode_token(token)
        assert payload["sub"] == "20"
        assert payload["type"] == "refresh"

    def test_decode_invalid_token_raises_401(self) -> None:
        """Malformed JWT raises HTTPException with 401 status."""
        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.valid.jwt")
        assert exc_info.value.status_code == 401

    def test_decode_expired_token_raises_401(self) -> None:
        """An expired token raises HTTPException with 401 status."""
        expired_payload: dict[str, Any] = {
            "sub": "99",
            "email": "expired@example.com",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "type": "access",
        }
        token = jwt.encode(
            expired_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower() or "invalid" in exc_info.value.detail.lower()

    def test_decode_token_missing_sub_raises_401(self) -> None:
        """A token without a 'sub' claim raises HTTPException with 401."""
        no_sub_payload: dict[str, Any] = {
            "email": "nosub@example.com",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
        }
        token = jwt.encode(
            no_sub_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401
        assert "missing subject" in exc_info.value.detail.lower()

    def test_decode_wrong_secret_raises_401(self) -> None:
        """A token signed with a different secret raises HTTPException."""
        payload: dict[str, Any] = {
            "sub": "1",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
        }
        token = jwt.encode(payload, "wrong-secret", algorithm=settings.jwt_algorithm)
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Schema aliases
# ---------------------------------------------------------------------------


class TestSchemaAliases:
    """Verify that UserCreate/UserLogin schemas exist and work correctly."""

    def test_user_create_schema(self) -> None:
        """UserCreate accepts email, password, and display_name."""
        from app.schemas.auth import UserCreate

        user = UserCreate(
            email="new@example.com",
            password="strong123",
            display_name="New User",
        )
        assert user.email == "new@example.com"
        assert user.password == "strong123"
        assert user.display_name == "New User"

    def test_user_create_rejects_short_password(self) -> None:
        """UserCreate rejects passwords shorter than 6 characters."""
        from pydantic import ValidationError

        from app.schemas.auth import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(
                email="bad@example.com",
                password="abc",
                display_name="Bad",
            )

    def test_user_create_rejects_invalid_email(self) -> None:
        """UserCreate rejects malformed email addresses."""
        from pydantic import ValidationError

        from app.schemas.auth import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(
                email="not-an-email",
                password="strongpw",
                display_name="Nomail",
            )

    def test_user_create_rejects_empty_display_name(self) -> None:
        """UserCreate rejects empty display_name."""
        from pydantic import ValidationError

        from app.schemas.auth import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(
                email="noname@example.com",
                password="strongpw",
                display_name="",
            )

    def test_user_login_schema(self) -> None:
        """UserLogin accepts email and password."""
        from app.schemas.auth import UserLogin

        login = UserLogin(email="user@example.com", password="pw123456")
        assert login.email == "user@example.com"
        assert login.password == "pw123456"

    def test_user_login_rejects_invalid_email(self) -> None:
        """UserLogin rejects malformed email addresses."""
        from pydantic import ValidationError

        from app.schemas.auth import UserLogin

        with pytest.raises(ValidationError):
            UserLogin(email="bad", password="pw123456")

    def test_token_response_schema(self) -> None:
        """TokenResponse includes access_token, refresh_token, token_type, and user."""
        from app.schemas.auth import TokenResponse, UserResponse

        user_resp = UserResponse(
            id=1,
            email="t@t.com",
            display_name="T",
            created_at=datetime.now(timezone.utc),
        )
        token_resp = TokenResponse(
            access_token="abc",
            refresh_token="def",
            user=user_resp,
        )
        assert token_resp.access_token == "abc"
        assert token_resp.refresh_token == "def"
        assert token_resp.token_type == "bearer"
        assert token_resp.user.email == "t@t.com"

    def test_backward_compat_aliases(self) -> None:
        """RegisterRequest and LoginRequest are aliases for UserCreate and UserLogin."""
        from app.schemas.auth import (
            LoginRequest,
            RegisterRequest,
            UserCreate,
            UserLogin,
        )

        assert RegisterRequest is UserCreate
        assert LoginRequest is UserLogin


# ---------------------------------------------------------------------------
# Re-export paths
# ---------------------------------------------------------------------------


class TestReExports:
    """Verify backward-compatible import paths still work."""

    def test_services_auth_exports(self) -> None:
        """app.services.auth re-exports all JWT functions."""
        from app.services.auth import (  # noqa: F401
            create_access_token,
            create_refresh_token,
            decode_token,
            hash_password,
            verify_password,
        )

    def test_services_permissions_exports_get_current_user(self) -> None:
        """app.services.permissions re-exports get_current_user."""
        from app.services.permissions import get_current_user  # noqa: F401

    def test_auth_package_exports(self) -> None:
        """app.auth package exports all public functions."""
        from app.auth import (  # noqa: F401
            create_access_token,
            create_refresh_token,
            decode_token,
            get_current_user,
            hash_password,
            verify_password,
        )
