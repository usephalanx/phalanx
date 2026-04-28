"""
CI failure event — normalized representation of a CI failure
regardless of which CI tool produced it.

Every provider webhook is normalized into CIFailureEvent before
being processed. This keeps the rest of the system CI-tool-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CIFailureEvent:
    """
    Normalized CI failure event.

    Produced by webhook handlers (GitHub, Buildkite, CircleCI, Jenkins)
    and consumed by the CI fixer pipeline.
    """

    provider: str
    """CI provider: 'github_actions' | 'buildkite' | 'circleci' | 'jenkins'"""

    repo_full_name: str
    """GitHub repo in 'owner/repo' format, e.g. 'acme/backend'"""

    branch: str
    """Branch the CI ran on, e.g. 'fix/payment-flow'"""

    commit_sha: str
    """SHA of the commit that triggered the failing CI run"""

    build_id: str
    """Provider-specific build/run ID (used to fetch logs)"""

    build_url: str
    """URL to the CI build for linking in PR comments"""

    failed_jobs: list[str] = field(default_factory=list)
    """Names of the jobs/steps that failed"""

    pr_number: int | None = None
    """GitHub PR number if this CI run was triggered by a PR"""

    log_url: str | None = None
    """Direct log URL if provided in the webhook payload (optional)"""

    pr_author: str | None = None
    """GitHub login of the PR/commit author — used for allowed_authors filtering"""

    ci_check_suite_id: int | None = None
    """GitHub check_suite.id (bug #11 A3 idempotency key). Multiple check_runs
    of the same suite share this; the webhook handler uses it to dedup
    deterministically. None for non-GitHub providers."""

    raw_payload: dict = field(default_factory=dict)
    """Original webhook payload for debugging"""

    integration_id: str | None = None
    """FK to CIIntegration — set after webhook is matched to an integration"""
