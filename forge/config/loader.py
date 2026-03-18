"""
Config Loader — reads YAML config files and returns validated Pydantic models.

Supports two modes:
  1. Path injection (for tests): ConfigLoader(config_dir=Path("/tmp/test-configs"))
  2. Default (production): ConfigLoader() reads from {repo_root}/configs/

All models are immutable (frozen=True) — no runtime mutation of config.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class WorkingHours(BaseModel, frozen=True):
    start: str = "09:00"
    end: str = "17:00"


class TeamMeta(BaseModel, frozen=True):
    name: str
    domain: str
    timezone: str = "UTC"
    working_hours: WorkingHours = Field(default_factory=WorkingHours)


MemberRole = Literal["frontend", "backend", "fullstack", "devops", "qa", "security", "tech_lead"]


class TeamMember(BaseModel, frozen=True):
    id: str
    handle: str
    ic_level: int = Field(ge=3, le=7)
    role: MemberRole
    skills: list[str] = Field(default_factory=list)
    max_concurrent_tasks: int = Field(default=1, ge=1)
    token_budget_per_task: int = Field(default=40000, ge=1000)

    @field_validator("id")
    @classmethod
    def id_must_be_slug(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z0-9_-]+$", v):
            raise ValueError(f"Member id '{v}' must be lowercase alphanumeric/dash/underscore")
        return v


class TeamConfig(BaseModel, frozen=True):
    team: TeamMeta
    members: list[TeamMember] = Field(min_length=1)

    def get_member(self, member_id: str) -> TeamMember | None:
        for m in self.members:
            if m.id == member_id:
                return m
        return None

    def members_by_ic(self, ic_level: int) -> list[TeamMember]:
        return [m for m in self.members if m.ic_level == ic_level]

    def tech_leads(self) -> list[TeamMember]:
        return [m for m in self.members if m.role == "tech_lead" or m.ic_level >= 6]


class Stack(BaseModel, frozen=True):
    language: str
    framework: str = ""
    test_command: str = "pytest"
    lint_command: str = "ruff check ."
    build_command: str = "docker build ."


class ProjectBranches(BaseModel, frozen=True):
    main: str = "main"
    staging: str = "staging"


class ProjectMeta(BaseModel, frozen=True):
    name: str
    repo: str = ""
    stack: Stack
    branches: ProjectBranches = Field(default_factory=ProjectBranches)


class ProjectConfig(BaseModel, frozen=True):
    project: ProjectMeta


ApprovalType = Literal["plan", "ship", "release", "hotfix"]
IC3ReviewLevel = Literal["ic4", "ic5", "ic6"]


class GuardrailsConfig(BaseModel, frozen=True):
    max_file_changes_per_task: int = Field(default=20, ge=1)
    require_tests_for_new_code: bool = True
    forbidden_patterns: list[str] = Field(default_factory=list)
    require_approval_for: list[ApprovalType] = Field(
        default_factory=lambda: ["plan", "ship", "release"]
    )
    max_run_duration_minutes: int = Field(default=240, ge=1)
    max_token_budget_per_run: int = Field(default=500_000, ge=1000)
    wip_limit_per_member: int = Field(default=2, ge=1)
    ic3_requires_review_from: IC3ReviewLevel = "ic5"


class GuardrailsFile(BaseModel, frozen=True):
    guardrails: GuardrailsConfig


class WorkflowPhase(BaseModel, frozen=True):
    name: str
    agent_role: str
    requires_approval: bool = False
    timeout_minutes: int = 60


class WorkflowMeta(BaseModel, frozen=True):
    phases: list[WorkflowPhase] = Field(default_factory=list)
    approval_timeout_hours: int = Field(default=24, ge=1)


class WorkflowConfig(BaseModel, frozen=True):
    workflow: WorkflowMeta


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DIR = Path(__file__).parent.parent.parent / "configs"


class ConfigLoader:
    """
    Loads and validates all FORGE config files from a directory.

    Raises ValueError with a clear message if any config is invalid.
    Models are cached after first load — call reload() to force re-read.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = config_dir or _DEFAULT_CONFIG_DIR
        self._team: TeamConfig | None = None
        self._project: ProjectConfig | None = None
        self._guardrails: GuardrailsConfig | None = None
        self._workflow: WorkflowConfig | None = None

    def _load_yaml(self, filename: str) -> dict:
        path = self._config_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open() as fh:
            return yaml.safe_load(fh) or {}

    @property
    def team(self) -> TeamConfig:
        if self._team is None:
            data = self._load_yaml("team.yaml")
            try:
                self._team = TeamConfig.model_validate(data)
            except Exception as exc:
                raise ValueError(f"Invalid team.yaml: {exc}") from exc
        return self._team

    @property
    def project(self) -> ProjectConfig:
        if self._project is None:
            data = self._load_yaml("project.yaml")
            try:
                self._project = ProjectConfig.model_validate(data)
            except Exception as exc:
                raise ValueError(f"Invalid project.yaml: {exc}") from exc
        return self._project

    @property
    def guardrails(self) -> GuardrailsConfig:
        if self._guardrails is None:
            data = self._load_yaml("guardrails.yaml")
            try:
                parsed = GuardrailsFile.model_validate(data)
                self._guardrails = parsed.guardrails
            except Exception as exc:
                raise ValueError(f"Invalid guardrails.yaml: {exc}") from exc
        return self._guardrails

    @property
    def workflow(self) -> WorkflowConfig:
        if self._workflow is None:
            data = self._load_yaml("workflow.yaml")
            try:
                self._workflow = WorkflowConfig.model_validate(data)
            except Exception as exc:
                raise ValueError(f"Invalid workflow.yaml: {exc}") from exc
        return self._workflow

    def reload(self) -> None:
        """Invalidate all caches and force re-read on next access."""
        self._team = None
        self._project = None
        self._guardrails = None
        self._workflow = None
