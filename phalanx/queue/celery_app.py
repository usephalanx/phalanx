"""
Celery application configuration.
One queue per agent type for priority isolation.
Builder queue is isolated — git ops happen there.
"""

from celery import Celery

from phalanx.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "forge",
    include=[
        "phalanx.agents.commander",
        "phalanx.agents.planner",
        "phalanx.agents.builder",
        "phalanx.agents.reviewer",
        "phalanx.agents.qa",
        "phalanx.agents.verifier",
        "phalanx.agents.integration_wiring",
        "phalanx.agents.security",
        "phalanx.agents.release",
        "phalanx.agents.sre",
        "phalanx.agents.ci_fixer",
        "phalanx.agents.ci_fixer_v2_task",
        # CI Fixer v3 — 4 new modules. Celery registers the @celery_app.task
        # decorators only for modules in this list; queue subscription alone
        # is not sufficient (learned the hard way during the v3 canary).
        "phalanx.agents.cifix_commander",
        "phalanx.agents.cifix_techlead",
        "phalanx.agents.cifix_challenger",  # v1.7 reviewer (shadow mode initially)
        "phalanx.agents.cifix_engineer",
        "phalanx.agents.cifix_sre",
        "phalanx.workflow.advance_run",
        "phalanx.maintenance.tasks",
        "phalanx.memory.tasks",
        "phalanx.skills.ingestion.tasks",
        "phalanx.skills.tasks",
    ],
)

celery_app.config_from_object(
    {
        "broker_url": settings.celery_broker_url,
        "result_backend": settings.celery_result_backend,
        # Serialization
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "enable_utc": True,
        # Reliability
        "task_acks_late": True,  # ack only after success — no job loss on crash
        "task_reject_on_worker_lost": True,
        "worker_prefetch_multiplier": 1,  # one task at a time per worker thread
        # Queues (one per agent type)
        "task_queues": {
            "default": {"exchange": "default", "routing_key": "default"},
            "commander": {"exchange": "commander", "routing_key": "commander"},
            "planner": {"exchange": "planner", "routing_key": "planner"},
            "builder": {"exchange": "builder", "routing_key": "builder"},
            "reviewer": {"exchange": "reviewer", "routing_key": "reviewer"},
            "qa": {"exchange": "qa", "routing_key": "qa"},
            "security": {"exchange": "security", "routing_key": "security"},
            "release": {"exchange": "release", "routing_key": "release"},
            "ingestion": {"exchange": "ingestion", "routing_key": "ingestion"},
            "skill_drills": {"exchange": "skill_drills", "routing_key": "skill_drills"},
            "ci_fixer": {"exchange": "ci_fixer", "routing_key": "ci_fixer"},
            "sre": {"exchange": "sre", "routing_key": "sre"},
            # CI Fixer v3 — multi-agent DAG (Commander → TechLead → Engineer → SRE)
            "cifix_commander": {"exchange": "cifix_commander", "routing_key": "cifix_commander"},
            "cifix_techlead": {"exchange": "cifix_techlead", "routing_key": "cifix_techlead"},
            "cifix_challenger": {"exchange": "cifix_challenger", "routing_key": "cifix_challenger"},
            "cifix_engineer": {"exchange": "cifix_engineer", "routing_key": "cifix_engineer"},
            "cifix_sre": {"exchange": "cifix_sre", "routing_key": "cifix_sre"},
        },
        "task_default_queue": "default",
        # Task routing (agent role → queue)
        "task_routes": {
            "phalanx.agents.commander.*": {"queue": "commander"},
            "phalanx.agents.planner.*": {"queue": "planner"},
            "phalanx.agents.builder.*": {"queue": "builder"},
            "phalanx.agents.reviewer.*": {"queue": "reviewer"},
            "phalanx.agents.qa.*": {"queue": "qa"},
            "phalanx.agents.security.*": {"queue": "security"},
            "phalanx.agents.release.*": {"queue": "release"},
            "phalanx.agents.sre.*": {"queue": "sre"},
            "phalanx.agents.ci_fixer.*": {"queue": "ci_fixer"},
            # v3 multi-agent routing (name-prefixed so they don't collide with v2)
            "phalanx.agents.cifix_commander.*": {"queue": "cifix_commander"},
            "phalanx.agents.cifix_techlead.*": {"queue": "cifix_techlead"},
            "phalanx.agents.cifix_challenger.*": {"queue": "cifix_challenger"},
            "phalanx.agents.cifix_engineer.*": {"queue": "cifix_engineer"},
            "phalanx.agents.cifix_sre.*": {"queue": "cifix_sre"},
            "phalanx.skills.ingestion.*": {"queue": "ingestion"},
            "phalanx.skills.drills.*": {"queue": "skill_drills"},
        },
        # Beat scheduler — redbeat stores schedule in Redis (no Django ORM required)
        "beat_scheduler": "redbeat.RedBeatScheduler",
        "redbeat_redis_url": settings.redis_url,
        # Beat schedule (scheduled tasks)
        "beat_schedule": {
            "check-blocked-runs": {
                "task": "phalanx.maintenance.tasks.check_blocked_runs",
                "schedule": 300,  # every 5 minutes — orphan watchdog
            },
            "decay-memory-relevance": {
                "task": "phalanx.memory.tasks.decay_relevance",
                "schedule": 86400 * 7,  # weekly
            },
            "check-skill-feeds": {
                "task": "phalanx.skills.ingestion.tasks.check_feeds",
                "schedule": 86400,  # daily
            },
            "check-skill-staleness": {
                "task": "phalanx.skills.tasks.check_staleness",
                "schedule": 86400 * 3,  # every 3 days
            },
            "poll-fix-outcomes": {
                "task": "phalanx.ci_fixer.outcome_tracker.poll_fix_outcomes",
                "schedule": 1800,  # every 30 minutes
            },
            "promote-fix-patterns": {
                "task": "phalanx.ci_fixer.pattern_promoter.promote_patterns",
                "schedule": 3600,  # every hour
            },
        },
        # Soft time limit: warn at 5 min, hard kill at 10 min
        # Builder tasks get longer limits
        "task_soft_time_limit": 300,
        "task_time_limit": 600,
    }
)

# Explicitly include all task modules — autodiscover only finds tasks.py files,
# but FORGE tasks live in per-agent modules (forge.agents.commander, etc.)
celery_app.autodiscover_tasks(
    [
        "phalanx.agents.commander",
        "phalanx.agents.planner",
        "phalanx.agents.builder",
        "phalanx.agents.reviewer",
        "phalanx.agents.qa",
        "phalanx.agents.verifier",
        "phalanx.agents.integration_wiring",
        "phalanx.agents.security",
        "phalanx.agents.release",
        "phalanx.agents.sre",
        "phalanx.agents.ci_fixer",
        "phalanx.ci_fixer.outcome_tracker",
        "phalanx.ci_fixer.pattern_promoter",
        "phalanx.ci_fixer.proactive_scanner",
        "phalanx.workflow",
        "phalanx.maintenance",
        "phalanx.memory",
        "phalanx.skills.ingestion",
        "phalanx.skills",
    ]
)
