"""
Integration Wiring Agent — wires entry-point files after all builders complete.

Runs as a DAG task AFTER all builder tasks finish and BEFORE the VerifierAgent.
Its sole job: ensure the project's entry-point file (app/page.tsx, main.py, App.tsx,
etc.) correctly imports and assembles everything the builder tasks produced.

This solves the "page.tsx doesn't import the components" problem structurally —
rather than relying on each builder to know what parallel builders will produce,
a dedicated agent reads all builder outputs and generates the correct wiring.

Strategy per integration_pattern:
  nextjs-app-router  → scan components/, write app/page.tsx with all sections
  fastapi-router     → scan routers/, register into main.py
  vite-app           → scan src/components/, wire into src/App.tsx
  rn-navigation      → scan screens/, wire into App.tsx navigation stack
  flutter-material   → scan lib/screens/, wire into lib/main.dart
  go-main            → scan handlers/, wire into main.go router
  generic-*          → LLM fallback: give Claude the file list + task descriptions

Non-fatal: if wiring fails, the VerifierAgent will catch the compile error.
The pipeline continues — this agent never blocks ship approval.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.agents.verification_profiles import (
    PROFILES,
    _discover_fastapi_routers,
    _discover_react_components,
    detect_tech_stack,
    get_profile,
    merge_workspace,
)
from phalanx.config.settings import get_settings
from phalanx.db.models import Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Celery entry-point
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(
    name="phalanx.agents.integration_wiring.execute_task",
    bind=True,
    max_retries=1,
    soft_time_limit=300,
    time_limit=360,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:
    agent = IntegrationWiringAgent(run_id=run_id, task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class IntegrationWiringAgent(BaseAgent):
    AGENT_ROLE = "integration_wiring"

    async def execute(self) -> AgentResult:
        self._log.info("integration_wiring.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            run = await self._load_run(session)

            stmt = select(Task).where(
                Task.run_id == str(self.run_id),
                Task.agent_role == "builder",
            )
            result = await session.execute(stmt)
            builder_tasks = list(result.scalars().all())

        if not builder_tasks:
            self._log.info("integration_wiring.no_builder_tasks")
            await self._complete(status="COMPLETED", output={"status": "skipped", "reason": "no builder tasks"})
            return AgentResult(success=True, output={"status": "skipped"})

        # Resolve tech_stack — read from planning hint, then auto-detect
        app_type = run.app_type or "web"
        planning_hint = (task.output or {}).get("tech_stack", "") if task else ""
        base = Path(settings.git_workspace) / run.project_id / self.run_id

        # Merge workspaces
        merged_dir = merge_workspace(base, builder_tasks)
        tech_stack = planning_hint or detect_tech_stack(merged_dir, app_type)
        profile = get_profile(tech_stack)

        self._log.info(
            "integration_wiring.resolved",
            tech_stack=tech_stack,
            integration_pattern=profile.integration_pattern,
            merged_dir=str(merged_dir),
        )

        # Wire entry points
        wiring_result = await self._wire(merged_dir, profile, builder_tasks)

        output = {
            "tech_stack": tech_stack,
            "integration_pattern": profile.integration_pattern,
            "files_wired": wiring_result.get("files_wired", []),
            "status": wiring_result.get("status", "unknown"),
            "notes": wiring_result.get("notes", []),
        }

        success = wiring_result.get("status") != "error"
        await self._complete(
            status="COMPLETED" if success else "ESCALATING",
            output={**(task.output or {}), **output},
            escalation_reason=wiring_result.get("error") if not success else None,
        )

        self._log.info(
            "integration_wiring.execute.done",
            status=wiring_result.get("status"),
            files_wired=wiring_result.get("files_wired", []),
        )
        return AgentResult(success=success, output=output)

    # ─────────────────────────────────────────────────────────────────────────
    # Wiring dispatcher
    # ─────────────────────────────────────────────────────────────────────────

    async def _wire(
        self, work_dir: Path, profile: "VerificationProfile", builder_tasks: list
    ) -> dict:
        pattern = profile.integration_pattern

        if pattern == "nextjs-app-router":
            return self._wire_nextjs(work_dir)
        if pattern == "vite-app":
            return self._wire_vite(work_dir)
        if pattern == "fastapi-router":
            return self._wire_fastapi(work_dir)
        if pattern in ("rn-navigation", "expo-tabs"):
            return self._wire_react_native(work_dir, profile.entry_points)
        if pattern == "flutter-material":
            return self._wire_flutter(work_dir)
        if pattern == "go-main":
            return self._wire_go(work_dir)

        # Generic fallback — use Claude to wire
        return await self._wire_with_llm(work_dir, profile, builder_tasks)

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic wiring strategies
    # ─────────────────────────────────────────────────────────────────────────

    def _wire_nextjs(self, work_dir: Path) -> dict:
        """
        Scan components/ for React components, write app/page.tsx importing them.
        Skips wiring if app/page.tsx already has non-trivial imports (trust the builder).
        """
        components_dir = work_dir / "components"
        if not components_dir.exists():
            return {"status": "skipped", "reason": "no components/ directory", "files_wired": [], "notes": []}

        # Discover exported components
        components = _discover_react_components(components_dir)
        if not components:
            return {"status": "skipped", "reason": "no React components found in components/", "files_wired": [], "notes": []}

        # Check if page.tsx already has meaningful imports (trust the builder)
        page_tsx = work_dir / "app" / "page.tsx"
        if page_tsx.exists():
            existing = page_tsx.read_text(errors="ignore")
            # Count existing component imports — if >1 found, trust builder
            existing_imports = re.findall(r"^import\s+\w+\s+from\s+['\"]@/components", existing, re.MULTILINE)
            if len(existing_imports) >= 2:
                return {
                    "status": "trusted",
                    "reason": f"page.tsx already has {len(existing_imports)} component imports",
                    "files_wired": [],
                    "notes": [f"Trusted existing page.tsx with {len(existing_imports)} imports"],
                }

        # Build page.tsx
        page_tsx.parent.mkdir(parents=True, exist_ok=True)
        imports = "\n".join(
            f"import {c['name']} from '@/components/{c['relative_path'].replace('.tsx', '').replace('.jsx', '')}'"
            for c in components
        )
        renders = "\n      ".join(f"<{c['name']} />" for c in components)
        content = f"""\
{imports}

export default function HomePage() {{
  return (
    <main>
      {renders}
    </main>
  );
}}
"""
        page_tsx.write_text(content, encoding="utf-8")
        self._log.info("integration_wiring.nextjs.wired", components=[c["name"] for c in components])
        return {
            "status": "wired",
            "files_wired": ["app/page.tsx"],
            "notes": [f"Wired {len(components)} component(s): {[c['name'] for c in components]}"],
        }

    def _wire_vite(self, work_dir: Path) -> dict:
        """Wire src/App.tsx to import components from src/components/."""
        components_dir = work_dir / "src" / "components"
        if not components_dir.exists():
            return {"status": "skipped", "reason": "no src/components/", "files_wired": [], "notes": []}

        components = _discover_react_components(components_dir)
        if not components:
            return {"status": "skipped", "reason": "no components found", "files_wired": [], "notes": []}

        app_tsx = work_dir / "src" / "App.tsx"
        if app_tsx.exists():
            existing = app_tsx.read_text(errors="ignore")
            if len(re.findall(r"^import ", existing, re.MULTILINE)) >= 3:
                return {"status": "trusted", "reason": "App.tsx has existing imports", "files_wired": [], "notes": []}

        app_tsx.parent.mkdir(parents=True, exist_ok=True)
        imports = "\n".join(f"import {c['name']} from './components/{c['name']}'" for c in components)
        renders = "\n      ".join(f"<{c['name']} />" for c in components)
        content = f"""\
import React from 'react';
{imports}

function App() {{
  return (
    <div>
      {renders}
    </div>
  );
}}

export default App;
"""
        app_tsx.write_text(content, encoding="utf-8")
        return {"status": "wired", "files_wired": ["src/App.tsx"], "notes": [f"Wired {len(components)} components"]}

    def _wire_fastapi(self, work_dir: Path) -> dict:
        """Discover APIRouter instances and include them in main.py."""
        # Find router files
        router_files = _discover_fastapi_routers(work_dir)
        if not router_files:
            return {"status": "skipped", "reason": "no APIRouter files found", "files_wired": [], "notes": []}

        main_candidates = [work_dir / "main.py", work_dir / "app" / "main.py"]
        main_py = next((p for p in main_candidates if p.exists()), None)

        if main_py:
            existing = main_py.read_text(errors="ignore")
            # Count include_router calls
            if existing.count("include_router") >= len(router_files):
                return {"status": "trusted", "reason": "main.py already has router includes", "files_wired": [], "notes": []}

        # Build minimal main.py
        main_py = main_candidates[0]
        main_py.parent.mkdir(parents=True, exist_ok=True)
        imports = "\n".join(f"from {r['module']} import router as {r['name']}_router" for r in router_files)
        includes = "\n".join(f"app.include_router({r['name']}_router)" for r in router_files)
        content = f"""\
from fastapi import FastAPI

{imports}

app = FastAPI()

{includes}
"""
        main_py.write_text(content, encoding="utf-8")
        return {"status": "wired", "files_wired": [str(main_py.relative_to(work_dir))], "notes": [f"Registered {len(router_files)} routers"]}

    def _wire_react_native(self, work_dir: Path, entry_points: list[str]) -> dict:
        """Check App.tsx exists; if not, generate minimal navigation wrapper."""
        for ep in entry_points:
            if (work_dir / ep).exists():
                return {"status": "trusted", "reason": f"{ep} exists", "files_wired": [], "notes": []}

        # Generate minimal App.tsx
        app_tsx = work_dir / "App.tsx"
        screens_dir = work_dir / "src" / "screens"
        screens = list(screens_dir.glob("*.tsx")) if screens_dir.exists() else []
        screen_names = [s.stem for s in screens]

        content = """\
import React from 'react';
import { View, Text, StyleSheet } from 'react-native';

export default function App() {
  return (
    <View style={styles.container}>
      <Text>App</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: 'center', justifyContent: 'center' },
});
"""
        app_tsx.write_text(content, encoding="utf-8")
        return {"status": "wired", "files_wired": ["App.tsx"], "notes": [f"Generated minimal App.tsx (screens: {screen_names})"]}

    def _wire_flutter(self, work_dir: Path) -> dict:
        """Ensure lib/main.dart exists."""
        main_dart = work_dir / "lib" / "main.dart"
        if main_dart.exists():
            return {"status": "trusted", "reason": "lib/main.dart exists", "files_wired": [], "notes": []}

        main_dart.parent.mkdir(parents=True, exist_ok=True)
        content = """\
import 'package:flutter/material.dart';

void main() => runApp(const MyApp());

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override
  Widget build(BuildContext context) {
    return const MaterialApp(home: Scaffold(body: Center(child: Text('App'))));
  }
}
"""
        main_dart.write_text(content, encoding="utf-8")
        return {"status": "wired", "files_wired": ["lib/main.dart"], "notes": ["Generated minimal main.dart"]}

    def _wire_go(self, work_dir: Path) -> dict:
        """Check main.go compiles; if missing, generate minimal stub."""
        main_go = work_dir / "main.go"
        if main_go.exists():
            return {"status": "trusted", "reason": "main.go exists", "files_wired": [], "notes": []}

        cmd_main = work_dir / "cmd" / "main.go"
        if cmd_main.exists():
            return {"status": "trusted", "reason": "cmd/main.go exists", "files_wired": [], "notes": []}

        main_go.write_text('package main\n\nfunc main() {}\n', encoding="utf-8")
        return {"status": "wired", "files_wired": ["main.go"], "notes": ["Generated minimal main.go stub"]}

    # ─────────────────────────────────────────────────────────────────────────
    # LLM fallback
    # ─────────────────────────────────────────────────────────────────────────

    async def _wire_with_llm(
        self, work_dir: Path, profile: "VerificationProfile", builder_tasks: list
    ) -> dict:
        """
        Use Claude (haiku) to generate the entry-point wiring when no
        deterministic strategy matches.
        """
        # Gather file listing
        files = sorted(
            str(f.relative_to(work_dir))
            for f in work_dir.rglob("*")
            if f.is_file() and not any(p in f.parts for p in (".git", "__pycache__", "node_modules", "_verify", "_merged"))
        )[:80]

        task_descriptions = [t.title for t in builder_tasks if t.title]

        prompt = f"""You are an integration engineer. Given the following project files and task descriptions,
write the entry-point file that correctly imports and wires together all the components/modules produced.

Tech stack: {profile.tech_stack}
Integration pattern: {profile.integration_pattern}
Expected entry points: {profile.entry_points}

Builder tasks completed:
{chr(10).join(f'- {d}' for d in task_descriptions)}

Files in workspace:
{chr(10).join(files)}

Return ONLY a JSON object with this shape (no markdown):
{{
  "entry_point_path": "app/page.tsx",
  "content": "full file content here"
}}"""

        try:
            response = self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system="You are an integration engineer. Output only valid JSON.",
                model=self._settings.anthropic_model_fast,
                max_tokens=4096,
            )
            # Strip markdown fences
            clean = re.sub(r"```(?:json)?\s*", "", response).strip()
            start, end = clean.find("{"), clean.rfind("}") + 1
            data = json.loads(clean[start:end])

            path = work_dir / data["entry_point_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data["content"], encoding="utf-8")
            self._log.info("integration_wiring.llm_wired", path=data["entry_point_path"])
            return {
                "status": "wired",
                "files_wired": [data["entry_point_path"]],
                "notes": ["LLM-generated wiring (generic fallback)"],
            }
        except Exception as exc:
            self._log.warning("integration_wiring.llm_failed", error=str(exc))
            return {"status": "skipped", "reason": f"LLM wiring failed: {exc}", "files_wired": [], "notes": []}

    # ─────────────────────────────────────────────────────────────────────────
    # DB helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _complete(self, status: str, output: dict, escalation_reason: str | None = None) -> None:
        async with get_db() as session:
            values: dict = {
                "status": status,
                "output": output,
                "completed_at": datetime.now(UTC),
            }
            if escalation_reason:
                values["escalation_reason"] = escalation_reason
            await session.execute(
                update(Task).where(Task.id == str(self.task_id)).values(**values)
            )
            await session.commit()


# _discover_react_components and _discover_fastapi_routers are imported from
# verification_profiles to keep them testable and avoid duplication.
