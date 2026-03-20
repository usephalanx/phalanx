# Changelog

All notable changes to Phalanx are documented here.

## [v1.1.0] — 2026-03-19

### Added
- Nginx reverse proxy serving landing page on port 80
- Landing page (`site/`) with full product documentation
- `site/privacy.html` and `site/terms.html`
- Mobile hamburger nav and responsive layout improvements
- Email waitlist capture on landing page
- Platform-agnostic hero messaging (Slack, Discord, API, voice)
- Real pipeline metrics in trust strip (~3m 20s avg run)
- `CONTRIBUTING.md`, `LICENSE`, `CHANGELOG.md`

### Changed
- Landing page headline: "Agents in formation. You command." → "Prompt in. PR out."
- Trust strip now shows real measured metrics instead of feature labels
- Comparison table: "Slack Integration" → "Platform Integrations" (Slack · Discord · API · Voice)
- Nav label "Formation" → "Agents"
- Footer privacy/terms links now resolve correctly

### Fixed
- Dead footer links (`/privacy`, `/terms`) — pages now exist
- Demo bar title no longer Slack-specific

---

## [v1.0.0-mvp] — 2026-03-19

### First complete end-to-end pipeline run

Full pipeline: **Planner → Builder → Reviewer → QA → Security → Release → READY_TO_MERGE** in ~3m 20s.

### Architecture
- Multi-agent Celery pipeline: Commander, Planner, Builder, Reviewer, QA, Security, Release
- 16-state deterministic workflow enforced by `RunStatus` state machine
- Slack Bolt socket-mode gateway — `/forge build "<task>"` dispatches to Commander
- PostgreSQL + pgvector for state, artifacts, and memory
- Redis for Celery broker and result backend
- GitHub integration: Builder clones repo, creates branch, pushes. Release opens PR.
- Docker Compose production deployment on AWS Lightsail

### Key fixes before MVP
- `SoftTimeLimitExceeded`: Commander/Builder time limits raised to 3600s/1800s
- `FORGE_WORKER=1` NullPool: added to docker-compose.prod.yml (prevents "Future attached to different loop")
- QA pytest path: uses `sys.executable` parent, not bare `pytest`
- pytest/ruff in prod image: added `[qa]` extras, `pip install ".[qa]"` in Dockerfile
- SecurityPipeline init: removed nonexistent `task_id`/`project_id` kwargs
- QA dependency install: auto-installs `requirements.txt`/`pyproject.toml` before test run
