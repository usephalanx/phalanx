"""Tests for SQLAlchemy models — User, Workspace, and WorkspaceMember."""

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember


@pytest.mark.asyncio
class TestUserModel:
    """Tests for the User ORM model."""

    async def test_create_user_minimal(
        self,
        db_session: AsyncSession,
    ) -> None:
        """A user can be created with only required fields."""
        user = User(
            email="alice@example.com",
            hashed_password="hashed_pw",
            display_name="Alice",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.id is not None
        assert user.email == "alice@example.com"
        assert user.hashed_password == "hashed_pw"
        assert user.display_name == "Alice"
        assert isinstance(user.created_at, datetime)

    async def test_user_email_unique_constraint(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Duplicate emails are rejected by the unique constraint."""
        user1 = User(
            email="dup@example.com",
            hashed_password="hash1",
            display_name="User One",
        )
        db_session.add(user1)
        await db_session.flush()

        user2 = User(
            email="dup@example.com",
            hashed_password="hash2",
            display_name="User Two",
        )
        db_session.add(user2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_user_tablename(self) -> None:
        """User model maps to the 'user' table."""
        assert User.__tablename__ == "user"

    async def test_user_avatar_url_nullable(
        self,
        db_session: AsyncSession,
    ) -> None:
        """avatar_url defaults to None when not provided."""
        user = User(
            email="avatar@example.com",
            hashed_password="hash",
            display_name="Avatar Test",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.avatar_url is None

    async def test_user_display_name_server_default(
        self,
        db_session: AsyncSession,
    ) -> None:
        """display_name has a server_default of empty string."""
        # Create via raw insert to bypass ORM default
        result = await db_session.execute(
            User.__table__.insert().values(
                email="noname@example.com",
                hashed_password="hash",
            )
        )
        await db_session.flush()

        row = await db_session.execute(
            select(User).where(User.email == "noname@example.com")
        )
        user = row.scalar_one()
        assert user.display_name == ""


@pytest.mark.asyncio
class TestWorkspaceModel:
    """Tests for the Workspace ORM model."""

    async def test_create_workspace(
        self,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        """A workspace can be created with required fields."""
        ws = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            owner_id=sample_user.id,
        )
        db_session.add(ws)
        await db_session.flush()

        assert ws.id is not None
        assert ws.name == "Test Workspace"
        assert ws.slug == "test-workspace"
        assert ws.owner_id == sample_user.id
        assert isinstance(ws.created_at, datetime)

    async def test_workspace_slug_unique(
        self,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        """Duplicate slugs are rejected by the unique constraint."""
        ws1 = Workspace(
            name="First", slug="same-slug", owner_id=sample_user.id
        )
        db_session.add(ws1)
        await db_session.flush()

        ws2 = Workspace(
            name="Second", slug="same-slug", owner_id=sample_user.id
        )
        db_session.add(ws2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_workspace_owner_relationship(
        self,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        """Workspace.owner resolves to the correct User."""
        ws = Workspace(
            name="Owned WS",
            slug="owned-ws",
            owner_id=sample_user.id,
        )
        db_session.add(ws)
        await db_session.flush()

        result = await db_session.execute(
            select(Workspace).where(Workspace.id == ws.id)
        )
        loaded = result.scalar_one()
        assert loaded.owner.id == sample_user.id
        assert loaded.owner.email == sample_user.email

    async def test_workspace_tablename(self) -> None:
        """Workspace model maps to the 'workspace' table."""
        assert Workspace.__tablename__ == "workspace"

    async def test_workspace_cascade_delete_members(
        self,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        """Deleting a workspace cascades to its members."""
        ws = Workspace(
            name="Cascade WS",
            slug="cascade-ws",
            owner_id=sample_user.id,
        )
        db_session.add(ws)
        await db_session.flush()

        member = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=ws.id,
            role="owner",
        )
        db_session.add(member)
        await db_session.flush()

        await db_session.delete(ws)
        await db_session.flush()

        result = await db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws.id
            )
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
class TestWorkspaceMemberModel:
    """Tests for the WorkspaceMember ORM model."""

    async def test_create_membership(
        self,
        db_session: AsyncSession,
        sample_user: User,
        sample_workspace: Workspace,
    ) -> None:
        """A workspace membership can be created."""
        member = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
            role="owner",
        )
        db_session.add(member)
        await db_session.flush()

        assert member.user_id == sample_user.id
        assert member.workspace_id == sample_workspace.id
        assert member.role == "owner"
        assert isinstance(member.joined_at, datetime)

    async def test_membership_composite_pk_unique(
        self,
        db_session: AsyncSession,
        sample_user: User,
        sample_workspace: Workspace,
    ) -> None:
        """Duplicate (user_id, workspace_id) pairs are rejected."""
        m1 = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
            role="member",
        )
        db_session.add(m1)
        await db_session.flush()

        m2 = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
            role="admin",
        )
        db_session.add(m2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_membership_default_role(
        self,
        db_session: AsyncSession,
        sample_user: User,
        sample_workspace: Workspace,
    ) -> None:
        """Default role is 'member' when not specified."""
        m = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
        )
        db_session.add(m)
        await db_session.flush()

        assert m.role == "member"

    async def test_membership_user_relationship(
        self,
        db_session: AsyncSession,
        sample_user: User,
        sample_workspace: Workspace,
    ) -> None:
        """WorkspaceMember.user resolves to the correct User."""
        m = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
            role="member",
        )
        db_session.add(m)
        await db_session.flush()

        result = await db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == sample_user.id,
                WorkspaceMember.workspace_id == sample_workspace.id,
            )
        )
        loaded = result.scalar_one()
        assert loaded.user.email == sample_user.email

    async def test_membership_workspace_relationship(
        self,
        db_session: AsyncSession,
        sample_user: User,
        sample_workspace: Workspace,
    ) -> None:
        """WorkspaceMember.workspace resolves to the correct Workspace."""
        m = WorkspaceMember(
            user_id=sample_user.id,
            workspace_id=sample_workspace.id,
            role="member",
        )
        db_session.add(m)
        await db_session.flush()

        result = await db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == sample_user.id,
                WorkspaceMember.workspace_id == sample_workspace.id,
            )
        )
        loaded = result.scalar_one()
        assert loaded.workspace.name == sample_workspace.name

    async def test_membership_tablename(self) -> None:
        """WorkspaceMember maps to the 'workspace_member' table."""
        assert WorkspaceMember.__tablename__ == "workspace_member"

    async def test_valid_roles(
        self,
        db_session: AsyncSession,
        sample_workspace: Workspace,
    ) -> None:
        """All four valid roles can be assigned."""
        valid_roles = ["owner", "admin", "member", "viewer"]
        for i, role in enumerate(valid_roles):
            user = User(
                email=f"role{i}@example.com",
                hashed_password="hash",
                display_name=f"Role {role}",
            )
            db_session.add(user)
            await db_session.flush()

            m = WorkspaceMember(
                user_id=user.id,
                workspace_id=sample_workspace.id,
                role=role,
            )
            db_session.add(m)
            await db_session.flush()
            assert m.role == role


@pytest.mark.asyncio
class TestBaseModel:
    """Tests for the Base declarative model."""

    async def test_base_has_metadata(self) -> None:
        """Base exposes a metadata attribute for table introspection."""
        assert hasattr(Base, "metadata")
        table_names = set(Base.metadata.tables.keys())
        assert "user" in table_names
        assert "workspace" in table_names
        assert "workspace_member" in table_names

    async def test_all_models_registered(self) -> None:
        """All expected models are registered with Base.metadata."""
        expected = {"user", "workspace", "workspace_member", "board", "column", "card"}
        actual = set(Base.metadata.tables.keys())
        assert expected.issubset(actual)
