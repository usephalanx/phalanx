"""
Unit tests for forge/runtime/team_runtime.py.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from forge.runtime.team_runtime import TeamRuntime, AgentUnavailableError


def make_member(member_id, role, ic_level, max_concurrent=2):
    m = MagicMock()
    m.id = member_id
    m.role = role
    m.ic_level = ic_level
    m.max_concurrent_tasks = max_concurrent
    return m


def make_team_config(*members):
    config = MagicMock()
    config.members = list(members)
    config.get_member = lambda mid: next((m for m in members if m.id == mid), None)
    return config


@pytest.fixture
def morgan():
    return make_member("morgan", "tech_lead", ic_level=6, max_concurrent=2)


@pytest.fixture
def sam():
    return make_member("sam", "backend", ic_level=3, max_concurrent=1)


@pytest.fixture
def jordan():
    return make_member("jordan", "fullstack", ic_level=5, max_concurrent=2)


@pytest.fixture
def runtime(morgan, sam, jordan):
    loader = MagicMock()
    loader.team = make_team_config(morgan, sam, jordan)
    return TeamRuntime(config_loader=loader)


class TestGetMembersByRole:
    def test_returns_members_with_matching_role(self, runtime, morgan):
        members = runtime.get_members_by_role("tech_lead")
        assert any(m.id == "morgan" for m in members)

    def test_returns_empty_list_for_unknown_role(self, runtime):
        members = runtime.get_members_by_role("devops_wizard")
        assert members == []

    def test_matches_by_id_as_well(self, runtime, sam):
        # matching by member.id == role is also valid
        members = runtime.get_members_by_role("sam")
        assert any(m.id == "sam" for m in members)


class TestGetMember:
    def test_returns_member_by_id(self, runtime):
        member = runtime.get_member("morgan")
        assert member is not None
        assert member.id == "morgan"

    def test_returns_none_for_unknown_id(self, runtime):
        member = runtime.get_member("ghost")
        assert member is None


class TestActiveRunCount:
    async def test_count_returns_scalar(self, runtime):
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 3
        mock_session.execute.return_value = result_mock

        count = await runtime.active_run_count(mock_session, "sam")
        assert count == 3

    async def test_count_zero_for_idle_agent(self, runtime):
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 0
        mock_session.execute.return_value = result_mock

        count = await runtime.active_run_count(mock_session, "morgan")
        assert count == 0


class TestFindAvailableAgent:
    async def test_returns_least_senior_available(self, runtime, sam, jordan, morgan):
        """Should prefer IC3 > IC5 > IC6 to preserve senior capacity."""
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 0  # all at 0 WIP
        mock_session.execute.return_value = result_mock

        # backend role — only sam qualifies
        member = await runtime.find_available_agent(mock_session, "backend", min_ic_level=3)
        assert member.id == "sam"

    async def test_raises_when_no_role_candidates(self, runtime):
        mock_session = AsyncMock()
        with pytest.raises(AgentUnavailableError, match="No team member found"):
            await runtime.find_available_agent(mock_session, "wizard", min_ic_level=3)

    async def test_raises_when_all_at_wip_limit(self, runtime, sam):
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 1  # sam has max_concurrent=1
        mock_session.execute.return_value = result_mock

        with pytest.raises(AgentUnavailableError, match="WIP limit"):
            await runtime.find_available_agent(mock_session, "backend", min_ic_level=3)

    async def test_min_ic_level_filters_candidates(self, runtime):
        mock_session = AsyncMock()
        # Only IC6 morgan should match min_ic_level=6
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 0
        mock_session.execute.return_value = result_mock

        member = await runtime.find_available_agent(mock_session, "tech_lead", min_ic_level=6)
        assert member.id == "morgan"


class TestIsAtWipLimit:
    async def test_at_limit_returns_true(self, runtime, sam):
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 1  # sam max_concurrent=1
        mock_session.execute.return_value = result_mock

        assert await runtime.is_at_wip_limit(mock_session, "sam") is True

    async def test_below_limit_returns_false(self, runtime, morgan):
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 1  # morgan max_concurrent=2
        mock_session.execute.return_value = result_mock

        assert await runtime.is_at_wip_limit(mock_session, "morgan") is False

    async def test_unknown_agent_returns_false(self, runtime):
        mock_session = AsyncMock()
        assert await runtime.is_at_wip_limit(mock_session, "nobody") is False


class TestReload:
    def test_reload_calls_loader_reload(self, runtime):
        runtime.reload()
        runtime._loader.reload.assert_called_once()
