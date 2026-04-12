"""Tests for authentication service — password hashing and JWT tokens."""

import pytest
from fastapi import HTTPException

from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_password_returns_bcrypt_hash() -> None:
    """hash_password returns a bcrypt-formatted string."""
    hashed = hash_password("my-secret-password")
    assert hashed.startswith("$2")
    assert hashed != "my-secret-password"


def test_verify_password_correct() -> None:
    """verify_password returns True for a matching plaintext/hash pair."""
    hashed = hash_password("correct-password")
    assert verify_password("correct-password", hashed) is True


def test_verify_password_incorrect() -> None:
    """verify_password returns False for a mismatched pair."""
    hashed = hash_password("correct-password")
    assert verify_password("wrong-password", hashed) is False


def test_create_access_token_returns_string() -> None:
    """create_access_token returns a JWT string."""
    token = create_access_token(user_id=42, email="test@example.com")
    assert isinstance(token, str)
    assert len(token) > 0


def test_create_refresh_token_returns_string() -> None:
    """create_refresh_token returns a JWT string."""
    token = create_refresh_token(user_id=42)
    assert isinstance(token, str)
    assert len(token) > 0


def test_decode_access_token_roundtrip() -> None:
    """A token created by create_access_token can be decoded back."""
    token = create_access_token(user_id=7, email="alice@test.com")
    payload = decode_token(token)
    assert payload["sub"] == "7"
    assert payload["email"] == "alice@test.com"
    assert payload["type"] == "access"


def test_decode_refresh_token_roundtrip() -> None:
    """A token created by create_refresh_token can be decoded back."""
    token = create_refresh_token(user_id=99)
    payload = decode_token(token)
    assert payload["sub"] == "99"
    assert payload["type"] == "refresh"


def test_decode_invalid_token_raises_401() -> None:
    """decode_token raises HTTPException for a garbage token."""
    with pytest.raises(HTTPException) as exc_info:
        decode_token("not-a-valid-jwt")
    assert exc_info.value.status_code == 401


def test_access_and_refresh_tokens_differ() -> None:
    """Access and refresh tokens for the same user are different."""
    access = create_access_token(user_id=1, email="a@b.com")
    refresh = create_refresh_token(user_id=1)
    assert access != refresh
