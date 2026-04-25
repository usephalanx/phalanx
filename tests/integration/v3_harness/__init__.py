"""Tier-1 v3 canary harness — fast, mock-heavy, runs on every commit.

Catches the bug classes that bit us during the humanize canary:
  - celery task registration (every new agent must be in include=)
  - env_detector correctness per language (Python, TS, JS, Java, C#)
  - fix_spec parser robustness across LLM output shapes
  - cifix_commander DAG persistence shape

Does NOT cover (Tier-2 territory):
  - real Docker provisioning correctness
  - real OpenAI/Anthropic provider message shapes
  - real GitHub commit_and_push

Each language has a fixture repo under fixtures/<lang>/ with the marker
files real CI workflows ship: pyproject.toml + workflows for Python,
package.json + lockfile + workflows for Node, pom.xml/.csproj/etc.
"""
