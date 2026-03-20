"""
Shared pytest fixtures for FORGE unit and integration tests.

Fixtures are designed for maximum isolation:
  - Unit tests: no I/O, all state in-process
  - Integration tests: real Postgres + Redis (via env vars or docker-compose)

Import structure is intentionally flat — avoid circular deps in test infra.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Event loop — use a single event loop per session for async tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Fake skill registry (pure in-memory, no disk I/O for unit tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def skill_registry_path(tmp_path: Path) -> Path:
    """
    Create a minimal skill registry on disk in a tmp directory.
    Returns the registry root path suitable for SkillEngine(registry_path=...).
    """
    registry = tmp_path / "skill-registry"
    registry.mkdir()

    # index.yaml
    index = {
        "skills": {
            "write-clean-code": "write_clean_code.yaml",
            "code-review": "code_review.yaml",
            "git-workflow": "git_workflow.yaml",
        }
    }
    (registry / "index.yaml").write_text(yaml.dump(index))

    # Skill: write-clean-code
    (registry / "write_clean_code.yaml").write_text(
        yaml.dump(
            {
                "id": "write-clean-code",
                "name": "Write Clean Code",
                "version": "1.0.0",
                "roles": ["frontend", "backend", "fullstack"],
                "principles": [
                    "Prefer readability over cleverness.",
                    "One function, one responsibility.",
                    "Name things what they are.",
                ],
                "procedures": {
                    "learning": [
                        "Read the existing code style guide.",
                        "Write a function that does one thing.",
                        "Add a docstring explaining the purpose.",
                    ],
                    "developing": [
                        "Apply SOLID principles to every new class.",
                        "Keep functions under 20 lines.",
                        "Write tests before writing implementation.",
                    ],
                    "proficient": [
                        "Extract common logic into shared utilities.",
                        "Enforce consistent naming conventions across modules.",
                        "Review for side effects before committing.",
                    ],
                    "expert": [
                        "Design APIs for minimal surface area.",
                        "Consider future maintainers in every naming decision.",
                    ],
                },
                "quality_criteria": [
                    "All functions have docstrings.",
                    "Cyclomatic complexity < 10.",
                    "No unused imports.",
                ],
                "anti_patterns": [
                    "Magic numbers without named constants.",
                    "Catch-all exception handlers.",
                ],
                "examples": [
                    "Use dataclasses for value objects instead of plain dicts.",
                ],
                "load_strategies": {
                    "3": "full_procedure",
                    "4": "summary",
                    "5": "principles_only",
                    "6": "none",
                },
            }
        )
    )

    # Skill: code-review
    (registry / "code_review.yaml").write_text(
        yaml.dump(
            {
                "id": "code-review",
                "name": "Code Review",
                "version": "1.1.0",
                "roles": ["fullstack", "backend", "tech_lead"],
                "principles": [
                    "Review the design before the implementation.",
                    "Separate style from correctness in feedback.",
                ],
                "procedures": {
                    "proficient": [
                        "Check that the change matches the stated requirement.",
                        "Verify tests exist for new behaviour.",
                        "Confirm no security regressions.",
                    ],
                    "expert": [
                        "Evaluate architectural impact.",
                        "Assess operability and observability.",
                    ],
                },
                "quality_criteria": [
                    "Every PR comment is actionable.",
                    "Approver has verified tests pass locally.",
                ],
                "prerequisites": ["write-clean-code"],
            }
        )
    )

    # Skill: git-workflow
    (registry / "git_workflow.yaml").write_text(
        yaml.dump(
            {
                "id": "git-workflow",
                "name": "Git Workflow",
                "version": "1.0.0",
                "roles": ["frontend", "backend", "fullstack", "devops"],
                "principles": ["Commit early, commit often.", "Atomic commits."],
                "procedures": {
                    "learning": [
                        "Create a branch for every task.",
                        "Write descriptive commit messages.",
                    ],
                    "proficient": [
                        "Squash fixup commits before opening a PR.",
                        "Rebase on main before review.",
                    ],
                },
                "quality_criteria": [
                    "No merge commits in feature branches.",
                    "Each commit passes CI.",
                ],
            }
        )
    )

    return registry


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_run_id() -> uuid.UUID:
    return uuid.UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def sample_project_id() -> uuid.UUID:
    return uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture
def sample_task_id() -> uuid.UUID:
    return uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def team_config() -> dict:
    return {
        "team": {
            "name": "Alpha Team",
            "domain": "web",
            "timezone": "America/New_York",
        },
        "members": [
            {
                "id": "morgan",
                "handle": "@morgan",
                "ic_level": 6,
                "role": "tech_lead",
                "skills": ["write-clean-code", "code-review"],
                "max_concurrent_tasks": 2,
                "token_budget_per_task": 100000,
            },
            {
                "id": "jordan",
                "handle": "@jordan",
                "ic_level": 5,
                "role": "fullstack",
                "skills": ["write-clean-code", "git-workflow"],
                "max_concurrent_tasks": 2,
                "token_budget_per_task": 80000,
            },
            {
                "id": "sam",
                "handle": "@sam",
                "ic_level": 3,
                "role": "backend",
                "skills": ["write-clean-code", "git-workflow"],
                "max_concurrent_tasks": 1,
                "token_budget_per_task": 40000,
            },
        ],
    }


@pytest.fixture
def project_config() -> dict:
    return {
        "project": {
            "name": "TeamWorks",
            "repo": "github.com/acme/teamworks",
            "stack": {
                "language": "python",
                "framework": "fastapi",
                "test_command": "pytest",
                "lint_command": "ruff check .",
                "build_command": "docker build .",
            },
            "branches": {
                "main": "main",
                "staging": "staging",
            },
        }
    }


@pytest.fixture
def guardrails_config() -> dict:
    return {
        "guardrails": {
            "max_file_changes_per_task": 20,
            "require_tests_for_new_code": True,
            "forbidden_patterns": ["TODO: remove", "FIXME", "HACK"],
            "require_approval_for": ["plan", "ship", "release"],
            "max_run_duration_minutes": 240,
            "max_token_budget_per_run": 500000,
            "wip_limit_per_member": 2,
            "ic3_requires_review_from": "ic5",
        }
    }


@pytest.fixture
def config_dir(tmp_path: Path, team_config, project_config, guardrails_config) -> Path:
    """Write config YAML files to a temp directory and return the path."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "team.yaml").write_text(yaml.dump(team_config))
    (configs / "project.yaml").write_text(yaml.dump(project_config))
    (configs / "guardrails.yaml").write_text(yaml.dump(guardrails_config))
    (configs / "workflow.yaml").write_text(
        yaml.dump(
            {
                "workflow": {
                    "phases": [
                        {"name": "research", "agent_role": "backend", "requires_approval": False},
                        {"name": "planning", "agent_role": "tech_lead", "requires_approval": True},
                        {"name": "executing", "agent_role": "backend", "requires_approval": False},
                        {"name": "shipping", "agent_role": "tech_lead", "requires_approval": True},
                    ],
                    "approval_timeout_hours": 24,
                }
            }
        )
    )
    return configs


# ---------------------------------------------------------------------------
# DB session mocks (unit tests — no real DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_session():
    """Async mock of SQLAlchemy AsyncSession for unit tests."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    session.get = AsyncMock()
    return session


@pytest.fixture
def mock_get_db(mock_db_session):
    """Patch forge.db.session.get_db to yield the mock session."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _mock_get_db():
        yield mock_db_session

    with patch("phalanx.db.session.get_db", _mock_get_db):
        yield mock_db_session


# ---------------------------------------------------------------------------
# Redis mock (unit tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock()
    redis.exists = AsyncMock(return_value=0)
    return redis
