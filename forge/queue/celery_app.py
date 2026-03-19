"""
Celery application configuration.
One queue per agent type for priority isolation.
Builder queue is isolated — git ops happen there.
"""
from celery import Celery
from forge.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "forge",
    include=[
        "forge.agents.commander",
        "forge.agents.planner",
        "forge.agents.builder",
        "forge.agents.reviewer",
        "forge.agents.qa",
        "forge.agents.security",
        "forge.agents.release",
        "forge.maintenance.tasks",
        "forge.memory.tasks",
        "forge.skills.ingestion.tasks",
        "forge.skills.tasks",
    ],
)

celery_app.config_from_object({
    "broker_url": settings.celery_broker_url,
    "result_backend": settings.celery_result_backend,

    # Serialization
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],
    "timezone": "UTC",
    "enable_utc": True,

    # Reliability
    "task_acks_late": True,           # ack only after success — no job loss on crash
    "task_reject_on_worker_lost": True,
    "worker_prefetch_multiplier": 1,  # one task at a time per worker thread

    # Queues (one per agent type)
    "task_queues": {
        "default":   {"exchange": "default",   "routing_key": "default"},
        "commander": {"exchange": "commander", "routing_key": "commander"},
        "planner":   {"exchange": "planner",   "routing_key": "planner"},
        "builder":   {"exchange": "builder",   "routing_key": "builder"},
        "reviewer":  {"exchange": "reviewer",  "routing_key": "reviewer"},
        "qa":        {"exchange": "qa",        "routing_key": "qa"},
        "security":  {"exchange": "security",  "routing_key": "security"},
        "release":   {"exchange": "release",   "routing_key": "release"},
        "ingestion": {"exchange": "ingestion", "routing_key": "ingestion"},
        "skill_drills": {"exchange": "skill_drills", "routing_key": "skill_drills"},
    },
    "task_default_queue": "default",

    # Task routing (agent role → queue)
    "task_routes": {
        "forge.agents.commander.*":   {"queue": "commander"},
        "forge.agents.planner.*":     {"queue": "planner"},
        "forge.agents.builder.*":     {"queue": "builder"},
        "forge.agents.reviewer.*":    {"queue": "reviewer"},
        "forge.agents.qa.*":          {"queue": "qa"},
        "forge.agents.security.*":    {"queue": "security"},
        "forge.agents.release.*":     {"queue": "release"},
        "forge.skills.ingestion.*":   {"queue": "ingestion"},
        "forge.skills.drills.*":      {"queue": "skill_drills"},
    },

    # Beat scheduler — redbeat stores schedule in Redis (no Django ORM required)
    "beat_scheduler": "redbeat.RedBeatScheduler",
    "redbeat_redis_url": settings.redis_url,

    # Beat schedule (scheduled tasks)
    "beat_schedule": {
        "check-blocked-runs": {
            "task": "forge.maintenance.tasks.check_blocked_runs",
            "schedule": 1800,          # every 30 minutes
        },
        "decay-memory-relevance": {
            "task": "forge.memory.tasks.decay_relevance",
            "schedule": 86400 * 7,     # weekly
        },
        "check-skill-feeds": {
            "task": "forge.skills.ingestion.tasks.check_feeds",
            "schedule": 86400,         # daily
        },
        "check-skill-staleness": {
            "task": "forge.skills.tasks.check_staleness",
            "schedule": 86400 * 3,     # every 3 days
        },
    },

    # Soft time limit: warn at 5 min, hard kill at 10 min
    # Builder tasks get longer limits
    "task_soft_time_limit": 300,
    "task_time_limit": 600,
})

# Explicitly include all task modules — autodiscover only finds tasks.py files,
# but FORGE tasks live in per-agent modules (forge.agents.commander, etc.)
celery_app.autodiscover_tasks([
    "forge.agents.commander",
    "forge.agents.planner",
    "forge.agents.builder",
    "forge.agents.reviewer",
    "forge.agents.qa",
    "forge.agents.security",
    "forge.agents.release",
    "forge.maintenance",
    "forge.memory",
    "forge.skills.ingestion",
    "forge.skills",
])
