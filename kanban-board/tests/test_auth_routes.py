"""Tests for auth router — register, login, refresh, and me endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    hash_password,
)


# --- POST /api/auth/register ---


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient) -> None:
    """Registration with valid data returns 201 with tokens and user."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "new@example.com",
            "password": "secret123",
            "display_name": "New User",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "new@example.com"
    assert data["user"]["display_name"] == "New User"
    assert "id" in data["user"]
    assert "created_at" in data["user"]


@pytest.mark.asyncio
async def test_register_duplicate_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Registration with an already-registered email returns 409."""
    user = User(
        email="dup@example.com",
        hashed_password=hash_password("password"),
        display_name="Existing",
    )
    db_session.add(user)
    await db_session.flush()

    response = await client.post(
        "/api/auth/register",
        json={
            "email": "dup@example.com",
            "password": "secret123",
            "display_name": "Another",
        },
    )
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_short_password(client: AsyncClient) -> None:
    """Registration with a password shorter than 6 chars returns 422."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "short@example.com",
            "password": "abc",
            "display_name": "Short",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email(client: AsyncClient) -> None:
    """Registration with an invalid email returns 422."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "not-an-email",
            "password": "secret123",
            "display_name": "Bad Email",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_empty_display_name(client: AsyncClient) -> None:
    """Registration with an empty display_name returns 422."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "empty@example.com",
            "password": "secret123",
            "display_name": "",
        },
    )
    assert response.status_code == 422


# --- POST /api/auth/login ---


@pytest.mark.asyncio
async def test_login_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Login with valid credentials returns tokens and user."""
    user = User(
        email="login@example.com",
        hashed_password=hash_password("mypassword"),
        display_name="Login User",
    )
    db_session.add(user)
    await db_session.flush()

    response = await client.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "mypassword"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "login@example.com"


@pytest.mark.asyncio
async def test_login_wrong_password(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Login with wrong password returns 401."""
    user = User(
        email="wrong@example.com",
        hashed_password=hash_password("correct"),
        display_name="Wrong",
    )
    db_session.add(user)
    await db_session.flush()

    response = await client.post(
        "/api/auth/login",
        json={"email": "wrong@example.com", "password": "incorrect"},
    )
    assert response.status_code == 401
    assert "Invalid email or password" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: AsyncClient) -> None:
    """Login with an unregistered email returns 401."""
    response = await client.post(
        "/api/auth/login",
        json={"email": "ghost@example.com", "password": "whatever"},
    )
    assert response.status_code == 401


# --- POST /api/auth/refresh ---


@pytest.mark.asyncio
async def test_refresh_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Refresh with a valid refresh token returns a new access token."""
    user = User(
        email="refresh@example.com",
        hashed_password=hash_password("pass"),
        display_name="Refresh",
    )
    db_session.add(user)
    await db_session.flush()

    refresh_token = create_refresh_token(user_id=user.id)

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_refresh_with_access_token_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Refresh rejects an access token (wrong type)."""
    user = User(
        email="badtype@example.com",
        hashed_password=hash_password("pass"),
        display_name="BadType",
    )
    db_session.add(user)
    await db_session.flush()

    access_token = create_access_token(user_id=user.id, email=user.email)

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": access_token},
    )
    assert response.status_code == 401
    assert "refresh token" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_missing_token(client: AsyncClient) -> None:
    """Refresh without a refresh_token returns 400."""
    response = await client.post("/api/auth/refresh", json={})
    assert response.status_code == 400
    assert "required" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_invalid_token(client: AsyncClient) -> None:
    """Refresh with a garbage token returns 401."""
    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": "not-a-real-jwt"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_deleted_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Refresh for a non-existent user returns 401."""
    refresh_token = create_refresh_token(user_id=99999)

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert response.status_code == 401


# --- GET /api/auth/me ---


@pytest.mark.asyncio
async def test_me_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET /me with a valid access token returns current user."""
    user = User(
        email="me@example.com",
        hashed_password=hash_password("pass"),
        display_name="Me User",
    )
    db_session.add(user)
    await db_session.flush()

    token = create_access_token(user_id=user.id, email=user.email)

    response = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "me@example.com"
    assert data["display_name"] == "Me User"
    assert data["id"] == user.id


@pytest.mark.asyncio
async def test_me_no_token(client: AsyncClient) -> None:
    """GET /me without an Authorization header returns 401."""
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_invalid_token(client: AsyncClient) -> None:
    """GET /me with an invalid token returns 401."""
    response = await client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer invalid-jwt"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_deleted_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET /me for a non-existent user returns 401."""
    token = create_access_token(user_id=99999, email="gone@example.com")

    response = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


# --- Integration: register then login then me ---


@pytest.mark.asyncio
async def test_full_auth_flow(client: AsyncClient) -> None:
    """Register, login, refresh, and access /me in sequence."""
    # Register
    reg_resp = await client.post(
        "/api/auth/register",
        json={
            "email": "flow@example.com",
            "password": "flowpass123",
            "display_name": "Flow User",
        },
    )
    assert reg_resp.status_code == 201
    reg_data = reg_resp.json()
    refresh_token = reg_data["refresh_token"]
    access_token = reg_data["access_token"]

    # Access /me with registration token
    me_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "flow@example.com"

    # Login with same credentials
    login_resp = await client.post(
        "/api/auth/login",
        json={"email": "flow@example.com", "password": "flowpass123"},
    )
    assert login_resp.status_code == 200

    # Refresh
    refresh_resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_resp.status_code == 200
    new_access = refresh_resp.json()["access_token"]

    # Access /me with refreshed token
    me_resp2 = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_access}"},
    )
    assert me_resp2.status_code == 200
    assert me_resp2.json()["email"] == "flow@example.com"
