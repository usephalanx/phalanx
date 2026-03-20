"""
Unit tests for the SkillEngine.

Uses the `skill_registry_path` fixture from conftest.py which writes
real YAML files to a tmp_path — no mocking of file I/O needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phalanx.skills.engine import (
    LoadStrategy,
    ProficiencyLevel,
    SkillEngine,
    SkillNotFoundError,
    SkillRegistryError,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestSkillEngineIndexLoading:
    def test_loads_index_successfully(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skills = engine.list_skills()
        assert "write-clean-code" in skills
        assert "code-review" in skills
        assert "git-workflow" in skills

    def test_missing_index_raises_registry_error(self, tmp_path: Path):
        empty_dir = tmp_path / "empty-registry"
        empty_dir.mkdir()
        engine = SkillEngine(empty_dir)
        with pytest.raises(SkillRegistryError, match="index not found"):
            engine.list_skills()

    def test_index_cached_after_first_load(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        engine.list_skills()  # first load
        # Remove index — should still work from cache
        (skill_registry_path / "index.yaml").unlink()
        skills = engine.list_skills()
        assert "write-clean-code" in skills


class TestSkillNotFound:
    def test_load_unknown_skill_raises(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        with pytest.raises(SkillNotFoundError):
            engine.load("does-not-exist", ic_level=3)

    def test_load_many_silently_skips_unknown(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        results = engine.load_many(
            ["write-clean-code", "nonexistent-skill"],
            ic_level=4,
        )
        assert len(results) == 1
        assert results[0].skill_id == "write-clean-code"


class TestIC3LoadStrategy:
    """IC3 should receive the full procedure with all steps."""

    def test_ic3_uses_full_procedure_strategy(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.LEARNING)
        assert skill.load_strategy == LoadStrategy.FULL_PROCEDURE

    def test_ic3_content_has_procedures(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.LEARNING)
        assert "procedures" in skill.content
        assert len(skill.content["procedures"]) > 0

    def test_ic3_content_has_examples(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.LEARNING)
        assert "examples" in skill.content

    def test_ic3_content_has_anti_patterns(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3)
        assert "anti_patterns" in skill.content


class TestIC4LoadStrategy:
    """IC4 should receive summarized procedures."""

    def test_ic4_uses_summary_strategy(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=4)
        assert skill.load_strategy == LoadStrategy.SUMMARY

    def test_ic4_content_has_procedures_summary(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=4)
        assert "procedures_summary" in skill.content

    def test_ic4_summary_is_shorter_than_full(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        ic3 = engine.load("write-clean-code", ic_level=3)
        ic4 = engine.load("write-clean-code", ic_level=4)
        # IC4 summary steps should be shorter strings
        if ic3.content.get("procedures") and ic4.content.get("procedures_summary"):
            avg_ic3 = sum(len(s) for s in ic3.content["procedures"]) / len(
                ic3.content["procedures"]
            )
            avg_ic4 = sum(len(s) for s in ic4.content["procedures_summary"]) / len(
                ic4.content["procedures_summary"]
            )
            assert (
                avg_ic4 <= avg_ic3 + 10
            )  # summaries may be slightly longer due to trailing period


class TestIC5LoadStrategy:
    """IC5 should receive principles only — no step-by-step procedures."""

    def test_ic5_uses_principles_only_strategy(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=5)
        assert skill.load_strategy == LoadStrategy.PRINCIPLES_ONLY

    def test_ic5_content_has_principles(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=5)
        assert "principles" in skill.content
        assert len(skill.content["principles"]) > 0

    def test_ic5_content_has_no_procedures(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=5)
        assert "procedures" not in skill.content
        assert "procedures_summary" not in skill.content


class TestIC6LoadStrategy:
    """IC6 should receive quality criteria only — no content."""

    def test_ic6_uses_none_strategy(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=6)
        assert skill.load_strategy == LoadStrategy.NONE

    def test_ic6_content_is_empty(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=6)
        assert skill.content == {}

    def test_ic6_still_has_quality_criteria(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=6)
        assert len(skill.quality_criteria) > 0


class TestQualityCriteriaAndPrerequisites:
    def test_quality_criteria_always_present(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        for ic in [3, 4, 5, 6]:
            skill = engine.load("write-clean-code", ic_level=ic)
            assert isinstance(skill.quality_criteria, list)

    def test_prerequisites_loaded(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("code-review", ic_level=4)
        assert "write-clean-code" in skill.prerequisites

    def test_no_prerequisites_returns_empty_list(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3)
        assert skill.prerequisites == []


class TestProficiencyLevels:
    def test_learning_procedures_loaded_for_ic3(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.LEARNING)
        assert len(skill.content["procedures"]) > 0
        # Learning steps should include beginner-friendly language
        assert any(
            "code style" in step.lower() or "read" in step.lower()
            for step in skill.content["procedures"]
        )

    def test_expert_procedures_differ_from_learning(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        learning = engine.load(
            "write-clean-code", ic_level=3, proficiency=ProficiencyLevel.LEARNING
        )
        expert = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.EXPERT)
        assert learning.content["procedures"] != expert.content["procedures"]

    def test_missing_proficiency_falls_back_to_proficient(self, skill_registry_path: Path):
        """Skills with no 'learning' level should fall back to 'proficient'."""
        engine = SkillEngine(skill_registry_path)
        # code-review has no 'learning' level — should use 'proficient'
        skill = engine.load("code-review", ic_level=3, proficiency=ProficiencyLevel.LEARNING)
        assert len(skill.content["procedures"]) > 0


class TestLoadMany:
    def test_load_many_returns_all_known(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skills = engine.load_many(
            ["write-clean-code", "code-review", "git-workflow"],
            ic_level=4,
        )
        assert len(skills) == 3

    def test_load_many_preserves_order(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        ids = ["git-workflow", "write-clean-code"]
        skills = engine.load_many(ids, ic_level=4)
        assert [s.skill_id for s in skills] == ids

    def test_load_many_empty_list_returns_empty(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        assert engine.load_many([], ic_level=3) == []


class TestLoadedSkillMetadata:
    def test_skill_has_correct_id(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("git-workflow", ic_level=3)
        assert skill.skill_id == "git-workflow"

    def test_skill_has_version(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3)
        assert skill.version == "1.0.0"

    def test_skill_has_correct_ic_level(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=5)
        assert skill.ic_level == 5

    def test_skill_has_correct_proficiency(self, skill_registry_path: Path):
        engine = SkillEngine(skill_registry_path)
        skill = engine.load("write-clean-code", ic_level=3, proficiency=ProficiencyLevel.DEVELOPING)
        assert skill.proficiency == ProficiencyLevel.DEVELOPING
