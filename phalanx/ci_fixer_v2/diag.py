"""Preflight diagnostic for CI Fixer v2.

Run this BEFORE the first live run on any host:

    python -m phalanx.ci_fixer_v2.diag [--repo acme/widget]

It walks the production checklist from
[docs/ci-fixer-v2-live-run.md](../../docs/ci-fixer-v2-live-run.md)
and prints pass/fail per check with actionable errors. Exit 0 when
every check passes; exit 1 on the first hard failure.

Checks performed:
  1. Required env vars + api keys present
  2. OpenAI model reachable (tiny ping — counts as 1 API call)
  3. Anthropic model reachable
  4. Database reachable + migrations at head
     (columns `memory_facts.agent_role`, `ci_fix_runs.cost_breakdown_json`)
  5. Redis reachable
  6. Docker daemon reachable (sandbox path mandatory per audit N3)
  7. Sandbox images present on the host
  8. CIIntegration row exists for --repo (when supplied)

Each check returns a DiagResult with ok + message. Output is a clean
table the operator can copy into an incident log.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)


REQUIRED_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


SANDBOX_IMAGES_EXPECTED: tuple[str, ...] = (
    # Image names match what `docker/sandbox/*/Dockerfile` builds produce.
    # Hyphen separator, not colon-tagged — the colon form was a docs-only
    # fiction in the original spec draft.
    "phalanx-sandbox-python:latest",
    "phalanx-sandbox-node:latest",
    "phalanx-sandbox-go:latest",
    "phalanx-sandbox-rust:latest",
)


@dataclass
class DiagResult:
    name: str
    ok: bool
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────


async def check_env_vars() -> DiagResult:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        return DiagResult("env_vars", False, f"missing: {missing}")
    return DiagResult("env_vars", True, f"all set ({len(REQUIRED_ENV_VARS)})")


async def check_openai_model() -> DiagResult:
    from phalanx.config.settings import get_settings

    settings = get_settings()
    model = settings.openai_model_reasoning_ci_fixer
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        return DiagResult("openai_model", False, f"openai SDK not installed: {exc}")

    # This ping exercises the EXACT combination the agent uses in prod:
    #   responses.create + tools + reasoning_effort
    # If this succeeds, agent calls will not fail on the reason that
    # broke our first live run (Chat Completions 400 with reasoning+tools).
    # A single tool is declared but marked uncallable via tool_choice;
    # we only care that the endpoint accepts the shape.
    dummy_tool = {
        "type": "function",
        "name": "diag_ping",
        "description": "Preflight noop — not meant to be called.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": False,
    }
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.responses.create(
            model=model,
            input=[{"type": "message", "role": "user", "content": "ping"}],
            instructions="Respond with one word only.",
            tools=[dummy_tool],
            reasoning={"effort": "low"},  # gpt-5.x accepts none/low/medium/high/xhigh; "low" is the cheapest valid ping
            tool_choice="none",  # don't make the model actually invoke the tool
            max_output_tokens=32,
            store=False,
        )
        if resp and getattr(resp, "output", None) is not None:
            return DiagResult("openai_model", True, f"{model} reachable (responses+tools+reasoning)")
        return DiagResult("openai_model", False, f"empty response from {model}")
    except Exception as exc:
        return DiagResult(
            "openai_model",
            False,
            f"{model} call failed: {exc!s:.200s} — check settings.openai_model_reasoning_ci_fixer",
        )


async def check_anthropic_model() -> DiagResult:
    from phalanx.config.settings import get_settings

    settings = get_settings()
    model = settings.anthropic_model_ci_fixer_coder
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        return DiagResult(
            "anthropic_model", False, f"anthropic SDK not installed: {exc}"
        )

    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
        if resp and getattr(resp, "content", None):
            return DiagResult("anthropic_model", True, f"{model} reachable")
        return DiagResult("anthropic_model", False, f"empty response from {model}")
    except Exception as exc:
        return DiagResult(
            "anthropic_model",
            False,
            f"{model} call failed: {exc!s:.200s} — "
            "check settings.anthropic_model_ci_fixer_coder",
        )


async def check_database_and_migrations() -> DiagResult:
    try:
        from sqlalchemy import text

        from phalanx.db.session import get_db

        async with get_db() as session:
            # Existence probes for the two new v2 columns.
            await session.execute(text("SELECT agent_role FROM memory_facts LIMIT 0"))
            await session.execute(
                text("SELECT cost_breakdown_json FROM ci_fix_runs LIMIT 0")
            )
        return DiagResult(
            "database", True, "reachable; v2 columns (agent_role, cost_breakdown_json) present"
        )
    except Exception as exc:
        return DiagResult(
            "database",
            False,
            f"db/migration check failed: {exc!s:.200s} — run `alembic upgrade head`",
        )


async def check_redis() -> DiagResult:
    try:
        import redis.asyncio as redis_lib

        from phalanx.config.settings import get_settings

        client = redis_lib.from_url(get_settings().redis_url)
        pong = await client.ping()
        await client.aclose()
        if pong:
            return DiagResult("redis", True, "PING/PONG ok")
        return DiagResult("redis", False, "PING did not return truthy")
    except Exception as exc:
        return DiagResult("redis", False, f"redis unreachable: {exc!s:.200s}")


async def check_docker() -> DiagResult:
    # `docker info` is the canonical reachability test. We keep it sync
    # in a threadpool because the docker CLI is not async-native.
    def _run():
        return subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    try:
        result = await asyncio.to_thread(_run)
    except FileNotFoundError:
        return DiagResult("docker", False, "docker binary not on PATH")
    except subprocess.TimeoutExpired:
        return DiagResult("docker", False, "docker info timed out (daemon stuck?)")
    if result.returncode != 0:
        return DiagResult(
            "docker",
            False,
            f"docker info failed: {result.stderr.strip()[:200]}",
        )
    return DiagResult("docker", True, f"server version {result.stdout.strip()}")


async def check_sandbox_images() -> DiagResult:
    def _run():
        return subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    try:
        result = await asyncio.to_thread(_run)
    except FileNotFoundError:
        return DiagResult("sandbox_images", False, "docker binary not on PATH")
    if result.returncode != 0:
        return DiagResult(
            "sandbox_images", False, f"docker images failed: {result.stderr.strip()[:200]}"
        )
    present = set(result.stdout.splitlines())
    missing = [img for img in SANDBOX_IMAGES_EXPECTED if img not in present]
    if missing:
        return DiagResult(
            "sandbox_images",
            False,
            f"missing: {missing} — rebuild via docker/sandbox/",
        )
    return DiagResult("sandbox_images", True, f"all {len(SANDBOX_IMAGES_EXPECTED)} present")


async def check_ci_integration(repo_full_name: str) -> DiagResult:
    try:
        from sqlalchemy import select

        from phalanx.db.models import CIIntegration
        from phalanx.db.session import get_db

        async with get_db() as session:
            result = await session.execute(
                select(CIIntegration).where(
                    CIIntegration.repo_full_name == repo_full_name
                )
            )
            row = result.scalar_one_or_none()
    except Exception as exc:
        return DiagResult(
            "ci_integration", False, f"db query failed: {exc!s:.200s}"
        )
    if row is None:
        return DiagResult(
            "ci_integration",
            False,
            f"no CIIntegration row for {repo_full_name} — insert one "
            "(see docs/ci-fixer-v2-live-run.md)",
        )
    if not row.enabled:
        return DiagResult(
            "ci_integration",
            False,
            f"CIIntegration for {repo_full_name} exists but enabled=False",
        )
    return DiagResult(
        "ci_integration",
        True,
        f"enabled for {repo_full_name} (integration_id={row.id})",
    )


# ─────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────


DEFAULT_CHECKS: list[tuple[str, Callable[[], Awaitable[DiagResult]]]] = [
    ("env_vars", check_env_vars),
    ("openai_model", check_openai_model),
    ("anthropic_model", check_anthropic_model),
    ("database", check_database_and_migrations),
    ("redis", check_redis),
    ("docker", check_docker),
    ("sandbox_images", check_sandbox_images),
]


async def run_diagnostics(
    repo: str | None = None,
    skip: frozenset[str] = frozenset(),
) -> list[DiagResult]:
    results: list[DiagResult] = []
    for name, check_fn in DEFAULT_CHECKS:
        if name in skip:
            results.append(DiagResult(name, True, "skipped"))
            continue
        results.append(await check_fn())
    if repo:
        results.append(await check_ci_integration(repo))
    return results


def _render_table(results: list[DiagResult]) -> str:
    width = max(len(r.name) for r in results) + 2
    lines = []
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        lines.append(f"  [{status}] {r.name.ljust(width)} {r.detail}")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    skip = frozenset(args.skip) if args.skip else frozenset()
    results = await run_diagnostics(repo=args.repo, skip=skip)
    print("\nPhalanx CI Fixer v2 — preflight diag")
    print(_render_table(results))
    failed = [r for r in results if not r.ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed — fix before running live.")
        return 1
    print(f"All {len(results)} checks passed. Ready for a live run.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight diagnostics for Phalanx CI Fixer v2"
    )
    parser.add_argument(
        "--repo",
        help="Optional 'owner/repo' to verify a CIIntegration row exists.",
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help=(
            "Check names to skip (e.g., --skip openai_model anthropic_model "
            "when deliberately running offline)."
        ),
    )
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
