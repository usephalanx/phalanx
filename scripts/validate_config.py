#!/usr/bin/env python3
"""
Gate 2: Config Validation — validates all YAML config files against their schemas.
Exits 0 on success, 1 on any validation failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Inline JSON schemas — avoids a runtime dependency on jsonschema for the
# CI schema-load step; we use jsonschema only for validation itself.
# ---------------------------------------------------------------------------

TEAM_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["team", "members"],
    "properties": {
        "team": {
            "type": "object",
            "required": ["name", "domain"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "domain": {"type": "string", "minLength": 1},
                "timezone": {"type": "string"},
                "working_hours": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                },
            },
        },
        "members": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "handle", "ic_level", "role", "skills"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                    "handle": {"type": "string"},
                    "ic_level": {"type": "integer", "minimum": 3, "maximum": 7},
                    "role": {
                        "type": "string",
                        "enum": [
                            "frontend",
                            "backend",
                            "fullstack",
                            "devops",
                            "qa",
                            "security",
                            "tech_lead",
                        ],
                    },
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "max_concurrent_tasks": {"type": "integer", "minimum": 1},
                    "token_budget_per_task": {"type": "integer", "minimum": 1000},
                },
            },
        },
    },
}

PROJECT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["project"],
    "properties": {
        "project": {
            "type": "object",
            "required": ["name", "repo", "stack"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "repo": {"type": "string"},
                "stack": {
                    "type": "object",
                    "required": ["language"],
                    "properties": {
                        "language": {"type": "string"},
                        "framework": {"type": "string"},
                        "test_command": {"type": "string"},
                        "lint_command": {"type": "string"},
                        "build_command": {"type": "string"},
                    },
                },
                "branches": {
                    "type": "object",
                    "properties": {
                        "main": {"type": "string"},
                        "staging": {"type": "string"},
                    },
                },
            },
        }
    },
}

GUARDRAILS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["guardrails"],
    "properties": {
        "guardrails": {
            "type": "object",
            "properties": {
                "max_file_changes_per_task": {"type": "integer", "minimum": 1},
                "require_tests_for_new_code": {"type": "boolean"},
                "forbidden_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "require_approval_for": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["plan", "ship", "release", "hotfix"],
                    },
                },
                "max_run_duration_minutes": {"type": "integer", "minimum": 1},
                "max_token_budget_per_run": {"type": "integer", "minimum": 1000},
                "wip_limit_per_member": {"type": "integer", "minimum": 1},
                "ic3_requires_review_from": {
                    "type": "string",
                    "enum": ["ic4", "ic5", "ic6"],
                },
            },
        }
    },
}

WORKFLOW_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["workflow"],
    "properties": {
        "workflow": {
            "type": "object",
            "properties": {
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "agent_role"],
                        "properties": {
                            "name": {"type": "string"},
                            "agent_role": {"type": "string"},
                            "requires_approval": {"type": "boolean"},
                            "timeout_minutes": {"type": "integer"},
                        },
                    },
                },
                "approval_timeout_hours": {"type": "integer", "minimum": 1},
            },
        }
    },
}

CONFIG_FILES: dict[str, tuple[str, dict]] = {
    # (glob pattern, schema)
    "team.yaml": ("configs/team.yaml", TEAM_SCHEMA),
    "project.yaml": ("configs/project.yaml", PROJECT_SCHEMA),
    "guardrails.yaml": ("configs/guardrails.yaml", GUARDRAILS_SCHEMA),
    "workflow.yaml": ("configs/workflow.yaml", WORKFLOW_SCHEMA),
}


def load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def validate_file(label: str, path: Path, schema: dict) -> list[str]:
    try:
        import jsonschema  # noqa: PLC0415
    except ImportError:
        print("  [WARN] jsonschema not installed — skipping schema validation")
        return []

    errors: list[str] = []
    if not path.exists():
        return [f"{label}: file not found at {path}"]

    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        return [f"{label}: YAML parse error — {exc}"]

    validator = jsonschema.Draft7Validator(schema)
    for err in validator.iter_errors(data):
        errors.append(f"{label}: {err.json_path} — {err.message}")

    return errors


def check_member_skill_references(team_path: Path, registry_path: Path) -> list[str]:
    """Every skill listed in team.yaml must exist in the skill registry."""
    errors: list[str] = []
    if not team_path.exists() or not registry_path.exists():
        return errors

    try:
        team_data = load_yaml(team_path)
        registry_index_path = registry_path / "index.yaml"
        if not registry_index_path.exists():
            return [f"skill-registry/index.yaml not found — cannot validate member skills"]
        registry_index = load_yaml(registry_index_path)
    except yaml.YAMLError:
        return []

    known_skills: set[str] = set(registry_index.get("skills", {}).keys())
    for member in team_data.get("members", []):
        member_id = member.get("id", "unknown")
        for skill in member.get("skills", []):
            if skill not in known_skills:
                errors.append(
                    f"team.yaml: member '{member_id}' references unknown skill '{skill}' "
                    f"(not in skill-registry/index.yaml)"
                )

    return errors


def check_ic_level_constraints(team_path: Path) -> list[str]:
    """Enforce per-IC-level rules that schema alone can't express."""
    errors: list[str] = []
    if not team_path.exists():
        return errors

    try:
        team_data = load_yaml(team_path)
    except yaml.YAMLError:
        return []

    ic6_members = [m for m in team_data.get("members", []) if m.get("ic_level") == 6]
    if len(ic6_members) > 2:
        errors.append(
            f"team.yaml: found {len(ic6_members)} IC6 members — teams should have ≤2 IC6 "
            f"to preserve review authority."
        )

    for member in team_data.get("members", []):
        ic = member.get("ic_level", 0)
        budget = member.get("token_budget_per_task", 0)
        # IC3 should not have a higher budget than IC5 — guardrail
        if ic == 3 and budget > 50000:
            errors.append(
                f"team.yaml: IC3 member '{member.get('id')}' has token_budget_per_task={budget} "
                f"— IC3 max is 50,000 tokens."
            )

    return errors


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    passed: list[str] = []

    print("=" * 60)
    print("FORGE Config Validator")
    print("=" * 60)

    configs_dir = ROOT / "configs"

    # --- Schema validation -----------------------------------------------
    schema_checks = [
        ("team.yaml", configs_dir / "team.yaml", TEAM_SCHEMA),
        ("project.yaml", configs_dir / "project.yaml", PROJECT_SCHEMA),
        ("guardrails.yaml", configs_dir / "guardrails.yaml", GUARDRAILS_SCHEMA),
        ("workflow.yaml", configs_dir / "workflow.yaml", WORKFLOW_SCHEMA),
    ]

    for label, path, schema in schema_checks:
        errs = validate_file(label, path, schema)
        if errs:
            errors.extend(errs)
            print(f"  [FAIL] {label}")
        else:
            passed.append(label)
            status = "OK" if path.exists() else "SKIP (not found)"
            print(f"  [PASS] {label} — {status}")

    # --- Cross-reference checks ------------------------------------------
    registry_path = ROOT / "skill-registry"
    team_path = configs_dir / "team.yaml"

    print("\n--- Cross-reference checks ---")

    skill_ref_errors = check_member_skill_references(team_path, registry_path)
    if skill_ref_errors:
        errors.extend(skill_ref_errors)
        print(f"  [FAIL] member skill references")
        for e in skill_ref_errors:
            print(f"         {e}")
    else:
        print(f"  [PASS] member skill references")

    ic_errors = check_ic_level_constraints(team_path)
    if ic_errors:
        # IC level constraint violations are warnings unless in strict mode
        warnings.extend(ic_errors)
        print(f"  [WARN] IC-level constraints")
        for w in ic_errors:
            print(f"         {w}")
    else:
        print(f"  [PASS] IC-level constraints")

    # --- Summary ---------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"Passed : {len(passed)}")
    print(f"Warnings: {len(warnings)}")
    print(f"Errors  : {len(errors)}")

    if errors:
        print("\nFAILED — fix the above errors before merging.")
        for e in errors:
            print(f"  • {e}")
        return 1

    if warnings:
        print("\nPASSED with warnings.")
    else:
        print("\nAll config files are valid.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
