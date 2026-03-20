"""
Seed script: bootstrap a FORGE team + project from YAML config files.

What it does:
  1. Loads configs/team.yaml and configs/project.yaml
  2. Creates a Project row (idempotent — updates if slug already exists)
  3. Creates Skill rows from skill-registry/index.yaml + per-skill YAMLs
  4. Creates SkillConfidence rows for each team member × skill combination
  5. Logs a summary

Usage:
  python scripts/seed_team.py
  python scripts/seed_team.py --config-dir /path/to/configs --registry-dir /path/to/skill-registry
  FORGE_ENV=production python scripts/seed_team.py

Design: idempotent — safe to run multiple times. Uses ON CONFLICT DO UPDATE
so reruns during deploy don't fail or duplicate data.
"""
from __future__ import annotations

import asyncio
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed(config_dir: Path, registry_dir: Path) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from phalanx.config.loader import ConfigLoader
    from phalanx.config.settings import get_settings
    from phalanx.db.models import Project, Skill, SkillConfidence
    from phalanx.skills.engine import SkillEngine

    settings = get_settings()
    print(f"[seed_team] env={settings.forge_env} db={settings.database_url!r}")

    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    loader = ConfigLoader(config_dir=config_dir)
    team_cfg = loader.team
    project_cfg = loader.project
    engine_obj = SkillEngine(registry_path=registry_dir)

    print(f"[seed_team] team={team_cfg.team.name} members={len(team_cfg.members)}")
    print(f"[seed_team] project={project_cfg.project.name}")

    async with AsyncSessionFactory() as session:
        async with session.begin():
            # ── 1. Upsert Project ────────────────────────────────────────────
            project_slug = project_cfg.project.name.lower().replace(" ", "-")
            stmt = pg_insert(Project).values(
                slug=project_slug,
                name=project_cfg.project.name,
                repo_url=project_cfg.project.repo or None,
                config={
                    "stack": project_cfg.project.stack.model_dump(),
                    "branches": project_cfg.project.branches.model_dump(),
                },
            ).on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "name": project_cfg.project.name,
                    "repo_url": project_cfg.project.repo or None,
                    "config": {
                        "stack": project_cfg.project.stack.model_dump(),
                        "branches": project_cfg.project.branches.model_dump(),
                    },
                },
            ).returning(Project.id)

            result = await session.execute(stmt)
            project_id: str = result.scalar_one()
            print(f"[seed_team] ✓ Project id={project_id} slug={project_slug!r}")

            # ── 2. Upsert Skills from registry ───────────────────────────────
            skill_ids = engine_obj.list_skills()
            print(f"[seed_team] Upserting {len(skill_ids)} skills...")
            for skill_id in skill_ids:
                try:
                    raw = engine_obj._load_raw_skill(skill_id)
                except Exception as exc:
                    print(f"[seed_team] ✗ Skill {skill_id}: {exc}")
                    continue

                stmt = pg_insert(Skill).values(
                    id=skill_id,
                    version=raw.get("version", "1.0.0"),
                    domain=raw.get("domain", "engineering"),
                    category=raw.get("category", "general"),
                    stability=raw.get("stability", "stable"),
                    applicable_roles=raw.get("applicable_roles", []),
                    min_level=raw.get("min_level", "ic3"),
                    token_cost_estimate=raw.get("token_cost_estimate", 2000),
                    spec=raw,
                ).on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "version": raw.get("version", "1.0.0"),
                        "spec": raw,
                        "applicable_roles": raw.get("applicable_roles", []),
                    },
                )
                await session.execute(stmt)
                print(f"[seed_team]   ✓ skill={skill_id}")

            # ── 3. Upsert SkillConfidence rows for each member × skill ────────
            print(f"[seed_team] Seeding SkillConfidence for {len(team_cfg.members)} members...")
            for member in team_cfg.members:
                for skill_id in member.skills:
                    if skill_id not in skill_ids:
                        print(f"[seed_team]   ⚠ member={member.id} skill={skill_id} not in registry, skipping")
                        continue

                    stmt = pg_insert(SkillConfidence).values(
                        agent_id=member.id,
                        skill_id=skill_id,
                        project_id=project_id,
                        score=0.70,
                        peak_score=0.70,
                        proficiency_level="developing",
                    ).on_conflict_do_update(
                        index_elements=["agent_id", "skill_id", "project_id"],
                        set_={"proficiency_level": "developing"},  # don't downgrade existing scores
                    )
                    await session.execute(stmt)

                print(f"[seed_team]   ✓ member={member.id} ic{member.ic_level} skills={member.skills}")

    await engine.dispose()
    print("[seed_team] ✅ Seed complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed FORGE team and project from YAML configs")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(__file__).parent.parent / "configs",
        help="Path to configs/ directory containing team.yaml, project.yaml, etc.",
    )
    parser.add_argument(
        "--registry-dir",
        type=Path,
        default=Path(__file__).parent.parent / "skill-registry",
        help="Path to skill-registry/ directory containing index.yaml and skill files.",
    )
    args = parser.parse_args()
    asyncio.run(seed(config_dir=args.config_dir, registry_dir=args.registry_dir))
