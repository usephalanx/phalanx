"""
Skill Engine — loads skills from the registry and filters content
by IC level and agent proficiency.

Load strategies by IC level:
  IC3 → full_procedure  (every step spelled out)
  IC4 → summary         (summarized steps + quality criteria)
  IC5 → principles_only (guiding principles, no procedures)
  IC6 → none            (quality criteria only — IC6 relies on experience)

This module is pure Python with no I/O at unit-test time (registry_path injected).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class ProficiencyLevel(StrEnum):
    LEARNING = "learning"
    DEVELOPING = "developing"
    PROFICIENT = "proficient"
    EXPERT = "expert"


class LoadStrategy(StrEnum):
    FULL_PROCEDURE = "full_procedure"
    SUMMARY = "summary"
    PRINCIPLES_ONLY = "principles_only"
    NONE = "none"


_DEFAULT_LOAD_STRATEGY: dict[int, LoadStrategy] = {
    3: LoadStrategy.FULL_PROCEDURE,
    4: LoadStrategy.SUMMARY,
    5: LoadStrategy.PRINCIPLES_ONLY,
    6: LoadStrategy.NONE,
}


@dataclass
class LoadedSkill:
    """The subset of a skill loaded for a specific agent at a specific IC level."""
    skill_id: str
    name: str
    version: str
    ic_level: int
    proficiency: ProficiencyLevel
    load_strategy: LoadStrategy
    content: dict[str, Any]  # varies by load strategy
    quality_criteria: list[str]
    prerequisites: list[str]


class SkillNotFoundError(KeyError):
    pass


class SkillRegistryError(RuntimeError):
    pass


class SkillEngine:
    """
    Loads skills from the YAML registry on disk.

    Injecting registry_path makes this fully unit-testable without touching
    real files — just pass a tmp_path with fixture skill files.
    """

    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self._index: dict[str, str] | None = None  # skill_id → file_path (relative)

    def _load_index(self) -> dict[str, str]:
        if self._index is not None:
            return self._index

        index_path = self.registry_path / "index.yaml"
        if not index_path.exists():
            raise SkillRegistryError(f"Skill registry index not found at {index_path}")

        with index_path.open() as fh:
            data = yaml.safe_load(fh) or {}

        self._index = data.get("skills", {})
        return self._index

    def _load_raw_skill(self, skill_id: str) -> dict:
        index = self._load_index()
        if skill_id not in index:
            raise SkillNotFoundError(f"Skill '{skill_id}' not in registry index")

        skill_path = self.registry_path / index[skill_id]
        if not skill_path.exists():
            raise SkillRegistryError(
                f"Skill file for '{skill_id}' not found at {skill_path}"
            )

        with skill_path.open() as fh:
            return yaml.safe_load(fh) or {}

    def get_load_strategy(self, raw_skill: dict, ic_level: int) -> LoadStrategy:
        """Determine load strategy: use per-skill override if declared, else default."""
        overrides = raw_skill.get("load_strategies", {})
        if str(ic_level) in overrides:
            return LoadStrategy(overrides[str(ic_level)])
        if ic_level in overrides:
            return LoadStrategy(overrides[ic_level])
        return _DEFAULT_LOAD_STRATEGY.get(ic_level, LoadStrategy.FULL_PROCEDURE)

    def _build_content(
        self,
        raw_skill: dict,
        strategy: LoadStrategy,
        proficiency: ProficiencyLevel,
    ) -> dict[str, Any]:
        """Extract relevant content based on load strategy."""
        procedures = raw_skill.get("procedures", {})
        proficiency_procedures = procedures.get(proficiency, procedures.get("proficient", []))

        if strategy == LoadStrategy.FULL_PROCEDURE:
            return {
                "procedures": proficiency_procedures,
                "examples": raw_skill.get("examples", []),
                "anti_patterns": raw_skill.get("anti_patterns", []),
            }

        elif strategy == LoadStrategy.SUMMARY:
            # Condense to first sentence of each procedure step
            summarized = [
                step.split(".")[0].strip() + "."
                for step in proficiency_procedures
                if step
            ]
            return {
                "procedures_summary": summarized,
                "anti_patterns": raw_skill.get("anti_patterns", []),
            }

        elif strategy == LoadStrategy.PRINCIPLES_ONLY:
            return {
                "principles": raw_skill.get("principles", []),
            }

        else:  # NONE — IC6: quality criteria only
            return {}

    def load(
        self,
        skill_id: str,
        ic_level: int,
        proficiency: ProficiencyLevel = ProficiencyLevel.PROFICIENT,
    ) -> LoadedSkill:
        """Load a skill for a given IC level and proficiency."""
        raw = self._load_raw_skill(skill_id)
        strategy = self.get_load_strategy(raw, ic_level)
        content = self._build_content(raw, strategy, proficiency)

        return LoadedSkill(
            skill_id=skill_id,
            name=raw.get("name", skill_id),
            version=raw.get("version", "0.0.1"),
            ic_level=ic_level,
            proficiency=proficiency,
            load_strategy=strategy,
            content=content,
            quality_criteria=raw.get("quality_criteria", []),
            prerequisites=raw.get("prerequisites", []),
        )

    def load_many(
        self,
        skill_ids: list[str],
        ic_level: int,
        proficiency: ProficiencyLevel = ProficiencyLevel.PROFICIENT,
    ) -> list[LoadedSkill]:
        """Load multiple skills, silently skipping any not in the registry."""
        results = []
        for skill_id in skill_ids:
            try:
                results.append(self.load(skill_id, ic_level, proficiency))
            except SkillNotFoundError:
                pass  # skill_id referenced but not in registry — skip
        return results

    def list_skills(self) -> list[str]:
        """Return all skill IDs in the registry."""
        return list(self._load_index().keys())
