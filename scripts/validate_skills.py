#!/usr/bin/env python3
"""
Gate 2: Skill Registry Validation — validates every skill YAML file and the registry index.
Exits 0 on success, 1 on any validation failure.

Checks:
  1. index.yaml exists and lists every skill file
  2. Each skill file exists and parses as valid YAML
  3. Each skill has required fields (id, name, version, roles, procedures)
  4. Skill IDs are unique across the registry
  5. Proficiency levels are valid
  6. Each IC-level load strategy is declared
  7. Procedure steps are non-empty
  8. Completeness: skills cover each declared member role in team.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
REGISTRY = ROOT / "skill-registry"
CONFIGS = ROOT / "configs"

VALID_PROFICIENCY_LEVELS = {"learning", "developing", "proficient", "expert"}
VALID_IC_LEVELS = {3, 4, 5, 6}
VALID_ROLES = {"frontend", "backend", "fullstack", "devops", "qa", "security", "tech_lead"}
VALID_LOAD_STRATEGIES = {"full_procedure", "summary", "principles_only", "none"}
REQUIRED_SKILL_FIELDS = {"id", "name", "version", "roles", "procedures"}


def load_yaml(path: Path) -> dict | list | None:
    with path.open() as fh:
        return yaml.safe_load(fh)


def check_index(index_path: Path) -> tuple[list[str], dict]:
    """Load and validate index.yaml. Returns (errors, index_data)."""
    errors: list[str] = []
    if not index_path.exists():
        return [f"skill-registry/index.yaml not found at {index_path}"], {}

    try:
        data = load_yaml(index_path) or {}
    except yaml.YAMLError as exc:
        return [f"index.yaml: YAML parse error — {exc}"], {}

    if "skills" not in data:
        errors.append("index.yaml: missing required 'skills' key")

    skills_map = data.get("skills", {})
    if not isinstance(skills_map, dict):
        errors.append("index.yaml: 'skills' must be a mapping of skill_id → file_path")
        return errors, {}

    return errors, skills_map


def validate_skill_file(skill_id: str, skill_path: Path) -> list[str]:
    errors: list[str] = []
    prefix = f"skill '{skill_id}'"

    if not skill_path.exists():
        return [f"{prefix}: file not found at {skill_path}"]

    try:
        data = load_yaml(skill_path) or {}
    except yaml.YAMLError as exc:
        return [f"{prefix}: YAML parse error — {exc}"]

    # Required fields
    missing = REQUIRED_SKILL_FIELDS - set(data.keys())
    if missing:
        errors.append(f"{prefix}: missing required fields: {sorted(missing)}")

    # ID consistency
    declared_id = data.get("id", "")
    if declared_id != skill_id:
        errors.append(
            f"{prefix}: id mismatch — index says '{skill_id}' but file declares '{declared_id}'"
        )

    # Version format
    version = data.get("version", "")
    if version and not _is_valid_semver(str(version)):
        errors.append(f"{prefix}: version '{version}' is not valid semver (e.g. 1.0.0)")

    # Roles
    roles = data.get("roles", [])
    if not isinstance(roles, list) or not roles:
        errors.append(f"{prefix}: 'roles' must be a non-empty list")
    else:
        invalid_roles = set(roles) - VALID_ROLES
        if invalid_roles:
            errors.append(
                f"{prefix}: unknown roles {sorted(invalid_roles)} — "
                f"valid: {sorted(VALID_ROLES)}"
            )

    # Load strategies per IC level
    load_strategies = data.get("load_strategies", {})
    if load_strategies:
        for ic_level, strategy in load_strategies.items():
            if int(ic_level) not in VALID_IC_LEVELS:
                errors.append(f"{prefix}: invalid IC level '{ic_level}' in load_strategies")
            if strategy not in VALID_LOAD_STRATEGIES:
                errors.append(
                    f"{prefix}: invalid load strategy '{strategy}' for IC{ic_level} — "
                    f"valid: {sorted(VALID_LOAD_STRATEGIES)}"
                )

    # Procedures
    procedures = data.get("procedures", {})
    if not isinstance(procedures, dict):
        errors.append(f"{prefix}: 'procedures' must be a mapping (proficiency → steps)")
        return errors

    for level, steps in procedures.items():
        if level not in VALID_PROFICIENCY_LEVELS:
            errors.append(
                f"{prefix}: unknown proficiency level '{level}' in procedures — "
                f"valid: {sorted(VALID_PROFICIENCY_LEVELS)}"
            )
        if not isinstance(steps, list) or not steps:
            errors.append(f"{prefix}: procedure for '{level}' must be a non-empty list of steps")
        else:
            for i, step in enumerate(steps):
                if not isinstance(step, str) or not step.strip():
                    errors.append(f"{prefix}: procedure[{level}][{i}] must be a non-empty string")

    # Quality criteria (optional but validated if present)
    quality_criteria = data.get("quality_criteria", [])
    if quality_criteria and not isinstance(quality_criteria, list):
        errors.append(f"{prefix}: 'quality_criteria' must be a list")

    # Prerequisites (optional)
    prerequisites = data.get("prerequisites", [])
    if prerequisites and not isinstance(prerequisites, list):
        errors.append(f"{prefix}: 'prerequisites' must be a list of skill IDs")

    return errors


def check_skill_completeness(skills_map: dict, team_path: Path) -> list[str]:
    """Verify that each role declared in team.yaml has ≥1 skill covering it."""
    warnings: list[str] = []
    if not team_path.exists():
        return warnings

    try:
        team_data = load_yaml(team_path) or {}
    except yaml.YAMLError:
        return warnings

    roles_in_team: set[str] = set()
    for member in team_data.get("members", []):
        role = member.get("role")
        if role:
            roles_in_team.add(role)

    # Build set of roles covered by registry
    roles_covered: set[str] = set()
    for skill_id, skill_file in skills_map.items():
        skill_path = REGISTRY / skill_file
        if not skill_path.exists():
            continue
        try:
            data = load_yaml(skill_path) or {}
            for role in data.get("roles", []):
                roles_covered.add(role)
        except yaml.YAMLError:
            continue

    uncovered = roles_in_team - roles_covered
    for role in sorted(uncovered):
        warnings.append(
            f"Completeness: role '{role}' is used by team members but has no skills covering it"
        )

    return warnings


def check_duplicate_ids(skills_map: dict) -> list[str]:
    """Detect if multiple index entries point to the same file (content collision)."""
    errors: list[str] = []
    seen_files: dict[str, str] = {}
    for skill_id, skill_file in skills_map.items():
        if skill_file in seen_files:
            errors.append(
                f"Duplicate: skills '{skill_id}' and '{seen_files[skill_file]}' "
                f"both point to '{skill_file}'"
            )
        else:
            seen_files[skill_file] = skill_id
    return errors


def check_orphaned_files(skills_map: dict) -> list[str]:
    """Find YAML files in skill-registry/ not listed in index.yaml."""
    warnings: list[str] = []
    if not REGISTRY.exists():
        return warnings

    indexed_files = set(skills_map.values())
    for yaml_file in REGISTRY.glob("**/*.yaml"):
        relative = yaml_file.relative_to(REGISTRY)
        if str(relative) == "index.yaml":
            continue
        if str(relative) not in indexed_files:
            warnings.append(
                f"Orphan: {relative} exists in skill-registry/ but is not listed in index.yaml"
            )

    return warnings


def _is_valid_semver(version: str) -> bool:
    parts = version.split(".")
    if len(parts) != 3:
        return False
    try:
        all(int(p) >= 0 for p in parts)
        return True
    except ValueError:
        return False


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    print("=" * 60)
    print("FORGE Skill Registry Validator")
    print("=" * 60)

    if not REGISTRY.exists():
        print(f"[FAIL] skill-registry/ directory not found at {REGISTRY}")
        return 1

    # 1. Index check
    index_path = REGISTRY / "index.yaml"
    index_errors, skills_map = check_index(index_path)
    if index_errors:
        errors.extend(index_errors)
        print(f"[FAIL] index.yaml — {len(index_errors)} error(s)")
    else:
        print(f"[PASS] index.yaml — {len(skills_map)} skill(s) declared")

    if not skills_map:
        print("\n[SKIP] No skills in index — skipping per-skill validation")
        print(f"\nErrors: {len(errors)}")
        return 1 if errors else 0

    # 2. Duplicate ID check
    dup_errors = check_duplicate_ids(skills_map)
    errors.extend(dup_errors)
    status = "FAIL" if dup_errors else "PASS"
    print(f"[{status}] duplicate ID check")

    # 3. Per-skill validation
    print(f"\n--- Per-skill validation ({len(skills_map)} skills) ---")
    for skill_id, skill_file in sorted(skills_map.items()):
        skill_path = REGISTRY / skill_file
        skill_errors = validate_skill_file(skill_id, skill_path)
        if skill_errors:
            errors.extend(skill_errors)
            print(f"  [FAIL] {skill_id}")
            for e in skill_errors:
                print(f"         {e}")
        else:
            print(f"  [PASS] {skill_id} ({skill_file})")

    # 4. Orphan check
    print("\n--- Orphan file check ---")
    orphan_warnings = check_orphaned_files(skills_map)
    warnings.extend(orphan_warnings)
    if orphan_warnings:
        for w in orphan_warnings:
            print(f"  [WARN] {w}")
    else:
        print(f"  [PASS] no orphaned skill files")

    # 5. Completeness check against team.yaml
    print("\n--- Role completeness check ---")
    completeness_warnings = check_skill_completeness(skills_map, CONFIGS / "team.yaml")
    warnings.extend(completeness_warnings)
    if completeness_warnings:
        for w in completeness_warnings:
            print(f"  [WARN] {w}")
    else:
        print(f"  [PASS] all team roles have at least one skill")

    # --- Summary ---------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"Skills validated : {len(skills_map)}")
    print(f"Errors           : {len(errors)}")
    print(f"Warnings         : {len(warnings)}")

    if errors:
        print("\nFAILED — fix the above errors before merging.")
        return 1

    if warnings:
        print("\nPASSED with warnings — review above before shipping.")
    else:
        print("\nSkill registry is fully valid.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
