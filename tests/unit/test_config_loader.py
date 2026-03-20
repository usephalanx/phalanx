"""
Unit tests for the ConfigLoader and Pydantic config models.

Uses the `config_dir` fixture from conftest.py which writes real YAML files
to a tmp_path directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from phalanx.config.loader import (
    ConfigLoader,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestTeamConfigLoading:
    def test_loads_team_name(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.team.team.name == "Alpha Team"

    def test_loads_members(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert len(loader.team.members) == 3

    def test_member_ic_levels(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        levels = {m.id: m.ic_level for m in loader.team.members}
        assert levels["morgan"] == 6
        assert levels["jordan"] == 5
        assert levels["sam"] == 3

    def test_member_roles(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        morgan = loader.team.get_member("morgan")
        assert morgan is not None
        assert morgan.role == "tech_lead"

    def test_get_member_returns_none_for_unknown(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.team.get_member("nobody") is None

    def test_members_by_ic(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        ic6 = loader.team.members_by_ic(6)
        assert len(ic6) == 1
        assert ic6[0].id == "morgan"

    def test_tech_leads_includes_ic6(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        leads = loader.team.tech_leads()
        ids = [m.id for m in leads]
        assert "morgan" in ids

    def test_member_skills_list(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        morgan = loader.team.get_member("morgan")
        assert "write-clean-code" in morgan.skills
        assert "code-review" in morgan.skills

    def test_team_config_is_cached(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        t1 = loader.team
        t2 = loader.team
        assert t1 is t2  # same object — cached

    def test_reload_invalidates_cache(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        t1 = loader.team
        loader.reload()
        t2 = loader.team
        assert t1 is not t2  # new object after reload


class TestTeamConfigValidation:
    def test_invalid_member_id_raises(self, config_dir: Path, team_config: dict, tmp_path: Path):
        bad_config = dict(team_config)
        bad_config["members"] = [{**team_config["members"][0], "id": "INVALID ID WITH SPACES"}]
        bad_dir = tmp_path / "bad-configs"
        bad_dir.mkdir()
        (bad_dir / "team.yaml").write_text(yaml.dump(bad_config))
        loader = ConfigLoader(bad_dir)
        with pytest.raises(ValueError, match="Invalid team.yaml"):
            _ = loader.team

    def test_ic_level_out_of_range_raises(self, tmp_path: Path, team_config: dict):
        bad_config = dict(team_config)
        bad_config["members"] = [{**team_config["members"][0], "ic_level": 99}]
        bad_dir = tmp_path / "bad-ic"
        bad_dir.mkdir()
        (bad_dir / "team.yaml").write_text(yaml.dump(bad_config))
        loader = ConfigLoader(bad_dir)
        with pytest.raises(ValueError, match="Invalid team.yaml"):
            _ = loader.team

    def test_empty_members_raises(self, tmp_path: Path):
        bad_dir = tmp_path / "empty-members"
        bad_dir.mkdir()
        (bad_dir / "team.yaml").write_text(
            yaml.dump({"team": {"name": "T", "domain": "web"}, "members": []})
        )
        loader = ConfigLoader(bad_dir)
        with pytest.raises(ValueError, match="Invalid team.yaml"):
            _ = loader.team

    def test_missing_file_raises_file_not_found(self, tmp_path: Path):
        empty = tmp_path / "nothing"
        empty.mkdir()
        loader = ConfigLoader(empty)
        with pytest.raises(FileNotFoundError):
            _ = loader.team


class TestProjectConfigLoading:
    def test_loads_project_name(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.project.project.name == "TeamWorks"

    def test_loads_stack_language(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.project.project.stack.language == "python"

    def test_loads_stack_framework(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.project.project.stack.framework == "fastapi"

    def test_test_command_default(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.project.project.stack.test_command == "pytest"

    def test_branches_main(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.project.project.branches.main == "main"


class TestGuardrailsConfigLoading:
    def test_loads_max_file_changes(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.guardrails.max_file_changes_per_task == 20

    def test_loads_require_tests(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.guardrails.require_tests_for_new_code is True

    def test_loads_forbidden_patterns(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert "TODO: remove" in loader.guardrails.forbidden_patterns

    def test_loads_approval_requirements(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert "plan" in loader.guardrails.require_approval_for
        assert "ship" in loader.guardrails.require_approval_for

    def test_wip_limit(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.guardrails.wip_limit_per_member == 2

    def test_ic3_review_level(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.guardrails.ic3_requires_review_from == "ic5"

    def test_invalid_wip_limit_raises(self, tmp_path: Path, guardrails_config: dict):
        bad = {"guardrails": {**guardrails_config["guardrails"], "wip_limit_per_member": 0}}
        d = tmp_path / "bad-gr"
        d.mkdir()
        (d / "guardrails.yaml").write_text(yaml.dump(bad))
        loader = ConfigLoader(d)
        with pytest.raises(ValueError, match="Invalid guardrails.yaml"):
            _ = loader.guardrails


class TestWorkflowConfigLoading:
    def test_loads_phases(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        phases = loader.workflow.workflow.phases
        assert len(phases) >= 2

    def test_approval_timeout(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        assert loader.workflow.workflow.approval_timeout_hours == 24

    def test_phase_requires_approval(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        phases = {p.name: p for p in loader.workflow.workflow.phases}
        assert phases["planning"].requires_approval is True
        assert phases["research"].requires_approval is False


class TestConfigImmutability:
    def test_team_config_is_frozen(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            loader.team.team = None  # type: ignore

    def test_guardrails_is_frozen(self, config_dir: Path):
        loader = ConfigLoader(config_dir)
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            loader.guardrails.wip_limit_per_member = 99  # type: ignore
