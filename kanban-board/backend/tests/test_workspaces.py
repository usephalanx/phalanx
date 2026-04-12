"""Tests for workspace CRUD and member management endpoints."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestCreateWorkspace:
    """Tests for POST /api/workspaces."""

    async def test_create_workspace_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Creating a workspace returns 201 with workspace data."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "My Workspace", "slug": "my-workspace"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Workspace"
        assert data["slug"] == "my-workspace"
        assert "id" in data
        assert "owner_id" in data
        assert "created_at" in data

    async def test_create_workspace_auto_adds_owner_member(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Creator is automatically added as an owner member."""
        ws = await client.post(
            "/api/workspaces",
            json={"name": "Owned WS", "slug": "owned-ws"},
            headers=auth_headers,
        )
        assert ws.status_code == 201
        ws_id = ws.json()["id"]

        members = await client.get(
            f"/api/workspaces/{ws_id}/members",
            headers=auth_headers,
        )
        assert members.status_code == 200
        member_list = members.json()
        assert len(member_list) == 1
        assert member_list[0]["role"] == "owner"

    async def test_create_workspace_duplicate_slug(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Duplicate slug returns 409."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "Another", "slug": workspace["slug"]},
            headers=auth_headers,
        )
        assert response.status_code == 409

    async def test_create_workspace_invalid_slug(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Invalid slug pattern returns 422."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "Bad Slug", "slug": "Has Spaces!"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_create_workspace_unauthenticated(
        self,
        client: AsyncClient,
    ) -> None:
        """Unauthenticated request returns 401."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "No Auth", "slug": "no-auth"},
        )
        assert response.status_code == 401

    async def test_create_workspace_name_only_auto_slug(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Creating a workspace with only name auto-generates a slug."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "Auto Slug Workspace"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Auto Slug Workspace"
        assert data["slug"] == "auto-slug-workspace"

    async def test_create_workspace_auto_slug_special_chars(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Auto-generated slug strips special characters and collapses hyphens."""
        response = await client.post(
            "/api/workspaces",
            json={"name": "Hello!! World  @#$  Test"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["slug"] == "hello-world-test"

    async def test_create_workspace_auto_slug_duplicate(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Auto-generated slug that collides returns 409."""
        # First workspace
        resp1 = await client.post(
            "/api/workspaces",
            json={"name": "Duplicate"},
            headers=auth_headers,
        )
        assert resp1.status_code == 201
        assert resp1.json()["slug"] == "duplicate"

        # Second workspace with the same name → same auto-slug → conflict
        resp2 = await client.post(
            "/api/workspaces",
            json={"name": "Duplicate"},
            headers=auth_headers,
        )
        assert resp2.status_code == 409


@pytest.mark.asyncio
class TestListWorkspaces:
    """Tests for GET /api/workspaces."""

    async def test_list_workspaces_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """User with no workspaces gets an empty list."""
        response = await client.get("/api/workspaces", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["workspaces"] == []
        assert data["count"] == 0

    async def test_list_workspaces_returns_user_workspaces(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """User sees workspaces they are a member of."""
        response = await client.get("/api/workspaces", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["workspaces"]) == 1
        assert data["workspaces"][0]["slug"] == "test-workspace"

    async def test_list_workspaces_excludes_non_member(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """User does not see workspaces they are not a member of."""
        response = await client.get("/api/workspaces", headers=second_auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["workspaces"] == []
        assert data["count"] == 0

    async def test_list_workspaces_multiple(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """User sees multiple workspaces with correct count."""
        await client.post(
            "/api/workspaces",
            json={"name": "Workspace A", "slug": "ws-a"},
            headers=auth_headers,
        )
        await client.post(
            "/api/workspaces",
            json={"name": "Workspace B", "slug": "ws-b"},
            headers=auth_headers,
        )

        response = await client.get("/api/workspaces", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["workspaces"]) == 2


@pytest.mark.asyncio
class TestGetWorkspace:
    """Tests for GET /api/workspaces/{id}."""

    async def test_get_workspace_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Member can retrieve a workspace by ID."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["id"] == workspace["id"]

    async def test_get_workspace_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-existent workspace returns 404."""
        response = await client.get("/api/workspaces/99999", headers=auth_headers)
        assert response.status_code == 404

    async def test_get_workspace_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-member cannot view a workspace."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestUpdateWorkspace:
    """Tests for PUT /api/workspaces/{id}."""

    async def test_update_workspace_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Owner can update the workspace name."""
        response = await client.put(
            f"/api/workspaces/{workspace['id']}",
            json={"name": "Updated Name"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"

    async def test_update_workspace_non_admin_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-admin member cannot update the workspace."""
        # Add second user as regular member
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com", "role": "member"},
            headers=auth_headers,
        )

        response = await client.put(
            f"/api/workspaces/{workspace['id']}",
            json={"name": "Hacked"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestDeleteWorkspace:
    """Tests for DELETE /api/workspaces/{id}."""

    async def test_delete_workspace_owner_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Owner can delete the workspace."""
        response = await client.delete(
            f"/api/workspaces/{workspace['id']}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        # Verify it's gone
        get_response = await client.get(
            f"/api/workspaces/{workspace['id']}",
            headers=auth_headers,
        )
        assert get_response.status_code == 404

    async def test_delete_workspace_non_owner_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-owner admin cannot delete the workspace."""
        # Add second user as admin
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com", "role": "admin"},
            headers=auth_headers,
        )

        response = await client.delete(
            f"/api/workspaces/{workspace['id']}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_delete_workspace_unauthenticated(
        self,
        client: AsyncClient,
        workspace: dict[str, Any],
    ) -> None:
        """Unauthenticated request to delete workspace returns 401."""
        response = await client.delete(
            f"/api/workspaces/{workspace['id']}",
        )
        assert response.status_code == 401


@pytest.mark.asyncio
class TestAddMember:
    """Tests for POST /api/workspaces/{id}/members."""

    async def test_add_member_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        workspace: dict[str, Any],
    ) -> None:
        """Owner can add a member by email."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com", "role": "member"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "member@example.com"
        assert data["role"] == "member"
        assert "joined_at" in data

    async def test_add_member_duplicate(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        workspace: dict[str, Any],
    ) -> None:
        """Adding an existing member returns 409."""
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com"},
            headers=auth_headers,
        )
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com"},
            headers=auth_headers,
        )
        assert response.status_code == 409

    async def test_add_member_user_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Adding a non-existent user returns 404."""
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "nobody@example.com"},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_add_member_non_admin_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Regular member cannot add new members."""
        # Add second user as regular member
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com", "role": "member"},
            headers=auth_headers,
        )

        # Register a third user
        third = await client.post(
            "/api/auth/register",
            json={
                "email": "third@example.com",
                "password": "securepassword",
                "display_name": "Third User",
            },
        )
        assert third.status_code == 201

        # Second user (member) tries to add third user
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "third@example.com"},
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestListMembers:
    """Tests for GET /api/workspaces/{id}/members."""

    async def test_list_members_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        workspace: dict[str, Any],
    ) -> None:
        """Members can list all workspace members."""
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com"},
            headers=auth_headers,
        )

        response = await client.get(
            f"/api/workspaces/{workspace['id']}/members",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    async def test_list_members_non_member_forbidden(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-member cannot list members."""
        response = await client.get(
            f"/api/workspaces/{workspace['id']}/members",
            headers=second_auth_headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestRemoveMember:
    """Tests for DELETE /api/workspaces/{id}/members/{user_id}."""

    async def test_remove_member_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        workspace: dict[str, Any],
    ) -> None:
        """Owner can remove a member."""
        add_resp = await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com"},
            headers=auth_headers,
        )
        member_user_id = add_resp.json()["user_id"]

        response = await client.delete(
            f"/api/workspaces/{workspace['id']}/members/{member_user_id}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        # Verify member is removed
        members = await client.get(
            f"/api/workspaces/{workspace['id']}/members",
            headers=auth_headers,
        )
        assert len(members.json()) == 1

    async def test_remove_owner_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, Any],
        workspace: dict[str, Any],
    ) -> None:
        """Owner cannot be removed from the workspace."""
        owner_id = registered_user["user"]["id"]
        response = await client.delete(
            f"/api/workspaces/{workspace['id']}/members/{owner_id}",
            headers=auth_headers,
        )
        assert response.status_code == 400

    async def test_remove_member_non_owner_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_user: dict[str, Any],
        second_auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Non-owner cannot remove members."""
        # Add second user as admin
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "member@example.com", "role": "admin"},
            headers=auth_headers,
        )

        # Register a third user and add them
        third = await client.post(
            "/api/auth/register",
            json={
                "email": "third@example.com",
                "password": "securepassword",
                "display_name": "Third User",
            },
        )
        third_id = third.json()["user"]["id"]
        await client.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"email": "third@example.com", "role": "member"},
            headers=auth_headers,
        )

        # Admin tries to remove third — only owner can
        response = await client.delete(
            f"/api/workspaces/{workspace['id']}/members/{third_id}",
            headers=second_auth_headers,
        )
        assert response.status_code == 403

    async def test_remove_nonexistent_member(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        workspace: dict[str, Any],
    ) -> None:
        """Removing a user who is not a member returns 404."""
        response = await client.delete(
            f"/api/workspaces/{workspace['id']}/members/99999",
            headers=auth_headers,
        )
        assert response.status_code == 404
