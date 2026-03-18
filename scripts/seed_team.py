"""
Seed script: bootstrap a team + project from config files.
Usage: python scripts/seed_team.py
       python scripts/seed_team.py --team website-alpha --project acme-website
"""
import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed(team_slug: str, project_slug: str):
    print(f"Seeding team={team_slug} project={project_slug}")
    # Placeholder: config loader + DB write will be implemented in M2
    print("✅ Seed complete. Implement in Milestone 2.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", default="website-alpha")
    parser.add_argument("--project", default="acme-website")
    args = parser.parse_args()
    asyncio.run(seed(args.team, args.project))
