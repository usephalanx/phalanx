"""Tests for JWT authentication endpoints — register, login, refresh, and me."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestRegister:
    """Tests for POST /api/auth/register."""

    async def test_register_success(self, client: AsyncClient) -> None:
        """Valid registration returns 201 with tokens and user data."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "new@example.com",
                "password": "strongpassword",
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

    async def test_register_duplicate_email(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Registering with an existing email returns 409."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "owner@example.com",
                "password": "anotherpassword",
                "display_name": "Duplicate",
            },
        )
        assert response.status_code == 409
        assert "already registered" in response.json()["detail"].lower()

    async def test_register_invalid_email(self, client: AsyncClient) -> None:
        """Invalid email format returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "not-an-email",
                "password": "strongpassword",
                "display_name": "Bad Email",
            },
        )
        assert response.status_code == 422

    async def test_register_short_password(self, client: AsyncClient) -> None:
        """Password shorter than 6 characters returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "short@example.com",
                "password": "abc",
                "display_name": "Short Pass",
            },
        )
        assert response.status_code == 422

    async def test_register_missing_display_name(self, client: AsyncClient) -> None:
        """Missing display_name returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "noname@example.com",
                "password": "strongpassword",
            },
        )
        assert response.status_code == 422

    async def test_register_empty_display_name(self, client: AsyncClient) -> None:
        """Empty display_name returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "empty@example.com",
                "password": "strongpassword",
                "display_name": "",
            },
        )
        assert response.status_code == 422

    async def test_register_password_is_hashed(self, client: AsyncClient) -> None:
        """Registered user's returned data does not expose raw password."""
        response = await client.post(
            "/api/auth/register",
            json={
                "email": "hash@example.com",
                "password": "mysecret123",
                "display_name": "Hashed",
            },
        )
        assert response.status_code == 201
        data = response.json()
        # Ensure no password field leaks in the user response
        assert "password" not in data["user"]
        assert "hashed_password" not in data["user"]

    async def test_register_tokens_are_valid_for_me(self, client: AsyncClient) -> None:
        """Access token from registration can be used to call /me."""
        reg = await client.post(
            "/api/auth/register",
            json={
                "email": "valid@example.com",
                "password": "strongpassword",
                "display_name": "Valid User",
            },
        )
        assert reg.status_code == 201
        token = reg.json()["access_token"]

        me_resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "valid@example.com"


@pytest.mark.asyncio
class TestLogin:
    """Tests for POST /api/auth/login."""

    async def test_login_success(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Valid credentials return tokens and user data."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "securepassword"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "owner@example.com"

    async def test_login_wrong_password(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Wrong password returns 401."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "wrongpassword"},
        )
        assert response.status_code == 401
        assert "invalid" in response.json()["detail"].lower()

    async def test_login_nonexistent_email(self, client: AsyncClient) -> None:
        """Non-existent email returns 401."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "ghost@example.com", "password": "anypassword"},
        )
        assert response.status_code == 401

    async def test_login_missing_email(self, client: AsyncClient) -> None:
        """Missing email field returns 422."""
        response = await client.post(
            "/api/auth/login",
            json={"password": "somepassword"},
        )
        assert response.status_code == 422

    async def test_login_missing_password(self, client: AsyncClient) -> None:
        """Missing password field returns 422."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "owner@example.com"},
        )
        assert response.status_code == 422

    async def test_login_token_works_for_me(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Access token from login can retrieve the user via /me."""
        login_resp = await client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "securepassword"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]

        me_resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "owner@example.com"


@pytest.mark.asyncio
class TestRefresh:
    """Tests for POST /api/auth/refresh."""

    async def test_refresh_success(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Valid refresh token returns a new access/refresh token pair."""
        response = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_refresh_new_token_works(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Refreshed access token can be used to call /me."""
        refresh_resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert refresh_resp.status_code == 200
        data = refresh_resp.json()
        new_token = data["access_token"]

        me_resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "owner@example.com"

    async def test_refresh_returns_usable_refresh_token(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """The new refresh token from a refresh call can itself be used to refresh again."""
        first_resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert first_resp.status_code == 200
        new_refresh = first_resp.json()["refresh_token"]

        second_resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        assert second_resp.status_code == 200
        assert "access_token" in second_resp.json()
        assert "refresh_token" in second_resp.json()

    async def test_refresh_with_access_token_fails(
        self,
        client: AsyncClient,
        registered_user: dict[str, Any],
    ) -> None:
        """Using an access token in the refresh endpoint returns 401."""
        response = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": registered_user["access_token"]},
        )
        assert response.status_code == 401
        assert "refresh" in response.json()["detail"].lower()

    async def test_refresh_with_invalid_token(self, client: AsyncClient) -> None:
        """Invalid JWT string returns 401."""
        response = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": "not.a.valid.jwt"},
        )
        assert response.status_code == 401

    async def test_refresh_missing_field(self, client: AsyncClient) -> None:
        """Missing refresh_token field returns 422."""
        response = await client.post(
            "/api/auth/refresh",
            json={},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestMe:
    """Tests for GET /api/auth/me."""

    async def test_me_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Authenticated user can retrieve their profile."""
        response = await client.get("/api/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "owner@example.com"
        assert data["display_name"] == "Owner User"
        assert "id" in data
        assert "created_at" in data

    async def test_me_no_token(self, client: AsyncClient) -> None:
        """Request without Authorization header returns 401."""
        response = await client.get("/api/auth/me")
        assert response.status_code == 401

    async def test_me_invalid_token(self, client: AsyncClient) -> None:
        """Request with a malformed token returns 401."""
        response = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer garbage.token.value"},
        )
        assert response.status_code == 401

    async def test_me_expired_token_format(self, client: AsyncClient) -> None:
        """Request with a completely bogus Bearer value returns 401."""
        response = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer xyz"},
        )
        assert response.status_code == 401

    async def test_me_does_not_expose_password(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """The /me response never contains password-related fields."""
        response = await client.get("/api/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "password" not in data
        assert "hashed_password" not in data


@pytest.mark.asyncio
class TestCORSMiddleware:
    """Verify CORS headers are present on responses."""

    async def test_cors_headers_on_preflight(self, client: AsyncClient) -> None:
        """OPTIONS request to auth endpoint returns CORS headers."""
        response = await client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        # FastAPI CORS middleware returns 200 for allowed origins
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers

    async def test_cors_headers_on_regular_request(
        self,
        client: AsyncClient,
    ) -> None:
        """Regular request with Origin header gets CORS headers back."""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers
