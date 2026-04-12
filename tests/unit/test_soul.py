"""
Unit tests for phalanx/agents/soul.py — character definitions and prompt templates.
"""

from __future__ import annotations

import pytest

from phalanx.agents.soul import (
    BUILDER_REFLECTION_PROMPT,
    BUILDER_SELF_CHECK_PROMPT,
    BUILDER_SOUL,
    COMMANDER_SOUL,
    QA_SOUL,
    REVIEWER_SOUL,
    TECH_LEAD_SOUL,
    get_reflection_prompt,
    get_soul,
)


class TestSoulDefinitions:
    def test_builder_soul_is_non_empty(self):
        assert len(BUILDER_SOUL) > 100

    def test_reviewer_soul_is_non_empty(self):
        assert len(REVIEWER_SOUL) > 100

    def test_all_souls_present(self):
        for role in ("builder", "reviewer", "tech_lead", "commander", "qa"):
            soul = get_soul(role)
            assert isinstance(soul, str)
            assert len(soul) > 50

    def test_unknown_role_returns_generic_soul(self):
        soul = get_soul("nonexistent_role")
        assert "senior engineer" in soul.lower()

    def test_builder_soul_mentions_import_paths(self):
        assert "import" in BUILDER_SOUL.lower()

    def test_reviewer_soul_is_adversarial(self):
        # Reviewer should explicitly mention adversarial or finding problems
        assert "adversarial" in REVIEWER_SOUL.lower() or "problems" in REVIEWER_SOUL.lower()

    def test_reviewer_soul_mentions_security(self):
        assert "security" in REVIEWER_SOUL.lower()

    def test_commander_soul_exists(self):
        assert len(COMMANDER_SOUL) > 50

    def test_qa_soul_exists(self):
        assert len(QA_SOUL) > 50

    def test_tech_lead_soul_exists(self):
        assert len(TECH_LEAD_SOUL) > 50


class TestReflectionPrompts:
    def test_builder_reflection_prompt_has_placeholders(self):
        assert "{task_description}" in BUILDER_REFLECTION_PROMPT
        assert "{context_section}" in BUILDER_REFLECTION_PROMPT

    def test_builder_self_check_has_placeholders(self):
        assert "{task_description}" in BUILDER_SELF_CHECK_PROMPT
        assert "{files_written}" in BUILDER_SELF_CHECK_PROMPT
        assert "{summary}" in BUILDER_SELF_CHECK_PROMPT

    def test_get_reflection_prompt_builder(self):
        prompt = get_reflection_prompt("builder")
        assert prompt is not None
        assert "{task_description}" in prompt

    def test_get_reflection_prompt_reviewer(self):
        prompt = get_reflection_prompt("reviewer")
        assert prompt is not None
        assert "{task_description}" in prompt

    def test_get_reflection_prompt_unknown_returns_none(self):
        prompt = get_reflection_prompt("nonexistent")
        assert prompt is None

    def test_builder_reflection_prompt_fills_template(self):
        filled = BUILDER_REFLECTION_PROMPT.format(
            task_description="Add login page",
            context_section="Plan: use React",
        )
        assert "Add login page" in filled
        assert "Plan: use React" in filled

    def test_self_check_prompt_has_task_description_placeholder(self):
        assert "{task_description}" in BUILDER_SELF_CHECK_PROMPT
        assert "{files_written}" in BUILDER_SELF_CHECK_PROMPT
        assert "{summary}" in BUILDER_SELF_CHECK_PROMPT
