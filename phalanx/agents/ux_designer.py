"""
UX Designer Agent — produces a language-agnostic design contract (DESIGN.md)
before builders write any code.

Responsibilities:
  1. Load the task + planner's architecture output for context
  2. Reflect on the app type, audience, and UX patterns that apply
  3. Generate a complete DESIGN.md: brand, color palette, typography, spacing,
     component taxonomy, SVG logo, and state definitions
  4. Self-check the design (contrast, consistency, completeness)
  5. Fix self-check issues if found (one pass)
  6. Write DESIGN.md to the workspace root
  7. Emit handoff note for builders
  8. Mark task COMPLETED

Design principles:
  - Fully language-agnostic: zero code in the output. Only design tokens,
    names, descriptions, and SVG.
  - Platform-aware: respects web vs iOS vs Android conventions.
  - Accessible by default: WCAG AA minimum on all color pairs.
  - Soul-driven: full reflection + self-check + uncertainty escalation per
    Anthropic model spec.

AP-003: exceptions propagate — Celery handles retries.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent, get_anthropic_client, mark_task_failed
from phalanx.agents.soul import (
    UX_DESIGNER_SELF_CHECK_PROMPT,
    UX_DESIGNER_SOUL,
    UX_DESIGNER_REFLECTION_PROMPT,
)
from phalanx.config.settings import get_settings
from phalanx.db.models import Artifact, Run, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()

_DESIGN_MAX_TOKENS = 4096

# Keywords that signal a UI/UX project — used by commander injection logic
UI_SIGNAL_WORDS = frozenset({
    "web", "webapp", "website", "app", "mobile", "ios", "android", "react",
    "vue", "angular", "svelte", "flutter", "dashboard", "ui", "frontend",
    "landing", "page", "portal", "interface", "screen", "form", "todo",
    "chat", "feed", "profile", "shop", "store", "booking", "blog",
})


def is_ui_project(title: str, description: str = "") -> bool:
    """Return True if the work order looks like a UI/UX project."""
    import re
    text = (title + " " + description).lower()
    words = set(re.findall(r"[a-z]+", text))
    return bool(words & UI_SIGNAL_WORDS)


class UXDesignerAgent(BaseAgent):
    """
    Senior UX designer agent — language-agnostic design contract generator.

    Produces DESIGN.md: brand identity, color tokens, typography, spacing
    scale, component taxonomy, SVG logo, and interaction state definitions.
    The builder reads this file before writing any UI code.
    """

    AGENT_ROLE = "ux_designer"

    async def execute(self) -> AgentResult:
        self._log.info("ux_designer.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False, output={}, error=f"Task {self.task_id} not found"
                )
            run = await self._load_run(session)

        workspace = Path(settings.git_workspace) / run.project_id / self.run_id
        workspace.mkdir(parents=True, exist_ok=True)

        # Load planner output for context (architecture plan)
        planner_context = await self._load_planner_context()

        # Detect app type from task description
        app_description = f"{task.title}: {task.description or ''}"
        app_type = self._infer_app_type(app_description, planner_context)
        target_audience = self._infer_audience(app_description)

        # ── Soul: reflection before designing ─────────────────────────────────
        reflection = self._reflect(
            task_description=f"APP TYPE: {app_type}\nTARGET AUDIENCE: {target_audience}\n\nAPP: {app_description}",
            context=planner_context,
            soul=UX_DESIGNER_SOUL,
        )
        if reflection:
            await self._trace(
                "reflection",
                reflection,
                {
                    "task_title": task.title,
                    "app_type": app_type,
                    "target_audience": target_audience,
                },
            )
            # Escalate if reflection surfaces genuine uncertainty
            if any(phrase in reflection.lower() for phrase in [
                "underspecified", "unclear", "ambiguous", "cannot determine",
                "need clarification", "too vague",
            ]):
                await self._trace(
                    "uncertainty",
                    f"UX design brief may be too vague to produce a good design:\n\n{reflection[:1000]}",
                    {"task_title": task.title, "app_type": app_type},
                )

        # ── Generate design spec ───────────────────────────────────────────────
        design_content = await self._generate_design(
            task=task,
            app_type=app_type,
            target_audience=target_audience,
            planner_context=planner_context,
            reflection=reflection,
        )

        # ── Soul: self-check the design ────────────────────────────────────────
        design_file = "DESIGN.md"
        self_check_result = self._self_check_design(
            app_description=app_description,
            files_written=design_file,
            design_content=design_content,
        )
        if self_check_result:
            await self._trace(
                "self_check",
                self_check_result,
                {"task_title": task.title, "files_written": [design_file]},
            )

        # Fix if issues found — one pass
        if self_check_result and "self-check passed" not in self_check_result.lower():
            self._log.info("ux_designer.self_check_fix.start", task_id=str(self.task_id))
            fixed_content = await self._fix_design_issues(
                task=task,
                app_type=app_type,
                original_design=design_content,
                self_check_result=self_check_result,
            )
            if fixed_content and len(fixed_content) > 200:
                design_content = fixed_content
                await self._trace(
                    "decision",
                    f"Design self-check fix applied. Issues resolved:\n{self_check_result[:500]}",
                    {"task_title": task.title, "source": "self_check_fix"},
                )

        # ── Write DESIGN.md to workspace ───────────────────────────────────────
        design_path = workspace / design_file
        design_path.write_text(design_content, encoding="utf-8")
        self._log.info("ux_designer.design_written", path=str(design_path), bytes=len(design_content))

        # ── Handoff note for builders ──────────────────────────────────────────
        handoff = self._write_design_handoff(
            app_description=app_description,
            app_type=app_type,
            design_content=design_content,
        )
        if handoff:
            await self._trace(
                "handoff",
                handoff,
                {"task_title": task.title, "files_written": [design_file]},
            )

        output = {
            "workspace": str(workspace),
            "files_written": [design_file],
            "app_type": app_type,
            "design_bytes": len(design_content),
        }

        async with get_db() as session:
            run_ref = await self._load_run(session)
            await self._persist_design_artifact(session, output, run_ref.project_id, design_content)
            await session.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(
                    status="COMPLETED",
                    output=output,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        self._log.info("ux_designer.execute.done", bytes=len(design_content))
        return AgentResult(success=True, output=output, tokens_used=self._tokens_used)

    # ── Design generation ──────────────────────────────────────────────────────

    async def _generate_design(
        self,
        task: Task,
        app_type: str,
        target_audience: str,
        planner_context: str,
        reflection: str,
    ) -> str:
        """Call Claude to produce the full DESIGN.md content."""
        system = UX_DESIGNER_SOUL

        planner_section = (
            f"\n\nARCHITECTURE CONTEXT (from planner):\n{planner_context[:2000]}"
            if planner_context else ""
        )
        reflection_section = (
            f"\n\nPRE-DESIGN REFLECTION:\n{reflection[:1000]}"
            if reflection else ""
        )

        user_message = (
            f"Design a complete visual identity and UX system for this product.\n\n"
            f"PRODUCT: {task.title}\n"
            f"DESCRIPTION: {task.description or 'No description provided.'}\n"
            f"APP TYPE: {app_type}\n"
            f"TARGET AUDIENCE: {target_audience}"
            f"{planner_section}"
            f"{reflection_section}\n\n"
            f"Produce a DESIGN.md file with ALL of the following sections:\n\n"
            f"## 1. Brand Identity\n"
            f"- App name, tagline, personality (2-3 adjectives), voice/tone\n\n"
            f"## 2. Color Palette\n"
            f"- Each color: name, hex value, semantic purpose, where NOT to use it\n"
            f"- Required: primary, primary-dark, surface, surface-raised, "
            f"text-primary, text-secondary, text-disabled, "
            f"success, warning, error, info\n"
            f"- Verify WCAG AA (4.5:1) contrast for all text/background pairs\n\n"
            f"## 3. Typography\n"
            f"- Font family (system-safe stack or single Google Font name)\n"
            f"- Scale: xs/sm/base/lg/xl/2xl/3xl with px sizes and line heights\n"
            f"- Font weights used and their semantic meaning\n\n"
            f"## 4. Spacing & Layout\n"
            f"- Base unit (4px), scale multipliers (1x through 16x)\n"
            f"- Max content width, column grid, breakpoints\n\n"
            f"## 5. Component Taxonomy\n"
            f"- List every UI component this app needs\n"
            f"- For each: canonical name, description, required props/inputs, "
            f"variants, states (default/hover/active/disabled/loading/error)\n\n"
            f"## 6. Logo\n"
            f"- Inline SVG (viewBox='0 0 40 40', suitable for 40x40px favicon)\n"
            f"- Color: use palette colors only\n"
            f"- Description of what it communicates\n\n"
            f"## 7. UX Patterns\n"
            f"- 3-5 key UX patterns applied in this app (e.g. progressive disclosure, "
            f"optimistic updates, inline validation)\n"
            f"- For each: name, where it applies, why it fits this product\n\n"
            f"## 8. Accessibility\n"
            f"- Minimum touch target size\n"
            f"- Focus ring style\n"
            f"- Screen reader annotations for key components\n"
            f"- Any WCAG AA exceptions and justification\n\n"
            f"Write DESIGN.md content directly. No code fences around the whole file. "
            f"Be specific and opinionated — no placeholder values."
        )

        try:
            result = self._call_claude(
                messages=[{"role": "user", "content": user_message}],
                system=system,
                max_tokens=_DESIGN_MAX_TOKENS,
            )
            self._log.info("ux_designer.design_generated", length=len(result))
            return result
        except Exception as exc:
            self._log.warning("ux_designer.design_failed", error=str(exc))
            return self._fallback_design(task.title, app_type)

    def _self_check_design(
        self,
        app_description: str,
        files_written: str,
        design_content: str,
    ) -> str:
        """LLM-based self-check of the generated design."""
        prompt = UX_DESIGNER_SELF_CHECK_PROMPT.format(
            app_description=app_description[:500],
            files_written=files_written,
        ) + f"\n\nDESIGN CONTENT TO CHECK:\n{design_content[:3000]}"

        try:
            result = self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system=UX_DESIGNER_SOUL,
                max_tokens=800,
            )
            return result
        except Exception as exc:
            self._log.warning("ux_designer.self_check_failed", error=str(exc))
            return ""

    async def _fix_design_issues(
        self,
        task: Task,
        app_type: str,
        original_design: str,
        self_check_result: str,
    ) -> str:
        """One targeted fix pass driven by self-check findings."""
        prompt = (
            f"The following DESIGN.md has issues identified in a self-check. "
            f"Fix ONLY the issues listed. Return the complete corrected DESIGN.md.\n\n"
            f"ISSUES TO FIX:\n{self_check_result[:1500]}\n\n"
            f"ORIGINAL DESIGN.md:\n{original_design[:3000]}"
        )
        try:
            result = self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system=UX_DESIGNER_SOUL,
                max_tokens=_DESIGN_MAX_TOKENS,
            )
            return result
        except Exception as exc:
            self._log.warning("ux_designer.fix_failed", error=str(exc))
            return ""

    def _write_design_handoff(
        self,
        app_description: str,
        app_type: str,
        design_content: str,
    ) -> str:
        """Brief handoff note summarising what designers decided for the builders."""
        # Extract key decisions from the design — first 600 chars is usually brand + colors
        preview = design_content[:600].replace("\n", " ")
        try:
            result = self._call_claude(
                messages=[{"role": "user", "content": (
                    f"Summarise this design spec in 3 sentences for a builder who is about to "
                    f"implement it. Focus on: (1) the visual personality, (2) the primary/surface "
                    f"colors and font, (3) any critical accessibility or consistency rules they "
                    f"must follow.\n\nDESIGN PREVIEW:\n{preview}\n\n"
                    f"APP: {app_description[:200]}"
                )}],
                system=UX_DESIGNER_SOUL,
                max_tokens=200,
            )
            return result
        except Exception as exc:
            self._log.warning("ux_designer.handoff_failed", error=str(exc))
            return ""

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    # ── Context helpers ────────────────────────────────────────────────────────

    async def _load_planner_context(self) -> str:
        """Load the planner task output for this run (architecture/plan content)."""
        from phalanx.db.session import get_db  # noqa: PLC0415

        try:
            async with get_db() as session:
                result = await session.execute(
                    select(Task)
                    .where(
                        Task.run_id == self.run_id,
                        Task.agent_role == "planner",
                        Task.status == "COMPLETED",
                    )
                    .order_by(Task.sequence_num)
                    .limit(1)
                )
                planner_task = result.scalar_one_or_none()
                if planner_task and planner_task.output:
                    return str(planner_task.output.get("plan", ""))[:2000]
        except Exception as exc:
            self._log.warning("ux_designer.planner_context_failed", error=str(exc))
        return ""

    def _infer_app_type(self, description: str, planner_context: str) -> str:
        """Infer app type from description for use in design prompts."""
        text = (description + " " + planner_context).lower()
        if any(w in text for w in ["ios", "swift", "swiftui", "uikit"]):
            return "iOS mobile app"
        if any(w in text for w in ["android", "kotlin", "jetpack"]):
            return "Android mobile app"
        if any(w in text for w in ["flutter", "dart"]):
            return "cross-platform mobile app (Flutter)"
        if any(w in text for w in ["react native", "expo"]):
            return "cross-platform mobile app (React Native)"
        if any(w in text for w in ["dashboard", "admin", "analytics", "backoffice"]):
            return "web dashboard / admin tool"
        if any(w in text for w in ["landing", "marketing", "homepage"]):
            return "marketing / landing page"
        if any(w in text for w in ["shop", "store", "ecommerce", "cart", "checkout"]):
            return "e-commerce web app"
        return "web application"

    def _infer_audience(self, description: str) -> str:
        """Infer target audience from description."""
        text = description.lower()
        if any(w in text for w in ["enterprise", "b2b", "saas", "team", "organization"]):
            return "business professionals"
        if any(w in text for w in ["student", "learn", "education", "course"]):
            return "students / learners"
        if any(w in text for w in ["developer", "engineer", "api", "technical"]):
            return "software developers"
        if any(w in text for w in ["child", "kid", "family"]):
            return "families / children"
        return "general consumers"

    def _fallback_design(self, title: str, app_type: str) -> str:
        """Minimal fallback DESIGN.md if Claude call fails entirely."""
        return f"""# {title} — Design Spec

## 1. Brand Identity
- **App name:** {title}
- **Personality:** Clean, functional, trustworthy
- **Voice:** Direct and helpful

## 2. Color Palette
| Name | Hex | Purpose |
|------|-----|---------|
| primary | #6366F1 | Primary actions, links |
| primary-dark | #4F46E5 | Hover on primary |
| surface | #FFFFFF | Page background |
| surface-raised | #F9FAFB | Card backgrounds |
| text-primary | #111827 | Body text |
| text-secondary | #6B7280 | Supporting text |
| text-disabled | #D1D5DB | Disabled state text |
| success | #10B981 | Success states |
| warning | #F59E0B | Warnings |
| error | #EF4444 | Errors |

## 3. Typography
- **Font:** System UI stack (-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif)
- **Scale:** xs=12px, sm=14px, base=16px, lg=18px, xl=20px, 2xl=24px, 3xl=30px

## 4. Spacing & Layout
- **Base unit:** 4px
- **Scale:** 4, 8, 12, 16, 24, 32, 48, 64px
- **Max content width:** 1200px

## 5. Component Taxonomy
See task description for required components.

## 6. Logo
<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
  <rect width="40" height="40" rx="8" fill="#6366F1"/>
  <text x="20" y="27" font-size="20" text-anchor="middle" fill="white" font-weight="bold">P</text>
</svg>

## 7. UX Patterns
- **Progressive disclosure:** Show only what's needed at each step
- **Inline validation:** Validate inputs as the user types
- **Empty states:** Every list/table has a designed empty state

## 8. Accessibility
- Minimum touch target: 44×44px
- Focus ring: 2px solid #6366F1, 2px offset
- All text meets WCAG AA (4.5:1 contrast ratio minimum)
"""

    async def _persist_design_artifact(
        self,
        session: Any,
        output: dict,
        project_id: str,
        design_content: str,
    ) -> None:
        """Persist DESIGN.md as a run artifact."""
        try:
            content_hash = hashlib.sha256(design_content.encode()).hexdigest()[:16]
            artifact = Artifact(
                run_id=self.run_id,
                task_id=str(self.task_id),
                project_id=project_id,
                artifact_type="design_spec",
                title="DESIGN.md",
                s3_key=f"runs/{self.run_id}/DESIGN.md",
                content_hash=content_hash,
                version=1,
                is_final=True,
                summary=f"Design spec: {output.get('app_type', 'web app')}",
                quality_evidence={},
            )
            session.add(artifact)
        except Exception as exc:
            self._log.warning("ux_designer.artifact_persist_failed", error=str(exc))


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.ux_designer.execute_task",
    bind=True,
    max_retries=2,
    soft_time_limit=600,
    time_limit=660,
    queue="ux_designer",
)
def execute_task(self: Any, task_id: str, run_id: str, **kwargs: Any) -> dict:
    """Celery entry point: run UX Designer for a task."""
    import asyncio  # noqa: PLC0415

    agent = UXDesignerAgent(run_id=run_id, agent_id="ux-designer", task_id=task_id)
    try:
        result = asyncio.run(agent.execute())
    except Exception as exc:
        log.exception("ux_designer.celery_task_unhandled", task_id=task_id, run_id=run_id)
        asyncio.run(mark_task_failed(task_id, str(exc)))
        raise self.retry(exc=exc, countdown=30)

    if not result.success:
        log.error("ux_designer.task_failed", task_id=task_id, run_id=run_id, error=result.error)
        asyncio.run(mark_task_failed(task_id, result.error or "UX Designer failed"))

    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "tokens_used": result.tokens_used,
    }
